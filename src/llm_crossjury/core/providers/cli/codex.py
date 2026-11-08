"""OpenAI models via the official ``codex`` CLI (experimental).

The user signs in to the Codex CLI themselves (ChatGPT sign-in or the CLI's own
API-key login); ``codex exec`` "reuses saved CLI authentication by default"
(https://learn.chatgpt.com/docs/non-interactive-mode). llm-crossjury never reads
``$CODEX_HOME`` (``~/.codex``) or the OS keyring; it only launches the executable.

Verified 2026-11-08 against the official docs and the openai/codex source at tag
``rust-v0.167.0``:

* ``codex exec -`` reads the prompt from stdin; ``--json`` prints JSONL events;
  ``--output-schema FILE`` constrains the final response; ``--ephemeral`` persists no
  session files; ``--skip-git-repo-check`` allows a non-git directory;
  ``--ignore-user-config`` skips ``$CODEX_HOME/config.toml`` (so the user's MCP servers,
  profiles and hooks are not loaded) while "authentication still uses ``CODEX_HOME``";
  ``--ignore-rules`` skips execpolicy ``.rules`` files; ``-s/--sandbox read-only``;
  ``-c key=value`` is a repeatable inline config override; ``--color never``
  (https://learn.chatgpt.com/docs/non-interactive-mode,
  https://learn.chatgpt.com/docs/developer-commands?surface=cli,
  https://github.com/openai/codex/blob/rust-v0.167.0/codex-rs/exec/src/cli.rs).
* Config keys used in ``-c`` overrides, all from the config reference
  (https://learn.chatgpt.com/docs/config-file/config-reference):
  ``approval_policy`` ("``never`` for non-interactive runs"), ``features.shell_tool``
  ("Enable command execution tools"), top-level ``web_search`` (``"disabled"`` removes
  the tool), ``features.view_image``, ``features.multi_agent``, ``features.apps``
  (connectors), ``features.plugins``, ``features.hooks``, ``features.memories``,
  ``features.browser_use``, ``features.computer_use``, ``project_doc_max_bytes`` and
  ``model_instructions_file`` ("Replacement for built-in instructions"; a relative path
  is resolved against the effective cwd, per ``codex-rs/core/src/config/mod.rs``).
  A ``-c`` value that is not valid TOML is taken as a raw string
  (``codex-rs/utils/cli/src/config_override.rs``), so no value needs quotes.
* JSONL events (``codex-rs/exec/src/exec_events.rs``): ``thread.started``,
  ``turn.started``, ``turn.completed {usage}``, ``turn.failed {error: {message}}``,
  ``item.started|updated|completed {item: {id, type, ...}}`` and ``error {message}``.
  The final answer is the last completed ``agent_message`` item (a JSON string when
  ``--output-schema`` is used).
* Usage in ``turn.completed``: ``input_tokens`` (includes ``cached_input_tokens``;
  Codex itself computes non-cached input as ``input_tokens - cached_input_tokens``),
  ``cached_input_tokens``, ``cache_write_input_tokens``, ``output_tokens`` and
  ``reasoning_output_tokens`` (Responses API semantics: reasoning is part of
  ``output_tokens``).

Files written by :meth:`CodexCliProvider.build_invocation` (system instructions, output
schema) go only into the given temp ``cwd`` and are referenced by fixed *relative*
names, so argv never carries a path. This relies on the child running with
``invocation.cwd`` as its working directory, which ``run_cli`` guarantees (ADR 0002 rule
5). The final message is parsed from stdout; ``-o`` is not used.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from llm_crossjury.core.providers.api._common import (
    ModelLookup,
    format_instructions,
    utc_now,
    validate_prompt,
)
from llm_crossjury.core.providers.base import (
    Backend,
    Capabilities,
    CompletionRequest,
    CompletionResult,
    Message,
    ProviderError,
    ProviderStatus,
    ProviderUnavailableError,
    ReasoningEffort,
    Role,
    Usage,
)
from llm_crossjury.core.providers.cli._subprocess import (
    CliInvocation,
    CliProviderBase,
    resolve_executable,
    run_cli,
)
from llm_crossjury.core.registry.schema import ModelEntry, ReasoningControlType

INSTRUCTIONS_FILE = "codex-instructions.md"
"""System instructions, written into the temp cwd (``model_instructions_file``)."""

SCHEMA_FILE = "codex-output-schema.json"
"""Strict JSON schema, written into the temp cwd (``--output-schema``)."""

BASE_ARGS: tuple[str, ...] = (
    "exec",
    "--json",
    "--color",
    "never",
    "--ephemeral",
    "--skip-git-repo-check",
    "--ignore-user-config",
    "--ignore-rules",
    "--sandbox",
    "read-only",
)
"""Fixed ``codex exec`` flags: JSONL on stdout, nothing persisted, no user config."""

LOCKDOWN_OVERRIDES: tuple[str, ...] = (
    "approval_policy=never",
    "features.shell_tool=false",
    "web_search=disabled",
    "features.view_image=false",
    "features.multi_agent=false",
    "features.apps=false",
    "features.plugins=false",
    "features.hooks=false",
    "features.memories=false",
    "features.browser_use=false",
    "features.computer_use=false",
    "project_doc_max_bytes=0",
)
"""``-c`` overrides that disable every tool, connector and side channel (ADR 0002
rule 6). Each is a documented config key; see the module docstring."""

TOOL_ITEM_TYPES: frozenset[str] = frozenset(
    {"command_execution", "file_change", "mcp_tool_call", "collab_tool_call", "web_search"}
)
"""Item types that mean the model used a tool. They must never appear; if one does,
the call fails instead of returning possibly tool-influenced output."""

DEFAULT_TIMEOUT_S = 600.0
"""Wall-clock limit when the request sets none."""

MAX_OUTPUT_BYTES = 16 * 1024 * 1024
"""Stdout cap for ``codex exec --json`` (events include reasoning summaries)."""

_CHECK_TIMEOUT_S = 30.0
_CHECK_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_DETAIL = 300
_LEVEL_CONTROLS = (ReasoningControlType.LEVELS, ReasoningControlType.BUDGET)


def _short(text: str) -> str:
    """Collapse whitespace and truncate a vendor message for an error."""
    flat = " ".join(text.split())
    return flat if len(flat) <= _MAX_DETAIL else flat[:_MAX_DETAIL] + "..."


def _int_or_none(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _nonneg(value: int) -> int | None:
    return value if value >= 0 else None


def map_usage(usage: object) -> Usage:
    """Map the ``turn.completed`` usage object onto :class:`Usage`.

    * ``input_tokens`` = ``input_tokens`` minus ``cached_input_tokens`` minus
      ``cache_write_input_tokens`` (both are included in ``input_tokens``); ``None`` if
      the total or the cached count is missing.
    * ``cached_input_tokens`` and ``cache_write_tokens`` as reported (``None`` if absent;
      older CLIs do not emit ``cache_write_input_tokens``).
    * ``reasoning_tokens`` = ``reasoning_output_tokens``.
    * ``output_tokens`` (visible) = ``output_tokens`` minus reasoning; ``None`` if either
      is missing.

    Unknown or inconsistent values are ``None``, never 0.

    Args:
        usage: The decoded ``usage`` value of a ``turn.completed`` event, or ``None``.

    Returns:
        The mapped usage.
    """
    if not isinstance(usage, Mapping):
        return Usage(
            input_tokens=None,
            cached_input_tokens=None,
            cache_write_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
        )
    total_in = _int_or_none(usage, "input_tokens")
    cached = _int_or_none(usage, "cached_input_tokens")
    cache_write = _int_or_none(usage, "cache_write_input_tokens")
    total_out = _int_or_none(usage, "output_tokens")
    reasoning = _int_or_none(usage, "reasoning_output_tokens")
    uncached = (
        _nonneg(total_in - cached - (cache_write or 0))
        if total_in is not None and cached is not None
        else None
    )
    visible = (
        _nonneg(total_out - reasoning) if total_out is not None and reasoning is not None else None
    )
    return Usage(
        input_tokens=uncached,
        cached_input_tokens=cached,
        cache_write_tokens=cache_write,
        output_tokens=visible,
        reasoning_tokens=reasoning,
    )


def render_stdin(messages: Sequence[Message]) -> str:
    """Render the non-system messages as the stdin prompt.

    A conversation of user messages only (the usual case) is sent verbatim. One with
    assistant turns is rendered with plain role headers, since ``codex exec`` takes a
    single prompt.

    Args:
        messages: The non-system messages, in order.

    Returns:
        The prompt text for stdin.
    """
    if all(m.role is Role.USER for m in messages):
        return "\n\n".join(m.content for m in messages)
    return "\n\n".join(f"[{m.role.value}]\n{m.content}" for m in messages)


class CodexCliProvider(CliProviderBase):
    """Runs ``codex exec`` non-interactively in a read-only sandbox with an empty cwd.

    Every tool is disabled through documented settings (see :data:`LOCKDOWN_OVERRIDES`),
    the user's ``config.toml`` is ignored, and the prompt goes over stdin. The model and
    reasoning flags come only from the registry entry's ``cli_flags``.

    Args:
        registry: Model registry; argv templates and capabilities come only from its
            entries. ``None`` means no model is usable.
    """

    name = "openai"
    command = "codex"

    def __init__(self, *, registry: ModelLookup | None = None) -> None:
        base_init = CliProviderBase.__init__
        if base_init is not object.__init__:
            # The shared base takes the registry too. Calling it through Any keeps this
            # working against both the scaffold and the full base class.
            cast(Any, base_init)(self, registry=registry)
        self._codex_registry = registry

    # ------------------------------------------------------------------ registry

    def _codex_entry(self, model: str) -> ModelEntry | None:
        if self._codex_registry is None:
            return None
        return self._codex_registry.get(self.name, model)

    def _registry_args(self, request: CompletionRequest) -> tuple[str, ...]:
        """Model and reasoning argv tokens from the registry entry's ``cli_flags``.

        Raises:
            ProviderError: no entry, no ``cli_flags`` or no model template, or a
                reasoning level that the entry cannot express on the CLI.
        """
        entry = self._codex_entry(request.model)
        flags = entry.cli_flags if entry is not None else None
        if entry is None or flags is None or flags.model is None:
            raise ProviderError(
                f"{self.name}/{request.model} has no registry cli_flags.model, so the "
                "codex CLI cannot be pointed at it"
            )
        args = tuple(token.replace("{model}", request.model) for token in flags.model)
        if request.reasoning is ReasoningEffort.DEFAULT:
            return args
        control = entry.reasoning_control
        value = (
            getattr(control.mapping, request.reasoning.value)
            if control.type in _LEVEL_CONTROLS and control.mapping is not None
            else None
        )
        if value is None or flags.reasoning is None:
            raise ProviderError(
                f"reasoning '{request.reasoning}' cannot be set via the codex CLI for "
                f"{self.name}/{request.model}"
            )
        return args + tuple(token.replace("{value}", str(value)) for token in flags.reasoning)

    # ------------------------------------------------------------------ contract

    def capabilities(self, model: str) -> Capabilities:
        """Capabilities of ``model`` through ``codex exec``.

        ``structured_output`` iff the registry entry says ``True`` (``--output-schema``
        is a documented ``codex exec`` flag). ``reasoning`` iff the entry has a
        levels/budget mapping and a ``cli_flags.reasoning`` template. Caching, batch and
        temperature cannot be controlled through the CLI.

        Args:
            model: Concrete model id.

        Returns:
            The capabilities; all False for an unregistered model.
        """
        entry = self._codex_entry(model)
        if entry is None:
            return Capabilities(
                structured_output=False,
                caching=False,
                batch=False,
                temperature=False,
                reasoning=False,
            )
        reasoning = (
            entry.reasoning_control.type in _LEVEL_CONTROLS
            and entry.reasoning_control.mapping is not None
            and entry.cli_flags is not None
            and entry.cli_flags.reasoning is not None
        )
        return Capabilities(
            structured_output=entry.structured_output is True,
            caching=False,
            batch=False,
            temperature=False,
            reasoning=reasoning,
        )

    def build_invocation(self, request: CompletionRequest, cwd: Path) -> CliInvocation:
        """Translate ``request`` into a locked-down ``codex exec`` invocation.

        Writes ``codex-instructions.md`` (system messages, if any) and
        ``codex-output-schema.json`` (if native structured output applies) into
        ``cwd``. Without native structured output, format instructions are appended to
        the prompt instead. ``temperature``, ``max_output_tokens`` and ``native_params``
        cannot be passed to the CLI safely and are ignored.

        Args:
            request: The completion request.
            cwd: The empty temporary working directory the child will run in.

        Returns:
            The invocation; argv holds only fixed flags and registry tokens.

        Raises:
            ProviderError: invalid prompt or no usable registry ``cli_flags``.
        """
        prompt = request.prompt
        validate_prompt(prompt.stable_prefix, prompt.variable_suffix)
        registry_args = self._registry_args(request)

        args: list[str] = list(BASE_ARGS)
        for override in LOCKDOWN_OVERRIDES:
            args += ["-c", override]
        args += registry_args

        system_text = "\n\n".join(
            m.content for m in prompt.stable_prefix if m.role is Role.SYSTEM
        ).strip()
        if system_text:
            (cwd / INSTRUCTIONS_FILE).write_text(system_text, encoding="utf-8")
            args += ["-c", f"model_instructions_file={INSTRUCTIONS_FILE}"]

        messages = [m for m in prompt.stable_prefix if m.role is not Role.SYSTEM]
        messages += prompt.variable_suffix
        stdin_text = render_stdin(messages)
        if request.response_schema is not None:
            if self.capabilities(request.model).structured_output:
                (cwd / SCHEMA_FILE).write_text(
                    json.dumps(request.response_schema, indent=2), encoding="utf-8"
                )
                args += ["--output-schema", SCHEMA_FILE]
            else:
                stdin_text += "\n\n" + format_instructions(request.response_schema)

        args.append("-")
        return CliInvocation(
            executable=resolve_executable(self.command),
            args=tuple(args),
            stdin_text=stdin_text,
            cwd=cwd,
            timeout_s=request.timeout_s if request.timeout_s is not None else DEFAULT_TIMEOUT_S,
            max_output_bytes=MAX_OUTPUT_BYTES,
        )

    def parse_output(self, stdout: str, request: CompletionRequest) -> CompletionResult:
        """Parse ``codex exec --json`` stdout.

        Lines that are not JSON objects and unknown event or item types are ignored.
        ``started_at`` is the parse time and ``latency_s`` is ``None``; the caller
        (``complete``) owns the timing.

        Args:
            stdout: The child's complete stdout.
            request: The request that produced it.

        Returns:
            The result with the last completed agent message as ``text``.

        Raises:
            ProviderError: a ``turn.failed`` event, an ``error`` event without a later
                ``turn.completed``, no ``turn.completed`` at all, no final agent message,
                or any tool item (tools are disabled, so one means the lockdown failed).
        """
        text: str | None = None
        usage: object = None
        completed = False
        last_error: str | None = None
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "turn.failed":
                error = event.get("error")
                message = error.get("message") if isinstance(error, dict) else None
                detail = _short(message) if isinstance(message, str) else "no details"
                raise ProviderError(f"codex turn failed: {detail}")
            if kind == "error":
                message = event.get("message")
                last_error = message if isinstance(message, str) else "unknown error"
            elif kind == "turn.completed":
                completed = True
                usage = event.get("usage")
            elif kind in ("item.started", "item.updated", "item.completed"):
                text = self._handle_item(kind, event.get("item"), text)
        if not completed:
            detail = f": {_short(last_error)}" if last_error else ""
            raise ProviderError(f"codex exec ended without a completed turn{detail}")
        if text is None:
            raise ProviderError("codex exec completed the turn without an agent message")
        return CompletionResult(
            text=text,
            usage=map_usage(usage),
            provider=self.name,
            model=request.model,
            backend=Backend.CLI,
            finish_reason="completed",
            started_at=utc_now(),
            latency_s=None,
            native_params_used={},
        )

    @staticmethod
    def _handle_item(kind: str, item: object, text: str | None) -> str | None:
        """Return the updated final text after one ``item.*`` event."""
        if not isinstance(item, dict):
            return text
        item_type = item.get("type")
        if item_type in TOOL_ITEM_TYPES:
            raise ProviderError(
                f"codex emitted a '{item_type}' item although tools are disabled; "
                "the output is discarded"
            )
        item_text = item.get("text")
        if kind == "item.completed" and item_type == "agent_message" and isinstance(item_text, str):
            return item_text
        return text

    async def check(self) -> ProviderStatus:
        """Run ``codex --version`` and ``codex login status``; spends no tokens.

        ``codex login status`` is documented to exit 0 when credentials are present
        (https://learn.chatgpt.com/docs/developer-commands?surface=cli); it only loads
        the CLI's own auth state and calls no model. Only its exit code is used: its
        stderr can contain a masked fragment of a stored API key, so it is never read,
        shown or logged here.

        Returns:
            The status; ``available`` iff the version query and the login status
            both succeed.
        """
        try:
            executable = resolve_executable(self.command)
        except ProviderUnavailableError:
            return ProviderStatus(
                provider=self.name,
                backend=self.backend,
                available=False,
                detail=f"'{self.command}' not found on PATH",
                version=None,
            )
        with tempfile.TemporaryDirectory(prefix="llm-crossjury-codex-") as tmp:
            cwd = Path(tmp)
            version = await self._run_version(executable, cwd)
            signed_in = version is not None and await self._run_login_status(executable, cwd)
        if version is None:
            return ProviderStatus(
                provider=self.name,
                backend=self.backend,
                available=False,
                detail=f"'{self.command} --version' failed",
                version=None,
            )
        login = (
            "signed in"
            if signed_in
            else f"not signed in (run '{self.command} login' yourself) or status check failed"
        )
        return ProviderStatus(
            provider=self.name,
            backend=self.backend,
            available=signed_in,
            detail=f"{version}; {login}",
            version=version,
        )

    @staticmethod
    def _check_invocation(executable: Path, cwd: Path, args: tuple[str, ...]) -> CliInvocation:
        return CliInvocation(
            executable=executable,
            args=args,
            stdin_text="",
            cwd=cwd,
            timeout_s=_CHECK_TIMEOUT_S,
            max_output_bytes=_CHECK_MAX_OUTPUT_BYTES,
        )

    async def _run_version(self, executable: Path, cwd: Path) -> str | None:
        try:
            stdout, _stderr, code = await run_cli(
                self._check_invocation(executable, cwd, ("--version",))
            )
        except ProviderError:
            return None
        lines = stdout.strip().splitlines()
        if code != 0 or not lines:
            return None
        return _short(lines[0])

    async def _run_login_status(self, executable: Path, cwd: Path) -> bool:
        try:
            _stdout, _stderr, code = await run_cli(
                self._check_invocation(executable, cwd, ("login", "status"))
            )
        except ProviderError:
            return False
        return code == 0
