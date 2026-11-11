"""Progressive step 1 (members stored as they finish) and per-member retry (CI-7)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from llm_crossjury.core.app_settings import AppSettingsStore
from llm_crossjury.core.compare_service import (
    CompareDeps,
    MemberResult,
    Selection,
    replace_member,
    run_compare,
)
from llm_crossjury.core.config import BudgetConfig
from llm_crossjury.core.providers.base import (
    CompletionRequest,
    CompletionResult,
    ProviderError,
)
from llm_crossjury.core.providers.mock import MockProvider
from llm_crossjury.core.sessions.models import JuryTurn, Session, TurnStatus
from llm_crossjury.core.sessions.store import SessionStore, new_id
from llm_crossjury.core.sessions.tasks import TaskRunner
from llm_crossjury.web import create_app
from llm_crossjury.web import session_hooks as hooks
from llm_crossjury.web.session_hooks import AppContext
from tests.unit._compare_fakes import (
    PROVIDERS,
    FakeCliProvider,
    echo_provider,
    make_deps,
)
from tests.unit._session_fakes import FakeSessionStore, FakeTaskRunner

TASK = "Which database should I use?"


class EventProvider(MockProvider):
    """Answers only once ``release`` is set."""

    def __init__(self, name: str) -> None:
        super().__init__(name, lambda req: f"{name} answer")
        self.release = asyncio.Event()

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        await asyncio.wait_for(self.release.wait(), timeout=5)
        return await super().complete(request)


class FlakyProvider(MockProvider):
    """Fails the first call, then answers."""

    def __init__(self, name: str) -> None:
        super().__init__(name, self._respond)
        self.calls = 0

    def _respond(self, request: CompletionRequest) -> str:
        self.calls += 1
        if self.calls == 1:
            raise ProviderError("vendor down")
        return f"{self.name} answer #{self.calls}"


def _sel(*providers: str) -> tuple[Selection, ...]:
    return tuple(Selection(provider=p, model="balanced") for p in providers)


# --------------------------------------------------------------------------- compare service


def test_on_member_is_called_in_completion_order() -> None:
    slow = EventProvider("anthropic")
    providers = {p: echo_provider(p) for p in PROVIDERS}
    providers["anthropic"] = slow
    deps = make_deps(providers=providers)
    seen: list[tuple[int, str]] = []

    def on_member(index: int, member: MemberResult) -> None:
        seen.append((index, member.provider))
        if member.provider == "openai":
            slow.release.set()

    result = asyncio.run(run_compare(TASK, _sel("anthropic", "openai"), deps, on_member=on_member))
    assert seen == [(1, "openai"), (0, "anthropic")]
    assert [m.provider for m in result.members] == ["anthropic", "openai"]


def test_on_member_failure_does_not_fail_the_run() -> None:
    def boom(index: int, member: MemberResult) -> None:
        raise RuntimeError("hook bug")

    result = asyncio.run(run_compare(TASK, _sel("openai", "gemini"), make_deps(), on_member=boom))
    assert all(m.error is None for m in result.members)


def test_on_member_reports_member_errors() -> None:
    providers = {p: echo_provider(p) for p in PROVIDERS}
    providers["openai"] = FlakyProvider("openai")
    seen: dict[int, MemberResult] = {}
    asyncio.run(
        run_compare(
            TASK,
            _sel("gemini", "openai"),
            make_deps(providers=providers),
            on_member=seen.__setitem__,
        )
    )
    assert seen[1].error == "vendor down"
    assert seen[0].error is None


def test_replace_member_recomputes_totals() -> None:
    providers = {p: echo_provider(p) for p in PROVIDERS}
    providers["openai"] = FlakyProvider("openai")
    deps = make_deps(providers=providers)
    first = asyncio.run(run_compare(TASK, _sel("gemini", "openai"), deps))
    assert first.total_cost == Decimal("0.01")
    again = asyncio.run(run_compare(TASK, _sel("openai"), deps))
    updated = replace_member(first, 1, again.members[0])
    assert updated.run_id == first.run_id
    assert updated.members[1].text == "openai answer #2"
    assert updated.total_cost == Decimal("0.02")
    with pytest.raises(IndexError):
        replace_member(first, 2, again.members[0])


# --------------------------------------------------------------------------- session hook


def _ctx(deps: CompareDeps) -> AppContext:
    return AppContext(
        deps=lambda: deps,
        sessions=FakeSessionStore(),
        tasks=FakeTaskRunner(),
        settings=AppSettingsStore(None),
    )


def test_members_done_is_written_as_each_member_finishes() -> None:
    slow = EventProvider("anthropic")
    providers = {p: echo_provider(p) for p in PROVIDERS}
    providers["anthropic"] = slow
    deps = make_deps(providers=providers)
    ctx = _ctx(deps)
    sid = ctx.sessions.create().id
    tid = new_id()
    snapshots: list[JuryTurn] = []

    def turn() -> JuryTurn:
        found = ctx.sessions.get(sid).turns[0]
        assert isinstance(found, JuryTurn)
        return found

    async def main() -> None:
        sels = _sel("anthropic", "openai", "gemini")
        work = run_compare(TASK, sels, deps, on_member=hooks.member_writer(ctx, sid, tid))
        job = asyncio.ensure_future(
            hooks.run_jury_turn(
                ctx,
                sid,
                tid,
                task=TASK,
                case_file=None,
                selections=sels,
                attachment_ids=(),
                work=work,
            )
        )
        for _ in range(200):
            await asyncio.sleep(0)
            if len(turn().members_done) == 2:
                break
        snapshots.append(turn())
        slow.release.set()
        await job

    asyncio.run(main())
    partial = snapshots[0]
    assert partial.status is TurnStatus.PENDING
    assert partial.step1 is None
    assert sorted(partial.members_done) == [1, 2]
    assert partial.members_done[1].provider == "openai"
    final = turn()
    assert final.status is TurnStatus.DONE
    assert final.step1 is not None
    assert [m.provider for m in final.step1.members] == ["anthropic", "openai", "gemini"]
    assert sorted(final.members_done) == [0, 1, 2]


def test_members_done_round_trips_through_json() -> None:
    turn = JuryTurn.model_validate(
        {
            "id": new_id(),
            "created_at": "2026-11-09T12:00:00Z",
            "task": TASK,
            "members_done": {},
        }
    )
    dumped = turn.model_dump(mode="json")
    assert JuryTurn.model_validate(dumped) == turn


# --------------------------------------------------------------------------- retry endpoint


class ForcedRunner(TaskRunner):
    """The real runner, plus keys a test declares as running."""

    def __init__(self) -> None:
        super().__init__()
        self.forced: set[str] = set()

    def running(self, key: str) -> bool:
        return key in self.forced or super().running(key)


class Env:
    def __init__(self, tmp_path: Path, deps: CompareDeps | None = None) -> None:
        self.deps = deps if deps is not None else make_deps()
        self.runner = ForcedRunner()
        self.app = create_app(
            lambda: self.deps,
            sessions_path=tmp_path / "sessions",
            settings_path=tmp_path / "config" / "settings.json",
            task_runner=self.runner,
        )
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.ctx: AppContext = self.app.state.crossjury

    def run(self, *providers: str, cli: tuple[str, ...] = ()) -> tuple[str, str]:
        sid = self.client.post("/api/sessions", json={}).json()["id"]
        tid = new_id()
        selections = [{"provider": p, "model": "balanced"} for p in providers]
        selections += [{"provider": p, "backend": "cli", "model": f"{p}-cli"} for p in cli]
        response = self.client.post(
            f"/api/run?session={sid}&turn={tid}",
            json={"task": TASK, "selections": selections, "allow_unknown_cost": bool(cli)},
        )
        assert response.status_code == 200, response.text
        return sid, tid

    def turn(self, sid: str) -> dict[str, Any]:
        data: dict[str, Any] = self.client.get(f"/api/sessions/{sid}").json()["turns"][0]
        return data

    def change(self, sid: str, tid: str, **update: Any) -> None:
        def apply(session: Session) -> Session:
            turns = tuple(t.model_copy(update=update) if t.id == tid else t for t in session.turns)
            return session.model_copy(update={"turns": turns})

        self.ctx.sessions.update(sid, apply)

    def retry(self, sid: str, tid: str, index: int, **extra: Any) -> Any:
        return self.client.post(
            f"/api/run/member?session={sid}&turn={tid}", json={"index": index, **extra}
        )


def _flaky_env(tmp_path: Path) -> Env:
    providers = {p: echo_provider(p) for p in PROVIDERS}
    providers["openai"] = FlakyProvider("openai")
    return Env(tmp_path, make_deps(providers=providers))


def test_run_with_session_stores_members_done(tmp_path: Path) -> None:
    env = Env(tmp_path)
    sid, _ = env.run("openai", "gemini")
    turn = env.turn(sid)
    assert sorted(turn["members_done"]) == ["0", "1"]
    assert turn["members_done"]["1"] == turn["step1"]["members"][1]


def test_retry_error_member_to_success(tmp_path: Path) -> None:
    env = _flaky_env(tmp_path)
    sid, tid = env.run("gemini", "openai")
    before = env.turn(sid)
    assert before["step1"]["members"][1]["error"] == "vendor down"
    assert Decimal(before["step1"]["total_cost"]) == Decimal("0.01")
    response = env.retry(sid, tid, 1)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["index"] == 1
    assert body["member"]["text"] == "openai answer #2"
    after = env.turn(sid)
    assert body["turn"] == after
    assert after["step1"]["members"][1]["error"] is None
    assert after["step1"]["members"][0] == before["step1"]["members"][0]
    assert after["members_done"]["1"] == after["step1"]["members"][1]
    assert Decimal(after["step1"]["total_cost"]) == Decimal("0.02")
    assert after["step1"]["run_id"] == before["step1"]["run_id"]
    assert after["retrying"] == []
    assert after["synthesis_stale"] is False


def test_retry_recounts_unknown_cost_members(tmp_path: Path) -> None:
    deps = make_deps(cli_providers={"openai": FakeCliProvider("openai")})
    env = Env(tmp_path, deps)
    sid, tid = env.run("gemini", cli=("openai",))
    assert env.turn(sid)["step1"]["unknown_cost_members"] == 1
    consent = env.retry(sid, tid, 1)
    assert consent.status_code == 409
    assert consent.json()["code"] == "consent_required"
    assert env.turn(sid)["retrying"] == []
    ok = env.retry(sid, tid, 1, allow_unknown_cost=True)
    assert ok.status_code == 200, ok.text
    assert ok.json()["turn"]["step1"]["unknown_cost_members"] == 1


def test_retry_prechecks_leave_the_turn_unchanged(tmp_path: Path) -> None:
    env = Env(tmp_path, make_deps(budget=BudgetConfig()))
    sid, tid = env.run("openai", "gemini")
    before = env.turn(sid)
    env.client.put(
        "/api/settings/app",
        json={"budget": {"run_limit": "5.00", "confirm_above": "0.001", "daily_limit": "15.00"}},
    )
    confirm = env.retry(sid, tid, 0)
    assert confirm.status_code == 409
    assert confirm.json()["code"] == "confirmation_required"
    assert confirm.json()["budget"]["needs_confirmation"] is True
    env.client.put(
        "/api/settings/app",
        json={"budget": {"run_limit": "0.001", "confirm_above": "0.0", "daily_limit": "15.00"}},
    )
    refused = env.retry(sid, tid, 0, confirmed=True)
    assert refused.status_code == 409
    assert refused.json()["code"] == "budget_refused"
    assert env.turn(sid) == {**before, "synthesis_stale": False}


def test_retry_target_errors(tmp_path: Path) -> None:
    env = Env(tmp_path)
    sid, tid = env.run("openai", "gemini")
    assert env.retry(sid, tid, 2).json()["code"] == "invalid_member_index"
    assert env.retry(sid, tid, 2).status_code == 400
    assert env.retry(sid, tid, -1).status_code == 400
    assert env.retry(sid, new_id(), 0).status_code == 404
    assert env.retry(new_id(), tid, 0).json()["code"] == "session_not_found"
    no_turn = env.client.post(f"/api/run/member?session={sid}", json={"index": 0})
    assert no_turn.json()["code"] == "invalid_turn_id"
    env.change(sid, tid, step1=None, status=TurnStatus.ERROR, error="failed")
    assert env.retry(sid, tid, 0).json()["code"] == "no_answers"


def test_retry_busy_cases(tmp_path: Path) -> None:
    env = Env(tmp_path)
    sid, tid = env.run("openai", "gemini")
    env.change(sid, tid, status=TurnStatus.PENDING)
    pending = env.retry(sid, tid, 0)
    assert (pending.status_code, pending.json()["code"]) == (409, "turn_busy")
    env.change(sid, tid, status=TurnStatus.DONE, retrying=(1,))
    env.runner.forced.add(f"{sid}/{tid}/member/1")
    busy = env.retry(sid, tid, 1)
    assert (busy.status_code, busy.json()["code"]) == (409, "turn_busy")
    other = env.retry(sid, tid, 0)  # another member may run again meanwhile
    assert other.status_code == 200, other.text
    assert other.json()["turn"]["retrying"] == [1]
    env.runner.forced.clear()
    stale = env.retry(sid, tid, 1)  # a leftover index (server restart) is not busy
    assert stale.status_code == 200
    assert stale.json()["turn"]["retrying"] == []
    env.change(sid, tid, synthesis={"pending": "agreement"})
    env.runner.forced.add(f"{sid}/{tid}/synthesis")
    step2 = env.retry(sid, tid, 0)
    assert (step2.status_code, step2.json()["code"]) == (409, "turn_busy")


def test_stage_target_refuses_while_retrying(tmp_path: Path) -> None:
    env = Env(tmp_path)
    sid, tid = env.run("openai", "gemini")
    env.change(sid, tid, retrying=(0,))
    assert hooks.check_stage_target(env.ctx, sid, tid) is None  # not running: stale
    env.runner.forced.add(f"{sid}/{tid}/member/0")
    refused = hooks.check_stage_target(env.ctx, sid, tid)
    assert refused is not None
    assert refused.status_code == 409


def test_retry_failure_keeps_the_old_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_path)
    sid, tid = env.run("openai", "gemini")
    before = env.turn(sid)

    async def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr("llm_crossjury.web.app.run_compare", explode)
    client = TestClient(env.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    response = client.post(f"/api/run/member?session={sid}&turn={tid}", json={"index": 0})
    assert response.status_code == 500
    after = env.turn(sid)
    assert after["step1"] == before["step1"]
    assert after["retrying"] == []


class _Stage(BaseModel):
    ok: bool = True


def test_synthesis_stale_is_set_and_cleared(tmp_path: Path) -> None:
    env = Env(tmp_path)
    sid, tid = env.run("openai", "gemini")
    first = env.retry(sid, tid, 0)
    assert first.json()["turn"]["synthesis_stale"] is False  # no step 2 yet
    env.change(sid, tid, synthesis={"agreement": {"run_id": "r"}})
    stale = env.retry(sid, tid, 1)
    assert stale.json()["turn"]["synthesis_stale"] is True

    async def stage() -> _Stage:
        return _Stage()

    async def start() -> None:
        await hooks.run_stage(env.ctx, sid, tid, "agreement", stage())

    asyncio.run(start())
    assert env.turn(sid)["synthesis_stale"] is False


def test_member_estimate(tmp_path: Path) -> None:
    env = Env(tmp_path)
    sid, tid = env.run("openai", "gemini")
    response = env.client.post(
        f"/api/run/member/estimate?session={sid}&turn={tid}", json={"index": 1}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [line["provider"] for line in body["lines"]] == ["gemini"]
    assert body["budget"]["refused"] is False
    bad = env.client.post(f"/api/run/member/estimate?session={sid}&turn={tid}", json={"index": 5})
    assert bad.status_code == 400


def test_start_up_clears_retrying(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = store.create()
    turn = JuryTurn(
        id=new_id(),
        created_at=session.created_at,
        status=TurnStatus.DONE,
        task=TASK,
        retrying=(0, 2),
    )
    store.update(session.id, lambda s: s.model_copy(update={"turns": (turn,)}))
    (recovered,) = SessionStore(tmp_path).get(session.id).turns
    assert isinstance(recovered, JuryTurn)
    assert recovered.retrying == ()
