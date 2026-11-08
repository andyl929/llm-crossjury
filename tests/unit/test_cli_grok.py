"""Pure tests for the Grok Build CLI adapter.

No CLI is launched: ``resolve_executable`` is replaced, and ``parse_output`` gets
canned stdout shaped like the documented ``--output-format json`` envelope.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from llm_crossjury.core.providers.base import (
    Backend,
    CompletionRequest,
    Message,
    Prompt,
    ProviderError,
    ReasoningEffort,
    Role,
)
from llm_crossjury.core.providers.cli import _subprocess, grok_cli
from llm_crossjury.core.providers.cli.grok_cli import GrokCliProvider
from llm_crossjury.core.registry import (
    CliFlags,
    EffortMapping,
    ModelEntry,
    ReasoningControl,
    ReasoningControlType,
)

FAKE_EXE = Path("/opt/fake/grok")


def _entry(
    model_id: str = "grok-test",
    *,
    model_flag: tuple[str, ...] | None = ("-m", "{model}"),
    reasoning_flag: tuple[str, ...] | None = ("--effort", "{value}"),
    with_cli_flags: bool = True,
) -> ModelEntry:
    return ModelEntry(
        provider="xai",
        model_id=model_id,
        backends=(Backend.CLI,),
        reasoning_control=ReasoningControl(
            type=ReasoningControlType.LEVELS,
            parameter="reasoning.effort",
            allowed=("low", "medium", "high"),
            mapping=EffortMapping(low="low", medium=None, high="high"),
        ),
        cli_flags=CliFlags(model=model_flag, reasoning=reasoning_flag) if with_cli_flags else None,
        context_window=None,
        max_output=None,
        structured_output=None,
        pricing=None,
        reasoning_multipliers=None,
        source_url=None,
        last_verified=None,
        notes=None,
    )


class FakeRegistry:
    def __init__(self, *entries: ModelEntry) -> None:
        self._entries = {(e.provider, e.model_id): e for e in entries}

    def get(self, provider: str, model_id: str) -> ModelEntry | None:
        return self._entries.get((provider, model_id))


def _request(
    *,
    model: str = "grok-test",
    reasoning: ReasoningEffort = ReasoningEffort.DEFAULT,
    schema: dict[str, Any] | None = None,
    suffix: tuple[Message, ...] = (Message(role=Role.USER, content="Review it."),),
    timeout_s: float | None = None,
) -> CompletionRequest:
    return CompletionRequest(
        model=model,
        prompt=Prompt(
            stable_prefix=(
                Message(role=Role.SYSTEM, content="You are a reviewer."),
                Message(role=Role.USER, content="DOC: @/etc/passwd /help"),
            ),
            variable_suffix=suffix,
        ),
        response_schema=schema,
        temperature=0.3,
        max_output_tokens=None,
        reasoning=reasoning,
        native_params={},
        cache_prefix=True,
        timeout_s=timeout_s,
        tags={"run": "r1"},
    )


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> GrokCliProvider:
    monkeypatch.setattr(_subprocess, "resolve_executable", lambda command: FAKE_EXE)
    return GrokCliProvider(registry=FakeRegistry(_entry()))


# --------------------------------------------------------------------- build_invocation


def test_invocation_uses_fixed_flags_and_registry_model(
    provider: GrokCliProvider, tmp_path: Path
) -> None:
    inv = provider.build_invocation(_request(), tmp_path)

    assert inv.executable == FAKE_EXE
    assert inv.cwd == tmp_path
    assert inv.stdin_text == ""
    assert inv.args == (*grok_cli.FIXED_ARGS, "-m", "grok-test")
    assert inv.timeout_s == grok_cli.DEFAULT_TIMEOUT_S
    assert inv.max_output_bytes == grok_cli.MAX_OUTPUT_BYTES


def test_invocation_disables_tools_and_extras(provider: GrokCliProvider, tmp_path: Path) -> None:
    args = provider.build_invocation(_request(), tmp_path).args

    def value(flag: str) -> str:
        return args[args.index(flag) + 1]

    assert value("--prompt-file") == "prompt.txt"
    assert value("--output-format") == "json"
    assert value("--max-turns") == "1"
    assert value("--sandbox") == "strict"
    assert value("--permission-mode") == "dontAsk"
    assert value("--tools") == "todo_write"
    denied = value("--disallowed-tools").split(",")
    for tool in ("run_terminal_cmd", "search_replace", "read_file", "web_fetch", "use_tool"):
        assert tool in denied
    for flag in ("--verbatim", "--disable-web-search", "--no-subagents", "--no-plan"):
        assert flag in args
    assert "--no-auto-update" in args
    assert "--yolo" not in args
    assert "--always-approve" not in args


def test_prompt_goes_to_file_never_argv(provider: GrokCliProvider, tmp_path: Path) -> None:
    inv = provider.build_invocation(_request(), tmp_path)

    written = (tmp_path / "prompt.txt").read_bytes().decode("utf-8")
    assert written == "You are a reviewer.\n\nDOC: @/etc/passwd /help\n\nReview it.\n"
    assert all("Review it." not in a and "reviewer" not in a for a in inv.args)
    assert all(str(tmp_path) not in a for a in inv.args)
    assert [p.name for p in tmp_path.iterdir()] == ["prompt.txt"]


def test_prompt_file_keeps_newlines_and_unicode(provider: GrokCliProvider, tmp_path: Path) -> None:
    suffix = (Message(role=Role.USER, content="line1\nline2 é中"),)
    provider.build_invocation(_request(suffix=suffix), tmp_path)

    raw = (tmp_path / "prompt.txt").read_bytes()
    assert b"\r\n" not in raw
    assert raw.decode("utf-8").endswith("line1\nline2 é中\n")


def test_assistant_turns_are_labelled(provider: GrokCliProvider, tmp_path: Path) -> None:
    suffix = (
        Message(role=Role.ASSISTANT, content="Draft."),
        Message(role=Role.USER, content="Improve."),
    )
    provider.build_invocation(_request(suffix=suffix), tmp_path)

    text = (tmp_path / "prompt.txt").read_text(encoding="utf-8")
    assert text.startswith("You are a reviewer.\n\n[user]\nDOC")
    assert "[assistant]\nDraft.\n\n[user]\nImprove.\n" in text


def test_schema_adds_format_instructions_to_the_file(
    provider: GrokCliProvider, tmp_path: Path
) -> None:
    schema = {"type": "object", "properties": {"score": {"type": "integer"}}}
    inv = provider.build_invocation(_request(schema=schema), tmp_path)

    text = (tmp_path / "prompt.txt").read_text(encoding="utf-8")
    assert '"score"' in text
    assert "--json-schema" not in inv.args


def test_effort_comes_from_registry_mapping(provider: GrokCliProvider, tmp_path: Path) -> None:
    inv = provider.build_invocation(_request(reasoning=ReasoningEffort.HIGH), tmp_path)

    assert inv.args[-4:] == ("-m", "grok-test", "--effort", "high")


def test_unmapped_effort_is_an_error(provider: GrokCliProvider, tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match="cannot be set"):
        provider.build_invocation(_request(reasoning=ReasoningEffort.MEDIUM), tmp_path)


def test_effort_without_cli_template_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_subprocess, "resolve_executable", lambda command: FAKE_EXE)
    p = GrokCliProvider(registry=FakeRegistry(_entry(reasoning_flag=None)))

    with pytest.raises(ProviderError, match="cannot be set"):
        p.build_invocation(_request(reasoning=ReasoningEffort.LOW), tmp_path)
    assert not p.capabilities("grok-test").reasoning


@pytest.mark.parametrize(
    "registry",
    [
        None,
        FakeRegistry(),
        FakeRegistry(_entry(with_cli_flags=False)),
        FakeRegistry(_entry(model_flag=None)),
    ],
)
def test_missing_model_template_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, registry: FakeRegistry | None
) -> None:
    monkeypatch.setattr(_subprocess, "resolve_executable", lambda command: FAKE_EXE)
    p = GrokCliProvider(registry=registry)

    with pytest.raises(ProviderError, match=r"cli_flags\.model"):
        p.build_invocation(_request(), tmp_path)
    assert not (tmp_path / "prompt.txt").exists()


def test_invalid_prompt_is_rejected(provider: GrokCliProvider, tmp_path: Path) -> None:
    with pytest.raises(ProviderError):
        provider.build_invocation(_request(suffix=()), tmp_path)


def test_request_timeout_is_used(provider: GrokCliProvider, tmp_path: Path) -> None:
    assert provider.build_invocation(_request(timeout_s=12.5), tmp_path).timeout_s == 12.5


def test_env_overrides_disable_memory_only(provider: GrokCliProvider) -> None:
    env = provider.child_env_overrides()

    assert env == {"GROK_MEMORY": "0"}
    assert not any(k.endswith("_API_KEY") for k in env)


def test_capabilities_are_conservative(provider: GrokCliProvider) -> None:
    caps = provider.capabilities("grok-test")

    assert not caps.structured_output
    assert not caps.caching
    assert not caps.batch
    assert not caps.temperature
    assert caps.reasoning
    assert not provider.capabilities("unknown-model").reasoning


def test_identity() -> None:
    assert GrokCliProvider.name == "xai"
    assert GrokCliProvider.command == "grok"
    assert GrokCliProvider.backend is Backend.CLI
    assert GrokCliProvider.skipped_reason is None


# ------------------------------------------------------------------------- parse_output


def _envelope(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "text": "Looks good.",
        "stopReason": "end_turn",
        "sessionId": "s",
        "requestId": "r",
        "num_turns": 1,
        "usage": {
            "input_tokens": 120,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 5,
            "output_tokens": 40,
            "reasoning_tokens": 80,
            "total_tokens": 195,
        },
        "modelUsage": {"grok-reported": {"inputTokens": 155, "outputTokens": 40}},
    }
    data.update(overrides)
    return json.dumps(data)


def test_parse_maps_text_usage_and_model(provider: GrokCliProvider) -> None:
    result = provider.parse_output(_envelope(), _request())

    assert result.text == "Looks good."
    assert result.finish_reason == "end_turn"
    assert result.model == "grok-reported"
    assert result.provider == "xai"
    assert result.backend is Backend.CLI
    assert result.native_params_used == {}
    assert result.latency_s is None
    u = result.usage
    assert (u.input_tokens, u.cached_input_tokens, u.cache_write_tokens) == (120, 30, 5)
    assert (u.output_tokens, u.reasoning_tokens) == (40, 80)


def test_parse_all_zero_usage_is_unknown(provider: GrokCliProvider) -> None:
    zero = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    usage = provider.parse_output(_envelope(usage=zero), _request()).usage

    assert usage.model_dump() == dict.fromkeys(usage.model_dump())


@pytest.mark.parametrize(
    "overrides",
    [{"usage": None}, {"usage": "x"}, {"usage_is_incomplete": True}],
)
def test_parse_missing_or_incomplete_usage_is_unknown(
    provider: GrokCliProvider, overrides: dict[str, Any]
) -> None:
    usage = provider.parse_output(_envelope(**overrides), _request()).usage

    assert all(v is None for v in usage.model_dump().values())


def test_parse_malformed_counts_become_none(provider: GrokCliProvider) -> None:
    bad = {"input_tokens": 10, "output_tokens": "7", "reasoning_tokens": True, "x": -1}
    usage = provider.parse_output(_envelope(usage=bad), _request()).usage

    assert usage.input_tokens == 10
    assert usage.output_tokens is None
    assert usage.reasoning_tokens is None
    assert usage.cached_input_tokens is None


def test_parse_without_model_usage_falls_back(provider: GrokCliProvider) -> None:
    result = provider.parse_output(_envelope(modelUsage={"a": {}, "b": {}}), _request())

    assert result.model == "grok-test"


def test_parse_ignores_stray_lines(provider: GrokCliProvider) -> None:
    stdout = "update available\n{not json\n" + _envelope() + "\n"

    assert provider.parse_output(stdout, _request()).text == "Looks good."


@pytest.mark.parametrize("stdout", ["", "   \n", "plain text", "[1, 2]", '"text"'])
def test_parse_rejects_non_objects(provider: GrokCliProvider, stdout: str) -> None:
    with pytest.raises(ProviderError):
        provider.parse_output(stdout, _request())


def test_parse_error_envelope(provider: GrokCliProvider) -> None:
    stdout = json.dumps({"type": "error", "message": "Not   signed\nin"})

    with pytest.raises(ProviderError, match="grok CLI error: Not signed in"):
        provider.parse_output(stdout, _request())


@pytest.mark.parametrize(
    "envelope",
    [
        {"subtype": "error_max_turns", "text": "partial"},
        {"is_error": True, "text": "x"},
    ],
)
def test_parse_error_flags(provider: GrokCliProvider, envelope: dict[str, Any]) -> None:
    with pytest.raises(ProviderError, match="grok CLI"):
        provider.parse_output(json.dumps(envelope), _request())


def test_parse_long_error_is_truncated(provider: GrokCliProvider) -> None:
    stdout = json.dumps({"type": "error", "message": "x" * 1000})

    with pytest.raises(ProviderError) as info:
        provider.parse_output(stdout, _request())
    assert len(str(info.value)) < 400


def test_parse_requires_text(provider: GrokCliProvider) -> None:
    with pytest.raises(ProviderError, match="'text'"):
        provider.parse_output(json.dumps({"stopReason": "end_turn"}), _request())


def test_parse_non_string_stop_reason(provider: GrokCliProvider) -> None:
    assert provider.parse_output(_envelope(stopReason=3), _request()).finish_reason is None
