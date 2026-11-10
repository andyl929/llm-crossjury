"""Demo mode: canned content, simulated providers, the demo app and ``ui --demo``."""

from __future__ import annotations

import asyncio
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from llm_crossjury.cli import app as cli_module
from llm_crossjury.core.anonymize import NAME_PATTERN
from llm_crossjury.core.cost.ledger import compute_cost
from llm_crossjury.core.providers.base import (
    Backend,
    CompletionRequest,
    Message,
    Prompt,
    ReasoningEffort,
    Role,
    Usage,
)
from llm_crossjury.core.registry.resolver import ModelRegistry
from llm_crossjury.core.schemas.agreement import AgreementJudgment
from llm_crossjury.core.synthesis import agreement as agreement_stage
from llm_crossjury.core.synthesis.types import AnonAnswer
from llm_crossjury.demo import BANNER_ID, DEMO_QUESTION, create_demo_app, responder, script
from llm_crossjury.demo import app as demo_app_module
from llm_crossjury.demo.providers import ANSWER_DELAY_S, STAGE_DELAY_S, DemoProvider

ALL = ("anthropic", "openai", "gemini", "xai")


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_demo_app(tmp_path, latency_scale=0), base_url="http://127.0.0.1")


def _selections(*providers: str, backend: str = "api") -> list[dict[str, str]]:
    return [{"provider": p, "backend": backend, "model": "balanced"} for p in providers]


def _run(client: TestClient, *providers: str) -> dict[str, Any]:
    body = {"task": DEMO_QUESTION, "selections": _selections(*providers)}
    response = client.post("/api/run", json=body)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def _answers(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"provider": m["provider"], "backend": m["backend"], "model": m["model"], "text": m["text"]}
        for m in run["members"]
    ]


def _label_of(labels: list[dict[str, str]], provider: str) -> str:
    return next(item["label"] for item in labels if item["provider"] == provider)


# --------------------------------------------------------------------------- script


def test_every_block_is_one_agreement_segment() -> None:
    assert len(responder.known_segments()) == sum(len(b) for b in script.ANSWERS.values())


def test_review_quotes_are_verbatim_in_their_answers() -> None:
    for provider, notes in script.REVIEW_NOTES.items():
        for issue in notes.issues:
            assert issue.quote in script.answer_text(provider), (provider, issue.quote)


def test_canned_text_never_names_a_model_or_vendor() -> None:
    texts = [DEMO_QUESTION, script.FINAL_TEXT, *map(script.answer_text, script.ANSWERS)]
    texts += [i for _, i in script.UNIQUE_NOT_INCLUDED]
    for text in texts:
        assert NAME_PATTERN.search(text) is None, text


def test_topics_cover_every_block() -> None:
    keys = {t.key for t in script.TOPICS}
    assert {b.topic for blocks in script.ANSWERS.values() for b in blocks} <= keys
    assert set(script.CONSENSUS_TOPICS) <= keys


# --------------------------------------------------------------------------- responder


def _request(text: str, schema: dict[str, Any] | None, stage: str = "answer") -> CompletionRequest:
    return CompletionRequest(
        model="m",
        prompt=Prompt(stable_prefix=(), variable_suffix=(Message(role=Role.USER, content=text),)),
        response_schema=schema,
        temperature=None,
        max_output_tokens=100,
        reasoning=ReasoningEffort.DEFAULT,
        native_params={},
        cache_prefix=False,
        timeout_s=None,
        tags={"stage": stage},
    )


def test_free_text_request_gets_the_canned_answer() -> None:
    assert responder.respond("gemini", _request("anything", None)) == script.answer_text("gemini")
    assert "no canned answer" in responder.respond("other", _request("x", None))


def test_chat_messages_get_the_canned_follow_up() -> None:
    from llm_crossjury.core.chat_service import CHAT_STAGE  # noqa: PLC0415

    reply = responder.respond("openai", _request("anything", None, stage=CHAT_STAGE))
    assert reply == script.CHAT_REPLY


def test_unknown_segments_become_their_own_groups() -> None:
    text = "Response A / s1: A sentence the script has never seen before."
    judgment = responder.agreement(text)
    assert len(judgment.groups) == 1
    assert judgment.groups[0].supporting[0].label == "Response A"


def test_agreement_with_only_contradicting_answers_uses_the_counter_statement() -> None:
    answers = [AnonAnswer(label="Response A", text=script.answer_text("xai"))]
    segments = [s for a in answers for s in agreement_stage.segment_answer(a)]
    text = "\n\n".join(f"{s.label} / {s.segment_id}: {s.text}" for s in segments)
    statements = {g.statement for g in responder.agreement(text).groups}
    pretest = next(t for t in script.TOPICS if t.key == "pretest")
    assert pretest.counter in statements
    assert all(not g.contradicting for g in responder.agreement(text).groups)


# --------------------------------------------------------------------------- providers


def test_demo_provider_waits_the_configured_latency() -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    registry = ModelRegistry.load(demo_app_module.shipped_registry_path())
    source = demo_app_module._api_adapters(registry)[0]
    provider = DemoProvider("anthropic", Backend.API, capabilities_from=source, sleep=sleep)
    result = asyncio.run(provider.complete(_request("q", None)))
    asyncio.run(provider.complete(_request("q", {"properties": {"groups": {}}}, "agreement")))
    assert slept == [ANSWER_DELAY_S["anthropic"], STAGE_DELAY_S["agreement"]]
    assert result.usage.output_tokens
    assert result.usage.input_tokens
    assert provider.capabilities("claude-sonnet-5-5") == source.capabilities("claude-sonnet-5-5")


# --------------------------------------------------------------------------- app


def test_demo_app_reports_demo_mode_and_needs_no_keys(tmp_path: Path) -> None:
    data = _client(tmp_path).get("/api/providers").json()
    assert data["demo"] is True
    by_name = {p["name"]: p for p in data["providers"]}
    assert set(by_name) == set(ALL)
    assert {p["key_status"] for p in by_name.values()} == {"not required"}
    for name in ("anthropic", "openai", "xai"):
        assert by_name[name]["cli"]["available"] is True
        assert "simulated" in by_name[name]["cli"]["detail"]
    assert by_name["gemini"]["cli"] is None
    assert data["budget"]["spent_today"] == "0"


def test_compare_run_prices_simulated_usage_with_registry_prices(tmp_path: Path) -> None:
    client = _client(tmp_path)
    run = _run(client, *ALL)
    registry = ModelRegistry.load(demo_app_module.shipped_registry_path())
    texts = {m["text"] for m in run["members"]}
    assert len(texts) == len(ALL)
    for member in run["members"]:
        assert member["error"] is None
        assert member["text"] == script.answer_text(member["provider"])
        entry = registry.get(member["provider"], member["model"])
        assert entry is not None
        assert entry.pricing is not None
        expected = compute_cost(Usage(**member["usage"]), entry.pricing, batched=False)
        assert Decimal(member["cost"]) == expected
        assert Decimal(member["cost"]) > 0
    assert (tmp_path / "ledger.sqlite3").is_file()
    spent = Decimal(client.get("/api/providers").json()["budget"]["spent_today"])
    assert spent == Decimal(run["total_cost"])


def test_subscription_members_report_unknown_usage(tmp_path: Path) -> None:
    body = {
        "task": DEMO_QUESTION,
        "selections": _selections("anthropic", "openai", backend="cli"),
        "allow_unknown_cost": True,
    }
    run = _client(tmp_path).post("/api/run", json=body).json()
    assert run["unknown_cost_members"] == 2
    for member in run["members"]:
        assert member["backend"] == "cli"
        assert member["cost"] is None
        assert member["usage"]["output_tokens"] is None


def test_full_synthesis_flow(tmp_path: Path) -> None:
    client = _client(tmp_path)
    base = {"task": DEMO_QUESTION, "answers": _answers(_run(client, *ALL))}
    step = client.post("/api/synthesis/agreement", json={**base, "confirmed": True})
    assert step.status_code == 200, step.text
    agreed = step.json()
    outcome = agreed["agreement"]
    assert (len(outcome["consensus"]), len(outcome["disputed"]), len(outcome["unique"])) == (
        4,
        3,
        4,
    )
    assert agreed["suggest_skip"] is False
    finish_body = {**base, "seed": agreed["seed"], "run_id": agreed["run_id"], "agreement": outcome}
    finished = client.post("/api/synthesis/finish", json=finish_body).json()
    labels = finished["labels"]
    # Ring review (k = 2): which answers meet depends on the seed, so ties are possible.
    scores = {r["label"]: r["score"] for r in finished["reviews"]["ranking"]}
    assert scores[_label_of(labels, "anthropic")] == max(scores.values())
    assert scores[_label_of(labels, "openai")] == min(scores.values())
    chair = finished["chair"]
    assert chair["synthesis"]["final_text"] == script.FINAL_TEXT
    assert len(chair["additional"]["disputed"]) == len(script.DISPUTES)
    assert len(chair["additional"]["unique"]) == len(script.UNIQUE_NOT_INCLUDED)
    wrong = {(w["target"], w["flagged_by"]) for w in chair["additional"]["possibly_wrong"]}
    assert wrong == {(_label_of(labels, "openai"), 2), (_label_of(labels, "gemini"), 2)}
    assert Decimal(finished["total_cost"]) > 0

    previous = chair["synthesis"]
    rulings = [
        {
            "kind": "dispute",
            "target": "d1",
            "decision": previous["disputed"][0]["positions"][0]["label"],
        },
        {"kind": "unique", "target": "0", "decision": "include"},
    ]
    redo = client.post(
        "/api/synthesis/resynthesize",
        json={
            **base,
            "seed": agreed["seed"],
            "agreement": outcome,
            "reviews": finished["reviews"],
            "previous": previous,
            "rulings": rulings,
        },
    )
    assert redo.status_code == 200, redo.text
    revised = redo.json()["chair"]["synthesis"]
    assert [d["id"] for d in revised["disputed"]] == ["d2"]
    assert len(revised["unique"]) == len(script.UNIQUE_NOT_INCLUDED) - 1
    assert "### Also consider" in revised["final_text"]


def test_synthesis_with_two_answers_keeps_only_valid_disputes(tmp_path: Path) -> None:
    client = _client(tmp_path)
    base = {"task": DEMO_QUESTION, "answers": _answers(_run(client, "anthropic", "openai"))}
    agreed = client.post("/api/synthesis/agreement", json={**base, "confirmed": True}).json()
    judgment = AgreementJudgment.model_validate(agreed["agreement"]["judgment"])
    assert judgment.groups
    finished = client.post(
        "/api/synthesis/finish",
        json={**base, "seed": agreed["seed"], "run_id": agreed["run_id"]},
    ).json()
    for point in finished["chair"]["synthesis"]["disputed"]:
        assert len(point["positions"]) >= 2


def test_demo_is_always_labelled(tmp_path: Path) -> None:
    client = _client(tmp_path)
    # The compare page shows its own badge from the providers endpoint...
    home = client.get("/")
    assert home.status_code == 200
    assert 'id="demo-badge"' in home.text
    assert f'id="{BANNER_ID}"' not in home.text
    assert client.get("/api/providers").json()["demo"] is True
    # ...other pages get the injected banner.
    page = client.get("/settings")
    assert page.status_code == 200
    assert f'id="{BANNER_ID}"' in page.text
    assert '<link rel="stylesheet" href="/demo.css">' in page.text
    css = client.get("/demo.css")
    assert css.status_code == 200
    assert ".demo-banner" in css.text
    assert BANNER_ID not in client.get("/app.js").text


def test_settings_page_is_isolated_and_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("ANTHROPIC_API_KEY=sk-should-never-be-read-123\n")
    (project / ".openai.key").write_text("sk-should-never-be-read-456\n")
    monkeypatch.chdir(project)
    monkeypatch.setenv("LLM_CROSSJURY_CONFIG_DIR", str(tmp_path / "real-config"))
    client = _client(tmp_path / "demo")
    keys = client.get("/api/keys").json()
    assert {row["status"] for row in keys["providers"]} == {"not set"}
    assert str(tmp_path / "demo") in keys["store_path"]
    onboarding = client.get("/api/onboarding").json()
    assert onboarding["show"] is False
    assert onboarding["tutorial_seen"] is True  # opens only from "How it works"
    tested = client.post("/api/keys/anthropic/test", json={}).json()
    assert "Demo mode" in tested["message"]
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert not (tmp_path / "real-config").exists()


# --------------------------------------------------------------------------- cli


class _UvicornSpy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, app: object, **kwargs: Any) -> None:
        self.calls.append({"app": app, **kwargs})


def test_ui_demo_serves_the_demo_app(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _UvicornSpy()
    monkeypatch.setattr("uvicorn.run", spy)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-not-real-0123456789")

    def forbidden(base_dir: Path) -> None:
        raise AssertionError("demo mode must not build the real deps")

    monkeypatch.setattr(cli_module, "build_default_deps", forbidden)
    result = CliRunner().invoke(cli_module.app, ["ui", "--demo", "--no-browser", "--port", "9123"])
    assert result.exit_code == 0, result.output
    assert "Demo mode" in result.output
    (call,) = spy.calls
    assert call["port"] == 9123
    client = TestClient(call["app"], base_url="http://127.0.0.1")
    # The temp data dir is gone once uvicorn.run (stubbed) returns: check pages only.
    assert 'id="demo-badge"' in client.get("/").text
    assert BANNER_ID in client.get("/settings").text
    assert "OPENAI_API_KEY" not in os.environ


def test_ui_help_lists_demo() -> None:
    result = CliRunner().invoke(cli_module.app, ["ui", "--help"])
    assert "--demo" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)


def test_demo_sessions_and_settings_stay_in_the_demo_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_data = tmp_path / "real-data"
    real_config = tmp_path / "real-config"
    monkeypatch.setenv("LLM_CROSSJURY_DATA_DIR", str(real_data))
    monkeypatch.setenv("LLM_CROSSJURY_CONFIG_DIR", str(real_config))
    demo_dir = tmp_path / "demo"
    demo_dir.mkdir()
    client = _client(demo_dir)
    session_id = client.post("/api/sessions", json={}).json()["id"]
    turn = "0" * 32
    response = client.post(
        f"/api/run?session={session_id}&turn={turn}",
        json={"task": DEMO_QUESTION, "selections": _selections("openai")},
    )
    assert response.status_code == 200, response.text
    assert client.get(f"/api/sessions/{session_id}").json()["turns"][0]["status"] == "done"
    settings = client.get("/api/settings/app").json()["settings"]
    assert Decimal(settings["budget"]["confirm_above"]) == Decimal("100")  # demo budget
    client.put("/api/settings/app", json={**settings, "save_history": True})
    assert (demo_dir / "sessions" / f"{session_id}.json").is_file()
    assert (demo_dir / "config" / "settings.json").is_file()
    assert not real_data.exists()
    assert not real_config.exists()
