"""In-memory fakes of the session store and task runner, plus turn factories.

They implement the Protocols of ``core.sessions.interfaces`` only; the real store and
runner are tested elsewhere.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from llm_crossjury.core.compare_service import CompareResult, MemberResult
from llm_crossjury.core.providers.base import Backend
from llm_crossjury.core.sessions.interfaces import AppSettings, SessionNotFoundError
from llm_crossjury.core.sessions.models import (
    ChatTurn,
    ContinuationRef,
    JuryTurn,
    MemoryState,
    ModelRef,
    Session,
    SessionSummary,
    Turn,
    TurnStatus,
    summarize,
)

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class FakeSessionStore:
    """A dict of sessions; ``update`` applies ``fn`` and bumps ``updated_at``."""

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.updates = 0

    def create(self, title: str = "") -> Session:
        session = Session(id=new_id(), title=title, created_at=NOW, updated_at=NOW)
        self.sessions[session.id] = session
        return session

    def put(self, session: Session) -> Session:
        self.sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session:
        try:
            return self.sessions[session_id]
        except KeyError:
            raise SessionNotFoundError(session_id) from None

    def list(self) -> list[SessionSummary]:
        return [summarize(s) for s in self.sessions.values()]

    def update(self, session_id: str, fn: Callable[[Session], Session]) -> Session:
        updated = fn(self.get(session_id)).model_copy(update={"updated_at": datetime.now(UTC)})
        self.sessions[session_id] = updated
        self.updates += 1
        return updated

    def delete(self, session_id: str) -> bool:
        return self.sessions.pop(session_id, None) is not None

    def delete_all(self) -> int:
        count = len(self.sessions)
        self.sessions.clear()
        return count

    def attachment_ids(self) -> frozenset[str]:
        return frozenset(
            a for s in self.sessions.values() for t in s.turns for a in t.attachment_ids
        )


class FakeTaskRunner:
    """Runs each coroutine as a task and awaits it (like the real runner, unshielded)."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self._running: set[str] = set()

    async def run(self, key: str, coro: Coroutine[Any, Any, Any]) -> Any:
        self.keys.append(key)
        self._running.add(key)
        try:
            return await asyncio.ensure_future(coro)
        finally:
            self._running.discard(key)

    def running(self, key: str) -> bool:
        return key in self._running


class CollectingBackground:
    """A ``BackgroundFn`` that keeps the coroutines so a test can run them."""

    def __init__(self) -> None:
        self.started: list[tuple[str, Coroutine[Any, Any, Any]]] = []

    def __call__(self, key: str, coro: Coroutine[Any, Any, Any]) -> None:
        self.started.append((key, coro))

    def close(self) -> None:
        for _, coro in self.started:
            coro.close()
        self.started.clear()


def settings_getter(settings: AppSettings | None = None) -> Callable[[], AppSettings]:
    value = settings or AppSettings()
    return lambda: value


def chat_turn(
    message: str,
    reply: str | None,
    *,
    provider: str = "openai",
    model: str = "openai-model",
    status: TurnStatus = TurnStatus.DONE,
    continued_from: ContinuationRef | None = None,
    attachment_ids: tuple[str, ...] = (),
    turn_id: str | None = None,
) -> ChatTurn:
    return ChatTurn(
        id=turn_id or new_id(),
        created_at=NOW,
        status=status,
        message=message,
        model=ModelRef(provider=provider, model=model),
        reply=reply,
        resolved_model=model if reply is not None else None,
        continued_from=continued_from,
        attachment_ids=attachment_ids,
    )


def member(provider: str, model: str, text: str | None, error: str | None = None) -> MemberResult:
    return MemberResult(
        provider=provider,
        backend=Backend.API,
        model=model,
        requested_model=model,
        text=text,
        error=error,
        usage=None,
        cost=None,
        currency="USD",
        latency_s=None,
    )


def jury_turn(
    task: str,
    answers: Sequence[tuple[str, str, str]],
    *,
    synthesis: dict[str, Any] | None = None,
    turn_id: str | None = None,
    status: TurnStatus = TurnStatus.DONE,
) -> JuryTurn:
    members = tuple(member(p, m, t) for p, m, t in answers)
    return JuryTurn(
        id=turn_id or new_id(),
        created_at=NOW,
        status=status,
        task=task,
        step1=CompareResult(
            run_id=new_id(),
            prompt_id="compare/answer.v1",
            currency="USD",
            members=members,
            total_cost=Decimal(0),
            unknown_cost_members=0,
        ),
        synthesis=synthesis or {},
    )


def finish_result(final_text: str | None, consensus: Sequence[str] = ()) -> dict[str, Any]:
    """The stored JSON shape of a ``finish``/``resynthesize`` result (the parts used)."""
    return {
        "run_id": new_id(),
        "chair": {
            "synthesis": {
                "consensus": [{"statement": s, "supporting": []} for s in consensus],
                "disputed": [],
                "unique": [],
                "final_text": final_text,
            },
            "additional": {"items": []},
        },
    }


def make_session(
    turns: Sequence[Turn] = (), memory: MemoryState | None = None, session_id: str | None = None
) -> Session:
    return Session(
        id=session_id or new_id(),
        created_at=NOW,
        updated_at=NOW,
        turns=tuple(turns),
        memory=memory or MemoryState(),
    )
