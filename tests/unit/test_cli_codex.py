"""Codex CLI adapter: argv construction and JSONL parsing on canned output (no real CLI)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest

from llm_crossjury.core.providers.base import (
    Attachment,
    AttachmentKind,
    Backend,
    CapabilityNotSupportedError,
    CompletionRequest,
    Message,
    Prompt,
    ProviderError,
    ProviderUnavailableError,
    ReasoningEffort,
    Role,
    Usage,
)
from llm_crossjury.core.providers.cli import codex
from llm_crossjury.core.providers.cli._subprocess import CliInvocation
from llm_crossjury.core.providers.cli.codex import (
    BASE_ARGS,
    INSTRUCTIONS_FILE,
    LOCKDOWN_OVERRIDES,
    SCHEMA_FILE,
    CodexCliProvider,
    map_usage,
)
from llm_crossjury.core.registry.schema import (
    CliFlags,
    EffortMapping,
    ModelEntry,
    ReasoningControl,
    ReasoningControlType,
)

T = TypeVar("T")
MODEL = "test-model-1"
FAKE_EXE = Path("/fake/bin/codex")
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
    "additionalProperties": False,
}


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


class FakeRegistry:
    def __init__(self, *entries: ModelEntry) -> None:
        self._entries = {(e.provider, e.model_id): e for e in entries}

    def get(self, provider: str, model_id: str) -> ModelEntry | None:
        return self._entries.get((provider, model_id))


def make_entry(
    *,
    model_id: str = MODEL,
    cli_flags: CliFlags | None = None,
    structured_output: bool | None = True,
    control: ReasoningControlType = ReasoningControlType.LEVELS,
    input_modalities: tuple[AttachmentKind, ...] | None = None,
) -> ModelEntry:
    mappable = control in (ReasoningControlType.LEVELS, ReasoningControlType.BUDGET)
    return ModelEntry(
        provider="openai",
        model_id=model_id,
        backends=(Backend.API, Backend.CLI),
        reasoning_control=ReasoningControl(
            type=control,
            parameter="reasoning.effort" if mappable else None,
            allowed=("lo", "mid", "hi") if mappable else None,
            mapping=EffortMapping(low="lo", medium="mid", high=None) if mappable else None,
        ),
        cli_flags=cli_flags
        if cli_flags is not None
        else CliFlags(model=("-m", "{model}"), reasoning=("-c", "model_reasoning_effort={value}")),
        context_window=None,
        max_output=None,
        structured_output=structured_output,
        pricing=None,
        reasoning_multipliers=None,
        source_url=None,
        last_verified=None,
        notes=None,
        input_modalities=input_modalities,
    )


def make_provider(*entries: ModelEntry) -> CodexCliProvider:
    return CodexCliProvider(registry=FakeRegistry(*(entries or (make_entry(),))))


def make_request(**overrides: Any) -> CompletionRequest:
    fields: dict[str, Any] = {
        "model": MODEL,
        "prompt": Prompt(
            stable_prefix=(
                Message(role=Role.SYSTEM, content="You are a careful reviewer."),
                Message(role=Role.USER, content="Document: hello world"),
            ),
            variable_suffix=(Message(role=Role.USER, content="Review it; & | \" ' % ^"),),
        ),
        "response_schema": None,
        "temperature": 0.3,
        "max_output_tokens": 100,
        "reasoning": ReasoningEffort.DEFAULT,
        "native_params": {"ignored": True},
        "cache_prefix": True,
        "timeout_s": None,
        "tags": {"run": "r1"},
    }
    fields.update(overrides)
    return CompletionRequest(**fields)


@pytest.fixture(autouse=True)
def fake_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(command: str) -> Path:
        assert command == "codex"
        return FAKE_EXE

    monkeypatch.setattr(codex, "resolve_executable", resolve)


def jsonl(*events: dict[str, Any]) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


USAGE = {
    "input_tokens": 1000,
    "cached_input_tokens": 600,
    "cache_write_input_tokens": 100,
    "output_tokens": 50,
    "reasoning_output_tokens": 20,
}


def ok_stream(text: str = "final answer", usage: dict[str, Any] | None = None) -> str:
    return jsonl(
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "hmm"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "draft"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": USAGE if usage is None else usage},
    )


# ---------------------------------------------------------------- capabilities


def test_capabilities_from_entry() -> None:
    caps = make_provider().capabilities(MODEL)
    assert caps.structured_output is True
    assert caps.reasoning is True
    assert (caps.caching, caps.batch, caps.temperature) == (False, False, False)


def test_capabilities_unregistered_or_unverified() -> None:
    provider = make_provider(make_entry(structured_output=None))
    assert provider.capabilities(MODEL).structured_output is False
    unknown = provider.capabilities("other")
    assert not any(unknown.model_dump().values())
    assert not any(CodexCliProvider().capabilities(MODEL).model_dump().values())


def test_capabilities_images_for_registered_models() -> None:
    assert make_provider().capabilities(MODEL).input_modalities == {AttachmentKind.IMAGE}
    verified = make_entry(input_modalities=(AttachmentKind.IMAGE, AttachmentKind.PDF))
    assert make_provider(verified).capabilities(MODEL).input_modalities == {AttachmentKind.IMAGE}
    text_only = make_entry(input_modalities=())
    assert make_provider(text_only).capabilities(MODEL).input_modalities == frozenset()
    assert make_provider().capabilities("other").input_modalities == frozenset()


def test_capabilities_reasoning_needs_cli_template() -> None:
    entry = make_entry(cli_flags=CliFlags(model=("-m", "{model}"), reasoning=None))
    assert make_provider(entry).capabilities(MODEL).reasoning is False
    entry = make_entry(control=ReasoningControlType.ALWAYS_ON)
    assert make_provider(entry).capabilities(MODEL).reasoning is False


# ---------------------------------------------------------------- build_invocation


def test_build_invocation_argv_is_fixed(tmp_path: Path) -> None:
    inv = make_provider().build_invocation(make_request(), tmp_path)
    assert isinstance(inv, CliInvocation)
    assert inv.executable == FAKE_EXE
    assert inv.cwd == tmp_path
    args = list(inv.args)
    assert tuple(args[: len(BASE_ARGS)]) == BASE_ARGS
    assert args[-1] == "-"
    for override in LOCKDOWN_OVERRIDES:
        index = args.index(override)
        assert args[index - 1] == "-c"
    assert args[args.index("-m") : args.index("-m") + 2] == ["-m", MODEL]
    assert f"model_instructions_file={INSTRUCTIONS_FILE}" in args
    # No prompt text, no absolute path, no reasoning flag for DEFAULT.
    joined = " ".join(args)
    assert "Review it" not in joined
    assert "hello world" not in joined
    assert str(tmp_path) not in joined
    assert not any("model_reasoning_effort" in a for a in args)
    assert "--output-schema" not in args


def test_build_invocation_lockdown_settings(tmp_path: Path) -> None:
    args = make_provider().build_invocation(make_request(), tmp_path).args
    for required in (
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--json",
        "approval_policy=never",
        "features.shell_tool=false",
        "web_search=disabled",
    ):
        assert required in args
    assert "danger-full-access" not in args
    assert "--dangerously-bypass-approvals-and-sandbox" not in args


def test_build_invocation_prompt_on_stdin_and_files_in_cwd(tmp_path: Path) -> None:
    inv = make_provider().build_invocation(make_request(), tmp_path)
    assert inv.stdin_text == "Document: hello world\n\nReview it; & | \" ' % ^"
    assert (tmp_path / INSTRUCTIONS_FILE).read_text(encoding="utf-8") == (
        "You are a careful reviewer."
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == [INSTRUCTIONS_FILE]
    assert inv.timeout_s == codex.DEFAULT_TIMEOUT_S
    assert inv.max_output_bytes == codex.MAX_OUTPUT_BYTES


def test_build_invocation_without_system_message(tmp_path: Path) -> None:
    prompt = Prompt(stable_prefix=(), variable_suffix=(Message(role=Role.USER, content="hi"),))
    inv = make_provider().build_invocation(make_request(prompt=prompt, timeout_s=5.0), tmp_path)
    assert not any(a.startswith("model_instructions_file") for a in inv.args)
    assert list(tmp_path.iterdir()) == []
    assert inv.stdin_text == "hi"
    assert inv.timeout_s == 5.0


def test_build_invocation_renders_assistant_turns(tmp_path: Path) -> None:
    prompt = Prompt(
        stable_prefix=(),
        variable_suffix=(
            Message(role=Role.USER, content="q1"),
            Message(role=Role.ASSISTANT, content="a1"),
            Message(role=Role.USER, content="q2"),
        ),
    )
    inv = make_provider().build_invocation(make_request(prompt=prompt), tmp_path)
    assert inv.stdin_text == "[user]\nq1\n\n[assistant]\na1\n\n[user]\nq2"


def test_build_invocation_reasoning_from_registry(tmp_path: Path) -> None:
    inv = make_provider().build_invocation(make_request(reasoning=ReasoningEffort.MEDIUM), tmp_path)
    args = list(inv.args)
    index = args.index("model_reasoning_effort=mid")
    assert args[index - 1] == "-c"


def test_build_invocation_unmappable_reasoning_raises(tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match="reasoning"):
        make_provider().build_invocation(make_request(reasoning=ReasoningEffort.HIGH), tmp_path)
    entry = make_entry(cli_flags=CliFlags(model=("-m", "{model}"), reasoning=None))
    with pytest.raises(ProviderError, match="reasoning"):
        make_provider(entry).build_invocation(make_request(reasoning=ReasoningEffort.LOW), tmp_path)


@pytest.mark.parametrize(
    "provider",
    [
        CodexCliProvider(),
        CodexCliProvider(registry=FakeRegistry()),
        CodexCliProvider(registry=FakeRegistry(make_entry(model_id="other"))),
    ],
)
def test_build_invocation_needs_registry_entry(provider: CodexCliProvider, tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match="cli_flags"):
        provider.build_invocation(make_request(), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_build_invocation_needs_model_template(tmp_path: Path) -> None:
    entry = make_entry(cli_flags=CliFlags(model=None, reasoning=None))
    with pytest.raises(ProviderError, match="cli_flags"):
        make_provider(entry).build_invocation(make_request(), tmp_path)


def test_build_invocation_native_schema(tmp_path: Path) -> None:
    inv = make_provider().build_invocation(make_request(response_schema=SCHEMA), tmp_path)
    args = list(inv.args)
    assert args[args.index("--output-schema") + 1] == SCHEMA_FILE
    assert json.loads((tmp_path / SCHEMA_FILE).read_text(encoding="utf-8")) == SCHEMA
    assert "JSON Schema" not in inv.stdin_text


def test_build_invocation_schema_fallback(tmp_path: Path) -> None:
    provider = make_provider(make_entry(structured_output=None))
    inv = provider.build_invocation(make_request(response_schema=SCHEMA), tmp_path)
    assert "--output-schema" not in inv.args
    assert not (tmp_path / SCHEMA_FILE).exists()
    assert inv.stdin_text.startswith("Document: hello world\n\nReview it")
    assert '"verdict"' in inv.stdin_text


def test_build_invocation_rejects_invalid_prompt(tmp_path: Path) -> None:
    prompt = Prompt(stable_prefix=(), variable_suffix=())
    with pytest.raises(ProviderError):
        make_provider().build_invocation(make_request(prompt=prompt), tmp_path)


def attachment(
    filename: str, media_type: str, data: bytes, *, fallback: str | None = None
) -> Attachment:
    kind = (
        AttachmentKind.IMAGE
        if media_type.startswith("image/")
        else AttachmentKind.PDF
        if media_type == "application/pdf"
        else AttachmentKind.DOCUMENT
    )
    return Attachment(
        kind=kind,
        media_type=media_type,
        filename=filename,
        sha256="ab" * 32,
        data=data,
        fallback_text=fallback,
    )


PNG = b"\x89PNG\r\n\x1a\nfake-png"
JPEG = b"\xff\xd8\xfffake-jpeg"


def image_request(*attachments: Attachment) -> CompletionRequest:
    prompt = Prompt(
        stable_prefix=(
            Message(role=Role.SYSTEM, content="You are a careful reviewer."),
            Message(role=Role.USER, content="Files attached by the user:", attachments=attachments),
        ),
        variable_suffix=(Message(role=Role.USER, content="What colour is it?"),),
    )
    return make_request(prompt=prompt)


def test_build_invocation_images_as_relative_files(tmp_path: Path) -> None:
    request = image_request(
        attachment("My Screenshot (1) & co.png", "image/png", PNG),
        attachment("C:\\evil\\photo.jpeg", "image/jpeg", JPEG),
    )
    inv = make_provider().build_invocation(request, tmp_path)
    args = list(inv.args)
    assert tuple(args[: len(BASE_ARGS)]) == BASE_ARGS
    images = [args[i + 1] for i, a in enumerate(args) if a == "--image"]
    assert images == ["img-1.png", "img-2.jpg"]
    assert (tmp_path / "img-1.png").read_bytes() == PNG
    assert (tmp_path / "img-2.jpg").read_bytes() == JPEG
    # --image is greedy: the token after each value is another flag, never the "-" prompt.
    for i, a in enumerate(args):
        if a == "--image":
            assert args[i + 2].startswith("-")
            assert args[i + 2] != "-"
    assert args[-1] == "-"
    joined = " ".join(args)
    assert "Screenshot" not in joined
    assert "photo" not in joined
    assert str(tmp_path) not in joined
    assert inv.stdin_text == "Files attached by the user:\n\nWhat colour is it?"


def test_build_invocation_non_images_are_inlined_as_text(tmp_path: Path) -> None:
    request = image_request(
        attachment("report.pdf", "application/pdf", b"%PDF", fallback="pdf text"),
        attachment("notes.docx", "application/octet-stream", b"PK", fallback="docx text"),
    )
    inv = make_provider().build_invocation(request, tmp_path)
    assert "--image" not in inv.args
    assert "pdf text" in inv.stdin_text
    assert "docx text" in inv.stdin_text
    assert '<attachment name="report.pdf"' in inv.stdin_text
    assert sorted(p.name for p in tmp_path.iterdir()) == [INSTRUCTIONS_FILE]


@pytest.mark.parametrize(
    ("entry", "media_type"),
    [(make_entry(), "image/bmp"), (make_entry(input_modalities=()), "image/png")],
)
def test_build_invocation_unsendable_image_raises(
    entry: ModelEntry, media_type: str, tmp_path: Path
) -> None:
    request = image_request(attachment("pic", media_type, PNG))
    with pytest.raises(CapabilityNotSupportedError, match="'pic'"):
        make_provider(entry).build_invocation(request, tmp_path)


# ---------------------------------------------------------------- parse_output


def test_parse_output_last_agent_message_and_usage() -> None:
    result = make_provider().parse_output(ok_stream(), make_request())
    assert result.text == "final answer"
    assert result.usage == Usage(
        input_tokens=300,
        cached_input_tokens=600,
        cache_write_tokens=100,
        output_tokens=30,
        reasoning_tokens=20,
    )
    assert result.provider == "openai"
    assert result.model == MODEL
    assert result.backend is Backend.CLI
    assert result.finish_reason == "completed"
    assert result.native_params_used == {}


def test_parse_output_ignores_noise_and_unknown_events() -> None:
    stdout = "Reading prompt from stdin...\n\n[1, 2]\n" + jsonl(
        {"type": "some.future.event", "data": 1},
        {"type": "item.completed", "item": "not-a-dict"},
        {"type": "item.completed", "item": {"id": "x", "type": "todo_list", "items": []}},
        {"type": "item.completed", "item": {"id": "e", "type": "error", "message": "warn"}},
        {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": USAGE},
    )
    assert make_provider().parse_output(stdout, make_request()).text == "ok"


def test_parse_output_structured_text_is_returned_verbatim() -> None:
    payload = json.dumps({"verdict": "pass"})
    result = make_provider().parse_output(ok_stream(text=payload), make_request())
    assert json.loads(result.text) == {"verdict": "pass"}


def test_map_usage_unknown_fields_are_none() -> None:
    assert map_usage(None) == Usage(
        input_tokens=None,
        cached_input_tokens=None,
        cache_write_tokens=None,
        output_tokens=None,
        reasoning_tokens=None,
    )
    older_cli = {
        "input_tokens": 100,
        "cached_input_tokens": 40,
        "output_tokens": 10,
        "reasoning_output_tokens": 0,
    }
    assert map_usage(older_cli) == Usage(
        input_tokens=60,
        cached_input_tokens=40,
        cache_write_tokens=None,
        output_tokens=10,
        reasoning_tokens=0,
    )
    partial = {"input_tokens": 100, "output_tokens": "x", "reasoning_output_tokens": True}
    assert map_usage(partial) == Usage(
        input_tokens=None,
        cached_input_tokens=None,
        cache_write_tokens=None,
        output_tokens=None,
        reasoning_tokens=None,
    )
    inconsistent = {**USAGE, "cached_input_tokens": 5000, "reasoning_output_tokens": 99}
    usage = map_usage(inconsistent)
    assert usage.input_tokens is None
    assert usage.output_tokens is None


def test_parse_output_turn_failed() -> None:
    stdout = jsonl(
        {"type": "turn.started"},
        {"type": "error", "message": "stream error"},
        {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}},
    )
    with pytest.raises(ProviderError, match="usage limit"):
        make_provider().parse_output(stdout, make_request())


def test_parse_output_error_without_completion() -> None:
    stdout = jsonl({"type": "turn.started"}, {"type": "error", "message": "model not found"})
    with pytest.raises(ProviderError, match="model not found"):
        make_provider().parse_output(stdout, make_request())


def test_parse_output_transient_error_then_completed() -> None:
    stdout = jsonl({"type": "error", "message": "Reconnecting... 1/5"}) + ok_stream()
    assert make_provider().parse_output(stdout, make_request()).text == "final answer"


@pytest.mark.parametrize("stdout", ["", "not json\n", jsonl({"type": "turn.started"})])
def test_parse_output_no_completed_turn(stdout: str) -> None:
    with pytest.raises(ProviderError, match="without a completed turn"):
        make_provider().parse_output(stdout, make_request())


def test_parse_output_no_agent_message() -> None:
    stdout = jsonl({"type": "turn.completed", "usage": USAGE})
    with pytest.raises(ProviderError, match="without an agent message"):
        make_provider().parse_output(stdout, make_request())


@pytest.mark.parametrize(
    "item_type", ["command_execution", "file_change", "mcp_tool_call", "web_search"]
)
def test_parse_output_rejects_tool_use(item_type: str) -> None:
    stdout = (
        jsonl(
            {
                "type": "item.started",
                "item": {"id": "t", "type": item_type, "status": "in_progress"},
            }
        )
        + ok_stream()
    )
    with pytest.raises(ProviderError, match="tools are disabled"):
        make_provider().parse_output(stdout, make_request())


# ---------------------------------------------------------------- check


class FakeRun:
    def __init__(self, results: dict[tuple[str, ...], tuple[str, str, int]]) -> None:
        self.results = results
        self.calls: list[CliInvocation] = []

    async def __call__(self, invocation: CliInvocation) -> tuple[str, str, int]:
        self.calls.append(invocation)
        return self.results[invocation.args]


def test_check_signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun(
        {
            ("--version",): ("codex-cli 9.9.9\n", "", 0),
            ("login", "status"): ("", "API key configured - secret-fragment", 0),
        }
    )
    monkeypatch.setattr(codex, "run_cli", fake)
    status = run(make_provider().check())
    assert status.available is True
    assert status.version == "codex-cli 9.9.9"
    assert status.detail == "codex-cli 9.9.9; signed in"
    assert "secret" not in status.detail
    assert [c.args for c in fake.calls] == [("--version",), ("login", "status")]
    assert all(c.stdin_text == "" and c.executable == FAKE_EXE for c in fake.calls)


def test_check_not_signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun(
        {
            ("--version",): ("codex-cli 9.9.9\n", "", 0),
            ("login", "status"): ("", "Not logged in", 1),
        }
    )
    monkeypatch.setattr(codex, "run_cli", fake)
    status = run(make_provider().check())
    assert status.available is False
    assert "not signed in" in status.detail
    assert status.version == "codex-cli 9.9.9"


def test_check_version_failure_skips_login(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun({("--version",): ("", "boom", 2)})
    monkeypatch.setattr(codex, "run_cli", fake)
    status = run(make_provider().check())
    assert status.available is False
    assert status.version is None
    assert len(fake.calls) == 1


def test_check_run_errors_are_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing(invocation: CliInvocation) -> tuple[str, str, int]:
        raise ProviderError("timed out")

    monkeypatch.setattr(codex, "run_cli", failing)
    assert run(make_provider().check()).available is False


def test_check_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(command: str) -> Path:
        raise ProviderUnavailableError("not found")

    monkeypatch.setattr(codex, "resolve_executable", missing)
    status = run(make_provider().check())
    assert status.available is False
    assert "not found on PATH" in status.detail
