"""Claude via the official ``claude`` CLI (Claude Code, experimental).

The user signs in to the unmodified ``claude`` binary with their own subscription;
llm-crossjury only launches it (ADR 0002). Every flag below was checked against the
official Claude Code documentation on 2026-11-08:

* CLI reference (flags): https://code.claude.com/docs/en/cli-reference
* Headless / ``-p`` usage, JSON envelope, ``--bare`` caveat:
  https://code.claude.com/docs/en/headless
* Structured output (``StructuredOutput`` tool, result subtypes):
  https://code.claude.com/docs/en/agent-sdk/structured-outputs
* Result message fields (``SDKResultMessage``) and the streamed user message
  (``SDKUserMessage``, whose ``message`` is an Anthropic ``MessageParam``):
  https://code.claude.com/docs/en/agent-sdk/typescript
* Image input through streaming input (base64 ``image`` blocks):
  https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode

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

Native attachments (ADR 0007, verified 2026-11-08 against the docs above and Claude
Code 2.1.317): when the request carries an image or PDF whose kind is in
``capabilities(model).input_modalities``, ``--output-format json`` is replaced by::

    --input-format stream-json --output-format stream-json --verbose

and stdin holds ONE newline-terminated ``SDKUserMessage`` JSON line whose
``message.content`` has base64 ``image``/``document`` blocks followed by the text. No
file path is written and no tool is needed, so tools stay disabled. Claude Code
rejects ``--input-format stream-json`` without ``--output-format stream-json``, and
``stream-json`` output in print mode without ``--verbose`` (both checked on the CLI
itself). ``--json-schema``, ``--max-turns`` and every fixed flag are unchanged. The
last ``result`` line of the stream has the same fields as the ``json`` envelope.
Without native attachments the plain ``json`` path above is used.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from llm_crossjury.core.providers.api._common import (
    attachment_base64,
    fallback_text_block,
    suffix_with_format_instructions,
    utc_now,
    validate_attachment_roles,
    validate_prompt,
)
from llm_crossjury.core.providers.base import (
    Attachment,
    AttachmentKind,
    Capabilities,
    CapabilityNotSupportedError,
    CompletionRequest,
    CompletionResult,
    Message,
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

MAX_TURNS_SCHEMA = 5
"""``StructuredOutput`` tool call and its result, plus room for validation retries.
Three turns failed about 1 in 50 chair calls in the M1.7 eval (``error_max_turns``);
no other tool is available, so extra turns cannot do anything else."""

MAX_ARGV_SCHEMA_CHARS = 8000
"""Longer schemas use the prompt fallback (Windows limits a command line to 32767
characters, and argv is visible in process listings)."""

NATIVE_KINDS: frozenset[AttachmentKind] = frozenset({AttachmentKind.IMAGE, AttachmentKind.PDF})
"""Attachment kinds this adapter can send natively (as ``image``/``document`` blocks
over stream-json stdin). A model gets the subset its registry entry declares."""

STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
"""The only tool name allowed to appear in the stream (the ``--json-schema`` answer)."""

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


def _json_lines(text: str) -> list[object]:
    """Every line of ``text`` that parses as JSON, in order (others are skipped)."""
    out: list[object] = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def load_envelope(stdout: str) -> dict[str, Any] | None:
    """Find the ``result`` envelope in ``json`` or ``stream-json`` stdout.

    Accepts a single JSON object, a JSON array of messages (the last ``result``
    message wins), or JSON lines (``stream-json``): the last line whose ``type`` is
    ``"result"`` wins, else the last parseable JSON line. Returns ``None`` if nothing
    usable is found.
    """
    text = stdout.strip()
    candidates: list[object] = []
    try:
        candidates.append(json.loads(text))
    except ValueError:
        lines = _json_lines(text)
        results = [m for m in lines if isinstance(m, dict) and m.get("type") == "result"]
        if results:
            candidates.append(results[-1])
        elif lines:
            candidates.append(lines[-1])
    for parsed in candidates:
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            results = [m for m in parsed if isinstance(m, dict) and m.get("type") == "result"]
            if results:
                return results[-1]
    return None


def unexpected_tools(stdout: str) -> list[str]:
    """Names of tools the model called in a ``stream-json`` run, other than
    :data:`STRUCTURED_OUTPUT_TOOL`.

    Tools are disabled, so this should always be empty; a non-empty list means the
    flags no longer hold and the answer is rejected. Non-stream output yields ``[]``.
    """
    names: list[str] = []
    for event in _json_lines(stdout.strip()):
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") not in _TOOL_BLOCKS:
                continue
            name = block.get("name")
            if name != STRUCTURED_OUTPUT_TOOL:
                names.append(name if isinstance(name, str) else "?")
    return names


_TOOL_BLOCKS = frozenset({"tool_use", "server_tool_use", "mcp_tool_use"})


def _attachment_block(attachment: Attachment) -> dict[str, Any]:
    """A native ``image`` or ``document`` content block with inline base64 data."""
    source = {
        "type": "base64",
        "media_type": attachment.media_type,
        "data": attachment_base64(attachment),
    }
    if attachment.kind is AttachmentKind.PDF:
        return {"type": "document", "source": source, "title": attachment.filename}
    return {"type": "image", "source": source}


def _message_text(message: Message, native: Sequence[Attachment], *, labelled: bool) -> str:
    """The message text plus the fallback blocks of its non-native attachments.

    Raises:
        CapabilityNotSupportedError: a non-native attachment has no text form.
    """
    parts = [message.content] if message.content else []
    for att in message.attachments:
        if att in native:
            continue
        if att.fallback_text is None:
            raise CapabilityNotSupportedError(
                f"this Claude Code model does not accept {att.kind.value} attachments "
                f"natively and {att.filename!r} has no text fallback"
            )
        parts.append(fallback_text_block(att))
    body = "\n\n".join(parts)
    return f"[{message.role.value}]\n{body}" if labelled else body


def stream_json_stdin(messages: Sequence[Message], native: Sequence[Attachment]) -> str:
    """One ``SDKUserMessage`` JSON line for ``--input-format stream-json``.

    The content blocks follow message order: each message's native attachments (as
    base64 blocks, files before text as Anthropic recommends), then its text (with
    the fallback blocks of non-native attachments). Several messages are labelled
    like :func:`render_transcript`. ``client_composed`` asks Claude Code to deliver
    the text as written (no ``@path`` expansion, no slash commands).

    Raises:
        CapabilityNotSupportedError: a non-native attachment has no text form.
    """
    labelled = not (len(messages) == 1 and messages[0].role is Role.USER)
    blocks: list[dict[str, Any]] = []
    for message in messages:
        blocks += [_attachment_block(a) for a in message.attachments if a in native]
        text = _message_text(message, native, labelled=labelled)
        if text:
            blocks.append({"type": "text", "text": text})
    line = {
        "type": "user",
        "message": {"role": "user", "content": blocks},
        "parent_tool_use_id": None,
        "client_composed": True,
    }
    return json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n"


def _inlined(message: Message) -> Message:
    """``message`` with every attachment replaced by its fallback text block.

    Used on the plain-text path, so no attachment is silently dropped.

    Raises:
        CapabilityNotSupportedError: an attachment has no text form.
    """
    if not message.attachments:
        return message
    return Message(role=message.role, content=_message_text(message, (), labelled=False))


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
    login_status_args = ("auth", "status")
    """``claude auth status`` exits 0 when signed in and 1 when not; it spends no tokens
    (https://code.claude.com/docs/en/cli-reference). Only the exit code is used."""

    def native_schema_usable(self) -> bool:
        """``--json-schema`` is used only when the executable is not a batch shim."""
        try:
            executable = resolve_executable(self.command)
        except ProviderError:
            return False
        return not is_batch_shim(executable)

    def capabilities(self, model: str) -> Capabilities:
        """Base CLI capabilities plus native ``input_modalities``.

        The model's registry ``input_modalities`` (a fact about the Anthropic model,
        ADR 0006) intersected with :data:`NATIVE_KINDS`, the kinds this adapter can
        deliver through stream-json stdin. Unknown or unverified models: none.
        """
        caps = super().capabilities(model)
        entry = self._entry(model)
        declared = frozenset(entry.input_modalities or ()) if entry is not None else frozenset()
        return caps.model_copy(update={"input_modalities": declared & NATIVE_KINDS})

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
        (not inside) ``cwd``; every other message goes on stdin. Attachments whose
        kind the model accepts natively switch stdin to one stream-json user message
        with base64 blocks (see the module docstring); other attachments are inlined
        as their canonical fallback text. Attachment data never enters argv.

        Raises:
            ProviderError: invalid prompt, attachments on a non-user message, or no
                registry ``cli_flags``.
            CapabilityNotSupportedError: an attachment can be sent neither natively
                nor as text (an image for a model without image input).
            ProviderUnavailableError: ``claude`` not found.
        """
        prompt = request.prompt
        validate_prompt(prompt.stable_prefix, prompt.variable_suffix)
        validate_attachment_roles((*prompt.stable_prefix, *prompt.variable_suffix))
        executable = self.resolve()
        model_args = self.registry_argv(request)
        schema = self._schema_arg(request, executable)

        suffix = prompt.variable_suffix
        if request.response_schema is not None and schema is None:
            suffix = suffix_with_format_instructions(suffix, request.response_schema)
        system = [m.content for m in prompt.stable_prefix if m.role is Role.SYSTEM]
        conversation = [m for m in prompt.stable_prefix if m.role is not Role.SYSTEM]
        messages = (*conversation, *suffix)
        modalities = self.capabilities(request.model).input_modalities
        native = [
            a
            for m in messages
            for a in m.attachments
            if a.kind is not AttachmentKind.DOCUMENT and a.kind in modalities
        ]
        if native:
            stdin_text = stream_json_stdin(messages, native)
            output = ["--input-format", "stream-json", "--output-format", "stream-json"]
            output.append("--verbose")
        else:
            stdin_text = render_transcript(tuple(_inlined(m) for m in messages))
            output = ["--output-format", "json"]

        args: list[str] = ["-p", *output, "--safe-mode", *model_args]
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
        """Parse the result envelope (``json``, or the last ``result`` line of
        ``stream-json``).

        * A ``stream-json`` run in which the model called any tool other than
          ``StructuredOutput`` raises (tools are disabled; this is a tripwire).
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
        tools = sorted(set(unexpected_tools(stdout)))
        if tools:
            raise ProviderError(
                f"claude used tools although they are disabled ({', '.join(tools)}); "
                "answer rejected"
            )

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
