"""Chat web API with in-memory session store, task runner and fake providers."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_crossjury.core.compare_service import CompareDeps
from llm_crossjury.core.memory.index import MemoryIndex
from llm_crossjury.core.providers.base import ProviderError
from llm_crossjury.core.sessions.interfaces import AppSettings, BudgetSettings
from llm_crossjury.core.sessions.models import ChatTurn, MemoryState, TurnStatus
from llm_crossjury.web.chat_api import create_chat_router
from tests.unit._compare_fakes import (
    CLI_PROVIDERS,
    FakeCliProvider,
    FakeLedger,
    echo_provider,
    make_deps,
    raising_provider,
)
from tests.unit._session_fakes import (
    CollectingBackground,
    FakeSessionStore,
    FakeTaskRunner,
    chat_turn,
    jury_turn,
    make_session,
    new_id,
    settings_getter,
)

# Generous limits: no confirmation for the fake estimate (0.04096 USD).
SETTINGS = AppSettings(budget=BudgetSettings(confirm_above=Decimal(1)))


class Harness:
    def __init__(
        self,
        deps: CompareDeps | None = None,
        settings: AppSettings = SETTINGS,
        *,
        background: CollectingBackground | None = None,
    ) -> None:
        self.deps = deps or make_deps()
        self.store = FakeSessionStore()
        self.tasks = FakeTaskRunner()
        self.index = MemoryIndex(None)
        self.background = background
        app = FastAPI()
        app.include_router(
            create_chat_router(
                lambda: self.deps,
                self.store,
                self.tasks,
                settings_getter(settings),
                None,
                index=self.index,
                cli_status=_no_clis,
                background=background,
            )
        )
        self.client = TestClient(app, base_url="http://127.0.0.1")


async def _no_clis() -> dict[str, bool]:
    return {}


@pytest.fixture
def harness() -> Iterator[Harness]:
    background = CollectingBackground()
    h = Harness(background=background)
    with h.client:
        yield h
    background.close()


def _body(**extra: Any) -> dict[str, Any]:
    return {
        "message": "And in Rust?",
        "model": {"provider": "openai", "model": "balanced"},
        **extra,
    }


def _session(h: Harness) -> str:
    session = make_session(
        [
            jury_turn("How to sort?", [("openai", "openai-model", "Use sorted().")]),
            chat_turn("Why?", "Because it is stable."),
        ]
    )
    return h.store.put(session).id


# --------------------------------------------------------------------------- estimate


def test_estimate(harness: Harness) -> None:
    sid = _session(harness)
    r = harness.client.post(f"/api/chat/estimate?session={sid}", json=_body())
    assert r.status_code == 200
    data = r.json()
    assert data["model"] == "openai-model"
    assert data["cli_calls"] == 0
    assert set(data["context"]) == {"prefix", "summary", "retrieved", "recent", "message"}
    assert data["context_tokens"] == sum(data["context"].values())
    assert data["context_budget_tokens"] == SETTINGS.context_budget_tokens
    assert data["budget"]["refused"] is False


def test_estimate_errors(harness: Harness) -> None:
    sid = _session(harness)
    r = harness.client.post(f"/api/chat/estimate?session={'0' * 32}", json=_body())
    assert r.status_code == 404
    assert r.json()["code"] == "session_not_found"
    r = harness.client.post("/api/chat/estimate?session=NOT-AN-ID", json=_body())
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_id"
    # A blank message is fine for an estimate (the meter shows the context alone).
    r = harness.client.post(f"/api/chat/estimate?session={sid}", json=_body(message=" "))
    assert r.status_code == 200
    r = harness.client.post(
        f"/api/chat/estimate?session={sid}", json=_body(attachment_ids=["a" * 64])
    )
    assert r.status_code == 400
    assert r.json()["code"] == "attachment_not_found"


# --------------------------------------------------------------------------- run


def test_chat_runs_stores_indexes_and_schedules_memory(harness: Harness) -> None:
    sid = _session(harness)
    tid = new_id()
    r = harness.client.post(f"/api/chat?session={sid}&turn={tid}", json=_body())
    assert r.status_code == 200
    turn = ChatTurn.model_validate(r.json())
    assert turn.id == tid
    assert turn.status is TurnStatus.DONE
    assert turn.reply is not None
    assert turn.resolved_model == "openai-model"
    stored = harness.store.get(sid).turns[-1]
    assert stored == turn
    assert harness.tasks.keys == [f"chat:{sid}:{tid}"]
    assert harness.index.search(sid, "Rust", 5)  # the new turn is indexed
    assert harness.background is not None
    assert [key for key, _ in harness.background.started] == [f"memory:{sid}"]
    _, coro = harness.background.started.pop()
    assert asyncio.run(coro) is None  # too little history to summarize


def test_duplicate_and_bad_turn_ids(harness: Harness) -> None:
    sid = _session(harness)
    existing = harness.store.get(sid).turns[0].id
    r = harness.client.post(f"/api/chat?session={sid}&turn={existing}", json=_body())
    assert r.status_code == 409
    assert r.json()["code"] == "turn_exists"
    r = harness.client.post(f"/api/chat?session={sid}&turn=XYZ", json=_body())
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_id"


def test_consent_required_leaves_the_session_unchanged() -> None:
    clis = {p: FakeCliProvider(p) for p in CLI_PROVIDERS}
    h = Harness(make_deps(cli_providers=clis), background=CollectingBackground())
    sid = _session(h)
    before = h.store.get(sid)
    body = _body(model={"provider": "anthropic", "backend": "cli", "model": "anthropic-cli"})
    with h.client:
        r = h.client.post(f"/api/chat?session={sid}&turn={new_id()}", json=body)
        assert r.status_code == 409
        data = r.json()
        assert data["code"] == "consent_required"
        assert data["budget"]["cli_calls"] == 1
        assert h.store.get(sid) == before
        body["allow_unknown_cost"] = True
        r = h.client.post(f"/api/chat?session={sid}&turn={new_id()}", json=body)
    assert r.status_code == 200
    assert r.json()["status"] == "done"
    assert r.json()["cost"] is None


def test_confirmation_required_then_confirmed() -> None:
    settings = AppSettings(budget=BudgetSettings(confirm_above=Decimal(0)))
    background = CollectingBackground()
    h = Harness(settings=settings, background=background)
    sid = _session(h)
    with h.client:
        r = h.client.post(f"/api/chat?session={sid}&turn={new_id()}", json=_body())
        assert r.status_code == 409
        assert r.json()["code"] == "confirmation_required"
        assert len(h.store.get(sid).turns) == 2
        r = h.client.post(f"/api/chat?session={sid}&turn={new_id()}", json=_body(confirmed=True))
    assert r.status_code == 200
    background.close()


def test_budget_refusal() -> None:
    settings = AppSettings(budget=BudgetSettings(run_limit=Decimal("0.001")))
    h = Harness(settings=settings, background=CollectingBackground())
    sid = _session(h)
    with h.client:
        r = h.client.post(f"/api/chat?session={sid}&turn={new_id()}", json=_body())
    assert r.status_code == 409
    data = r.json()
    assert data["code"] == "budget_refused"
    assert data["budget"]["refused"] is True
    assert len(h.store.get(sid).turns) == 2


def test_provider_failure_is_written_as_an_error_turn() -> None:
    ledger = FakeLedger()
    deps = make_deps(
        providers={"openai": raising_provider("openai", ProviderError("vendor down"))},
        ledger=ledger,
    )
    background = CollectingBackground()
    h = Harness(deps, background=background)
    sid = _session(h)
    tid = new_id()
    with h.client:
        r = h.client.post(f"/api/chat?session={sid}&turn={tid}", json=_body())
    assert r.status_code == 200
    assert r.json()["status"] == "error"
    assert r.json()["error"] == "vendor down"
    stored = h.store.get(sid).turns[-1]
    assert stored.id == tid
    assert stored.status is TurnStatus.ERROR
    assert background.started == []  # no summary after a failed turn


def test_default_background_runs_the_memory_update() -> None:
    deps = make_deps(providers={"openai": echo_provider("openai")})
    h = Harness(deps)
    sid = _session(h)
    with h.client:
        r = h.client.post(f"/api/chat?session={sid}&turn={new_id()}", json=_body())
        assert r.status_code == 200
    assert f"memory:{sid}" in h.tasks.keys


# --------------------------------------------------------------------------- memory


def test_memory_endpoint(harness: Harness) -> None:
    memory = MemoryState(summary="So far.", summarized_through=1, facts=("A fact.",))
    session = harness.store.put(make_session(memory=memory))
    r = harness.client.get(f"/api/sessions/{session.id}/memory")
    assert r.status_code == 200
    assert MemoryState.model_validate(r.json()) == memory
    assert harness.client.get(f"/api/sessions/{'0' * 32}/memory").status_code == 404
    assert harness.client.get("/api/sessions/bad/memory").status_code == 400
