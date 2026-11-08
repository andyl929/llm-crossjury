"""Claude via the official ``claude`` CLI (Claude Code, experimental).

The user signs in to the unmodified ``claude`` binary with their own subscription;
llm-crossjury only launches it (ADR 0002). Every flag below was checked against the
official Claude Code documentation on 2026-11-08:

* CLI reference (flags): https://code.claude.com/docs/en/cli-reference
* Headless / ``-p`` usage, JSON envelope, ``--bare`` caveat:
  https://code.claude.com/docs/en/headless
* Structured output (``StructuredOutput`` tool, result subtypes):
  https://code.claude.com/docs/en/agent-sdk/structured-outputs
* Result message fields (``SDKResultMessage``):
  https://code.claude.com/docs/en/agent-sdk/typescript

Command line (argv holds only fixed flags plus registry-derived model/effort tokens;
the prompt goes on stdin)::

    claude -p --output-format json --safe-mode --model <id> [--effort <level>]
      [--system-prompt-file <tmp>/inputs/system.md] [--json-schema <schema>]
      --tools "" --disallowedTools "mcp__*" --strict-mcp-config
      --disable-slash-commands --no-session-persistence
      --permission-mode dontAsk --max-turns <N>

* ``--bare`` is NOT used: in bare mode Claude Code "never reads OAuth credentials or
  the system keychain", so the subscription login would not work.
* ``--safe-mode`` keeps authentication but does not load CLAUDE.md, skills, plugins,
  hooks, MCP servers, custom commands/agents, output styles or auto memory.
* ``--tools ""`` disables all built-in tools; ``--disallowedTools "mcp__*"`` and
  ``--strict-mcp-config`` (with no ``--mcp-config``) keep MCP tools out;
  ``--permission-mode dontAsk`` denies anything that would prompt.
* ``--json-schema`` has no file form, so the schema is only put in argv when the
  executable is not a ``.cmd``/``.bat`` shim (cmd.exe would re-parse it) and the
  schema is short; otherwise format instructions go into the prompt and the caller
  validates the text (the contract's fallback).
* Structured output is returned through a ``StructuredOutput`` tool call, so a
  schema run needs more than one agentic turn.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from llm_crossjury.core.providers.api._common import (
    suffix_with_format_instructions,
    utc_now,
    validate_prompt,
)
from llm_crossjury.core.providers.base import (
    CompletionRequest,
    CompletionResult,
    ProviderError,
    Role,
    Usage,
)
from llm_crossjury.core.providers.cli._subprocess import (
    CliInvocation,
    CliProviderBase,
    inputs_dir,
    is_batch_shim,
    redact,
    render_transcript,
    resolve_executable,
)

MAX_TURNS_TEXT = 1
"""A free-text answer is a single model turn (no tools are available)."""

MAX_TURNS_SCHEMA = 3
"""``StructuredOutput`` tool call, its result, plus one validation retry."""

MAX_ARGV_SCHEMA_CHARS = 8000
"""Longer schemas use the prompt fallback (Windows limits a command line to 32767
characters, and argv is visible in process listings)."""

_FIXED_FLAGS: tuple[str, ...] = (
    "--tools",
    "",
    "--disallowedTools",
    "mcp__*",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--no-session-persistence",
    "--permission-mode",
    "dontAsk",
)


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def map_usage(raw: object) -> Usage:
    """Map the envelope's ``usage`` object onto :class:`Usage`.

    ``input_tokens`` -> ``input_tokens``; ``cache_read_input_tokens`` ->
    ``cached_input_tokens``; ``cache_creation_input_tokens`` -> ``cache_write_tokens``;
    ``output_tokens`` -> ``output_tokens``. Reasoning tokens are not reported
    separately, so ``reasoning_tokens`` is ``None``. Missing or malformed fields are
    ``None`` (unknown), never zero.
    """
    usage: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    return Usage(
        input_tokens=_int_or_none(usage.get("input_tokens")),
        cached_input_tokens=_int_or_none(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_int_or_none(usage.get("cache_creation_input_tokens")),
        output_tokens=_int_or_none(usage.get("output_tokens")),
        reasoning_tokens=None,
    )


def load_envelope(stdout: str) -> dict[str, Any] | None:
    """Find the ``result`` envelope in ``--output-format json`` stdout.

    Accepts a single JSON object, a JSON array of messages (the last ``result``
    message wins), or several lines where the last parseable JSON line is used.
    Returns ``None`` if nothing usable is found.
    """
    text = stdout.strip()
    candidates: list[object] = []
    try:
        candidates.append(json.loads(text))
    except ValueError:
        for line in reversed(text.splitlines()):
            try:
                candidates.append(json.loads(line))
                break
            except ValueError:
                continue
    for parsed in candidates:
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            results = [m for m in parsed if isinstance(m, dict) and m.get("type") == "result"]
            if results:
                return results[-1]
    return None


def _envelope_error(envelope: Mapping[str, Any]) -> str | None:
    """Return a redacted error description, or ``None`` if the run succeeded."""
    subtype = envelope.get("subtype")
    failed = envelope.get("is_error") is True or (
        isinstance(subtype, str) and subtype.startswith("error")
    )
    if not failed:
        return None
    parts = [f"subtype={subtype}" if isinstance(subtype, str) else "is_error=true"]
    status = envelope.get("api_error_status")
    if isinstance(status, int) and not isinstance(status, bool):
        parts.append(f"HTTP {status}")
    reason = envelope.get("terminal_reason")
    if isinstance(reason, str):
        parts.append(f"terminal_reason={reason}")
    errors = envelope.get("errors")
    messages = [e for e in errors if isinstance(e, str)] if isinstance(errors, list) else []
    result = envelope.get("result")
    if isinstance(result, str):
        messages.append(result)
    if messages:
        parts.append(redact(" | ".join(messages)))
    return "; ".join(parts)


class ClaudeCliProvider(CliProviderBase):
    """Runs ``claude`` in non-interactive print mode with tools disabled."""

    name = "anthropic"
    command = "claude"
    native_schema = True
    extra_env = ("CLAUDE_CONFIG_DIR",)
    """Documented override of Claude Code's config directory; without it a user who
    set it would appear logged out. Passed through unread."""

    def native_schema_usable(self) -> bool:
        """``--json-schema`` is used only when the executable is not a batch shim."""
        try:
            executable = resolve_executable(self.command)
        except ProviderError:
            return False
        return not is_batch_shim(executable)

    def _schema_arg(self, request: CompletionRequest, executable: Path) -> str | None:
        """The ``--json-schema`` value, or ``None`` to use the prompt fallback."""
        if request.response_schema is None or is_batch_shim(executable):
            return None
        if not self.capabilities(request.model).structured_output:
            return None
        schema = json.dumps(request.response_schema, separators=(",", ":"), sort_keys=True)
        return schema if len(schema) <= MAX_ARGV_SCHEMA_CHARS else None

    def build_invocation(self, request: CompletionRequest, cwd: Path) -> CliInvocation:
        """Build the ``claude -p`` command line for ``request``.

        System messages from ``stable_prefix`` go into a system prompt file next to
        (not inside) ``cwd``; every other message goes on stdin.

        Raises:
            ProviderError: invalid prompt, or no registry ``cli_flags``.
            ProviderUnavailableError: ``claude`` not found.
        """
        prompt = request.prompt
        validate_prompt(prompt.stable_prefix, prompt.variable_suffix)
        executable = self.resolve()
        model_args = self.registry_argv(request)
        schema = self._schema_arg(request, executable)

        suffix = prompt.variable_suffix
        if request.response_schema is not None and schema is None:
            suffix = suffix_with_format_instructions(suffix, request.response_schema)
        system = [m.content for m in prompt.stable_prefix if m.role is Role.SYSTEM]
        conversation = [m for m in prompt.stable_prefix if m.role is not Role.SYSTEM]
        stdin_text = render_transcript((*conversation, *suffix))

        args: list[str] = ["-p", "--output-format", "json", "--safe-mode", *model_args]
        if system:
            system_file = inputs_dir(cwd) / "system.md"
            system_file.write_text("\n\n".join(system), encoding="utf-8")
            args += ["--system-prompt-file", str(system_file)]
        if schema is not None:
            args += ["--json-schema", schema]
        args += [*_FIXED_FLAGS, "--max-turns", str(MAX_TURNS_SCHEMA if schema else MAX_TURNS_TEXT)]
        return CliInvocation(
            executable=executable,
            args=tuple(args),
            stdin_text=stdin_text,
            cwd=cwd,
            timeout_s=self.timeout_for(request),
            max_output_bytes=self.max_output_bytes,
        )

    def parse_output(self, stdout: str, request: CompletionRequest) -> CompletionResult:
        """Parse the ``--output-format json`` result envelope.

        * Error results (``is_error`` or an ``error_*`` subtype) raise.
        * ``text`` is the JSON-serialized ``structured_output`` when a schema was
          requested and the field is present, otherwise ``result``.
        * ``finish_reason`` is the envelope's ``stop_reason`` (``"refusal"`` marks a
          refusal; the caller decides what to do with it).
        * ``total_cost_usd`` is a client-side estimate, not billing, and is ignored.

        Raises:
            ProviderError: unparseable output, an error result, or no text.
        """
        envelope = load_envelope(stdout)
        if envelope is None:
            raise ProviderError("claude returned output that is not a JSON result envelope")
        error = _envelope_error(envelope)
        if error is not None:
            raise ProviderError(f"claude reported an error: {error}")

        structured = envelope.get("structured_output")
        result = envelope.get("result")
        if request.response_schema is not None and structured is not None:
            text = json.dumps(structured, ensure_ascii=False)
        elif isinstance(result, str):
            text = result
        else:
            raise ProviderError("claude result envelope has no text result")

        stop_reason = envelope.get("stop_reason")
        model_usage = envelope.get("modelUsage")
        model = request.model
        if isinstance(model_usage, Mapping) and len(model_usage) == 1:
            (reported,) = model_usage.keys()
            if isinstance(reported, str) and reported:
                model = reported
        return CompletionResult(
            text=text,
            usage=map_usage(envelope.get("usage")),
            provider=self.name,
            model=model,
            backend=self.backend,
            finish_reason=stop_reason if isinstance(stop_reason, str) else None,
            started_at=utc_now(),
            latency_s=None,
            native_params_used={"cli_args": list(self.registry_argv(request))},
        )

    def describe_failure(self, stdout: str, stderr: str) -> str:
        """Claude prints in-run failures (such as missing login) as the stdout result."""
        envelope = load_envelope(stdout)
        if envelope is not None:
            error = _envelope_error(envelope)
            if error is not None:
                return f"{error}; stderr: {stderr}" if stderr else error
        return stderr
