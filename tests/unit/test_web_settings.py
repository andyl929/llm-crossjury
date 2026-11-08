"""Settings API and pages: key management, key checks, onboarding, guards, no leaks."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from llm_crossjury.core import key_check, keys
from llm_crossjury.core.keys import CONFIG_DIR_ENV, PROVIDER_KEY_ENVS
from llm_crossjury.web import create_app
from tests.unit._compare_fakes import make_deps

FAKE_KEY = "sk-fake-TOPSECRET-0123456789abcdef"
OTHER_KEY = "xai-fake-OTHERSECRET-0000111122223333"
JSON = {"Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    directory = tmp_path / "config"
    monkeypatch.setenv(CONFIG_DIR_ENV, str(directory))
    keys.forget_origins()
    yield directory
    keys.forget_origins()
    for var in PROVIDER_KEY_ENVS.values():  # the code under test sets os.environ directly
        os.environ.pop(var, None)


@pytest.fixture
def base(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    return directory


@pytest.fixture
def client(base: Path) -> TestClient:
    return TestClient(create_app(make_deps, key_base_dir=base), base_url="http://127.0.0.1")


def assert_no_leak(text: str, value: str = FAKE_KEY) -> None:
    for fragment in (value, value[:8], value[-8:], value[8:20]):
        assert fragment not in text
    assert f"{len(value)} char" not in text


def _row(client: TestClient, provider: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = client.get("/api/keys").json()["providers"]
    return next(r for r in rows if r["name"] == provider)


def _put(client: TestClient, provider: str, value: str) -> Any:
    return client.put(f"/api/keys/{provider}", json={"key": value})


# --------------------------------------------------------------------------- pages


def test_settings_page_and_assets_are_served(client: TestClient) -> None:
    page = client.get("/settings")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert 'href="/settings?onboarding=1"' in page.text
    assert client.get("/settings.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/settings.css").headers["content-type"].startswith("text/css")


def test_settings_assets_are_ascii_local_and_never_use_html_sinks(client: TestClient) -> None:
    for path in ("/settings", "/settings.js", "/settings.css"):
        text = client.get(path).text
        assert text.isascii(), path
        assert not re.search(r"https?://\w", text), path  # vendor URLs come from the API
    script = client.get("/settings.js").text
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script
    for storage in ("localStorage", "sessionStorage", "console."):
        assert storage not in script


def test_key_inputs_are_password_fields(client: TestClient) -> None:
    assert 'id="wizard-key" type="password"' in client.get("/settings").text
    assert 'type: "password"' in client.get("/settings.js").text


# --------------------------------------------------------------------------- GET /api/keys


def test_keys_start_not_set(client: TestClient, config_dir: Path) -> None:
    data = client.get("/api/keys").json()
    assert [r["name"] for r in data["providers"]] == ["anthropic", "openai", "gemini", "xai"]
    assert all(r["status"] == "not set" and r["source"] is None for r in data["providers"])
    assert all(r["key_page_url"].startswith("https://") for r in data["providers"])
    assert data["store_path"] == str(config_dir.resolve() / "keys.env")


def test_sources_are_reported_with_removal_hints(
    client: TestClient, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    (base / ".env").write_text("OPENAI_API_KEY=dotenv-value-1234\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "dotenv-value-1234")
    (base / ".gemini.key").write_text("keyfile-value-1234", encoding="utf-8")
    keys.load_all_keys(base, PROVIDER_KEY_ENVS)

    response = client.get("/api/keys")
    assert_no_leak(response.text)
    rows = {r["name"]: r for r in response.json()["providers"]}
    assert rows["anthropic"]["source"] == "environment"
    assert "ANTHROPIC_API_KEY" in rows["anthropic"]["how_to_remove"]
    assert rows["openai"]["source"] == "dotenv"
    assert ".env" in rows["openai"]["how_to_remove"]
    assert rows["gemini"]["source"] == "key_file"
    assert ".gemini.key" in rows["gemini"]["how_to_remove"]
    assert rows["xai"]["status"] == "not set"
    assert not any(r["stored"] for r in rows.values())


# --------------------------------------------------------------------------- PUT / DELETE


def test_save_applies_without_restart_and_never_echoes(client: TestClient) -> None:
    response = _put(client, "openai", FAKE_KEY)
    assert response.status_code == 200
    assert_no_leak(response.text)
    body = response.json()
    assert body["applied"] is True
    assert body["provider"]["status"] == "set"
    assert body["provider"]["source"] == "settings"
    assert os.environ["OPENAI_API_KEY"] == FAKE_KEY
    listing = client.get("/api/keys")
    assert_no_leak(listing.text)
    assert _row(client, "openai")["stored"] is True


def test_save_under_a_higher_source_is_not_applied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XAI_API_KEY", OTHER_KEY)
    body = _put(client, "xai", FAKE_KEY).json()
    assert body["applied"] is False
    assert body["provider"]["source"] == "environment"
    assert body["provider"]["overridden"] is True
    assert os.environ["XAI_API_KEY"] == OTHER_KEY


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": FAKE_KEY},
        {"key": FAKE_KEY, "extra": FAKE_KEY},
        {"key": 12345678},
        [FAKE_KEY],
        "x" * 10_000 + FAKE_KEY,
    ],
)
def test_bad_bodies_never_echo_the_key(client: TestClient, payload: Any) -> None:
    response = client.put("/api/keys/anthropic", json=payload)
    assert response.status_code == 400
    assert_no_leak(response.text)
    assert "ANTHROPIC_API_KEY" not in os.environ


@pytest.mark.parametrize("value", ["short", f"has space {FAKE_KEY}", "x" * 5000 + FAKE_KEY])
def test_invalid_keys_are_rejected_without_echo(client: TestClient, value: str) -> None:
    response = _put(client, "gemini", value)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_key"
    assert_no_leak(response.text)
    assert "short" not in response.text


def test_unknown_provider_is_404_without_echo(client: TestClient) -> None:
    response = _put(client, FAKE_KEY, FAKE_KEY)
    assert response.status_code == 404
    assert_no_leak(response.text)
    assert client.delete("/api/keys/mistral").status_code == 404
    assert client.post("/api/keys/mistral/test", json={}).status_code == 404


def test_delete_saved_key(client: TestClient) -> None:
    _put(client, "anthropic", FAKE_KEY)
    response = client.delete("/api/keys/anthropic")
    assert response.status_code == 200
    assert response.json()["status"] == "not set"
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_delete_without_saved_key_explains_the_source(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    response = client.delete("/api/keys/openai")
    assert response.status_code == 409
    assert "OPENAI_API_KEY" in response.json()["detail"]
    assert_no_leak(response.text)
    assert os.environ["OPENAI_API_KEY"] == FAKE_KEY


# --------------------------------------------------------------------------- guards


def test_writes_require_json(client: TestClient) -> None:
    response = client.put(
        "/api/keys/openai", content=f"key={FAKE_KEY}", headers={"Content-Type": "text/plain"}
    )
    assert response.status_code == 415
    assert client.post("/api/keys/openai/test").status_code == 415
    assert "OPENAI_API_KEY" not in os.environ


def test_cross_origin_writes_are_rejected(client: TestClient) -> None:
    evil = {"Origin": "http://evil.example", **JSON}
    assert client.put("/api/keys/openai", json={"key": FAKE_KEY}, headers=evil).status_code == 403
    assert client.delete("/api/keys/openai", headers=evil).status_code == 403
    assert client.post("/api/keys/openai/test", json={}, headers=evil).status_code == 403
    assert client.post("/api/onboarding/dismiss", json={}, headers=evil).status_code == 403
    same = {"Origin": "http://127.0.0.1", **JSON}
    assert client.put("/api/keys/openai", json={"key": FAKE_KEY}, headers=same).status_code == 200


def test_foreign_host_is_rejected(base: Path) -> None:
    rebinding = TestClient(create_app(make_deps, key_base_dir=base), base_url="http://evil.example")
    assert rebinding.get("/api/keys").status_code == 400
    assert rebinding.get("/settings").status_code == 400
    assert rebinding.put("/api/keys/openai", json={"key": FAKE_KEY}).status_code == 400


# --------------------------------------------------------------------------- key test


class _UnauthorizedError(Exception):
    status_code = 401


def test_key_check_endpoint_reports_valid(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def ok(api_key: str, timeout_s: float) -> None:
        seen.append(api_key)

    monkeypatch.setitem(key_check.DEFAULT_LISTERS, "anthropic", ok)
    _put(client, "anthropic", FAKE_KEY)
    response = client.post("/api/keys/anthropic/test", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "valid"
    assert seen == [FAKE_KEY]
    assert_no_leak(response.text)


def test_key_check_endpoint_sanitizes_vendor_errors(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")

    async def reject(api_key: str, timeout_s: float) -> None:
        raise _UnauthorizedError(f"Incorrect API key provided: {api_key}")

    monkeypatch.setitem(key_check.DEFAULT_LISTERS, "openai", reject)
    _put(client, "openai", FAKE_KEY)
    response = client.post("/api/keys/openai/test", json={})
    assert response.json()["status"] == "invalid"
    out = capsys.readouterr()
    assert_no_leak(response.text + out.out + out.err + caplog.text)


def test_key_check_without_key(client: TestClient) -> None:
    assert client.post("/api/keys/xai/test", json={}).json()["status"] == "not_set"


# --------------------------------------------------------------------------- onboarding


def test_onboarding_shows_when_no_key_is_set(client: TestClient) -> None:
    data = client.get("/api/onboarding").json()
    assert data["show"] is True
    assert data["any_key_set"] is False
    assert data["subscription_mode"] == "coming_soon"
    urls = {p["name"]: p["key_page_url"] for p in data["providers"]}
    assert urls == {
        "anthropic": "https://platform.claude.com/settings/keys",
        "openai": "https://platform.openai.com/settings/organization/api-keys",
        "gemini": "https://aistudio.google.com/apikey",
        "xai": "https://console.x.ai/team/default/api-keys",
    }
    assert set(data["budget"]) == {"currency", "run_limit", "confirm_above", "daily_limit"}


def test_onboarding_hides_once_any_source_has_a_key(
    client: TestClient, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (base / ".xai.key").write_text(OTHER_KEY, encoding="utf-8")
    keys.load_all_keys(base, PROVIDER_KEY_ENVS)
    data = client.get("/api/onboarding").json()
    assert data["show"] is False
    assert data["any_key_set"] is True


def test_onboarding_can_be_dismissed_and_restored(client: TestClient, config_dir: Path) -> None:
    assert client.post("/api/onboarding/dismiss", json={"dismissed": True}).json() == {
        "dismissed": True
    }
    data = client.get("/api/onboarding").json()
    assert data["show"] is False
    assert data["dismissed"] is True
    assert (config_dir / "ui-state.json").is_file()
    client.post("/api/onboarding/dismiss", json={"dismissed": False})
    assert client.get("/api/onboarding").json()["show"] is True
    assert client.post("/api/onboarding/dismiss", json={"dismissed": "yes"}).status_code == 400


def test_main_app_routes_still_work(client: TestClient) -> None:
    assert client.get("/").status_code == 200
    assert client.get("/api/providers").status_code == 200
