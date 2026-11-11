"""First-run onboarding: when the wizard shows, subscription CLI states, the tutorial flag.

Fake CLI providers only; no real CLI is ever run, and no real key or config is read.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from llm_crossjury.core import keys
from llm_crossjury.core.keys import CONFIG_DIR_ENV, PROVIDER_KEY_ENVS
from llm_crossjury.core.providers.base import Backend, ProviderStatus
from llm_crossjury.demo import DEMO_QUESTION
from llm_crossjury.web import create_app
from llm_crossjury.web.app import CLI_PRODUCTS, cli_state
from tests.unit._compare_fakes import CLI_PROVIDERS, FakeCliProvider, make_deps

KEY_ENVS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY",
}
FAKE_KEY = "xai-fake-OTHERSECRET-0000111122223333"


class SignedOutCli(FakeCliProvider):
    """Found (answers ``--version``) but its login check failed."""

    async def check(self) -> ProviderStatus:
        self.checks += 1
        return ProviderStatus(
            provider=self.name,
            backend=Backend.CLI,
            available=False,
            detail="fake CLI found (1.0) but not signed in",
            version="1.0",
        )


class LoginCheckedCli(FakeCliProvider):
    login_status_args = ("auth", "status")


@pytest.fixture(autouse=True)
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    directory = tmp_path / "config"
    monkeypatch.setenv(CONFIG_DIR_ENV, str(directory))
    keys.forget_origins()
    yield directory
    keys.forget_origins()
    for var in PROVIDER_KEY_ENVS.values():
        os.environ.pop(var, None)


@pytest.fixture
def base(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    return directory


def _client(base: Path, clis: dict[str, FakeCliProvider] | None = None) -> TestClient:
    providers = clis if clis is not None else {p: FakeCliProvider(p) for p in CLI_PROVIDERS}
    deps = make_deps(cli_providers=providers, key_envs=KEY_ENVS)
    app = create_app(lambda: deps, key_base_dir=base)
    return TestClient(app, base_url="http://127.0.0.1")


def _onboarding(client: TestClient) -> dict[str, Any]:
    data: dict[str, Any] = client.get("/api/onboarding").json()
    return data


# --------------------------------------------------------------------------- first run


def test_before_any_cli_check_the_decision_rests_on_keys(base: Path) -> None:
    clis = {p: FakeCliProvider(p) for p in CLI_PROVIDERS}
    data = _onboarding(_client(base, clis))
    assert data["show"] is True
    assert data["cli_checked"] is False
    assert data["any_cli_ready"] is False
    assert {s["state"] for s in data["subscriptions"]} == {"not_checked"}
    assert all(cli.checks == 0 for cli in clis.values())  # never runs a check itself


def test_a_ready_cli_counts_as_a_usable_provider(base: Path) -> None:
    clis = {
        "anthropic": FakeCliProvider("anthropic"),
        "openai": FakeCliProvider("openai", available=False, detail="codex not found"),
        "xai": FakeCliProvider("xai", available=False, detail="grok not found"),
    }
    client = _client(base, clis)
    client.get("/api/providers")  # runs and caches the checks
    data = _onboarding(client)
    assert data["show"] is False
    assert data["any_key_set"] is False
    assert data["any_cli_ready"] is True
    assert data["cli_checked"] is True
    by = {s["name"]: s for s in data["subscriptions"]}
    assert by["anthropic"] == {
        "name": "anthropic",
        "label": "Anthropic",
        "product": "Claude Code",
        "plan": "Claude Pro or Max",
        "experimental": False,
        "state": "ready",
    }
    assert by["openai"]["state"] == "not_installed"
    assert by["xai"]["experimental"] is True
    assert "gemini" not in by  # API key only


def test_installed_but_signed_out_clis_still_show_the_wizard(base: Path) -> None:
    clis: dict[str, FakeCliProvider] = {p: SignedOutCli(p) for p in CLI_PROVIDERS}
    client = _client(base, clis)
    client.get("/api/providers")
    data = _onboarding(client)
    assert data["show"] is True
    assert {s["state"] for s in data["subscriptions"]} == {"signed_out"}


def test_a_key_hides_the_wizard_whatever_the_clis_say(base: Path) -> None:
    (base / ".xai.key").write_text(FAKE_KEY, encoding="utf-8")
    keys.load_all_keys(base, PROVIDER_KEY_ENVS)
    clis = {p: FakeCliProvider(p, available=False) for p in CLI_PROVIDERS}
    client = _client(base, clis)
    client.get("/api/providers")
    data = _onboarding(client)
    assert data["show"] is False
    assert data["any_key_set"] is True
    assert FAKE_KEY not in json.dumps(data)


def test_dismissed_wizard_stays_hidden(base: Path) -> None:
    client = _client(base, {p: FakeCliProvider(p, available=False) for p in CLI_PROVIDERS})
    client.post("/api/onboarding/dismiss", json={"dismissed": True})
    assert _onboarding(client)["show"] is False


def test_recheck_refines_the_decision(base: Path) -> None:
    cli = FakeCliProvider("anthropic", available=False)
    client = _client(base, {"anthropic": cli})
    client.get("/api/providers")
    assert _onboarding(client)["show"] is True
    cli.available = True  # the user installed and signed in
    client.get("/api/providers?refresh=true")
    assert _onboarding(client)["show"] is False


# --------------------------------------------------------------------------- tutorial


def test_tutorial_flag_is_saved_next_to_the_dismiss_flag(base: Path, config_dir: Path) -> None:
    client = _client(base)
    assert _onboarding(client)["tutorial_seen"] is False
    assert client.post("/api/onboarding/tutorial", json={"seen": True}).json() == {"seen": True}
    client.post("/api/onboarding/dismiss", json={"dismissed": True})
    data = _onboarding(client)
    assert data["tutorial_seen"] is True
    assert data["dismissed"] is True
    state = json.loads((config_dir / "ui-state.json").read_text(encoding="utf-8"))
    assert state == {"onboarding_dismissed": True, "tutorial_seen": True}
    client.post("/api/onboarding/tutorial", json={"seen": False})
    assert _onboarding(client)["tutorial_seen"] is False
    assert _onboarding(client)["dismissed"] is True


def test_tutorial_endpoint_validates_and_guards(base: Path) -> None:
    client = _client(base)
    assert client.post("/api/onboarding/tutorial", json={"seen": "yes"}).status_code == 400
    plain = client.post(
        "/api/onboarding/tutorial",
        content=b'{"seen": true}',
        headers={"Content-Type": "text/plain"},
    )
    assert plain.status_code == 415
    evil = {"Origin": "http://evil.example"}
    assert client.post("/api/onboarding/tutorial", json={}, headers=evil).status_code == 403


def test_old_ui_state_file_is_still_read(base: Path, config_dir: Path) -> None:
    config_dir.mkdir(parents=True)
    (config_dir / "ui-state.json").write_text('{"onboarding_dismissed": true}\n', "utf-8")
    data = _onboarding(_client(base))
    assert data["dismissed"] is True
    assert data["tutorial_seen"] is False


# --------------------------------------------------------------------------- providers


def test_cli_status_reports_state_and_install_commands(base: Path) -> None:
    clis: dict[str, FakeCliProvider] = {
        "anthropic": LoginCheckedCli("anthropic"),
        "openai": SignedOutCli("openai"),
        "xai": FakeCliProvider("xai", available=False, detail="grok not found"),
    }
    data = _client(base, clis).get("/api/providers").json()
    by = {p["name"]: p["cli"] for p in data["providers"] if p["cli"]}
    assert by["anthropic"]["state"] == "ready"
    assert by["anthropic"]["login_checked"] is True
    assert by["openai"]["state"] == "signed_out"
    assert by["openai"]["login_checked"] is False
    assert by["xai"]["state"] == "not_installed"
    for name, cli in by.items():
        product = CLI_PRODUCTS[name]
        assert cli["sign_in_command"] == product.sign_in_command
        assert [c["command"] for c in cli["install_commands"]] == [
            c.command for c in product.install_commands
        ]
        assert cli["install"] == [f"{c.label}: {c.command}" for c in product.install_commands]
        assert cli["plan"] == product.plan
    platforms = {c["platform"] for c in by["anthropic"]["install_commands"]}
    assert platforms == {"windows", "unix"}


def test_install_commands_match_the_readme() -> None:
    readme = (Path(__file__).parents[2] / "README.md").read_text(encoding="utf-8")
    for product in CLI_PRODUCTS.values():
        for command in product.install_commands:
            assert command.command in readme, command.command
        assert product.sign_in_command in readme


def test_cli_state_classification() -> None:
    def status(available: bool, version: str | None) -> ProviderStatus:
        return ProviderStatus(
            provider="x", backend=Backend.CLI, available=available, detail="", version=version
        )

    assert cli_state(None) == "not_checked"
    assert cli_state(status(True, "1.0")) == "ready"
    assert cli_state(status(False, "1.0")) == "signed_out"
    assert cli_state(status(False, None)) == "not_installed"


# --------------------------------------------------------------------------- pages


def test_home_page_offers_the_tour_and_the_example(base: Path) -> None:
    client = _client(base)
    page = client.get("/").text
    assert 'id="tour-btn"' in page
    assert 'aria-label="How it works"' in page
    assert '<script src="/tour.js"></script>' in page
    tour = client.get("/tour.js")
    assert tour.headers["content-type"].startswith("text/javascript")
    assert DEMO_QUESTION in tour.text  # the example question, so demo answers fit
    assert '"/api/onboarding/tutorial"' in tour.text
    assert "cli_checked === true" in client.get("/app.js").text


def test_wizard_page_never_runs_commands(base: Path) -> None:
    client = _client(base)
    page = client.get("/settings").text
    for step in ("welcome", "subs", "keys", "done"):
        assert f'data-step="{step}"' in page
    script = client.get("/settings.js").text
    assert "?refresh=true" in script  # Check again re-runs the status check only
    assert '"/?example=1"' in script
