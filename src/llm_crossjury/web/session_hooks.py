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
    MemberCallback,
    MemberResult,
    Selection,
    SelectionError,
    replace_member,
)
from llm_crossjury.core.cost.pricing import PricingError
from llm_crossjury.core.registry.resolver import ResolutionError
from llm_crossjury.core.sessions.interfaces import (
    CaseFileBuilder,
    SessionNotFoundError,
    SessionStoreLike,
    TaskRunnerLike,
)
from llm_crossjury.core.sessions.models import (
    SYNTHESIS_STAGES,
    JuryTurn,
    Session,
    TurnStatus,
)
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


def member_writer(ctx: AppContext, session_id: str, turn_id: str) -> MemberCallback:
    """A progress hook that stores each finished step-1 member in ``turn.members_done``
    (so a page can show answers before the slowest member is done)."""

    def write(index: int, member: MemberResult) -> None:
        def apply(turn: JuryTurn) -> JuryTurn:
            done = dict(turn.members_done)
            done[index] = member
            return turn.model_copy(update={"members_done": done})

        _update_turn(ctx, session_id, turn_id, apply)

    return write


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
    if _retrying(ctx, found.id, turn):
        return error(409, "turn_busy", "A member answer is being run again for this turn.")
    return None


# --------------------------------------------------------------------------- member retry


def _member_key(session_id: str, turn_id: str, index: int) -> str:
    return f"{session_id}/{turn_id}/member/{index}"


def _retrying(ctx: AppContext, session_id: str, turn: JuryTurn) -> bool:
    """Some member of ``turn`` is being re-run (a stale index after a restart is not)."""
    return any(ctx.tasks.running(_member_key(session_id, turn.id, i)) for i in turn.retrying)


def find_member_target(
    ctx: AppContext, session_id: str | None, turn_id: str | None, index: int
) -> tuple[Session, JuryTurn] | JSONResponse:
    """The session and jury turn of a member retry or its estimate, or the error.

    Errors: the usual session and turn id errors, 404 ``turn_not_found`` for a missing
    or chat turn, 409 ``turn_busy`` while step 1 is running, 409 ``no_answers`` when
    step 1 failed as a whole, and 400 ``invalid_member_index``.
    """
    found = _jury_turn(ctx, session_id, turn_id)
    if isinstance(found, JSONResponse):
        return found
    _, turn = found
    if turn.status is TurnStatus.PENDING:
        return error(409, "turn_busy", "Step 1 is still running for this turn.")
    if turn.step1 is None:
        return error(409, "no_answers", "This turn has no step-1 answers to run again.")
    if not 0 <= index < min(len(turn.selections), len(turn.step1.members)):
        return error(400, "invalid_member_index", "There is no member with this index.")
    return found


def _jury_turn(
    ctx: AppContext, session_id: str | None, turn_id: str | None
) -> tuple[Session, JuryTurn] | JSONResponse:
    found = get_session(ctx, session_id)
    if isinstance(found, JSONResponse):
        return found
    if turn_id is None or not is_session_id(turn_id):
        return invalid_turn_id()
    turn = _find_turn(found, turn_id)
    if turn is None:
        return turn_not_found()
    return found, turn


def check_member_busy(
    ctx: AppContext, session_id: str, turn: JuryTurn, index: int
) -> JSONResponse | None:
    """Refuse a retry while that member is already re-running or step 2 is running."""
    if index in turn.retrying and ctx.tasks.running(_member_key(session_id, turn.id, index)):
        return error(409, "turn_busy", "This member is already running again.")
    if "pending" in turn.synthesis and ctx.tasks.running(_stage_key(session_id, turn.id)):
        return error(409, "turn_busy", "A synthesis step is still running for this turn.")
    return None


def _find_turn(session: Session, turn_id: str) -> JuryTurn | None:
    turn = next((t for t in session.turns if t.id == turn_id), None)
    return turn if isinstance(turn, JuryTurn) else None


async def run_member_retry(
    ctx: AppContext,
    session_id: str,
    turn_id: str,
    index: int,
    work: Coroutine[Any, Any, CompareResult],
) -> tuple[MemberResult, JuryTurn | None]:
    """Mark member ``index`` as retrying, run ``work`` (a one-member compare) detached,
    and store its result in the turn.

    On success the member replaces ``step1.members[index]`` and
    ``members_done[index]``, the step-1 totals are recomputed, and
    ``synthesis_stale`` is set when step 2 has any result. On failure the old member
    is kept. ``index`` leaves ``retrying`` in both cases.

    Returns:
        The new member and the updated turn (``None`` if it was deleted meanwhile).
    """

    def start(turn: JuryTurn) -> JuryTurn:
        retrying = tuple(sorted({*turn.retrying, index}))
        return turn.model_copy(update={"retrying": retrying})

    try:
        ctx.sessions.update(session_id, _replace_turn(turn_id, start))
    except BaseException:
        work.close()
        raise

    def stop(turn: JuryTurn) -> JuryTurn:
        return turn.model_copy(update={"retrying": tuple(i for i in turn.retrying if i != index)})

    def store(member: MemberResult) -> Callable[[JuryTurn], JuryTurn]:
        def apply(turn: JuryTurn) -> JuryTurn:
            turn = stop(turn)
            if turn.step1 is None or not 0 <= index < len(turn.step1.members):
                return turn
            done = dict(turn.members_done)
            done[index] = member
            has_step2 = any(stage in turn.synthesis for stage in SYNTHESIS_STAGES)
            return turn.model_copy(
                update={
                    "step1": replace_member(turn.step1, index, member),
                    "members_done": done,
                    "synthesis_stale": turn.synthesis_stale or has_step2,
                }
            )

        return apply

    async def detached() -> tuple[MemberResult, JuryTurn | None]:
        try:
            result = await work
        except BaseException:
            _update_turn(ctx, session_id, turn_id, stop)
            raise
        (member,) = result.members
        try:
            session = ctx.sessions.update(session_id, _replace_turn(turn_id, store(member)))
        except LookupError:
            return member, None
        return member, _find_turn(session, turn_id)

    outcome: tuple[MemberResult, JuryTurn | None] = await ctx.tasks.run(
        _member_key(session_id, turn_id, index), detached()
    )
    return outcome


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
        return turn.model_copy(update={"synthesis": synthesis, "synthesis_stale": False})

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
    "check_member_busy",
    "check_new_turn",
    "check_stage_target",
    "find_member_target",
    "get_session",
    "member_writer",
    "run_jury_turn",
    "run_member_retry",
    "run_stage",
    "turn_error_message",
    "with_context",
]
