"""Session hooks of the run-type endpoints (ADR 0008).

``POST /api/estimate``, ``/api/run`` and ``/api/synthesis/{agreement,finish,
resynthesize}`` accept ``?session=<id>&turn=<turn id>``. The endpoint runs its usual
pre-checks first (a 409 leaves the session unchanged), then records the pending work in
the session and runs it through the task runner, so the result (or the error) lands in
the session even if the page that asked for it is gone.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from llm_crossjury.core.app_settings import AppSettingsStore
from llm_crossjury.core.attachments import AttachmentError
from llm_crossjury.core.compare_service import (
    BudgetRefusedError,
    CompareDeps,
    CompareResult,
    ConsentRequiredError,
    Selection,
    SelectionError,
)
from llm_crossjury.core.cost.pricing import PricingError
from llm_crossjury.core.registry.resolver import ResolutionError
from llm_crossjury.core.sessions.interfaces import (
    CaseFileBuilder,
    SessionNotFoundError,
    SessionStoreLike,
    TaskRunnerLike,
)
from llm_crossjury.core.sessions.models import JuryTurn, Session, TurnStatus
from llm_crossjury.core.sessions.store import INTERRUPTED_ERROR, is_session_id
from llm_crossjury.core.synthesis_service import SynthesisStageError
from llm_crossjury.web.sessions_api import (
    error,
    invalid_session_id,
    invalid_turn_id,
    session_not_found,
    turn_not_found,
)

_SAFE_ERRORS: tuple[type[Exception], ...] = (
    SelectionError,
    ResolutionError,
    PricingError,
    ConsentRequiredError,
    BudgetRefusedError,
    AttachmentError,
    SynthesisStageError,
)
"""Exceptions whose message is written for the user (no secrets, no content)."""


@dataclass
class AppContext:
    """What the app's routers share; available as ``app.state.crossjury``.

    Attributes:
        deps: Returns the compare dependencies with the effective budget
            (``AppSettings.budget``).
        sessions: The session store.
        tasks: Runs work detached from the request.
        settings: The app settings store.
        allowed_hosts: Accepted ``Host`` names (for routers that check requests).
        case_file_builder: Conversation context for a jury question asked
            mid-session (``None`` = no case file). Read on every request, so it may be
            set after the app is created.
    """

    deps: Callable[[], CompareDeps]
    sessions: SessionStoreLike
    tasks: TaskRunnerLike
    settings: AppSettingsStore
    allowed_hosts: Sequence[str] = ()
    case_file_builder: CaseFileBuilder | None = field(default=None)


def turn_error_message(exc: BaseException) -> str:
    """The error text stored in a turn (never an arbitrary exception message)."""
    if isinstance(exc, asyncio.CancelledError):
        return INTERRUPTED_ERROR
    if isinstance(exc, _SAFE_ERRORS):
        return str(exc) or type(exc).__name__
    return f"The request failed ({type(exc).__name__})."


def with_context(task: str, case_file: str | None) -> str:
    """The text the members receive: the task, after the case file if there is one."""
    if not case_file:
        return task
    return f"<conversation_context>\n{case_file}\n</conversation_context>\n\n{task}"


def _now() -> datetime:
    return datetime.now(UTC)


def get_session(ctx: AppContext, session_id: str | None) -> Session | JSONResponse:
    """The session named by the ``session`` query parameter, or the error response."""
    if session_id is None or not is_session_id(session_id):
        return invalid_session_id()
    try:
        return ctx.sessions.get(session_id)
    except SessionNotFoundError:
        return session_not_found()


async def case_file_for(ctx: AppContext, session: Session, task: str) -> str | None:
    """The case file for a jury question in ``session`` (``None`` for a first turn)."""
    builder = ctx.case_file_builder
    if builder is None or not session.turns:
        return None
    return await builder(session, task)


def check_new_turn(session: Session, turn_id: str | None) -> JSONResponse | None:
    """Refuse a malformed or already used turn id."""
    if turn_id is None or not is_session_id(turn_id):
        return invalid_turn_id()
    if any(t.id == turn_id for t in session.turns):
        return error(409, "turn_exists", "This turn id is already used in the session.")
    return None


def _replace_turn(turn_id: str, fn: Callable[[JuryTurn], JuryTurn]) -> Callable[[Session], Session]:
    def apply(session: Session) -> Session:
        turns = list(session.turns)
        for index, turn in enumerate(turns):
            if turn.id == turn_id and isinstance(turn, JuryTurn):
                turns[index] = fn(turn)
                return session.model_copy(update={"turns": tuple(turns)})
        raise LookupError(turn_id)

    return apply


def _update_turn(
    ctx: AppContext, session_id: str, turn_id: str, fn: Callable[[JuryTurn], JuryTurn]
) -> None:
    """Change a jury turn; a session or turn deleted meanwhile is not an error."""
    try:
        ctx.sessions.update(session_id, _replace_turn(turn_id, fn))
    except LookupError:  # includes SessionNotFoundError
        return


async def run_jury_turn(
    ctx: AppContext,
    session_id: str,
    turn_id: str,
    *,
    task: str,
    case_file: str | None,
    selections: tuple[Selection, ...],
    attachment_ids: tuple[str, ...],
    work: Coroutine[Any, Any, CompareResult],
) -> CompareResult:
    """Record a pending jury turn, run step 1 detached, and store its outcome."""

    def append(session: Session) -> Session:
        turn = JuryTurn(
            id=turn_id,
            created_at=_now(),
            task=task,
            case_file=case_file,
            selections=selections,
            attachment_ids=attachment_ids,
        )
        return session.model_copy(update={"turns": (*session.turns, turn)})

    try:
        ctx.sessions.update(session_id, append)
    except BaseException:
        work.close()
        raise

    async def detached() -> CompareResult:
        try:
            result = await work
        except BaseException as exc:
            message = turn_error_message(exc)
            _update_turn(
                ctx,
                session_id,
                turn_id,
                lambda t: t.model_copy(update={"status": TurnStatus.ERROR, "error": message}),
            )
            raise
        _update_turn(
            ctx,
            session_id,
            turn_id,
            lambda t: t.model_copy(update={"status": TurnStatus.DONE, "step1": result}),
        )
        return result

    result: CompareResult = await ctx.tasks.run(f"{session_id}/{turn_id}", detached())
    return result


def _stage_key(session_id: str, turn_id: str) -> str:
    return f"{session_id}/{turn_id}/synthesis"


def check_stage_target(
    ctx: AppContext, session_id: str | None, turn_id: str | None
) -> JSONResponse | None:
    """Refuse a step-2 request for an unknown or busy jury turn."""
    found = get_session(ctx, session_id)
    if isinstance(found, JSONResponse):
        return found
    if turn_id is None or not is_session_id(turn_id):
        return invalid_turn_id()
    turn = next((t for t in found.turns if t.id == turn_id), None)
    if not isinstance(turn, JuryTurn):
        return turn_not_found()
    if "pending" in turn.synthesis and ctx.tasks.running(_stage_key(found.id, turn_id)):
        return error(409, "turn_busy", "A synthesis step is still running for this turn.")
    return None


async def run_stage(
    ctx: AppContext,
    session_id: str,
    turn_id: str,
    stage: str,
    work: Coroutine[Any, Any, BaseModel],
) -> Any:
    """Mark ``stage`` pending on the turn, run it detached, and store its outcome."""

    def start(turn: JuryTurn) -> JuryTurn:
        synthesis = dict(turn.synthesis)
        synthesis["pending"] = stage
        synthesis.pop(f"{stage}_error", None)
        return turn.model_copy(update={"synthesis": synthesis})

    try:
        ctx.sessions.update(session_id, _replace_turn(turn_id, start))
    except BaseException:
        work.close()
        raise

    def finish(key: str, value: Any) -> Callable[[JuryTurn], JuryTurn]:
        def apply(turn: JuryTurn) -> JuryTurn:
            synthesis = dict(turn.synthesis)
            synthesis.pop("pending", None)
            synthesis[key] = value
            return turn.model_copy(update={"synthesis": synthesis})

        return apply

    async def detached() -> BaseModel:
        try:
            result = await work
        except BaseException as exc:
            _update_turn(
                ctx, session_id, turn_id, finish(f"{stage}_error", turn_error_message(exc))
            )
            raise
        _update_turn(ctx, session_id, turn_id, finish(stage, result.model_dump(mode="json")))
        return result

    return await ctx.tasks.run(_stage_key(session_id, turn_id), detached())


__all__ = [
    "AppContext",
    "case_file_for",
    "check_new_turn",
    "check_stage_target",
    "get_session",
    "run_jury_turn",
    "run_stage",
    "turn_error_message",
    "with_context",
]
