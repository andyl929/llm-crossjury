"""Sessions API, app settings API, session hooks on run-type endpoints, static files."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from llm_crossjury.core.app_settings import AppSettingsStore
from llm_crossjury.core.attachments.store import AttachmentStore
from llm_crossjury.core.compare_service import CompareDeps
from llm_crossjury.core.config import BudgetConfig
from llm_crossjury.core.providers.base import AttachmentKind, ProviderError
from llm_crossjury.core.sessions.models import JuryTurn, Session
from llm_crossjury.core.sessions.store import SessionStore, new_id
from llm_crossjury.web import create_app
from llm_crossjury.web.session_hooks import AppContext
from tests.unit._attachment_fakes import built
from tests.unit._compare_fakes import (
    PROVIDERS,
    FakeLedger,
    echo_provider,
    make_deps,
    raising_provider,
    settings_for,
)
from tests.unit._synthesis_fakes import FakeStages

TASK = "Which database should I use?"
ANSWERS = [
    {"provider": "anthropic", "model": "anthropic-model", "text": "Postgres."},
    {"provider": "openai", "model": "openai-model", "text": "SQLite."},
]


class Env:
    def __init__(self, tmp_path: Path, deps: CompareDeps | None = None, **kwargs: Any) -> None:
        self.deps = deps if deps is not None else make_deps()
        self.sessions_dir = tmp_path / "sessions"
        self.settings_path = tmp_path / "config" / "settings.json"
        self.app = create_app(
            lambda: self.deps,
            sessions_path=self.sessions_dir,
            settings_path=self.settings_path,
            **kwargs,
        )
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.ctx: AppContext = self.app.state.crossjury

    def new_session(self) -> str:
        response = self.client.post("/api/sessions", json={})
        assert response.status_code == 200
        session_id: str = response.json()["id"]
        return session_id

    def session(self, session_id: str) -> dict[str, Any]:
        response = self.client.get(f"/api/sessions/{session_id}")
        assert response.status_code == 200, response.text
        data: dict[str, Any] = response.json()
        return data


def _run_body(*providers: str, **extra: Any) -> dict[str, Any]:
    return {
        "task": TASK,
        "selections": [{"provider": p, "model": "balanced"} for p in providers],
        **extra,
    }


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# --------------------------------------------------------------------------- sessions


def test_session_crud(env: Env) -> None:
    client = env.client
    assert client.get("/api/sessions").json() == {"sessions": []}
    created = client.post("/api/sessions", json={"title": "  Plans  "}).json()
    Session.model_validate(created)
    sid = created["id"]
    assert created["title"] == "Plans"
    assert (env.sessions_dir / f"{sid}.json").is_file()
    other = env.new_session()
    rows = client.get("/api/sessions").json()["sessions"]
    assert {r["id"] for r in rows} == {sid, other}

    renamed = client.post(f"/api/sessions/{sid}/title", json={"title": "Renamed"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Renamed"
    assert renamed.json()["turns"] == 0
    assert client.get("/api/sessions").json()["sessions"][0]["id"] == sid  # newest first

    assert client.delete(f"/api/sessions/{sid}").json() == {"deleted": True}
    assert client.delete(f"/api/sessions/{sid}").json() == {"deleted": False}
    assert client.delete("/api/sessions").json() == {"deleted": 1}
    assert client.get("/api/sessions").json() == {"sessions": []}


def test_unknown_and_malformed_session_ids(env: Env) -> None:
    missing = env.client.get(f"/api/sessions/{new_id()}")
    assert missing.status_code == 404
    assert missing.json()["code"] == "session_not_found"
    for bad in ("nothex", "A" * 32, "..%2F..%2Fetc"):
        response = env.client.get(f"/api/sessions/{bad}")
        assert response.status_code in (400, 404)
        if response.status_code == 400:
            assert response.json()["code"] == "invalid_session_id"
    assert env.client.get("/api/sessions/" + "z" * 32).json()["code"] == "invalid_session_id"
    assert env.client.delete("/api/sessions/" + "z" * 32).status_code == 400
    title = env.client.post(f"/api/sessions/{new_id()}/title", json={"title": "x"})
    assert title.status_code == 404


def test_title_is_bounded(env: Env) -> None:
    sid = env.new_session()
    response = env.client.post(f"/api/sessions/{sid}/title", json={"title": "x" * 201})
    assert response.status_code == 422


def test_synthesis_settings_are_stored_on_jury_turns(env: Env) -> None:
    sid = env.new_session()
    turn = new_id()
    assert (
        env.client.post(f"/api/run?session={sid}&turn={turn}", json=_run_body("openai")).status_code
        == 200
    )
    url = f"/api/sessions/{sid}/turns/{turn}/synthesis-settings"
    response = env.client.post(url, json={"settings": {"chair": "openai", "seed": 3}})
    assert response.json() == {"ok": True}
    stored = env.session(sid)["turns"][0]["synthesis_settings"]
    assert stored == {"chair": "openai", "seed": 3}
    missing = env.client.post(
        f"/api/sessions/{sid}/turns/{new_id()}/synthesis-settings", json={"settings": {}}
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "turn_not_found"
    bad = env.client.post(
        f"/api/sessions/{sid}/turns/nope/synthesis-settings", json={"settings": {}}
    )
    assert bad.json()["code"] == "invalid_turn_id"


def test_session_writes_need_json_and_same_origin(env: Env) -> None:
    form = env.client.post(
        "/api/sessions", content=b"title=x", headers={"Content-Type": "text/plain"}
    )
    assert form.status_code == 415
    sid = env.new_session()
    cross = env.client.delete(f"/api/sessions/{sid}", headers={"Origin": "http://evil.example"})
    assert cross.status_code == 403
    put = env.client.put(
        "/api/settings/app",
        content=b"{}",
        headers={"Origin": "http://evil.example", "Content-Type": "application/json"},
    )
    assert put.status_code == 403


# --------------------------------------------------------------------------- settings


def test_app_settings_get_and_put(env: Env) -> None:
    data = env.client.get("/api/settings/app").json()
    assert data["settings"] == data["defaults"]
    assert data["settings"]["budget"] == {
        "run_limit": "5.00",
        "confirm_above": "1.00",
        "daily_limit": "15.00",
    }
    assert data["settings"]["save_history"] is True
    assert data["settings"]["memory_model"] is None
    new = {
        "budget": {"run_limit": "2.00", "confirm_above": "0.50", "daily_limit": "8.00"},
        "save_history": True,
        "memory_model": {"provider": "gemini", "backend": "api", "model": "fast"},
        "context_budget_tokens": 60000,
    }
    saved = env.client.put("/api/settings/app", json=new)
    assert saved.status_code == 200, saved.text
    assert saved.json()["settings"]["memory_model"]["provider"] == "gemini"
    assert env.settings_path.is_file()
    assert env.client.get("/api/settings/app").json()["settings"]["context_budget_tokens"] == 60000


def test_app_settings_validation(env: Env) -> None:
    bad = env.client.put("/api/settings/app", json={"context_budget_tokens": 10})
    assert bad.status_code == 422
    unknown = env.client.put("/api/settings/app", json={"surprise": 1})
    assert unknown.status_code == 422


def test_budget_from_settings_applies_everywhere(env: Env) -> None:
    providers = env.client.get("/api/providers").json()
    assert providers["budget"]["run_limit"] == "5.00"
    assert providers["budget"]["daily_remaining"] == "15.00"
    env.client.put(
        "/api/settings/app",
        json={"budget": {"run_limit": "0.01", "confirm_above": "0.001", "daily_limit": "3.00"}},
    )
    providers = env.client.get("/api/providers").json()
    assert providers["budget"]["run_limit"] == "0.01"
    assert providers["budget"]["daily_limit"] == "3.00"
    est = env.client.post("/api/estimate", json=_run_body("openai")).json()
    assert est["budget"]["refused"] is True
    assert est["budget"]["run_limit"] == "0.01"
    run = env.client.post("/api/run", json=_run_body("openai"))
    assert run.status_code == 409
    onboarding = env.client.get("/api/onboarding").json()
    assert onboarding["budget"]["run_limit"] == "0.01"


def test_save_history_switch(env: Env) -> None:
    saved = env.new_session()
    env.client.put("/api/settings/app", json={"save_history": False})
    memory = env.new_session()
    assert (env.sessions_dir / f"{saved}.json").is_file()  # not deleted
    assert not (env.sessions_dir / f"{memory}.json").exists()
    assert {r["id"] for r in env.client.get("/api/sessions").json()["sessions"]} == {saved, memory}
    env.client.put("/api/settings/app", json={"save_history": True})
    assert (env.sessions_dir / f"{memory}.json").is_file()


def test_saved_preference_applies_at_start_up(tmp_path: Path) -> None:
    path = tmp_path / "config" / "settings.json"
    AppSettingsStore(path).save(
        settings_for(BudgetConfig()).get().model_copy(update={"save_history": False})
    )
    env = Env(tmp_path)
    sid = env.new_session()
    assert not (env.sessions_dir / f"{sid}.json").exists()


# --------------------------------------------------------------------------- run hooks


def test_run_with_session_records_the_turn(env: Env) -> None:
    sid = env.new_session()
    turn = new_id()
    response = env.client.post(
        f"/api/run?session={sid}&turn={turn}", json=_run_body("openai", "gemini")
    )
    assert response.status_code == 200, response.text
    session = env.session(sid)
    (stored,) = session["turns"]
    assert stored["kind"] == "jury"
    assert stored["id"] == turn
    assert stored["status"] == "done"
    assert stored["task"] == TASK
    assert stored["case_file"] is None
    assert [s["provider"] for s in stored["selections"]] == ["openai", "gemini"]
    assert stored["step1"] == response.json()
    rows = env.client.get("/api/sessions").json()["sessions"]
    assert rows[0]["title"] == TASK
    assert rows[0]["pending"] is False


def test_run_without_params_is_unchanged(env: Env) -> None:
    response = env.client.post("/api/run", json=_run_body("openai"))
    assert response.status_code == 200
    assert env.client.get("/api/sessions").json() == {"sessions": []}


def test_run_prechecks_leave_the_session_unchanged(tmp_path: Path) -> None:
    deps = make_deps(budget=BudgetConfig())
    env = Env(tmp_path, deps)
    env.client.put(
        "/api/settings/app",
        json={"budget": {"run_limit": "5.00", "confirm_above": "0.001", "daily_limit": "15.00"}},
    )
    sid = env.new_session()
    turn = new_id()
    response = env.client.post(f"/api/run?session={sid}&turn={turn}", json=_run_body("openai"))
    assert response.status_code == 409
    assert response.json()["code"] == "confirmation_required"
    assert env.session(sid)["turns"] == []
    ok = env.client.post(
        f"/api/run?session={sid}&turn={turn}", json=_run_body("openai", confirmed=True)
    )
    assert ok.status_code == 200
    again = env.client.post(
        f"/api/run?session={sid}&turn={turn}", json=_run_body("openai", confirmed=True)
    )
    assert again.status_code == 409
    assert again.json()["code"] == "turn_exists"


def test_run_hook_ids_are_checked(env: Env) -> None:
    sid = env.new_session()
    missing = env.client.post(
        f"/api/run?session={new_id()}&turn={new_id()}", json=_run_body("openai")
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "session_not_found"
    no_turn = env.client.post(f"/api/run?session={sid}", json=_run_body("openai"))
    assert no_turn.status_code == 400
    assert no_turn.json()["code"] == "invalid_turn_id"
    bad = env.client.post(f"/api/run?session=../x&turn={new_id()}", json=_run_body("openai"))
    assert bad.json()["code"] == "invalid_session_id"
    only_turn = env.client.post(f"/api/run?turn={new_id()}", json=_run_body("openai"))
    assert only_turn.json()["code"] == "invalid_session_id"


def test_run_failure_is_written_into_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deps = make_deps()
    env = Env(tmp_path, deps)
    sid = env.new_session()
    turn = new_id()

    async def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("secret detail sk-123")

    monkeypatch.setattr("llm_crossjury.web.app.run_compare", explode)
    client = TestClient(env.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    response = client.post(f"/api/run?session={sid}&turn={turn}", json=_run_body("openai"))
    assert response.status_code == 500
    (stored,) = env.session(sid)["turns"]
    assert stored["status"] == "error"
    assert stored["error"] == "The request failed (RuntimeError)."
    assert "sk-123" not in str(stored)


def test_member_errors_still_complete_the_turn(tmp_path: Path) -> None:
    providers = {p: echo_provider(p) for p in PROVIDERS}
    providers["openai"] = raising_provider("openai", ProviderError("vendor down"))
    env = Env(tmp_path, make_deps(providers=providers))
    sid = env.new_session()
    turn = new_id()
    env.client.post(f"/api/run?session={sid}&turn={turn}", json=_run_body("openai", "gemini"))
    assert env.session(sid)["turns"][0]["status"] == "done"


def test_case_file_is_sent_and_stored_mid_session(tmp_path: Path) -> None:
    calls: list[tuple[int, str]] = []

    async def builder(session: Session, task: str) -> str | None:
        calls.append((len(session.turns), task))
        return "Earlier we chose Postgres."

    env = Env(tmp_path, case_file_builder=builder)
    sid = env.new_session()
    first = new_id()
    env.client.post(f"/api/run?session={sid}&turn={first}", json=_run_body("openai"))
    assert calls == []  # first turn: no context needed
    est = env.client.post(f"/api/estimate?session={sid}", json=_run_body("openai"))
    assert est.status_code == 200
    second = new_id()
    response = env.client.post(f"/api/run?session={sid}&turn={second}", json=_run_body("openai"))
    assert response.status_code == 200
    expected = (
        "<conversation_context>\nEarlier we chose Postgres.\n</conversation_context>\n\n" + TASK
    )
    member = response.json()["members"][0]
    assert expected in member["text"]
    stored = env.session(sid)["turns"][1]
    assert stored["task"] == TASK
    assert stored["case_file"] == "Earlier we chose Postgres."
    assert [n for n, _ in calls] == [1, 1]  # estimate and run, both with 1 earlier turn
    assert all(t == TASK for _, t in calls)


def test_case_file_builder_can_be_set_later(tmp_path: Path) -> None:
    env = Env(tmp_path)

    async def builder(session: Session, task: str) -> str | None:
        return "CTX"

    env.ctx.case_file_builder = builder
    sid = env.new_session()
    env.client.post(f"/api/run?session={sid}&turn={new_id()}", json=_run_body("openai"))
    turn = new_id()
    env.client.post(f"/api/run?session={sid}&turn={turn}", json=_run_body("openai"))
    assert env.session(sid)["turns"][1]["case_file"] == "CTX"


def test_estimate_unknown_session_is_404(env: Env) -> None:
    response = env.client.post(f"/api/estimate?session={new_id()}", json=_run_body("openai"))
    assert response.status_code == 404
    assert response.json()["code"] == "session_not_found"


def test_extra_routers_get_the_context(tmp_path: Path) -> None:
    seen: list[AppContext] = []

    def factory(ctx: AppContext) -> APIRouter:
        seen.append(ctx)
        router = APIRouter()

        @router.get("/api/extra")
        async def extra() -> dict[str, int]:
            return {"sessions": len(ctx.sessions.list())}

        return router

    env = Env(tmp_path, extra_routers=[factory])
    assert seen == [env.ctx]
    assert env.client.get("/api/extra").json() == {"sessions": 0}


# --------------------------------------------------------------------------- synthesis hooks


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch) -> FakeStages:
    return FakeStages().install(monkeypatch)


def _synth_body(**extra: Any) -> dict[str, Any]:
    return {"task": TASK, "answers": ANSWERS, "seed": 7, **extra}


def test_synthesis_results_are_stored(env: Env, stages: FakeStages) -> None:
    sid = env.new_session()
    turn = new_id()
    env.client.post(f"/api/run?session={sid}&turn={turn}", json=_run_body("openai"))
    q = f"?session={sid}&turn={turn}"
    first = env.client.post("/api/synthesis/agreement" + q, json=_synth_body())
    assert first.status_code == 200, first.text
    step1 = first.json()
    second = env.client.post(
        "/api/synthesis/finish" + q,
        json=_synth_body(run_id=step1["run_id"], agreement=step1["agreement"]),
    )
    assert second.status_code == 200, second.text
    synthesis = env.session(sid)["turns"][0]["synthesis"]
    assert synthesis["agreement"] == step1
    assert synthesis["finish"] == second.json()
    assert "pending" not in synthesis


def test_synthesis_failure_is_stored(env: Env, stages: FakeStages, tmp_path: Path) -> None:
    providers = {p: echo_provider(p) for p in PROVIDERS}
    providers["anthropic"] = raising_provider("anthropic", ProviderError("down"))
    providers["openai"] = raising_provider("openai", ProviderError("down"))
    providers["gemini"] = raising_provider("gemini", ProviderError("down"))
    env.deps = make_deps(providers=providers)
    env.app = create_app(
        lambda: env.deps, sessions_path=env.sessions_dir, settings_path=env.settings_path
    )
    env.client = TestClient(env.app, base_url="http://127.0.0.1")
    sid = env.new_session()
    turn = new_id()
    env.client.post(f"/api/run?session={sid}&turn={turn}", json=_run_body("openai"))
    response = env.client.post(
        f"/api/synthesis/agreement?session={sid}&turn={turn}", json=_synth_body()
    )
    assert response.status_code == 502
    synthesis = env.session(sid)["turns"][0]["synthesis"]
    assert "pending" not in synthesis
    assert synthesis["agreement_error"]
    assert "agreement" not in synthesis


def test_synthesis_hook_targets_are_checked(env: Env, stages: FakeStages) -> None:
    sid = env.new_session()
    missing = env.client.post(
        f"/api/synthesis/agreement?session={sid}&turn={new_id()}", json=_synth_body()
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "turn_not_found"
    assert "run_agreement" not in stages.seen
    unknown = env.client.post(
        f"/api/synthesis/finish?session={new_id()}&turn={new_id()}", json=_synth_body()
    )
    assert unknown.json()["code"] == "session_not_found"


def test_synthesis_precheck_leaves_turn_unchanged(tmp_path: Path, stages: FakeStages) -> None:
    env = Env(tmp_path)
    sid = env.new_session()
    turn = new_id()
    env.client.post(f"/api/run?session={sid}&turn={turn}", json=_run_body("openai"))
    env.client.put(
        "/api/settings/app",
        json={"budget": {"run_limit": "5.00", "confirm_above": "0.0001", "daily_limit": "15.00"}},
    )
    response = env.client.post(
        f"/api/synthesis/agreement?session={sid}&turn={turn}", json=_synth_body()
    )
    assert response.json()["code"] == "confirmation_required"
    assert env.session(sid)["turns"][0]["synthesis"] == {}


# --------------------------------------------------------------------------- attachments


def test_attachments_of_sessions_survive_the_ttl(tmp_path: Path) -> None:
    clock = [1000.0]
    store = AttachmentStore(tmp_path / "att", ttl_s=10, clock=lambda: clock[0])
    deps = make_deps()
    deps = CompareDeps(**{**deps.__dict__, "attachment_store": store})
    env = Env(tmp_path, deps)
    kept = store.put(built(AttachmentKind.PDF, name="kept.pdf"))
    dropped = store.put(built(AttachmentKind.PDF, name="dropped.pdf", payload=b"%PDF-other"))
    sessions: SessionStore = env.ctx.sessions  # type: ignore[assignment]
    sid = env.new_session()
    env.client.get("/api/providers")  # builds the deps and wires the keep callback
    turn = JuryTurn(id=new_id(), created_at=datetime.now(UTC), task="t", attachment_ids=(kept,))
    sessions.update(sid, lambda s: s.model_copy(update={"turns": (turn,)}))
    clock[0] += 100
    assert store.purge_expired() == 1
    assert store.get(kept).attachment.sha256 == kept
    assert not (tmp_path / "att" / dropped).exists()


# --------------------------------------------------------------------------- static files


def test_generic_static_route(env: Env) -> None:
    for path, media in (
        ("/app.js", "text/javascript"),
        ("/theme.js", "text/javascript"),
        ("/app.css", "text/css"),
        ("/settings.css", "text/css"),
        ("/favicon.svg", "image/svg+xml"),
        ("/index.html", "text/html"),
    ):
        response = env.client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith(media), path
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
    for path in ("/", "/settings"):
        assert env.client.get(path).status_code == 200
    for path in (
        "/missing.js",
        "/../pyproject.toml",
        "/%2e%2e/app.py",
        "/app.py",
        "/__init__.py",
        "/.hidden.js",
        "/static/app.js",
    ):
        assert env.client.get(path).status_code == 404, path


def test_static_route_does_not_shadow_later_routes(env: Env) -> None:
    @env.app.get("/demo-extra.css")
    async def extra() -> dict[str, str]:
        return {"ok": "yes"}

    assert env.client.get("/demo-extra.css").json() == {"ok": "yes"}


def test_default_locations_follow_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_CROSSJURY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LLM_CROSSJURY_CONFIG_DIR", str(tmp_path / "cfg"))
    client = TestClient(create_app(make_deps, settings_store=None), base_url="http://127.0.0.1")
    sid = client.post("/api/sessions", json={}).json()["id"]
    assert (tmp_path / "data" / "sessions" / f"{sid}.json").is_file()
    client.put("/api/settings/app", json={"save_history": True})
    assert (tmp_path / "cfg" / "settings.json").is_file()


def test_deps_budget_currency_is_kept(tmp_path: Path) -> None:
    env = Env(tmp_path, make_deps(budget=BudgetConfig(currency="EUR")))
    budget = env.client.get("/api/providers").json()["budget"]
    assert budget["currency"] == "EUR"
    assert Decimal(budget["run_limit"]) == Decimal("5.00")


def test_ledger_spend_is_reported(tmp_path: Path) -> None:
    env = Env(tmp_path, make_deps(ledger=FakeLedger(spent_today=Decimal("2.50"))))
    budget = env.client.get("/api/providers").json()["budget"]
    assert budget["spent_today"] == "2.50"
    assert budget["daily_remaining"] == "12.50"


def test_settings_page_has_app_sections(env: Env) -> None:
    page = env.client.get("/settings").text
    for node in (
        'id="budget-form"',
        'id="history-save"',
        'id="history-clear"',
        'id="memory-model"',
    ):
        assert node in page
    script = env.client.get("/settings.js").text
    assert '"/api/settings/app"' in script
    assert '"/api/sessions"' in script
    assert "window.confirm(" in script
