"""One-click install and sign-in: whitelist, job lifecycle, redaction, device codes.

Every command here is a fake: a Python one-liner standing in for the vendor tool. No
real installer or CLI runs, and nothing touches the network.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from llm_crossjury.core import keys
from llm_crossjury.core.keys import CONFIG_DIR_ENV
from llm_crossjury.web import create_app
from llm_crossjury.web.setup_api import (
    DEFAULT_CATALOG,
    CliSetup,
    SetupCommand,
    SetupJobs,
    parse_device_code,
    sanitize_line,
)
from tests.unit._compare_fakes import CLI_PROVIDERS, FakeCliProvider, make_deps

KEY_ENVS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY",
}
EVIL = {"Origin": "http://evil.example"}


def py(code: str) -> SetupCommand:
    """A fake tool: ``fakepy -c <code>`` where ``fakepy`` resolves to this Python."""
    return SetupCommand(("fakepy", "-c", code))


SLEEP = "import time; print('working', flush=True); time.sleep(30)"


def catalog(
    install: str = "print('installed')",
    login: str = "print('signed in')",
    device: str = "print('Go to https://auth.example.com/device and enter ABCD-EFGH', flush=True)",
) -> dict[str, CliSetup]:
    def one(product: str) -> CliSetup:
        return CliSetup(
            product=product,
            install={"windows": (py(install),), "unix": (py(install),)},
            login={"browser": py(login), "device": py(device)},
            missing_message="Install Node.js first.",
            missing_url="https://nodejs.org/",
        )

    return {"anthropic": one("Claude Code"), "openai": one("Codex CLI")}


def which_fake(name: str) -> str | None:
    return sys.executable if name == "fakepy" else None


@pytest.fixture(autouse=True)
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    directory = tmp_path / "config"
    monkeypatch.setenv(CONFIG_DIR_ENV, str(directory))
    keys.forget_origins()
    yield directory
    keys.forget_origins()


def make_client(
    tmp_path: Path, jobs: SetupJobs, clis: dict[str, FakeCliProvider] | None = None
) -> TestClient:
    providers = clis if clis is not None else {p: FakeCliProvider(p) for p in CLI_PROVIDERS}
    deps = make_deps(cli_providers=providers, key_envs=KEY_ENVS)
    app = create_app(lambda: deps, key_base_dir=tmp_path, setup_jobs=jobs)
    return TestClient(app, base_url="http://127.0.0.1")


def jobs_with(**kwargs: Any) -> SetupJobs:
    cat = kwargs.pop("catalog", None) or catalog()
    return SetupJobs(cat, which=kwargs.pop("which", which_fake), platform="windows", **kwargs)


def wait_done(client: TestClient, job_id: str, limit: float = 20.0) -> dict[str, Any]:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        data: dict[str, Any] = client.get(f"/api/setup/jobs/{job_id}").json()
        if data["state"] != "running":
            return data
        time.sleep(0.05)
    raise AssertionError("job did not finish")


# --------------------------------------------------------------------------- whitelist


def test_setup_info_shows_the_exact_commands(tmp_path: Path) -> None:
    with make_client(tmp_path, jobs_with()) as client:
        data = client.get("/api/setup").json()
    assert data["enabled"] is True
    by = {c["name"]: c for c in data["clis"]}
    assert by["anthropic"]["install"]["available"] is True
    assert by["anthropic"]["install"]["command"].startswith("fakepy -c ")
    assert [o["mode"] for o in by["openai"]["login"]] == ["browser", "device"]
    assert by["anthropic"]["job"] is None


def test_missing_program_is_explained_not_run(tmp_path: Path) -> None:
    with make_client(tmp_path, jobs_with(which=lambda name: None)) as client:
        info = {c["name"]: c for c in client.get("/api/setup").json()["clis"]}
        assert info["openai"]["install"] == {
            "available": False,
            "command": None,
            "missing": "Install Node.js first.",
            "missing_url": "https://nodejs.org/",
        }
        refused = client.post("/api/setup/openai/install", json={})
        assert refused.status_code == 409
        assert refused.json()["code"] == "missing_program"


def test_only_whitelisted_clis_modes_and_bodies(tmp_path: Path) -> None:
    with make_client(tmp_path, jobs_with()) as client:
        assert client.post("/api/setup/gemini/install", json={}).status_code == 404
        assert client.post("/api/setup/rm/login", json={}).status_code == 404
        bad = client.post("/api/setup/anthropic/install", json={"command": "rm -rf /"})
        assert bad.status_code == 400
        assert "rm -rf" not in bad.text
        assert client.post("/api/setup/anthropic/login", json={"mode": "shell"}).status_code == 400
        assert client.post("/api/setup/anthropic/login", json={"args": ["x"]}).status_code == 400


def test_writes_are_guarded(tmp_path: Path) -> None:
    with make_client(tmp_path, jobs_with()) as client:
        assert client.post("/api/setup/anthropic/install", json={}, headers=EVIL).status_code == 403
        plain = client.post(
            "/api/setup/anthropic/install", content=b"{}", headers={"Content-Type": "text/plain"}
        )
        assert plain.status_code == 415
        assert client.get("/api/setup", headers={"Host": "evil.example"}).status_code == 400


def test_default_catalog_holds_only_the_official_commands() -> None:
    claude, codex, grok = (
        DEFAULT_CATALOG["anthropic"],
        DEFAULT_CATALOG["openai"],
        DEFAULT_CATALOG["xai"],
    )
    assert claude.login["browser"].argv == ("claude", "auth", "login", "--claudeai")
    assert "device" not in claude.login
    assert codex.login["browser"].argv == ("codex", "login")
    assert codex.login["device"].argv == ("codex", "login", "--device-auth")
    assert grok.login["browser"].argv == ("grok", "login", "--oauth")
    assert grok.login["device"].argv == ("grok", "login", "--device-auth")
    assert codex.install["windows"][0].argv == ("npm", "install", "-g", "@openai/codex")
    assert codex.missing_url == "https://nodejs.org/"
    assert claude.install["windows"][0].argv[:4] == (
        "winget",
        "install",
        "--id",
        "Anthropic.ClaudeCode",
    )
    assert claude.install["windows"][1].argv[-1] == "irm https://claude.ai/install.ps1 | iex"
    assert claude.install["unix"][0].argv == (
        "bash",
        "-c",
        "curl -fsSL https://claude.ai/install.sh | bash",
    )
    assert grok.install["windows"][0].argv[-1] == "irm https://x.ai/cli/install.ps1 | iex"
    assert grok.install["unix"][0].argv[-1] == "curl -fsSL https://x.ai/cli/install.sh | bash"


def test_install_falls_back_to_the_next_official_command() -> None:
    jobs = SetupJobs(
        DEFAULT_CATALOG,
        which=lambda name: None if name == "winget" else f"C:/bin/{name}.exe",
        platform="windows",
    )
    command = jobs.install_command("anthropic")
    assert command is not None
    assert command.argv[0] == "powershell"


def test_demo_has_no_setup_actions(tmp_path: Path) -> None:
    deps = make_deps(key_envs=KEY_ENVS)
    app = create_app(lambda: deps, key_base_dir=tmp_path, setup_enabled=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/setup").json()["enabled"] is False
        assert client.post("/api/setup/anthropic/install", json={}).status_code == 404


# --------------------------------------------------------------------------- lifecycle


def test_install_job_runs_to_success_and_refreshes_the_cli_check(tmp_path: Path) -> None:
    cli = FakeCliProvider("anthropic", available=False)
    with make_client(tmp_path, jobs_with(), {"anthropic": cli}) as client:
        client.get("/api/providers")
        assert cli.checks == 1
        started = client.post("/api/setup/anthropic/install", json={}).json()
        assert started["state"] == "running"
        assert started["kind"] == "install"
        done = wait_done(client, started["id"])
        assert done["state"] == "succeeded"
        assert done["exit_code"] == 0
        assert "installed" in done["output"]
        cli.available = True
        provider = client.get("/api/providers").json()["providers"][0]
        assert cli.checks == 2  # the cached result was dropped when the job ended
        assert provider["cli"]["state"] == "ready"


def test_failed_job_reports_the_exit_code(tmp_path: Path) -> None:
    jobs = jobs_with(catalog=catalog(install="import sys; print('boom'); sys.exit(3)"))
    with make_client(tmp_path, jobs) as client:
        job = client.post("/api/setup/anthropic/install", json={}).json()
        done = wait_done(client, job["id"])
    assert done["state"] == "failed"
    assert done["exit_code"] == 3
    assert "exit code 3" in done["message"]


def test_one_job_per_cli(tmp_path: Path) -> None:
    with make_client(tmp_path, jobs_with(catalog=catalog(install=SLEEP))) as client:
        first = client.post("/api/setup/anthropic/install", json={}).json()
        again = client.post("/api/setup/anthropic/login", json={})
        assert again.status_code == 409
        assert again.json()["code"] == "job_running"
        info = {c["name"]: c for c in client.get("/api/setup").json()["clis"]}
        assert info["anthropic"]["job"]["id"] == first["id"]
        other = client.post("/api/setup/openai/install", json={}).json()
        assert other["state"] == "running"
        for job in (first, other):
            client.post(f"/api/setup/jobs/{job['id']}/cancel", json={})
            wait_done(client, job["id"])


def test_cancel_kills_the_process(tmp_path: Path) -> None:
    with make_client(tmp_path, jobs_with(catalog=catalog(install=SLEEP))) as client:
        job = client.post("/api/setup/anthropic/install", json={}).json()
        start = time.monotonic()
        cancelled = client.post(f"/api/setup/jobs/{job['id']}/cancel", json={})
        assert cancelled.status_code == 200
        done = wait_done(client, job["id"])
        assert done["state"] == "cancelled"
        assert time.monotonic() - start < 15
        assert client.post("/api/setup/jobs/" + "0" * 32 + "/cancel", json={}).status_code == 404
        assert client.get("/api/setup/jobs/nope").status_code == 404


def test_timeout_kills_the_process(tmp_path: Path) -> None:
    jobs = jobs_with(catalog=catalog(login=SLEEP), timeouts={"login": 1.0})
    with make_client(tmp_path, jobs) as client:
        job = client.post("/api/setup/anthropic/login", json={"mode": "browser"}).json()
        done = wait_done(client, job["id"])
    assert done["state"] == "timed_out"
    assert done["elapsed_s"] < 15
    assert "working" in done["output"]


def test_child_gets_no_api_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-should-not-pass-0123456789")
    code = "import os; print('KEY=' + ('present' if 'OPENAI_API_KEY' in os.environ else 'absent'))"
    with make_client(tmp_path, jobs_with(catalog=catalog(install=code))) as client:
        job = client.post("/api/setup/anthropic/install", json={}).json()
        done = wait_done(client, job["id"])
    assert "KEY=absent" in done["output"]


# --------------------------------------------------------------------------- output


def test_output_is_redacted(tmp_path: Path) -> None:
    code = (
        "print('token sk-ant-oat01-SECRETSECRETSECRET1234567890');"
        "print('If the browser did not open, visit https://claude.ai/oauth/authorize?code=SEKRIT&state=abc');"
        "print(r'Saved to C:\\Users\\me\\.claude\\.credentials.json');"
        "print('\\x1b[32mDone\\x1b[0m')"
    )
    with make_client(tmp_path, jobs_with(catalog=catalog(login=code))) as client:
        job = client.post("/api/setup/anthropic/login", json={}).json()
        done = wait_done(client, job["id"])
    out = done["output"]
    for secret in ("SECRETSECRET", "SEKRIT", "state=abc", "oauth/authorize", "credentials", "\x1b"):
        assert secret not in out
    assert "https://claude.ai/..." in out
    assert "Done" in out


def test_device_code_is_returned_as_fields(tmp_path: Path) -> None:
    with make_client(tmp_path, jobs_with()) as client:
        job = client.post("/api/setup/openai/login", json={"mode": "device"}).json()
        done = wait_done(client, job["id"])
    assert done["mode"] == "device"
    assert done["device"] == {"url": "https://auth.example.com/device", "code": "ABCD-EFGH"}
    assert "https://auth.example.com/device" in done["output"]


def test_browser_login_never_reports_a_device_code(tmp_path: Path) -> None:
    code = "print('Open https://auth.example.com/device code ABCD-EFGH')"
    with make_client(tmp_path, jobs_with(catalog=catalog(login=code))) as client:
        job = client.post("/api/setup/openai/login", json={"mode": "browser"}).json()
        assert wait_done(client, job["id"])["device"] is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Open this link: https://auth.openai.com/codex/device\nEnter code: WXYZ-1234",
            ("https://auth.openai.com/codex/device", "WXYZ-1234"),
        ),
        (
            "\x1b[1mVisit\x1b[0m https://x.ai/help then https://accounts.x.ai/activate."
            " Code K7PD-QMRT.",
            ("https://accounts.x.ai/activate", "K7PD-QMRT"),
        ),
        ("codex-cli 0.167.0 at https://example.com/device", None),
        ("Code ABCD-EFGH but no link", None),
        ("http://insecure.example/device ABCD-EFGH", None),
    ],
)
def test_parse_device_code(text: str, expected: tuple[str, str] | None) -> None:
    assert parse_device_code(text) == expected


def test_sanitize_line_keeps_only_the_device_link() -> None:
    line = "Go to https://auth.example.com/device?user_code=AB12 or https://example.com/a/b?x=1"
    out = sanitize_line(line, keep_url="https://auth.example.com/device?user_code=AB12")
    assert "https://auth.example.com/device?user_code=AB12" in out
    assert "https://example.com/..." in out
    assert "x=1" not in out


def test_codex_reports_its_sign_in_check() -> None:
    from llm_crossjury.core.providers.cli.codex import CodexCliProvider  # noqa: PLC0415

    assert CodexCliProvider.login_status_args == ("login", "status")
