"""Web API for the subscription (cli) backend: CLI status, consent, estimates, synthesis.

Fake CLI providers only; no real CLI is ever run.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from llm_crossjury.core.compare_service import CompareDeps
from llm_crossjury.core.providers.base import Backend
from llm_crossjury.web import create_app
from llm_crossjury.web.app import CONSENT_TEXT
from tests.unit._compare_fakes import CLI_PROVIDERS, FakeCliProvider, FakeLedger, make_deps
from tests.unit._synthesis_fakes import FakeStages

KEY_ENVS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY",
}
FAKE_KEY = "sk-fake-TOPSECRET-0123456789abcdef"
TASK = "Explain sorting."


def _client(deps: CompareDeps) -> TestClient:
    return TestClient(create_app(lambda: deps), base_url="http://127.0.0.1")


def _deps(**kwargs: Any) -> tuple[CompareDeps, dict[str, FakeCliProvider]]:
    clis = kwargs.pop("clis", None) or {p: FakeCliProvider(p) for p in CLI_PROVIDERS}
    return make_deps(cli_providers=clis, key_envs=KEY_ENVS, **kwargs), clis


def _mixed_body(**extra: Any) -> dict[str, Any]:
    return {
        "task": TASK,
        "selections": [
            {"provider": "anthropic", "backend": "cli", "model": "anthropic-cli"},
            {"provider": "openai", "model": "balanced"},
        ],
        **extra,
    }


# --------------------------------------------------------------------------- providers


def test_providers_report_cli_status_and_models() -> None:
    clis = {
        "anthropic": FakeCliProvider("anthropic"),
        "openai": FakeCliProvider("openai", available=False, detail="codex not found on PATH"),
        "xai": FakeCliProvider("xai", check_error=RuntimeError("boom sk-secret")),
    }
    deps, _ = _deps(clis=clis)
    data = _client(deps).get("/api/providers").json()
    assert data["consent_text"] == CONSENT_TEXT
    by = {p["name"]: p for p in data["providers"]}

    claude = by["anthropic"]["cli"]
    assert claude["available"] is True
    assert claude["command"] == "claude"
    assert claude["product"] == "Claude Code"
    assert claude["experimental"] is False
    assert [m["id"] for m in claude["models"]] == ["anthropic-cli"]
    assert claude["default_model"] == "anthropic-cli"
    assert claude["install"]
    assert claude["sign_in"]
    assert [m["id"] for m in by["anthropic"]["models"]] == ["anthropic-model"]

    codex = by["openai"]["cli"]
    assert codex["available"] is False
    assert codex["detail"] == "codex not found on PATH"

    grok = by["xai"]["cli"]
    assert grok["experimental"] is True
    assert grok["available"] is False
    assert grok["detail"] == "The CLI check failed (RuntimeError)."
    assert "sk-secret" not in str(data)

    assert by["gemini"]["cli"] is None
    assert by["gemini"]["subscription_note"] == "Gemini requires an API key (Google AI Studio)."


def test_cli_status_never_leaks_key_material(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in KEY_ENVS.values():
        monkeypatch.setenv(env, FAKE_KEY)
    clis = {p: FakeCliProvider(p, detail=f"logged in via {FAKE_KEY}") for p in CLI_PROVIDERS}
    deps, _ = _deps(clis=clis)
    text = _client(deps).get("/api/providers").text
    for fragment in (FAKE_KEY, "TOPSECRET", "0123456789abcdef", "sk-fake"):
        assert fragment not in text
    assert "[redacted]" in text


def test_cli_checks_are_cached_and_refreshable() -> None:
    deps, clis = _deps()
    client = _client(deps)
    client.get("/api/providers")
    client.get("/api/providers")
    assert [c.checks for c in clis.values()] == [1, 1, 1]
    client.get("/api/providers?refresh=true")
    assert [c.checks for c in clis.values()] == [2, 2, 2]


def test_no_cli_check_without_a_provider_request() -> None:
    deps, clis = _deps()
    client = _client(deps)
    client.get("/")
    assert [c.checks for c in clis.values()] == [0, 0, 0]


# --------------------------------------------------------------------------- estimate / run


def test_estimate_shows_dollars_for_api_and_calls_for_cli() -> None:
    deps, _ = _deps()
    data = _client(deps).post("/api/estimate", json=_mixed_body()).json()
    assert data["cli_calls"] == 1
    assert data["budget"]["cli_calls"] == 1
    assert data["budget"]["refused"] is False
    assert Decimal(data["total_high"]) == Decimal("0.04096")
    lines = {line["provider"]: line for line in data["lines"]}
    assert lines["anthropic"]["backend"] == "cli"
    assert lines["anthropic"]["cost_high"] is None
    assert lines["anthropic"]["calls"] == 1
    assert lines["openai"]["backend"] == "api"


def test_run_with_cli_member_requires_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    ledger = FakeLedger()
    deps, clis = _deps(ledger=ledger)
    client = _client(deps)
    refused = client.post("/api/run", json=_mixed_body())
    assert refused.status_code == 409
    assert refused.json()["code"] == "consent_required"
    assert refused.json()["budget"]["cli_calls"] == 1
    assert clis["anthropic"].requests == []
    assert ledger.entries == []

    done = client.post("/api/run", json=_mixed_body(allow_unknown_cost=True))
    assert done.status_code == 200
    by = {m["provider"]: m for m in done.json()["members"]}
    assert by["anthropic"]["backend"] == "cli"
    assert by["anthropic"]["cost"] is None
    assert by["anthropic"]["error"] is None
    cli_entry = next(e for e in ledger.entries if e.backend is Backend.CLI)
    assert cli_entry.cost is None
    assert cli_entry.duration_s is not None
    assert FAKE_KEY not in done.text


def test_unknown_backend_is_rejected() -> None:
    deps, _ = _deps()
    body = {"task": TASK, "selections": [{"provider": "gemini", "backend": "cli", "model": "x"}]}
    response = _client(deps).post("/api/estimate", json=body)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_selection"


# --------------------------------------------------------------------------- synthesis

CLI_ANSWERS = [
    {"provider": "anthropic", "backend": "cli", "model": "anthropic-cli", "text": "A."},
    {"provider": "openai", "backend": "cli", "model": "openai-cli", "text": "B."},
]


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch) -> FakeStages:
    return FakeStages().install(monkeypatch)


def test_synthesis_with_cli_members(stages: FakeStages) -> None:
    deps, clis = _deps()
    client = _client(deps)
    body: dict[str, Any] = {"task": TASK, "answers": CLI_ANSWERS, "seed": 3}
    est = client.post("/api/synthesis/estimate", json=body).json()
    assert est["cli_calls"] == 4
    assert est["chair_backend"] == "cli"
    assert Decimal(est["total_high"]) == 0

    refused = client.post("/api/synthesis/agreement", json=body)
    assert refused.status_code == 409
    assert refused.json()["code"] == "consent_required"

    body["allow_unknown_cost"] = True
    first = client.post("/api/synthesis/agreement", json=body)
    assert first.status_code == 200
    assert [c["backend"] for c in first.json()["calls"]] == ["cli"]

    finish = client.post(
        "/api/synthesis/finish",
        json={**body, "run_id": first.json()["run_id"], "agreement": first.json()["agreement"]},
    )
    assert finish.status_code == 200
    calls = finish.json()["calls"]
    assert {c["backend"] for c in calls} == {"cli"}
    assert all(c["cost"] is None for c in calls)
    assert sum(len(c.requests) for c in clis.values()) == 4
