"""Tests for the ``cli`` backend subprocess scaffolding (ADR 0002).

No real vendor CLI is used: fake executables are small Python scripts run through
``sys.executable``, either directly or behind a PATH launcher (a ``.cmd`` shim on
Windows, a ``#!/bin/sh`` script elsewhere).
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from llm_crossjury.core.providers.base import (
    Backend,
    CompletionRequest,
    CompletionResult,
    Message,
    Prompt,
    ProviderError,
    ProviderUnavailableError,
    ReasoningEffort,
    Role,
    Usage,
)
from llm_crossjury.core.providers.cli._subprocess import (
    ENV_ALLOWLIST,
    CliInvocation,
    CliProviderBase,
    build_child_env,
    inputs_dir,
    redact,
    render_transcript,
    resolve_executable,
    run_cli,
    temp_workspace,
)
from llm_crossjury.core.registry.schema import (
    CliFlags,
    EffortMapping,
    ModelEntry,
    ReasoningControl,
    ReasoningControlType,
)

FAKE_CLI_SOURCE = r"""
import json, os, subprocess, sys, time
from pathlib import Path

here = Path(__file__).resolve().parent
def opt(name, default=""):
    p = here / name
    return p.read_text(encoding="utf-8") if p.exists() else default

stdin = sys.stdin.buffer.read().decode("utf-8")
record = {
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "cwd_entries": sorted(os.listdir(os.getcwd())),
    "env": sorted(os.environ.keys()),
    "stdin": stdin,
}
(here / "record.json").write_text(json.dumps(record), encoding="utf-8")
mode = opt("mode.txt", "echo").strip()
if mode == "sleep":
    loop = (
        "import time, sys\n"
        "while True:\n"
        "    open(sys.argv[1], 'w').write(str(time.time()))\n"
        "    time.sleep(0.05)\n"
    )
    subprocess.Popen([sys.executable, "-c", loop, str(here / "heartbeat.txt")])
    time.sleep(120)
elif mode == "flood":
    chunk = "x" * 65536
    for _ in range(10000):
        sys.stdout.write(chunk)
        sys.stdout.flush()
sys.stdout.buffer.write(opt("response.txt").encode("utf-8"))
sys.stdout.buffer.flush()
sys.stderr.buffer.write(opt("stderr.txt").encode("utf-8"))
sys.stderr.buffer.flush()
first = sys.argv[1] if len(sys.argv) > 1 else ""
with open(here / "calls.jsonl", "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + chr(10))
code = opt("exit_code_" + first + ".txt", "") or opt("exit_code.txt", "0") or "0"
sys.exit(int(code))
"""


class FakeCli:
    """A fake CLI living in its own directory; behaviour is set through files."""

    def __init__(self, root: Path, name: str) -> None:
        self.dir = root
        self.name = name
        self.script = root / "fake_cli.py"
        self.script.write_text(FAKE_CLI_SOURCE, encoding="utf-8")

    def install_launcher(self, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Put a ``name`` launcher on PATH (``.cmd`` on Windows) and return it."""
        if sys.platform == "win32":
            launcher = self.dir / f"{self.name}.cmd"
            launcher.write_text(f'@"{sys.executable}" "%~dp0fake_cli.py" %*\r\n', encoding="utf-8")
        else:
            launcher = self.dir / self.name
            launcher.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{self.script}" "$@"\n', encoding="utf-8"
            )
            launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        monkeypatch.setenv("PATH", str(self.dir) + os.pathsep + os.environ.get("PATH", ""))
        return launcher

    def set(
        self,
        *,
        response: str | None = None,
        mode: str | None = None,
        exit_code: int | None = None,
        stderr: str | None = None,
    ) -> None:
        for filename, value in (
            ("response.txt", response),
            ("mode.txt", mode),
            ("exit_code.txt", None if exit_code is None else str(exit_code)),
            ("stderr.txt", stderr),
        ):
            if value is not None:
                (self.dir / filename).write_text(value, encoding="utf-8")

    def set_exit_for(self, first_arg: str, code: int) -> None:
        """Exit with ``code`` when argv[1] is ``first_arg`` (e.g. ``"auth"``)."""
        (self.dir / f"exit_code_{first_arg}.txt").write_text(str(code), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        """Every argv the fake was started with, in order."""
        path = self.dir / "calls.jsonl"
        if not path.exists():
            return []
        lines = path.read_text("utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def record(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.dir / "record.json").read_text("utf-8"))
        return data

    def invocation(
        self, cwd: Path, *, stdin: str = "hello", timeout_s: float = 30.0, cap: int = 1 << 20
    ) -> CliInvocation:
        return CliInvocation(
            executable=Path(sys.executable),
            args=(str(self.script), "--flag"),
            stdin_text=stdin,
            cwd=cwd,
            timeout_s=timeout_s,
            max_output_bytes=cap,
        )


@pytest.fixture
def fake(tmp_path: Path) -> FakeCli:
    root = tmp_path / "fakebin"
    root.mkdir()
    return FakeCli(root, "fakecli")


def make_entry(
    *,
    provider: str = "acme",
    model_id: str = "acme-1",
    cli_flags: CliFlags | None = None,
    structured_output: bool | None = True,
    control: ReasoningControlType = ReasoningControlType.LEVELS,
) -> ModelEntry:
    mappable = control in (ReasoningControlType.LEVELS, ReasoningControlType.BUDGET)
    return ModelEntry(
        provider=provider,
        model_id=model_id,
        backends=(Backend.API, Backend.CLI),
        reasoning_control=ReasoningControl(
            type=control,
            parameter="effort" if mappable else None,
            allowed=("low", "medium", "high") if mappable else None,
            mapping=EffortMapping(low="low", medium="medium", high="high") if mappable else None,
        ),
        cli_flags=cli_flags,
        context_window=None,
        max_output=None,
        structured_output=structured_output,
        pricing=None,
        reasoning_multipliers=None,
        source_url=None,
        last_verified=date(2026, 1, 1),
        notes=None,
    )


DEFAULT_FLAGS = CliFlags(model=("--model", "{model}"), reasoning=("--effort", "{value}"))


class FakeRegistry:
    def __init__(self, *entries: ModelEntry) -> None:
        self._entries = {(e.provider, e.model_id): e for e in entries}

    def get(self, provider: str, model_id: str) -> ModelEntry | None:
        return self._entries.get((provider, model_id))


def make_request(
    *,
    model: str = "acme-1",
    system: str | None = "SYSTEM-INSTRUCTIONS",
    user: str = "SECRET-PROMPT-TEXT",
    schema: dict[str, Any] | None = None,
    reasoning: ReasoningEffort = ReasoningEffort.DEFAULT,
    timeout_s: float | None = 30.0,
) -> CompletionRequest:
    prefix = (Message(role=Role.SYSTEM, content=system),) if system else ()
    return CompletionRequest(
        model=model,
        prompt=Prompt(
            stable_prefix=prefix,
            variable_suffix=(Message(role=Role.USER, content=user),),
        ),
        response_schema=schema,
        temperature=None,
        max_output_tokens=None,
        reasoning=reasoning,
        native_params={},
        cache_prefix=False,
        timeout_s=timeout_s,
        tags={},
    )


# --------------------------------------------------------------- resolve / env


def test_resolve_executable_missing_raises_unavailable() -> None:
    with pytest.raises(ProviderUnavailableError, match="not found"):
        resolve_executable("definitely-not-a-real-cli-xyz")


def test_resolve_executable_finds_launcher(fake: FakeCli, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = fake.install_launcher(monkeypatch)
    resolved = resolve_executable(fake.name)
    assert resolved.is_absolute()
    assert resolved.name.lower() == launcher.name.lower()


def test_build_child_env_is_allowlist_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-value")
    monkeypatch.setenv("SOME_OTHER_VAR", "x")
    monkeypatch.setenv("LANG", "C.UTF-8")
    env = build_child_env(extra=("MY_API_KEY", "SOME_OTHER_VAR"))
    assert not [k for k in env if k.upper().endswith("_API_KEY")]
    assert set(env) <= {*ENV_ALLOWLIST, "SOME_OTHER_VAR"}
    assert env["LANG"] == "C.UTF-8"
    assert "SOME_OTHER_VAR" in env


def test_allowlist_has_no_api_keys() -> None:
    assert not [k for k in ENV_ALLOWLIST if "KEY" in k or "TOKEN" in k]


# ------------------------------------------------------------------ redaction


def test_redact_strips_tokens_and_paths() -> None:
    raw = (
        "Error: Authorization: Bearer abc.def.ghi failed\n"
        "api_key=sk-ant-0123456789abcdef token: hunter2hunter2 "
        "file C:\\Users\\me\\.claude\\.credentials.json and /home/me/.config/x/y "
        "blob A1b2C3d4E5f6G7h8I9j0K1l2"
    )
    out = redact(raw)
    for secret in ("abc.def.ghi", "sk-ant", "hunter2", ".credentials", "/home/me", "A1b2C3d4"):
        assert secret not in out
    assert "\n" not in out
    assert "Error" in out


def test_redact_limits_length() -> None:
    assert len(redact("word " * 500)) <= 303


# ------------------------------------------------------------------ workspace


def test_temp_workspace_is_empty_and_removed() -> None:
    with temp_workspace() as cwd:
        assert cwd.is_dir()
        assert list(cwd.iterdir()) == []
        inputs = inputs_dir(cwd)
        (inputs / "system.md").write_text("x", encoding="utf-8")
        assert list(cwd.iterdir()) == []
        root = cwd.parent
    assert not root.exists()


def test_render_transcript() -> None:
    user = Message(role=Role.USER, content="hi")
    assert render_transcript((user,)) == "hi"
    multi = render_transcript((user, Message(role=Role.ASSISTANT, content="yo"), user))
    assert multi == "[user]\nhi\n\n[assistant]\nyo\n\n[user]\nhi"
    with pytest.raises(ProviderError):
        render_transcript((Message(role=Role.SYSTEM, content="s"),))


# -------------------------------------------------------------------- run_cli


def test_run_cli_stdin_cwd_env(fake: FakeCli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key-value")
    monkeypatch.setenv("UNRELATED_VAR", "x")
    fake.set(response="OUT", stderr="warn", exit_code=3)
    with temp_workspace() as cwd:
        stdout, stderr, code = asyncio.run(run_cli(fake.invocation(cwd, stdin="PROMPT ü")))
        rec = fake.record()
        assert Path(rec["cwd"]).resolve() == cwd.resolve()
    assert (stdout, stderr, code) == ("OUT", "warn", 3)
    assert rec["stdin"] == "PROMPT ü"
    assert rec["argv"] == ["--flag"]
    assert rec["cwd_entries"] == []
    env_names = {n.upper() for n in rec["env"]}
    assert not [n for n in env_names if n.endswith("_API_KEY")]
    assert "UNRELATED_VAR" not in env_names


def test_run_cli_redacts_stderr(fake: FakeCli) -> None:
    fake.set(stderr="failed with token=abcdefghijklmnop0123456789")
    with temp_workspace() as cwd:
        _, stderr, _ = asyncio.run(run_cli(fake.invocation(cwd)))
    assert "abcdefghijklmnop" not in stderr


def test_run_cli_output_cap_raises(fake: FakeCli) -> None:
    fake.set(mode="flood")
    with temp_workspace() as cwd, pytest.raises(ProviderError, match="output rejected"):
        asyncio.run(run_cli(fake.invocation(cwd, cap=100_000)))


def test_run_cli_timeout_kills_process_tree(fake: FakeCli) -> None:
    fake.set(mode="sleep")
    heartbeat = fake.dir / "heartbeat.txt"
    started = time.monotonic()
    with temp_workspace() as cwd, pytest.raises(ProviderError, match="timed out"):
        asyncio.run(run_cli(fake.invocation(cwd, timeout_s=3.0)))
    assert time.monotonic() - started < 30
    assert heartbeat.exists(), "grandchild never started"
    time.sleep(0.5)
    first = heartbeat.read_text(encoding="utf-8")
    time.sleep(0.6)
    assert heartbeat.read_text(encoding="utf-8") == first, "grandchild still running"


def test_run_cli_unstartable_executable(tmp_path: Path) -> None:
    invocation = CliInvocation(
        executable=tmp_path / "missing-exe",
        args=(),
        stdin_text="",
        cwd=tmp_path,
        timeout_s=5,
        max_output_bytes=100,
    )
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(run_cli(invocation))


# ----------------------------------------------------------- CliProviderBase


class EchoProvider(CliProviderBase):
    """Minimal adapter around the fake CLI: argv = registry flags, prompt on stdin."""

    name = "acme"
    command = "fakecli"

    def build_invocation(self, request: CompletionRequest, cwd: Path) -> CliInvocation:
        return CliInvocation(
            executable=self.resolve(),
            args=self.registry_argv(request),
            stdin_text=render_transcript(request.prompt.variable_suffix),
            cwd=cwd,
            timeout_s=self.timeout_for(request),
            max_output_bytes=self.max_output_bytes,
        )

    def parse_output(self, stdout: str, request: CompletionRequest) -> CompletionResult:
        return CompletionResult(
            text=stdout,
            usage=Usage(
                input_tokens=None,
                cached_input_tokens=None,
                cache_write_tokens=None,
                output_tokens=None,
                reasoning_tokens=None,
            ),
            provider=self.name,
            model=request.model,
            backend=self.backend,
            finish_reason=None,
            started_at=datetime(2000, 1, 1, tzinfo=UTC),
            latency_s=None,
            native_params_used={},
        )


def test_registry_argv_from_cli_flags() -> None:
    provider = EchoProvider(registry=FakeRegistry(make_entry(cli_flags=DEFAULT_FLAGS)))
    assert provider.registry_argv(make_request()) == ("--model", "acme-1")
    high = make_request(reasoning=ReasoningEffort.HIGH)
    assert provider.registry_argv(high) == ("--model", "acme-1", "--effort", "high")


def test_registry_argv_requires_entry_and_flags() -> None:
    with pytest.raises(ProviderError, match="cli_flags"):
        EchoProvider().registry_argv(make_request())
    no_flags = EchoProvider(registry=FakeRegistry(make_entry(cli_flags=None)))
    with pytest.raises(ProviderError, match="cli_flags"):
        no_flags.registry_argv(make_request())


def test_registry_argv_unmappable_reasoning_raises() -> None:
    flags = CliFlags(model=("--model", "{model}"), reasoning=None)
    provider = EchoProvider(registry=FakeRegistry(make_entry(cli_flags=flags)))
    with pytest.raises(ProviderError, match="reasoning"):
        provider.registry_argv(make_request(reasoning=ReasoningEffort.LOW))


def test_registry_argv_rejects_unsafe_tokens() -> None:
    entry = make_entry(model_id="bad model&calc", cli_flags=DEFAULT_FLAGS)
    provider = EchoProvider(registry=FakeRegistry(entry))
    with pytest.raises(ProviderError, match="unsafe"):
        provider.registry_argv(make_request(model="bad model&calc"))


def test_capabilities_from_entry() -> None:
    entry = make_entry(cli_flags=DEFAULT_FLAGS, structured_output=True)
    plain = EchoProvider(registry=FakeRegistry(entry)).capabilities("acme-1")
    assert not plain.structured_output  # adapter does not declare native schema support
    assert plain.reasoning
    assert not plain.caching
    assert not plain.batch
    assert not plain.temperature

    class SchemaProvider(EchoProvider):
        native_schema = True

    assert SchemaProvider(registry=FakeRegistry(entry)).capabilities("acme-1").structured_output
    unverified = make_entry(cli_flags=DEFAULT_FLAGS, structured_output=None)
    unverified_caps = SchemaProvider(registry=FakeRegistry(unverified)).capabilities("acme-1")
    assert not unverified_caps.structured_output
    always_on = make_entry(cli_flags=DEFAULT_FLAGS, control=ReasoningControlType.ALWAYS_ON)
    assert not EchoProvider(registry=FakeRegistry(always_on)).capabilities("acme-1").reasoning
    unknown = EchoProvider().capabilities("nope")
    assert not any(unknown.model_dump().values())


def test_complete_end_to_end(fake: FakeCli, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install_launcher(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-value")
    fake.set(response="answer")
    provider = EchoProvider(registry=FakeRegistry(make_entry(cli_flags=DEFAULT_FLAGS)))
    result = asyncio.run(provider.complete(make_request(reasoning=ReasoningEffort.MEDIUM)))
    rec = fake.record()
    assert result.text == "answer"
    assert result.started_at.year > 2000  # overwritten by complete()
    assert result.latency_s is not None
    assert result.latency_s > 0
    assert rec["argv"] == ["--model", "acme-1", "--effort", "medium"]
    assert not any("SECRET-PROMPT-TEXT" in a for a in rec["argv"])
    assert rec["stdin"] == "SECRET-PROMPT-TEXT"
    assert rec["cwd_entries"] == []
    assert not [n for n in rec["env"] if n.upper().endswith("_API_KEY")]
    assert not Path(rec["cwd"]).exists(), "temp cwd was not removed"


def test_complete_nonzero_exit_is_redacted_error(
    fake: FakeCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.install_launcher(monkeypatch)
    fake.set(exit_code=2, stderr="boom secret=abcdefghijklmnopqrstuvwxyz0123")
    provider = EchoProvider(registry=FakeRegistry(make_entry(cli_flags=DEFAULT_FLAGS)))
    with pytest.raises(ProviderError, match="exited with code 2") as info:
        asyncio.run(provider.complete(make_request()))
    assert "abcdefghijklmnop" not in str(info.value)
    assert "boom" in str(info.value)


def test_complete_missing_cli_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(EchoProvider, "command", "definitely-not-a-real-cli-xyz")
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(EchoProvider().complete(make_request()))


def test_skipped_provider_is_unavailable() -> None:
    class Skipped(EchoProvider):
        skipped_reason = "S3: tools cannot be disabled"

    with pytest.raises(ProviderUnavailableError, match="S3"):
        asyncio.run(Skipped().complete(make_request()))
    status = asyncio.run(Skipped().check())
    assert not status.available
    assert "S3" in status.detail


def test_count_input_tokens_is_none() -> None:
    assert asyncio.run(EchoProvider().count_input_tokens(make_request())) is None


def test_check_runs_version_only(fake: FakeCli, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = fake.install_launcher(monkeypatch)
    fake.set(response="9.9.9 (Fake CLI)\n")
    status = asyncio.run(EchoProvider().check())
    assert status.available
    assert status.version == "9.9.9 (Fake CLI)"
    assert fake.record()["argv"] == ["--version"]
    assert str(launcher.parent) not in status.detail
    assert status.backend is Backend.CLI


def test_check_missing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(EchoProvider, "command", "definitely-not-a-real-cli-xyz")
    status = asyncio.run(EchoProvider().check())
    assert not status.available
    assert status.version is None


def test_check_nonzero_exit(fake: FakeCli, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install_launcher(monkeypatch)
    fake.set(exit_code=1)
    assert not asyncio.run(EchoProvider().check()).available


def test_build_child_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "C.UTF-8")
    env = build_child_env(overrides={"GROK_MEMORY": "0", "LANG": "C"})
    assert env["GROK_MEMORY"] == "0"
    assert env["LANG"] == "C"
    with pytest.raises(ProviderError, match="XAI_API_KEY"):
        build_child_env(overrides={"XAI_API_KEY": "x"})
    with pytest.raises(ProviderError):
        build_child_env(overrides={"my_api_key": "x"})


def test_run_cli_empty_stdin(fake: FakeCli) -> None:
    fake.set(response="ok")
    with temp_workspace() as cwd:
        stdout, _, code = asyncio.run(run_cli(fake.invocation(cwd, stdin="")))
    assert (stdout, code) == ("ok", 0)
    assert fake.record()["stdin"] == ""


class OverrideProvider(EchoProvider):
    def __init__(self, overrides: dict[str, str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._overrides = overrides

    def child_env_overrides(self) -> dict[str, str]:
        return self._overrides


def test_complete_applies_env_overrides(fake: FakeCli, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install_launcher(monkeypatch)
    fake.set(response="answer")
    registry = FakeRegistry(make_entry(cli_flags=DEFAULT_FLAGS))
    provider = OverrideProvider({"FAKE_MEMORY": "0"}, registry=registry)
    asyncio.run(provider.complete(make_request()))
    assert "FAKE_MEMORY" in {n.upper() for n in fake.record()["env"]}
    bad = OverrideProvider({"ANTHROPIC_API_KEY": "x"}, registry=registry)
    with pytest.raises(ProviderError, match="refusing"):
        asyncio.run(bad.complete(make_request()))


class FailingParseProvider(EchoProvider):
    def parse_output(self, stdout: str, request: CompletionRequest) -> CompletionResult:
        raise ProviderError(f"envelope error: {stdout}")


def test_complete_redacts_parse_errors(fake: FakeCli, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install_launcher(monkeypatch)
    fake.set(response="bad token=abcdefghijklmnopqrstuvwxyz0123")
    provider = FailingParseProvider(registry=FakeRegistry(make_entry(cli_flags=DEFAULT_FLAGS)))
    with pytest.raises(ProviderError, match="envelope error") as info:
        asyncio.run(provider.complete(make_request()))
    assert "abcdefghijklmnop" not in str(info.value)


@pytest.mark.parametrize(
    "masked",
    [
        "sk-proj-\u2026abcde",
        "sk-proj-abc...vwxyz",
        "AbCd1234...Wxyz9",
        "abcdefgh\u2026vwxyz",
        "ab12****yz89",
    ],
)
def test_redact_masked_key_fragments(masked: str) -> None:
    out = redact(f"Logged in using an API key - {masked} (status ok)")
    assert masked not in out
    for fragment in ("abcde", "vwxyz", "Wxyz9", "yz89", "proj"):
        if fragment in masked:
            assert fragment not in out
    assert "Logged in" in out


def test_run_cli_redacts_masked_fragment_on_stderr(fake: FakeCli) -> None:
    fake.set(stderr="Logged in using an API key - sk-proj-\u2026abcde\n", exit_code=0)
    with temp_workspace() as cwd:
        _, stderr, _ = asyncio.run(run_cli(fake.invocation(cwd)))
    assert "abcde" not in stderr
    assert "proj" not in stderr


@pytest.mark.skipif(sys.platform != "win32", reason="registry PATH fallback is Windows-only")
def test_resolve_falls_back_to_persistent_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import llm_crossjury.core.providers.cli._subprocess as sub  # noqa: PLC0415

    tool_dir = tmp_path / "newly-installed"
    tool_dir.mkdir()
    (tool_dir / "freshcli.cmd").write_text("@echo off\r\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(sub, "_registry_path", lambda: str(tool_dir))
    assert sub.resolve_executable("freshcli").parent == tool_dir
    assert str(tool_dir) in sub.build_child_env()["PATH"]
