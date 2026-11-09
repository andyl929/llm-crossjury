"""Compare web UI: API endpoints with fake deps, request guards, and the ui command."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from llm_crossjury.cli import app as cli_module
from llm_crossjury.core.compare_service import CompareDeps
from llm_crossjury.core.config import BudgetConfig
from llm_crossjury.core.providers.base import ProviderError
from llm_crossjury.web import create_app
from tests.unit._compare_fakes import (
    PROVIDERS,
    FakeLedger,
    echo_provider,
    make_deps,
    raising_provider,
    settings_for,
)

KEY_ENVS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY",
}
FAKE_KEY = "sk-fake-TOPSECRET-0123456789abcdef"
TASK = "Explain <b>bold</b> & {braces}."


def _client(deps: CompareDeps) -> TestClient:
    return TestClient(
        create_app(lambda: deps, settings_store=settings_for(deps.budget)),
        base_url="http://127.0.0.1",
    )


def _body(*providers: str, model: str = "balanced", **extra: Any) -> dict[str, Any]:
    return {
        "task": TASK,
        "selections": [{"provider": p, "model": model} for p in providers],
        **extra,
    }


@pytest.fixture
def all_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in KEY_ENVS.values():
        monkeypatch.setenv(env, FAKE_KEY)


# --------------------------------------------------------------------------- static


def test_index_and_assets_are_served() -> None:
    client = _client(make_deps())
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "llm-crossjury" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert client.get("/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/app.css").headers["content-type"].startswith("text/css")


def test_page_uses_no_external_resources() -> None:
    client = _client(make_deps())
    for path in ("/", "/app.js", "/app.css"):
        text = client.get(path).text
        assert not re.search(r"https?://", text), path
        assert text.isascii(), path  # non-ASCII text uses escapes; no encoding surprises


def test_script_never_renders_server_text_as_html() -> None:
    script = _client(make_deps()).get("/app.js").text
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script


def test_theme_script_and_icon_are_served_locally() -> None:
    client = _client(make_deps())
    theme = client.get("/theme.js")
    assert theme.status_code == 200
    assert theme.headers["content-type"].startswith("text/javascript")
    assert "default-src 'self'" in theme.headers["content-security-policy"]
    assert theme.text.isascii()
    assert not re.search(r"https?://", theme.text)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in theme.text
    assert "try" in theme.text  # storage access survives blocked storage
    icon = client.get("/favicon.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert "<script" not in icon.text
    for page in ("/", "/settings"):
        html = client.get(page).text
        assert '<script src="/theme.js"></script>' in html
        assert 'href="/favicon.svg"' in html


def test_markdown_links_are_limited_to_http_and_open_without_opener() -> None:
    script = _client(make_deps()).get("/app.js").text
    assert 'url.protocol === "https:" || url.protocol === "http:"' in script
    assert 'rel: "noopener noreferrer"' in script


def test_page_shows_a_demo_badge_when_the_server_says_demo() -> None:
    client = _client(make_deps())
    assert 'id="demo-badge"' in client.get("/").text
    assert "data.demo !== true" in client.get("/app.js").text


def test_page_has_keyboard_shortcut_and_skip_link() -> None:
    client = _client(make_deps())
    page = client.get("/").text
    assert 'class="skip-link" href="#main"' in page
    assert 'aria-keyshortcuts="Control+Enter Meta+Enter"' in page
    assert "event.ctrlKey || event.metaKey" in client.get("/app.js").text


# --------------------------------------------------------------------------- providers


def test_providers_never_leak_key_material(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    response = _client(make_deps(key_envs=KEY_ENVS)).get("/api/providers")
    assert response.status_code == 200
    text = response.text
    for fragment in (FAKE_KEY, "TOPSECRET", "0123456789abcdef", "sk-fake", str(len(FAKE_KEY))):
        assert fragment not in text
    by = {p["name"]: p for p in response.json()["providers"]}
    assert by["anthropic"]["key_status"] == "set"
    assert by["openai"]["key_status"] == "not set"
    assert by["openai"]["key_env"] == "OPENAI_API_KEY"


def test_providers_list_usable_models_and_budget() -> None:
    data = _client(make_deps(key_envs=KEY_ENVS)).get("/api/providers").json()
    assert [p["name"] for p in data["providers"]] == list(PROVIDERS)
    openai = data["providers"][1]
    assert openai["label"] == "OpenAI"
    assert [m["id"] for m in openai["models"]] == ["openai-model"]  # unpriced one hidden
    assert openai["models"][0]["aliases"] == ["balanced"]
    assert openai["default_model"] == "openai-model"
    assert data["default_max_output_tokens"] == 32_000
    assert data["budget"] == {
        "currency": "USD",
        "run_limit": "5.00",
        "confirm_above": "1.00",
        "daily_limit": "15.00",
        "spent_today": "0",
        "daily_remaining": "15.00",
    }


def test_deps_factory_is_called_once() -> None:
    calls: list[int] = []
    deps = make_deps()

    def factory() -> CompareDeps:
        calls.append(1)
        return deps

    client = TestClient(create_app(factory), base_url="http://127.0.0.1")
    client.get("/api/providers")
    client.get("/api/providers")
    assert calls == [1]


# --------------------------------------------------------------------------- estimate


def test_estimate_returns_range_and_budget() -> None:
    response = _client(make_deps()).post("/api/estimate", json=_body("openai", "gemini"))
    assert response.status_code == 200
    data = response.json()
    assert Decimal(data["total_high"]) == Decimal("0.08192")
    assert Decimal(data["total_low"]) < Decimal(data["total_high"])
    assert [line["provider"] for line in data["lines"]] == ["openai", "gemini"]
    assert data["budget"]["needs_confirmation"] is False
    assert data["budget"]["refused"] is False
    assert data["budget"]["daily_remaining"] == "15.00"


def test_estimate_flags_confirmation_and_refusal() -> None:
    confirm = _client(make_deps(budget=BudgetConfig(confirm_above=Decimal("0.01"))))
    assert confirm.post("/api/estimate", json=_body("openai")).json()["budget"][
        "needs_confirmation"
    ]
    refuse = _client(make_deps(budget=BudgetConfig(run_limit=Decimal("0.01"))))
    budget = refuse.post("/api/estimate", json=_body("openai")).json()["budget"]
    assert budget["refused"] is True
    assert "run limit" in budget["refusal_reason"]


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (_body("openai", model="openai-unpriced"), 400, "pricing"),
        (_body("openai", "openai"), 400, "invalid_selection"),
        (_body("nope"), 400, "invalid_selection"),
        ({"task": "   ", "selections": [{"provider": "openai", "model": "x"}]}, 400, None),
    ],
)
def test_estimate_errors_are_clear_4xx(body: dict[str, Any], status: int, code: str | None) -> None:
    response = _client(make_deps()).post("/api/estimate", json=body)
    assert response.status_code == status
    assert response.json()["detail"]
    if code is not None:
        assert response.json()["code"] == code


def test_request_validation_is_422() -> None:
    response = _client(make_deps()).post("/api/estimate", json={"selections": []})
    assert response.status_code == 422


# --------------------------------------------------------------------------- run


def test_run_returns_results_side_by_side(all_keys: None) -> None:
    ledger = FakeLedger()
    deps = make_deps(key_envs=KEY_ENVS, ledger=ledger)
    response = _client(deps).post("/api/run", json=_body(*PROVIDERS))
    assert response.status_code == 200
    data = response.json()
    assert [m["provider"] for m in data["members"]] == list(PROVIDERS)
    assert all(m["error"] is None and TASK in m["text"] for m in data["members"])
    assert data["total_cost"] == "0.04"
    assert len(ledger.entries) == 4
    assert FAKE_KEY not in response.text


def test_run_one_failure_does_not_break_the_others() -> None:
    deps = make_deps(
        providers={
            **{p: echo_provider(p) for p in PROVIDERS},
            "openai": raising_provider("openai", ProviderError("rate limited")),
        }
    )
    response = _client(deps).post("/api/run", json=_body(*PROVIDERS))
    assert response.status_code == 200
    members = {m["provider"]: m for m in response.json()["members"]}
    assert members["openai"]["error"] == "rate limited"
    assert members["openai"]["text"] is None
    for name in ("anthropic", "gemini", "xai"):
        assert members[name]["error"] is None
        assert members[name]["text"]


def test_run_missing_key_is_a_member_error() -> None:
    response = _client(make_deps(key_envs=KEY_ENVS)).post("/api/run", json=_body("xai"))
    assert response.status_code == 200
    (member,) = response.json()["members"]
    assert member["error"] == "XAI_API_KEY is not set. Add it in Settings > API Keys (/settings)."


def test_run_budget_refusal_is_4xx_and_calls_nothing() -> None:
    providers = {p: echo_provider(p) for p in PROVIDERS}
    deps = make_deps(providers=providers, ledger=FakeLedger(spent_today=Decimal("14.99")))
    response = _client(deps).post("/api/run", json=_body("openai"))
    assert response.status_code == 409
    data = response.json()
    assert data["code"] == "budget_refused"
    assert "remaining" in data["detail"]
    assert data["budget"]["daily_remaining"] == "0.01"
    assert all(not p.requests for p in providers.values())


def test_run_above_threshold_requires_confirmation() -> None:
    client = _client(make_deps(budget=BudgetConfig(confirm_above=Decimal("0.01"))))
    unconfirmed = client.post("/api/run", json=_body("openai"))
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["code"] == "confirmation_required"
    confirmed = client.post("/api/run", json=_body("openai", confirmed=True))
    assert confirmed.status_code == 200


# --------------------------------------------------------------------------- guards


def test_foreign_host_is_rejected() -> None:
    client = TestClient(create_app(make_deps), base_url="http://evil.example")
    assert client.get("/api/providers").status_code == 400


def test_post_requires_json() -> None:
    client = _client(make_deps())
    response = client.post(
        "/api/run", content="task=x", headers={"content-type": "application/x-www-form-urlencoded"}
    )
    assert response.status_code == 415


def test_cross_origin_post_is_rejected() -> None:
    client = _client(make_deps())
    evil = client.post(
        "/api/estimate", json=_body("openai"), headers={"origin": "http://evil.example"}
    )
    assert evil.status_code == 403
    same = client.post(
        "/api/estimate", json=_body("openai"), headers={"origin": "http://127.0.0.1"}
    )
    assert same.status_code == 200


def test_wildcard_hosts_accept_any_host() -> None:
    client = TestClient(create_app(make_deps, allowed_hosts=("*",)), base_url="http://lan-host")
    assert client.get("/api/providers").status_code == 200


# --------------------------------------------------------------------------- cli


class _UvicornSpy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, app: object, **kwargs: Any) -> None:
        self.calls.append({"app": app, **kwargs})


class _TimerSpy:
    started: ClassVar[list[tuple[Any, ...]]] = []

    def __init__(self, interval: float, function: object, args: tuple[Any, ...]) -> None:
        self.args = args
        self.daemon = False

    def start(self) -> None:
        _TimerSpy.started.append(self.args)


def test_ui_help() -> None:
    result = CliRunner().invoke(cli_module.app, ["ui", "--help"])
    assert result.exit_code == 0, result.output
    for option in ("--host", "--port", "--no-browser"):
        assert option in re.sub(r"\x1b\[[0-9;]*m", "", result.output)


def test_ui_starts_uvicorn_on_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _UvicornSpy()
    _TimerSpy.started = []
    monkeypatch.setattr(cli_module, "build_default_deps", lambda base_dir: make_deps())
    monkeypatch.setattr("uvicorn.run", spy)
    monkeypatch.setattr("threading.Timer", _TimerSpy)
    result = CliRunner().invoke(cli_module.app, ["ui"])
    assert result.exit_code == 0, result.output
    (call,) = spy.calls
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 8765
    assert _TimerSpy.started == [("http://127.0.0.1:8765/",)]
    assert "http://127.0.0.1:8765/" in result.output


def test_ui_no_browser_and_custom_port(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _UvicornSpy()
    _TimerSpy.started = []
    monkeypatch.setattr(cli_module, "build_default_deps", lambda base_dir: make_deps())
    monkeypatch.setattr("uvicorn.run", spy)
    monkeypatch.setattr("threading.Timer", _TimerSpy)
    result = CliRunner().invoke(cli_module.app, ["ui", "--port", "9000", "--no-browser"])
    assert result.exit_code == 0, result.output
    assert spy.calls[0]["port"] == 9000
    assert _TimerSpy.started == []
