"""Tests for the Claude Code CLI adapter (no real ``claude`` binary is used)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from llm_crossjury.core.providers.base import (
    Message,
    Prompt,
    ProviderError,
    ReasoningEffort,
    Role,
)
from llm_crossjury.core.providers.cli import claude_code
from llm_crossjury.core.providers.cli._subprocess import temp_workspace
from llm_crossjury.core.providers.cli.claude_code import (
    ClaudeCliProvider,
    load_envelope,
    map_usage,
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


def _provider(*, structured_output: bool | None = True) -> ClaudeCliProvider:
    entry = make_entry(
        provider="anthropic",
        model_id=MODEL,
        cli_flags=FLAGS,
        structured_output=structured_output,
    )
    return ClaudeCliProvider(registry=FakeRegistry(entry))


def _envelope(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 1200,
        "num_turns": 1,
        "result": "Hello there.",
        "stop_reason": "end_turn",
        "session_id": "abc",
        "total_cost_usd": 0.0123,
        "usage": {
            "input_tokens": 11,
            "cache_creation_input_tokens": 7,
            "cache_read_input_tokens": 5,
            "output_tokens": 3,
        },
        "modelUsage": {MODEL: {"inputTokens": 11}},
        "permission_denials": [],
    }
    data.update(overrides)
    return json.dumps(data)


@pytest.fixture
def fake_exe(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Make ``claude`` resolve to a native (non-shim) executable path."""
    holder = [Path(sys.executable).with_name("claude.exe" if sys.platform == "win32" else "claude")]
    monkeypatch.setattr(claude_code, "resolve_executable", lambda _cmd: holder[0])
    monkeypatch.setattr(
        "llm_crossjury.core.providers.cli._subprocess.resolve_executable", lambda _cmd: holder[0]
    )
    return holder


# ---------------------------------------------------------------- invocation


def test_invocation_flags_and_stdin(fake_exe: list[Path]) -> None:
    provider = _provider()
    request = make_request(model=MODEL, reasoning=ReasoningEffort.HIGH)
    with temp_workspace() as cwd:
        inv = provider.build_invocation(request, cwd)
        system_file = Path(inv.args[inv.args.index("--system-prompt-file") + 1])
        assert system_file.read_text(encoding="utf-8") == "SYSTEM-INSTRUCTIONS"
        assert list(cwd.iterdir()) == []
        assert system_file.parent != cwd
    args = inv.args
    assert args[:4] == ("-p", "--output-format", "json", "--safe-mode")
    assert "--bare" not in args
    for flag in (
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ):
        assert flag in args
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--disallowedTools") + 1] == "mcp__*"
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--max-turns") + 1] == "1"
    assert args[args.index("--model") + 1] == MODEL
    assert args[args.index("--effort") + 1] == "high"
    assert "--json-schema" not in args
    assert inv.stdin_text == "SECRET-PROMPT-TEXT"
    assert not any("SECRET-PROMPT-TEXT" in a or "SYSTEM-INSTRUCTIONS" in a for a in args)
    assert inv.timeout_s == 30.0


def test_invocation_without_system_has_no_system_file(fake_exe: list[Path]) -> None:
    with temp_workspace() as cwd:
        inv = _provider().build_invocation(make_request(model=MODEL, system=None), cwd)
    assert "--system-prompt-file" not in inv.args


def test_invocation_requires_registry_flags(fake_exe: list[Path]) -> None:
    with temp_workspace() as cwd, pytest.raises(ProviderError, match="cli_flags"):
        ClaudeCliProvider().build_invocation(make_request(model=MODEL), cwd)


def test_native_schema_in_argv_for_real_executable(fake_exe: list[Path]) -> None:
    provider = _provider()
    assert provider.capabilities(MODEL).structured_output
    with temp_workspace() as cwd:
        inv = provider.build_invocation(make_request(model=MODEL, schema=SCHEMA), cwd)
    assert json.loads(inv.args[inv.args.index("--json-schema") + 1]) == SCHEMA
    assert inv.args[inv.args.index("--max-turns") + 1] == str(claude_code.MAX_TURNS_SCHEMA)
    assert inv.stdin_text == "SECRET-PROMPT-TEXT"


@pytest.mark.parametrize("shim", ["claude.cmd", "CLAUDE.BAT"])
def test_schema_never_in_argv_for_batch_shims(fake_exe: list[Path], shim: str) -> None:
    fake_exe[0] = fake_exe[0].with_name(shim)
    provider = _provider()
    assert not provider.capabilities(MODEL).structured_output
    with temp_workspace() as cwd:
        inv = provider.build_invocation(make_request(model=MODEL, schema=SCHEMA), cwd)
    assert "--json-schema" not in inv.args
    assert inv.args[inv.args.index("--max-turns") + 1] == "1"
    assert inv.stdin_text.startswith("SECRET-PROMPT-TEXT")
    assert '"verdict"' in inv.stdin_text  # format instructions carry the schema


def test_schema_fallback_when_registry_unverified(fake_exe: list[Path]) -> None:
    provider = _provider(structured_output=None)
    with temp_workspace() as cwd:
        inv = provider.build_invocation(make_request(model=MODEL, schema=SCHEMA), cwd)
    assert "--json-schema" not in inv.args
    assert '"verdict"' in inv.stdin_text


def test_multi_turn_prompt_on_stdin(fake_exe: list[Path]) -> None:
    request = make_request(model=MODEL).model_copy(
        update={
            "prompt": Prompt(
                stable_prefix=(
                    Message(role=Role.SYSTEM, content="S"),
                    Message(role=Role.USER, content="context"),
                ),
                variable_suffix=(Message(role=Role.USER, content="question"),),
            )
        }
    )
    with temp_workspace() as cwd:
        inv = _provider().build_invocation(request, cwd)
    assert inv.stdin_text == "[user]\ncontext\n\n[user]\nquestion"


# ------------------------------------------------------------------- parsing


def test_parse_success_envelope() -> None:
    result = _provider().parse_output(_envelope(), make_request(model=MODEL))
    assert result.text == "Hello there."
    assert result.usage.input_tokens == 11
    assert result.usage.cached_input_tokens == 5
    assert result.usage.cache_write_tokens == 7
    assert result.usage.output_tokens == 3
    assert result.usage.reasoning_tokens is None
    assert result.finish_reason == "end_turn"
    assert result.model == MODEL
    assert result.provider == "anthropic"
    assert result.native_params_used == {"cli_args": ["--model", MODEL]}


def test_parse_structured_output() -> None:
    stdout = _envelope(result="", structured_output={"verdict": "ok"})
    result = _provider().parse_output(stdout, make_request(model=MODEL, schema=SCHEMA))
    assert json.loads(result.text) == {"verdict": "ok"}


def test_parse_missing_structured_output_falls_back_to_result() -> None:
    result = _provider().parse_output(
        _envelope(result="I can't help with that.", stop_reason="refusal"),
        make_request(model=MODEL, schema=SCHEMA),
    )
    assert result.text == "I can't help with that."
    assert result.finish_reason == "refusal"


@pytest.mark.parametrize(
    "overrides",
    [
        {"subtype": "error_max_turns", "is_error": True, "errors": ["turn limit"]},
        {"subtype": "error_max_structured_output_retries", "errors": []},
        {"subtype": "success", "is_error": True, "result": "Not logged in"},
    ],
)
def test_parse_error_envelopes_raise(overrides: dict[str, Any]) -> None:
    with pytest.raises(ProviderError, match="claude reported an error"):
        _provider().parse_output(_envelope(**overrides), make_request(model=MODEL))


def test_parse_error_is_redacted() -> None:
    stdout = _envelope(is_error=True, result="bad token=abcdefghijklmnopqrstuvwxyz0123")
    with pytest.raises(ProviderError) as info:
        _provider().parse_output(stdout, make_request(model=MODEL))
    assert "abcdefghijklmnop" not in str(info.value)


@pytest.mark.parametrize("stdout", ["", "not json", "[1, 2]", '"string"'])
def test_parse_garbage_raises(stdout: str) -> None:
    with pytest.raises(ProviderError):
        _provider().parse_output(stdout, make_request(model=MODEL))


def test_parse_without_text_raises() -> None:
    with pytest.raises(ProviderError, match="no text"):
        _provider().parse_output(_envelope(result=None), make_request(model=MODEL))


def test_load_envelope_variants() -> None:
    env = json.loads(_envelope())
    assert load_envelope(json.dumps(env)) == env
    assert load_envelope(json.dumps([{"type": "system"}, env])) == env
    assert load_envelope("some warning line\n" + json.dumps(env) + "\n") == env


def test_map_usage_unknown_is_none() -> None:
    usage = map_usage({"input_tokens": "12", "output_tokens": -1, "cache_read_input_tokens": True})
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.cached_input_tokens is None
    assert map_usage(None).cache_write_tokens is None


def test_unknown_model_usage_keys_keep_requested_model() -> None:
    two = _envelope(modelUsage={"a": {}, "b": {}})
    assert _provider().parse_output(two, make_request(model=MODEL)).model == MODEL


# ---------------------------------------------------------------- end to end


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCli:
    root = tmp_path / "bin"
    root.mkdir()
    fake = FakeCli(root, "claude")
    fake.install_launcher(monkeypatch)
    return fake


def test_complete_end_to_end(fake_claude: FakeCli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-value")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere")
    fake_claude.set(response=_envelope())
    result = asyncio.run(_provider().complete(make_request(model=MODEL, schema=SCHEMA)))
    rec = fake_claude.record()
    assert result.text == "Hello there."
    assert result.latency_s is not None
    assert rec["stdin"].startswith("SECRET-PROMPT-TEXT")
    assert not any("SECRET-PROMPT-TEXT" in a for a in rec["argv"])
    assert rec["argv"][:4] == ["-p", "--output-format", "json", "--safe-mode"]
    assert "" in rec["argv"]  # --tools "" survives the launcher
    assert rec["argv"][rec["argv"].index("--tools") + 1] == ""
    assert rec["cwd_entries"] == []
    env_names = {n.upper() for n in rec["env"]}
    assert not [n for n in env_names if n.endswith("_API_KEY")]
    assert "CLAUDE_CONFIG_DIR" in env_names
    if sys.platform == "win32":
        assert "--json-schema" not in rec["argv"]  # the launcher is a .cmd shim
    else:
        assert "--json-schema" in rec["argv"]


def test_complete_nonzero_exit_uses_envelope(
    fake_claude: FakeCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_claude.set(
        response=_envelope(is_error=True, result="Not logged in · Please run /login"),
        exit_code=1,
    )
    with pytest.raises(ProviderError, match="Not logged in"):
        asyncio.run(_provider().complete(make_request(model=MODEL)))


def test_check_runs_version_only(fake_claude: FakeCli) -> None:
    fake_claude.set(response="2.1.300 (Claude Code)\n")
    status = asyncio.run(_provider().check())
    assert status.available
    assert status.version == "2.1.300 (Claude Code)"
    assert fake_claude.record()["argv"] == ["--version"]
