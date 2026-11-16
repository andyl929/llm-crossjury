"""Native image/PDF input for the Claude Code CLI adapter (stream-json stdin, ADR 0007).

No real ``claude`` binary is used.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from llm_crossjury.core.attachments.prompt import with_attachments
from llm_crossjury.core.providers.base import (
    Attachment,
    AttachmentKind,
    CapabilityNotSupportedError,
    CompletionRequest,
    Message,
    Prompt,
    ProviderError,
    Role,
)
from llm_crossjury.core.providers.cli import claude_code
from llm_crossjury.core.providers.cli._subprocess import temp_workspace
from llm_crossjury.core.providers.cli.claude_code import (
    ClaudeCliProvider,
    load_envelope,
    unexpected_tools,
)
from llm_crossjury.core.registry.schema import CliFlags
from tests.unit.test_cli_subprocess import FakeCli, FakeRegistry, make_entry, make_request

MODEL = "claude-test-model"
FLAGS = CliFlags(model=("--model", "{model}"), reasoning=("--effort", "{value}"))
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
    "additionalProperties": False,
}

IMAGE_BYTES = b"\x89PNG-IMAGE-SECRET-BYTES"
PDF_BYTES = b"%PDF-1.4 PDF-SECRET-BYTES"


def _attachment(
    kind: AttachmentKind, data: bytes, filename: str, media_type: str, text: str | None
) -> Attachment:
    return Attachment(
        kind=kind,
        media_type=media_type,
        filename=filename,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
        fallback_text=text,
    )


IMAGE = _attachment(AttachmentKind.IMAGE, IMAGE_BYTES, "shot.png", "image/png", None)
PDF = _attachment(AttachmentKind.PDF, PDF_BYTES, "paper.pdf", "application/pdf", "PDF-TEXT")
DOCX = _attachment(
    AttachmentKind.DOCUMENT,
    b"PK-DOCX-BYTES",
    "notes.docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "DOCX-TEXT",
)


def _provider(modalities: tuple[AttachmentKind, ...] | None) -> ClaudeCliProvider:
    entry = make_entry(provider="anthropic", model_id=MODEL, cli_flags=FLAGS).model_copy(
        update={"input_modalities": modalities}
    )
    return ClaudeCliProvider(registry=FakeRegistry(entry))


def _request(*attachments: Attachment, schema: dict[str, Any] | None = None) -> CompletionRequest:
    base = make_request(model=MODEL, schema=schema)
    prompt = with_attachments(base.prompt, list(attachments))
    return base.model_copy(update={"prompt": prompt})


def _stdin_json(stdin: str) -> dict[str, Any]:
    assert stdin.endswith("\n")
    lines = stdin.splitlines()
    assert len(lines) == 1
    data: dict[str, Any] = json.loads(lines[0])
    return data


@pytest.fixture
def fake_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    exe = Path(sys.executable).with_name("claude.exe" if sys.platform == "win32" else "claude")
    monkeypatch.setattr(claude_code, "resolve_executable", lambda _cmd: exe)
    monkeypatch.setattr(
        "llm_crossjury.core.providers.cli._subprocess.resolve_executable", lambda _cmd: exe
    )


# ---------------------------------------------------------------- capabilities


def test_capabilities_follow_registry_within_native_kinds() -> None:
    caps = _provider((AttachmentKind.IMAGE, AttachmentKind.PDF, AttachmentKind.DOCUMENT))
    assert caps.capabilities(MODEL).input_modalities == {AttachmentKind.IMAGE, AttachmentKind.PDF}
    assert _provider(None).capabilities(MODEL).input_modalities == frozenset()
    assert _provider((AttachmentKind.IMAGE,)).capabilities(MODEL).input_modalities == {
        AttachmentKind.IMAGE
    }
    assert ClaudeCliProvider(registry=None).capabilities(MODEL).input_modalities == frozenset()


# ---------------------------------------------------------------- invocation


def test_native_image_and_pdf_go_as_base64_blocks_on_stdin(fake_exe: None) -> None:
    provider = _provider((AttachmentKind.IMAGE, AttachmentKind.PDF))
    with temp_workspace() as cwd:
        inv = provider.build_invocation(_request(IMAGE, PDF, DOCX), cwd)
        assert list(cwd.iterdir()) == []
    args = inv.args
    assert args[:6] == (
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
    )
    assert "json" not in args  # no plain --output-format json
    # Tools stay disabled exactly as on the text path.
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--disallowedTools") + 1] == "mcp__*"
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in args
    assert "--disable-slash-commands" in args
    assert args[args.index("--max-turns") + 1] == "1"
    # No attachment data, file name or prompt text in argv.
    joined = "\n".join(args)
    for secret in (
        base64.b64encode(IMAGE_BYTES).decode(),
        base64.b64encode(PDF_BYTES).decode(),
        "shot.png",
        "paper.pdf",
        "SECRET-PROMPT-TEXT",
        "DOCX-TEXT",
    ):
        assert secret not in joined

    line = _stdin_json(inv.stdin_text)
    assert line["type"] == "user"
    assert line["parent_tool_use_id"] is None
    assert line["client_composed"] is True
    assert line["message"]["role"] == "user"
    blocks = line["message"]["content"]
    by_type = [b["type"] for b in blocks]
    assert by_type[:2] == ["image", "document"] or by_type[:2] == ["document", "image"]
    image = next(b for b in blocks if b["type"] == "image")
    assert image["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.b64encode(IMAGE_BYTES).decode(),
    }
    document = next(b for b in blocks if b["type"] == "document")
    assert document["title"] == "paper.pdf"
    assert document["source"]["media_type"] == "application/pdf"
    assert document["source"]["data"] == base64.b64encode(PDF_BYTES).decode()
    texts = "\n".join(b["text"] for b in blocks if b["type"] == "text")
    assert "SECRET-PROMPT-TEXT" in texts
    assert "DOCX-TEXT" in texts  # DOCX is never native: inlined as fallback text
    assert "PDF-TEXT" not in texts  # the native PDF is not duplicated as text
    assert all(b["text"] for b in blocks if b["type"] == "text")


def test_schema_and_max_turns_still_apply_on_stream_path(fake_exe: None) -> None:
    provider = _provider((AttachmentKind.IMAGE,))
    with temp_workspace() as cwd:
        inv = provider.build_invocation(_request(IMAGE, schema=SCHEMA), cwd)
    assert "--input-format" in inv.args
    assert json.loads(inv.args[inv.args.index("--json-schema") + 1]) == SCHEMA
    assert inv.args[inv.args.index("--max-turns") + 1] == str(claude_code.MAX_TURNS_SCHEMA)


def test_without_native_kinds_the_text_path_inlines_fallback(fake_exe: None) -> None:
    provider = _provider((AttachmentKind.IMAGE,))
    with temp_workspace() as cwd:
        inv = provider.build_invocation(_request(PDF, DOCX), cwd)
    assert inv.args[:4] == ("-p", "--output-format", "json", "--safe-mode")
    assert "--input-format" not in inv.args
    assert "PDF-TEXT" in inv.stdin_text
    assert "DOCX-TEXT" in inv.stdin_text
    assert "PDF-SECRET-BYTES" not in inv.stdin_text


def test_image_without_support_is_refused_without_data(fake_exe: None) -> None:
    with temp_workspace() as cwd, pytest.raises(CapabilityNotSupportedError) as info:
        _provider(None).build_invocation(_request(IMAGE), cwd)
    message = str(info.value)
    assert "shot.png" in message
    assert base64.b64encode(IMAGE_BYTES).decode() not in message
    assert "SECRET-BYTES" not in message


def test_attachments_on_assistant_messages_are_rejected(fake_exe: None) -> None:
    base = make_request(model=MODEL)
    prompt = Prompt(
        stable_prefix=base.prompt.stable_prefix,
        variable_suffix=(
            Message(role=Role.ASSISTANT, content="earlier", attachments=(IMAGE,)),
            Message(role=Role.USER, content="now"),
        ),
    )
    request = base.model_copy(update={"prompt": prompt})
    with temp_workspace() as cwd, pytest.raises(ProviderError, match="user messages only"):
        _provider((AttachmentKind.IMAGE,)).build_invocation(request, cwd)


# ---------------------------------------------------------------- stream-json parsing


def _stream(*events: dict[str, Any]) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


_INIT = {"type": "system", "subtype": "init", "tools": [], "model": MODEL}
_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "Red",
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 2, "cache_creation_input_tokens": 766, "output_tokens": 4},
    "modelUsage": {"claude-reported-model": {}},
}


def _assistant(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(content)}}


def test_parse_stream_json_result() -> None:
    stdout = _stream(_INIT, _assistant({"type": "text", "text": "Red"}), _RESULT)
    result = _provider((AttachmentKind.IMAGE,)).parse_output(stdout, _request(IMAGE))
    assert result.text == "Red"
    assert result.model == "claude-reported-model"
    assert result.finish_reason == "end_turn"
    assert result.usage.input_tokens == 2
    assert result.usage.cache_write_tokens == 766
    assert result.usage.cached_input_tokens is None


def test_parse_stream_json_takes_last_result_and_skips_noise() -> None:
    first = dict(_RESULT, result="old")
    stdout = "not json\n" + _stream(_INIT, first, _RESULT, {"type": "system", "subtype": "x"})
    envelope = load_envelope(stdout)
    assert envelope is not None
    assert envelope["result"] == "Red"


def test_parse_stream_json_structured_output() -> None:
    result_event = dict(_RESULT, structured_output={"verdict": "ok"}, stop_reason="tool_use")
    stdout = _stream(
        _INIT,
        _assistant({"type": "tool_use", "name": "StructuredOutput", "input": {"verdict": "ok"}}),
        result_event,
    )
    result = _provider((AttachmentKind.IMAGE,)).parse_output(stdout, _request(IMAGE, schema=SCHEMA))
    assert json.loads(result.text) == {"verdict": "ok"}


def test_parse_stream_json_error_result_raises() -> None:
    error = {"type": "result", "subtype": "error_during_execution", "is_error": True, "errors": []}
    with pytest.raises(ProviderError, match="error_during_execution"):
        _provider((AttachmentKind.IMAGE,)).parse_output(_stream(_INIT, error), _request(IMAGE))


def test_parse_stream_json_without_result_raises() -> None:
    stdout = _stream(_INIT, _assistant({"type": "text", "text": "partial"}))
    with pytest.raises(ProviderError):
        _provider((AttachmentKind.IMAGE,)).parse_output(stdout, _request(IMAGE))


def test_tool_use_in_stream_is_rejected() -> None:
    stdout = _stream(_INIT, _assistant({"type": "tool_use", "name": "Read", "input": {}}), _RESULT)
    assert unexpected_tools(stdout) == ["Read"]
    with pytest.raises(ProviderError, match="Read"):
        _provider((AttachmentKind.IMAGE,)).parse_output(stdout, _request(IMAGE))
    assert unexpected_tools(json.dumps(_RESULT)) == []


# ---------------------------------------------------------------- end to end


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCli:
    root = tmp_path / "bin"
    root.mkdir()
    fake = FakeCli(root, "claude")
    fake.install_launcher(monkeypatch)
    return fake


def test_complete_with_image_end_to_end(fake_claude: FakeCli) -> None:
    fake_claude.set(response=_stream(_INIT, _assistant({"type": "text", "text": "Red"}), _RESULT))
    provider = _provider((AttachmentKind.IMAGE,))
    result = asyncio.run(provider.complete(_request(IMAGE)))
    rec = fake_claude.record()
    assert result.text == "Red"
    argv: list[str] = rec["argv"]
    assert argv[:6] == [
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    assert argv[argv.index("--tools") + 1] == ""
    encoded = base64.b64encode(IMAGE_BYTES).decode()
    assert not any(encoded in a or "shot.png" in a for a in argv)
    line = _stdin_json(rec["stdin"])
    image = next(b for b in line["message"]["content"] if b["type"] == "image")
    assert image["source"]["data"] == encoded
    assert rec["cwd_entries"] == []


def test_failed_run_error_has_no_attachment_data(fake_claude: FakeCli) -> None:
    encoded = base64.b64encode(IMAGE_BYTES).decode()
    fake_claude.set(response="", stderr=f"bad request {encoded}", exit_code=1)
    with pytest.raises(ProviderError) as info:
        asyncio.run(_provider((AttachmentKind.IMAGE,)).complete(_request(IMAGE)))
    assert encoded not in str(info.value)
    assert "SECRET-BYTES" not in str(info.value)
