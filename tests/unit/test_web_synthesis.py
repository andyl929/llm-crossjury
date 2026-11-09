"""Synthesis web API: stateless flow with fake stages, errors, guards, no key leaks."""

from __future__ import annotations

import re
from decimal import Decimal
from importlib import resources
from typing import Any

import pytest
from fastapi.testclient import TestClient

from llm_crossjury.core.compare_service import CompareDeps
from llm_crossjury.core.config import BudgetConfig
from llm_crossjury.core.providers.base import ProviderError
from llm_crossjury.web import create_app
from tests.unit._compare_fakes import FakeLedger, make_deps, raising_provider, settings_for
from tests.unit._synthesis_fakes import FakeStages

KEY_ENVS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY",
}
FAKE_KEY = "sk-fake-TOPSECRET-0123456789abcdef"
TASK = "Which sorting algorithm should I use?"
ANSWERS = [
    {"provider": "anthropic", "model": "anthropic-model", "text": "Claude says: quicksort."},
    {"provider": "openai", "model": "openai-model", "text": "Merge sort."},
    {"provider": "gemini", "model": "gemini-model", "text": "Timsort <b>is</b> best."},
]


def _client(deps: CompareDeps) -> TestClient:
    return TestClient(
        create_app(lambda: deps, settings_store=settings_for(deps.budget)),
        base_url="http://127.0.0.1",
    )


def _body(**extra: Any) -> dict[str, Any]:
    return {"task": TASK, "answers": ANSWERS, "seed": 7, **extra}


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch) -> FakeStages:
    return FakeStages().install(monkeypatch)


@pytest.fixture
def all_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in KEY_ENVS.values():
        monkeypatch.setenv(env, FAKE_KEY)


def test_estimate_returns_stages_models_and_budget(stages: FakeStages) -> None:
    response = _client(make_deps()).post("/api/synthesis/estimate", json=_body())
    assert response.status_code == 200
    data = response.json()
    assert [s["stage"] for s in data["stages"]] == ["agreement", "review", "chair"]
    assert data["seed"] == 7
    assert data["chair"]["provider"] in {"anthropic", "openai", "gemini"}
    assert Decimal(data["total_high"]) == Decimal("0.05")
    assert data["budget"]["refused"] is False
    assert data["scrubbed"] is True


def test_stateless_flow_end_to_end(stages: FakeStages, all_keys: None) -> None:
    ledger = FakeLedger()
    client = _client(make_deps(key_envs=KEY_ENVS, ledger=ledger))
    est = client.post("/api/synthesis/estimate", json=_body(seed=None)).json()
    settings = {"seed": est["seed"], "chair": est["chair"], "extra_instructions": "Be brief."}

    first = client.post("/api/synthesis/agreement", json=_body(**settings))
    assert first.status_code == 200, first.text
    step1 = first.json()
    assert step1["seed"] == est["seed"]
    assert step1["suggest_skip"] is False
    labels = step1["labels"]
    assert sorted(item["label"] for item in labels) == ["Response A", "Response B", "Response C"]

    second = client.post(
        "/api/synthesis/finish",
        json=_body(**settings, run_id=step1["run_id"], agreement=step1["agreement"]),
    )
    assert second.status_code == 200, second.text
    step2 = second.json()
    assert step2["labels"] == labels  # stable given the seed
    assert step2["run_id"] == step1["run_id"]
    assert step2["chair"]["synthesis"]["final_text"] == "Final answer."
    assert [s["stage"] for s in step2["stage_costs"]] == ["review", "chair"]
    assert stages.seen["run_reviews"][0]["extra_instructions"] == "Be brief."

    dispute = step2["chair"]["synthesis"]["disputed"][0]
    rulings = [
        {"kind": "dispute", "target": dispute["id"], "decision": dispute["positions"][1]["label"]},
        {"kind": "unique", "target": "0", "decision": "include"},
    ]
    resynth_body = _body(
        **settings,
        agreement=step1["agreement"],
        reviews=step2["reviews"],
        previous=step2["chair"]["synthesis"],
        rulings=rulings,
    )
    est2 = client.post("/api/synthesis/resynthesize-estimate", json=resynth_body)
    assert est2.status_code == 200
    assert est2.json()["budget"]["refused"] is False
    third = client.post("/api/synthesis/resynthesize", json=resynth_body)
    assert third.status_code == 200, third.text
    assert third.json()["labels"] == labels
    chair_call = stages.seen["run_chair"][-1]
    assert [r.model_dump() for r in chair_call["rulings"]] == rulings
    assert chair_call["reviews"] is not None
    assert {e.stage for e in ledger.entries} == {"agreement", "review", "chair"}

    for response in (first, second, third, est2):
        assert FAKE_KEY not in response.text
        assert "TOPSECRET" not in response.text


def test_skip_review_is_honoured(stages: FakeStages) -> None:
    response = _client(make_deps()).post("/api/synthesis/finish", json=_body(skip_review=True))
    assert response.status_code == 200
    assert response.json()["review_skipped"] is True
    assert response.json()["reviews"] is None
    assert "run_reviews" not in stages.seen


def test_budget_refusal_is_409_and_calls_nothing(stages: FakeStages) -> None:
    ledger = FakeLedger()
    deps = make_deps(ledger=ledger, budget=BudgetConfig(run_limit=Decimal("0.015")))
    client = _client(deps)
    for path, body in (
        ("/api/synthesis/agreement", _body()),
        ("/api/synthesis/finish", _body()),
    ):
        response = client.post(path, json=body)
        assert response.status_code == 409, path
        assert response.json()["code"] == "budget_refused"
        assert response.json()["budget"]["refused"] is True
    assert ledger.entries == []


def test_confirmation_required_above_threshold(stages: FakeStages) -> None:
    deps = make_deps(budget=BudgetConfig(confirm_above=Decimal("0.001")))
    client = _client(deps)
    response = client.post("/api/synthesis/agreement", json=_body())
    assert response.status_code == 409
    assert response.json()["code"] == "confirmation_required"
    assert "run_agreement" not in stages.seen
    assert client.post("/api/synthesis/agreement", json=_body(confirmed=True)).status_code == 200


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (_body(answers=ANSWERS[:1]), 400, "invalid_selection"),
        (_body(task="   "), 400, "invalid_selection"),
        (_body(chair={"provider": "xai", "model_id": "xai-unpriced"}), 400, "pricing"),
        (_body(extra_instructions="x" * 20_001), 400, "invalid_selection"),
        (_body(seed=-1), 422, None),
        (_body(run_id="not-a-run-id"), 422, None),
    ],
)
def test_errors_are_clear_4xx(
    stages: FakeStages, body: dict[str, Any], status: int, code: str | None
) -> None:
    path = "/api/synthesis/finish" if "run_id" in body else "/api/synthesis/estimate"
    response = _client(make_deps()).post(path, json=body)
    assert response.status_code == status
    if code is not None:
        assert response.json()["code"] == code


def test_invalid_ruling_is_400(stages: FakeStages) -> None:
    previous = {
        "consensus": [],
        "disputed": [],
        "unique": [],
        "final_text": "x",
    }
    body = _body(previous=previous, rulings=[{"kind": "dispute", "target": "d1", "decision": "A"}])
    response = _client(make_deps()).post("/api/synthesis/resynthesize", json=body)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_selection"


def test_stage_failure_is_502_without_key_material(
    stages: FakeStages, monkeypatch: pytest.MonkeyPatch
) -> None:
    for env in KEY_ENVS.values():
        monkeypatch.setenv(env, FAKE_KEY)
    deps = make_deps(key_envs=KEY_ENVS)
    providers = dict(deps.providers)
    for key in providers:
        providers[key] = raising_provider(key[0], ProviderError("vendor unavailable"))
    broken = CompareDeps(**{**deps.__dict__, "providers": providers})
    response = _client(broken).post("/api/synthesis/agreement", json=_body())
    assert response.status_code == 502
    assert response.json()["code"] == "provider_error"
    assert FAKE_KEY not in response.text


def test_synthesis_posts_keep_the_guards(stages: FakeStages) -> None:
    client = _client(make_deps())
    assert (
        client.post(
            "/api/synthesis/estimate", content="{}", headers={"content-type": "text/plain"}
        ).status_code
        == 415
    )
    cross = client.post(
        "/api/synthesis/estimate", json=_body(), headers={"origin": "http://evil.example"}
    )
    assert cross.status_code == 403
    foreign = TestClient(create_app(make_deps), base_url="http://evil.example")
    assert foreign.post("/api/synthesis/estimate", json=_body()).status_code == 400


def test_page_has_the_synthesis_panel() -> None:
    client = _client(make_deps())
    page = client.get("/").text
    static = resources.files("llm_crossjury.web").joinpath("static")
    scripts = re.findall(r'<script src="/([\w.-]+\.js)"></script>', page)
    assert "jury.js" in scripts
    # Every jury turn in the thread builds its own step-2 panel (jury.js).
    script = static.joinpath("jury.js").read_text(encoding="utf-8")
    for cls in ("synthesis", "syn-btn", "synth-form", "synth-result", "resynth-btn"):
        assert f'"{cls}' in script or f" {cls}" in script
    for path in ("estimate", "agreement", "finish", "resynthesize-estimate", "resynthesize"):
        assert f'"/api/synthesis/{path}"' in script
    assert "innerHTML" not in script
