"""Grok via xAI's official Grok Build CLI, command ``grok`` (experimental).

Verified 2026-11-08 against the official Grok Build user guide in
https://github.com/xai-org/grok-build (``crates/codegen/xai-grok-pager/docs/user-guide/``):

* Headless mode (``14-headless-mode.md``): ``--prompt-file <PATH>`` triggers headless
  mode and reads the prompt from a file. Headless mode "does not read piped stdin
  into the prompt", so (ADR 0002, rule 2) the prompt is written to ``prompt.txt``
  inside the empty per-call working directory and passed by the *fixed* relative
  name ``prompt.txt``. No path, prompt text or other variable string reaches argv
  except the registry's model id and effort value.
* ``--verbatim`` ("Send prompt exactly as given") keeps the document under review
  from being expanded (mentions, slash commands).
* ``--output-format json`` emits one JSON object after the response completes, with
  ``text``, ``stopReason``, ``usage`` (``input_tokens`` uncached,
  ``cache_read_input_tokens``, ``cache_creation_input_tokens``, ``output_tokens``,
  ``reasoning_tokens``, ``total_tokens``), ``modelUsage`` and the flags
  ``usage_is_incomplete`` / ``cost_is_partial``. Failures exit non-zero and emit
  ``{"type":"error","message":"..."}``. Logs and update messages go to stderr.
* Tools: ``--tools`` is an allowlist of built-in tools and ``--disallowed-tools`` a
  denylist that wins over it. MCP meta-tools "remain available unless denied".
  Whether an *empty* allowlist is accepted is not documented, so the allowlist is
  the single read-only tool ``todo_write`` and every other documented tool id is
  denied (see :data:`DENIED_TOOLS`). ``--disable-web-search``, ``--no-subagents``,
  ``--no-plan``, ``--max-turns 1`` and ``--no-auto-update`` are documented headless
  flags. ``GROK_MEMORY=0`` disables cross-session memory (see
  :meth:`GrokCliProvider.child_env_overrides`).
* Permissions (``22-permissions-and-safety.md``): ``--permission-mode dontAsk`` runs
  "only pre-approved tools and built-in read-only handling".
* Sandbox (``18-sandbox.md``): ``--sandbox strict`` limits reads to the working
  directory, system paths and ``~/.grok``, and writes to the working directory (here
  an empty temp dir). It is enforced on Linux (Landlock) and macOS (Seatbelt). On
  other platforms grok "logs a warning and continues without enforcement".
* ``-m, --model <MODEL>`` and ``--effort <LEVEL>`` come only from the registry's
  ``cli_flags`` templates (ADR 0006).
* ``grok --version`` (``01-getting-started.md``) is the only command ``check`` may run.

Login: the CLI signs in through grok.com or falls back to ``XAI_API_KEY``. The child
environment never contains ``XAI_API_KEY`` (ADR 0002, rules 4 and 10), so this
backend always runs on the user's own CLI login. llm-crossjury never reads anything
under ``~/.grok``.

Terms: xAI's announcement (https://x.ai/news/grok-build-cli) says "Headless mode
(``-p``) allows easily running agents inside scripts and automations". The verbatim
consumer Terms of Service and Acceptable Use Policy (x.ai/legal) could not be
retrieved (HTTP 403), so S6 is unverified and the adapter stays experimental.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from llm_crossjury.core.providers.api._common import (
    ModelLookup,
    suffix_with_format_instructions,
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
    ReasoningEffort,
    Role,
    Usage,
)
from llm_crossjury.core.providers.cli import _subprocess
from llm_crossjury.core.providers.cli._subprocess import CliInvocation, CliProviderBase
from llm_crossjury.core.registry.schema import ModelEntry

PROMPT_FILE_NAME = "prompt.txt"
"""Fixed name of the prompt file inside the per-call working directory."""

DEFAULT_TIMEOUT_S = 600.0
"""Hard limit when the request sets no ``timeout_s``."""

MAX_OUTPUT_BYTES = 8 * 1024 * 1024
"""Stdout cap; exceeding it is an error (ADR 0002, rule 7)."""

ALLOWED_TOOLS = "todo_write"
"""Allowlist for ``--tools``: one built-in tool the permissions guide lists as
read-only, because an empty allowlist is not documented."""

DENIED_TOOLS: tuple[str, ...] = (
    "Agent",
    "spawn_subagent",
    "run_terminal_cmd",
    "run_terminal_command",
    "search_replace",
    "read_file",
    "list_dir",
    "grep",
    "web_search",
    "web_fetch",
    "search_tool",
    "use_tool",
    "get_command_or_subagent_output",
    "kill_command_or_subagent",
)
"""Every tool id the official guide names, except :data:`ALLOWED_TOOLS`. The guide
spells the shell tool both ``run_terminal_cmd`` (headless mode) and
``run_terminal_command`` (hooks), so both are denied. ``search_tool`` and
``use_tool`` are the MCP meta-tools (``07-mcp-servers.md``)."""

FIXED_ARGS: tuple[str, ...] = (
    "--prompt-file",
    PROMPT_FILE_NAME,
    "--verbatim",
    "--output-format",
    "json",
    "--tools",
    ALLOWED_TOOLS,
    "--disallowed-tools",
    ",".join(DENIED_TOOLS),
    "--disable-web-search",
    "--no-subagents",
    "--no-plan",
    "--max-turns",
    "1",
    "--permission-mode",
    "dontAsk",
    "--sandbox",
    "strict",
    "--no-auto-update",
)
"""Fixed headless flags; see the module docstring for the source of each."""

CHILD_ENV_OVERRIDES: Mapping[str, str] = {"GROK_MEMORY": "0"}
"""Extra child environment: disable cross-session memory (headless guide)."""

_MAX_ERROR_DETAIL = 300


def _fill(template: tuple[str, ...], placeholder: str, value: str) -> tuple[str, ...]:
    return tuple(token.replace(placeholder, value) for token in template)


def render_prompt(messages: tuple[Message, ...]) -> str:
    """Flatten a prompt into the single text the CLI takes.

    System messages come first: the CLI's only separate system channel
    (``--system-prompt-override``) would put text in argv. If the conversation has
    assistant turns, every non-system message is labelled with its role so the turns
    stay distinguishable; otherwise the parts are joined with blank lines.
    """
    system = [m.content for m in messages if m.role is Role.SYSTEM]
    turns = [m for m in messages if m.role is not Role.SYSTEM]
    labelled = any(m.role is Role.ASSISTANT for m in turns)
    parts = list(system)
    for message in turns:
        parts.append(f"[{message.role.value}]\n{message.content}" if labelled else message.content)
    return "\n\n".join(parts) + "\n"


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


_UNKNOWN_USAGE = Usage(
    input_tokens=None,
    cached_input_tokens=None,
    cache_write_tokens=None,
    output_tokens=None,
    reasoning_tokens=None,
)


def map_usage(envelope: Mapping[str, Any]) -> Usage:
    """Map the JSON envelope's ``usage`` onto :class:`Usage`.

    * ``input_tokens`` <- ``usage.input_tokens`` (documented as uncached).
    * ``cached_input_tokens`` <- ``usage.cache_read_input_tokens``.
    * ``cache_write_tokens`` <- ``usage.cache_creation_input_tokens``.
    * ``output_tokens`` <- ``usage.output_tokens``.
    * ``reasoning_tokens`` <- ``usage.reasoning_tokens``. The guide's formula
      ``total_tokens = input + cache_read + cache_creation + output`` does not say
      whether reasoning is inside ``output_tokens``; values are passed as reported.

    Every field is ``None`` (unknown) when ``usage`` is missing or malformed, when
    ``usage_is_incomplete`` is true, or when every count is zero. A single missing
    or malformed count is ``None`` on its own.
    """
    raw = envelope.get("usage")
    if not isinstance(raw, Mapping) or envelope.get("usage_is_incomplete") is True:
        return _UNKNOWN_USAGE
    usage = Usage(
        input_tokens=_int_or_none(raw.get("input_tokens")),
        cached_input_tokens=_int_or_none(raw.get("cache_read_input_tokens")),
        cache_write_tokens=_int_or_none(raw.get("cache_creation_input_tokens")),
        output_tokens=_int_or_none(raw.get("output_tokens")),
        reasoning_tokens=_int_or_none(raw.get("reasoning_tokens")),
    )
    if all(not v for v in usage.model_dump().values()):
        return _UNKNOWN_USAGE
    return usage


def _clean_detail(text: str) -> str:
    detail = " ".join(text.split())
    if len(detail) > _MAX_ERROR_DETAIL:
        detail = detail[:_MAX_ERROR_DETAIL] + "..."
    return detail


def _load_envelope(stdout: str) -> dict[str, Any]:
    """Return the JSON object on stdout.

    The whole stdout is tried first. If that fails (stray non-JSON lines), the last
    line that parses as a JSON object is used.

    Raises:
        ProviderError: if no JSON object is found.
    """
    text = stdout.strip()
    if not text:
        raise ProviderError("grok CLI produced no output")
    parsed: object = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            candidate = line.strip()
            if not candidate.startswith("{"):
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            break
    if not isinstance(parsed, dict):
        raise ProviderError("grok CLI output is not a JSON object")
    return parsed


def _reported_model(envelope: Mapping[str, Any], fallback: str) -> str:
    model_usage = envelope.get("modelUsage")
    if isinstance(model_usage, Mapping) and len(model_usage) == 1:
        (name,) = model_usage.keys()
        if isinstance(name, str) and name:
            return name
    return fallback


def _error_message(envelope: Mapping[str, Any]) -> str | None:
    """Return a sanitized error message if ``envelope`` reports a failure."""
    subtype = envelope.get("subtype")
    failed = (
        envelope.get("type") == "error"
        or envelope.get("is_error") is True
        or (isinstance(subtype, str) and subtype.startswith("error"))
    )
    if not failed:
        return None
    label = subtype if isinstance(subtype, str) and subtype else "error"
    message = envelope.get("message")
    detail = _clean_detail(message) if isinstance(message, str) else ""
    return f"grok CLI {label}" + (f": {detail}" if detail else "")


class GrokCliProvider(CliProviderBase):
    """Grok Build CLI adapter (experimental).

    The CLI supports subscription login and ``XAI_API_KEY``. Under ADR 0002 the child
    environment never contains ``XAI_API_KEY``, so this backend always uses the
    user's own CLI login.

    Capabilities are conservative: no native structured output (``--json-schema``
    is only mentioned, not documented, in the headless guide), no caching control,
    no batch, no temperature. Reasoning is settable only if the registry entry has a
    ``cli_flags.reasoning`` template and an effort mapping.

    Args:
        registry: Model registry; the model id and effort flags come only from its
            ``cli_flags`` templates.
    """

    name = "xai"
    command = "grok"

    def __init__(self, *, registry: ModelLookup | None = None) -> None:
        if CliProviderBase.__init__ is object.__init__:
            super().__init__()
        else:  # pragma: no cover - depends on the shared base implementation
            base_init: Callable[..., None] = super().__init__
            base_init(registry=registry)
        self._registry = registry

    def _entry(self, model: str) -> ModelEntry | None:
        if self._registry is None:
            return None
        return self._registry.get(self.name, model)

    def child_env_overrides(self) -> dict[str, str]:
        """Fixed variables to add to the minimal child environment.

        Returns ``{"GROK_MEMORY": "0"}``. These are llm-crossjury constants, never
        secrets; the base class is expected to merge them on top of
        ``build_child_env()``.
        """
        return dict(CHILD_ENV_OVERRIDES)

    def capabilities(self, model: str) -> Capabilities:
        """Conservative capabilities; ``reasoning`` only with a registry effort flag."""
        entry = self._entry(model)
        reasoning = (
            entry is not None
            and entry.cli_flags is not None
            and entry.cli_flags.reasoning is not None
            and entry.reasoning_control.mapping is not None
        )
        return Capabilities(
            structured_output=False,
            caching=False,
            batch=False,
            temperature=False,
            reasoning=reasoning,
        )

    def _model_args(self, request: CompletionRequest) -> tuple[str, ...]:
        entry = self._entry(request.model)
        if entry is None or entry.cli_flags is None or entry.cli_flags.model is None:
            raise ProviderError(
                f"xai/{request.model} has no registry cli_flags.model template; "
                "register it in config/models.yaml to use it on the cli backend"
            )
        args = _fill(entry.cli_flags.model, "{model}", entry.model_id)
        if request.reasoning is ReasoningEffort.DEFAULT:
            return args
        mapping = entry.reasoning_control.mapping
        value = getattr(mapping, request.reasoning.value) if mapping is not None else None
        template = entry.cli_flags.reasoning
        if value is None or template is None:
            raise ProviderError(
                f"reasoning '{request.reasoning}' cannot be set via the grok CLI for "
                f"xai/{request.model}"
            )
        return args + _fill(template, "{value}", str(value))

    def build_invocation(self, request: CompletionRequest, cwd: Path) -> CliInvocation:
        """Write the prompt to ``cwd/prompt.txt`` and return a fixed-flag invocation.

        The prompt file is the only file written. ``cwd`` must be the empty per-call
        temp directory, which is also the child's working directory, so the fixed
        relative name resolves to it. ``stdin_text`` is empty because the CLI does
        not read stdin in headless mode.

        Raises:
            ProviderError: invalid prompt, or the model has no registry
                ``cli_flags.model`` template (or cannot take the requested effort).
            ProviderUnavailableError: ``grok`` is not on PATH.
        """
        prompt = request.prompt
        validate_prompt(prompt.stable_prefix, prompt.variable_suffix)
        suffix = prompt.variable_suffix
        if request.response_schema is not None:
            suffix = suffix_with_format_instructions(suffix, request.response_schema)
        args = FIXED_ARGS + self._model_args(request)
        executable = _subprocess.resolve_executable(self.command)
        (cwd / PROMPT_FILE_NAME).write_text(
            render_prompt(prompt.stable_prefix + suffix), encoding="utf-8", newline=""
        )
        return CliInvocation(
            executable=executable,
            args=args,
            stdin_text="",
            cwd=cwd,
            timeout_s=request.timeout_s or DEFAULT_TIMEOUT_S,
            max_output_bytes=MAX_OUTPUT_BYTES,
        )

    def parse_output(self, stdout: str, request: CompletionRequest) -> CompletionResult:
        """Parse the ``--output-format json`` envelope.

        ``started_at`` is set to now and ``latency_s`` to ``None``; the caller that
        timed the subprocess is expected to replace both.

        Raises:
            ProviderError: no JSON object, an error envelope, or no string ``text``.
        """
        envelope = _load_envelope(stdout)
        error = _error_message(envelope)
        if error is not None:
            raise ProviderError(error)
        text = envelope.get("text")
        if not isinstance(text, str):
            raise ProviderError("grok CLI JSON output has no 'text' field")
        stop = envelope.get("stopReason")
        return CompletionResult(
            text=text,
            usage=map_usage(envelope),
            provider=self.name,
            model=_reported_model(envelope, request.model),
            backend=Backend.CLI,
            finish_reason=stop if isinstance(stop, str) else None,
            started_at=utc_now(),
            latency_s=None,
            native_params_used={},
        )
