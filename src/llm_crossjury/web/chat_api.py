"""Chat endpoints (ADR 0008): talk to one model inside a session.

* ``POST /api/chat/estimate?session=S``: :class:`~llm_crossjury.core.chat_service.ChatEstimate`
  (cost range, budget verdict, CLI calls, context breakdown).
* ``POST /api/chat?session=S&turn=T``: pre-checks first (consent, budget refusal,
  confirmation: a 409 leaves the session unchanged), then the pending
  :class:`ChatTurn` is appended, the call runs as a task detached from the request,
  and the reply (or the error) is written into the turn whether or not the page is
  still open. Returns the finished turn. Afterwards the search index is updated and
  the memory summary is refreshed in the background.
* ``GET /api/sessions/{id}/memory``: the session's :class:`MemoryState`.

Errors use the JSON shape of :mod:`llm_crossjury.web.app` (``code``, ``detail``,
``budget``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from llm_crossjury.core.attachments import AttachmentError, AttachmentTooLargeError
from llm_crossjury.core.attachments.store import AttachmentNotFoundError
from llm_crossjury.core.chat_service import (
    ChatEstimate,
    ChatRequest,
    CliStatusFn,
    PreparedChat,
    chat_estimate,
    deps_with_settings,
    prepare_chat,
    run_chat,
    update_session_memory,
)
from llm_crossjury.core.compare_service import (
    BudgetRefusedError,
    BudgetStatus,
    CompareDeps,
    CompareError,
    ConsentRequiredError,
    SelectionError,
)
from llm_crossjury.core.cost.pricing import PricingError
from llm_crossjury.core.memory.index import MemoryIndex, open_index
from llm_crossjury.core.providers.base import Backend
from llm_crossjury.core.registry.resolver import ResolutionError
from llm_crossjury.core.sessions.interfaces import (
    AppSettings,
    SessionNotFoundError,
    SessionStoreLike,
    SettingsGetter,
    TaskRunnerLike,
)
from llm_crossjury.core.sessions.models import (
    ChatTurn,
    MemoryState,
    Session,
    TurnStatus,
)

logger = logging.getLogger(__name__)

_ID = re.compile(r"^[0-9a-f]{32}$")

CLI_STATUS_TTL_S = 60.0
CLI_STATUS_TIMEOUT_S = 15.0

BackgroundFn = Callable[[str, Coroutine[Any, Any, Any]], None]
"""Starts ``coro`` under the task key ``key`` without waiting for it."""


# --------------------------------------------------------------------------- errors


def _error(status: int, code: str, detail: str, budget: BudgetStatus | None = None) -> JSONResponse:
    """The error JSON of ``web.app`` (``{"code", "detail", "budget"}``)."""
    content = {
        "code": code,
        "detail": detail,
        "budget": None if budget is None else budget.model_dump(mode="json"),
    }
    return JSONResponse(status_code=status, content=content)


def _consent_error(exc: ConsentRequiredError, status: BudgetStatus | None = None) -> JSONResponse:
    return _error(409, "consent_required", str(exc), status)


def _confirmation_error(status: BudgetStatus) -> JSONResponse:
    return _error(
        409,
        "confirmation_required",
        f"The high estimate ({status.currency} {status.estimate_high}) exceeds the "
        f"confirmation threshold ({status.currency} {status.confirm_above}).",
        status,
    )


def _service_error(exc: Exception) -> JSONResponse | None:  # noqa: PLR0911 - one per type
    """Map a service refusal to the response ``web.app`` gives it (``None`` = not one)."""
    if isinstance(exc, ConsentRequiredError):
        return _consent_error(exc)
    if isinstance(exc, BudgetRefusedError):
        return _error(409, "budget_refused", str(exc), exc.status)
    if isinstance(exc, SelectionError):
        return _error(400, "invalid_selection", str(exc))
    if isinstance(exc, ResolutionError):
        return _error(400, "resolution", f"Model resolution failed: {exc}")
    if isinstance(exc, PricingError):
        return _error(400, "pricing", f"Pricing refused the run: {exc}")
    if isinstance(exc, AttachmentNotFoundError):
        return _error(400, "attachment_not_found", f"{exc}.")
    if isinstance(exc, AttachmentError):
        code = "attachment_too_large" if isinstance(exc, AttachmentTooLargeError) else "attachment"
        return _error(400, code, f"Attachment refused: {exc}.")
    return None


def _not_found() -> JSONResponse:
    return _error(404, "session_not_found", "No session with that id.")


def _bad_id(what: str) -> JSONResponse:
    return _error(400, "invalid_id", f"The {what} id must be 32 lowercase hex characters.")


def _failure_text(exc: Exception) -> str:
    """Turn error text: service messages are safe; anything else only by type name."""
    if isinstance(exc, (CompareError, AttachmentError, ResolutionError, PricingError)):
        return str(exc) or type(exc).__name__
    return f"Unexpected error ({type(exc).__name__})."


# --------------------------------------------------------------------------- helpers


class _CliAvailability:
    """``check()`` of every ``cli`` provider, cached briefly (memory model choice)."""

    def __init__(self, deps: Callable[[], CompareDeps]) -> None:
        self._deps = deps
        self._at = 0.0
        self._value: dict[str, bool] = {}
        self._lock = asyncio.Lock()

    async def __call__(self) -> dict[str, bool]:
        async with self._lock:
            if self._value and time.monotonic() - self._at < CLI_STATUS_TTL_S:
                return dict(self._value)
            d = self._deps()
            names = [name for name, backend in d.providers if backend is Backend.CLI]

            async def one(name: str) -> bool:
                try:
                    status = await asyncio.wait_for(
                        d.providers[(name, Backend.CLI)].check(), timeout=CLI_STATUS_TIMEOUT_S
                    )
                except Exception:  # unavailable, whatever the reason
                    return False
                return status.available

            results = await asyncio.gather(*(one(n) for n in names))
            self._value = dict(zip(names, results, strict=True))
            self._at = time.monotonic()
            return dict(self._value)


def _turn_id_problem(session: Session, turn: str) -> JSONResponse | None:
    if not _ID.fullmatch(turn):
        return _bad_id("turn")
    if any(t.id == turn for t in session.turns):
        return _error(409, "turn_exists", "The session already has a turn with that id.")
    return None


def _replace_turn(session: Session, turn: ChatTurn) -> Session:
    turns = tuple(turn if t.id == turn.id else t for t in session.turns)
    return session.model_copy(update={"turns": turns})


def _append_turn(turn: ChatTurn) -> Callable[[Session], Session]:
    def apply(session: Session) -> Session:
        return session.model_copy(update={"turns": (*session.turns, turn)})

    return apply


# --------------------------------------------------------------------------- router


def create_chat_router(  # noqa: PLR0915 - the routes share the closure state
    deps: Callable[[], CompareDeps],
    sessions: SessionStoreLike,
    tasks: TaskRunnerLike,
    settings: SettingsGetter,
    index_path: Path | None,
    *,
    index: MemoryIndex | None = None,
    cli_status: CliStatusFn | None = None,
    background: BackgroundFn | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    """Create the chat router.

    Args:
        deps: Compare dependencies (called per request; the budget limits of the app
            settings are applied on top).
        sessions: The session store.
        tasks: Runs chat calls and memory updates detached from the request.
        settings: Current app settings (budget, context budget, memory model).
        index_path: The search index file (``<data dir>/sessions/index.sqlite3``);
            ``None`` keeps it in memory. Shared per path with
            :func:`~llm_crossjury.core.memory.index.open_index`, so pass the same path
            to :func:`~llm_crossjury.core.memory.case_file.make_case_file_builder`
            via ``open_index(path)``.
        index: Explicit index (overrides ``index_path``; tests).
        cli_status: Provider id -> signed-in CLI, for the memory model choice
            (default: each CLI provider's ``check()``, cached for a minute).
        background: Starts the memory update without waiting (default: a task run
            through ``tasks`` under the key ``memory:<session id>``).
        clock: Clock for ``created_at``.
    """
    router = APIRouter()
    memory_index = index if index is not None else open_index(index_path)
    cli_available: CliStatusFn = cli_status or _CliAvailability(deps)
    running: set[asyncio.Future[Any]] = set()

    def start_background(key: str, coro: Coroutine[Any, Any, Any]) -> None:
        if tasks.running(key):
            coro.close()  # an update is already running; the next turn tries again
            return
        future = asyncio.ensure_future(tasks.run(key, coro))
        running.add(future)
        future.add_done_callback(running.discard)

    spawn = background or start_background

    def load(session_id: str) -> Session | JSONResponse:
        if not _ID.fullmatch(session_id):
            return _bad_id("session")
        try:
            return sessions.get(session_id)
        except SessionNotFoundError:
            return _not_found()

    def refresh_memory(session_id: str, d: CompareDeps) -> None:
        spawn(
            f"memory:{session_id}",
            update_session_memory(
                session_id, sessions, d, settings=settings(), cli_status=cli_available
            ),
        )

    @router.post("/api/chat/estimate", response_model=None)
    async def chat_estimate_route(session: str, body: ChatRequest) -> ChatEstimate | JSONResponse:
        found = load(session)
        if isinstance(found, JSONResponse):
            return found
        app_settings = settings()
        d = deps_with_settings(deps(), app_settings)
        try:
            prepared = prepare_chat(found, body, d, settings=app_settings, index=memory_index)
        except Exception as exc:
            response = _service_error(exc)
            if response is None:
                raise
            return response
        return chat_estimate(prepared, d)

    def precheck(
        found: Session, turn: str, body: ChatRequest
    ) -> tuple[CompareDeps, AppSettings, PreparedChat] | JSONResponse:
        """Turn id, consent, budget refusal and confirmation, before anything is written."""
        problem = _turn_id_problem(found, turn)
        if problem is not None:
            return problem
        app_settings = settings()
        d = deps_with_settings(deps(), app_settings)
        try:
            prepared = prepare_chat(found, body, d, settings=app_settings, index=memory_index)
        except Exception as exc:
            response = _service_error(exc)
            if response is None:
                raise
            return response
        status = chat_estimate(prepared, d).budget
        if status.cli_calls and not body.allow_unknown_cost:
            return _consent_error(ConsentRequiredError(status.cli_calls), status)
        if status.refused:
            reason = status.refusal_reason or "Refused by the budget."
            return _error(409, "budget_refused", reason, status)
        if status.needs_confirmation and not body.confirmed:
            return _confirmation_error(status)
        return d, app_settings, prepared

    @router.post("/api/chat", response_model=None)
    async def chat_route(session: str, turn: str, body: ChatRequest) -> ChatTurn | JSONResponse:
        found = load(session)
        if isinstance(found, JSONResponse):
            return found
        checked = precheck(found, turn, body)
        if isinstance(checked, JSONResponse):
            return checked
        d, app_settings, prepared = checked
        pending = ChatTurn(
            id=turn,
            created_at=clock(),
            message=body.message,
            attachment_ids=body.attachment_ids,
            model=body.model,
            max_output_tokens=body.max_output_tokens,
            continued_from=body.continued_from,
            resolved_model=prepared.resolved.model_id,
            context_tokens=prepared.context.breakdown.total,
        )
        try:
            sessions.update(session, _append_turn(pending))
        except SessionNotFoundError:
            return _not_found()

        async def work() -> ChatTurn:
            try:
                current = sessions.get(session)
                done = await run_chat(
                    current, body, d, settings=app_settings, turn=pending, index=memory_index
                )
            except Exception as exc:  # pre-checks changed meanwhile, or a bug
                done = pending.model_copy(
                    update={"status": TurnStatus.ERROR, "error": _failure_text(exc)}
                )
            try:
                stored = sessions.update(session, lambda s: _replace_turn(s, done))
            except SessionNotFoundError:
                return done  # deleted while running: nothing to store
            try:
                await asyncio.to_thread(memory_index.sync_session, stored)
            except Exception as exc:  # the index is a cache; retrieval catches up later
                logger.warning("indexing failed (%s)", type(exc).__name__)
            if done.status is TurnStatus.DONE:
                refresh_memory(session, d)
            return done

        result: ChatTurn = await tasks.run(f"chat:{session}:{turn}", work())
        return result

    @router.get("/api/sessions/{session_id}/memory", response_model=None)
    async def session_memory(session_id: str) -> MemoryState | JSONResponse:
        found = load(session_id)
        if isinstance(found, JSONResponse):
            return found
        return found.memory

    return router


__all__ = ["BackgroundFn", "create_chat_router"]
