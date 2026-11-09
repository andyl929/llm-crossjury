"""Sessions and app-settings API (ADR 0008, docs/design/sessions-chat-memory.md).

Routes (all JSON):

* ``GET /api/sessions``: ``{"sessions": [SessionSummary]}``, newest first.
* ``POST /api/sessions`` with ``{"title"?: str}``: a new, empty ``Session``.
* ``GET /api/sessions/{id}``: the ``Session`` (404 ``session_not_found``).
* ``POST /api/sessions/{id}/title`` with ``{"title": str}``: the ``SessionSummary``.
* ``POST /api/sessions/{id}/turns/{turn}/synthesis-settings`` with
  ``{"settings": object}``: ``{"ok": true}`` (404 ``turn_not_found`` unless a jury turn).
* ``DELETE /api/sessions/{id}``: ``{"deleted": bool}``.
* ``DELETE /api/sessions``: ``{"deleted": int}`` (every session).
* ``GET /api/settings/app``: ``{"settings": AppSettings, "defaults": AppSettings}``.
* ``PUT /api/settings/app`` with an ``AppSettings`` body: ``{"settings": AppSettings}``.

A malformed session or turn id is a 400 (``invalid_session_id`` / ``invalid_turn_id``).
Every route applies the same request guards as the settings router.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from llm_crossjury.core.app_settings import AppSettingsStore
from llm_crossjury.core.sessions.interfaces import (
    AppSettings,
    SessionNotFoundError,
    SessionStoreLike,
)
from llm_crossjury.core.sessions.models import JuryTurn, Session, summarize
from llm_crossjury.core.sessions.store import is_session_id
from llm_crossjury.web.settings_api import DEFAULT_ALLOWED_HOSTS, check_request

MAX_TITLE_CHARS = 200
MAX_SYNTHESIS_SETTINGS_BYTES = 64 * 1024


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CreateSessionRequest(_Body):
    title: str = Field(default="", max_length=MAX_TITLE_CHARS)


class TitleRequest(_Body):
    title: str = Field(max_length=MAX_TITLE_CHARS)


class SynthesisSettingsRequest(_Body):
    settings: dict[str, Any]
    """Opaque to the server: the page's step-2 settings."""


def error(status: int, code: str, detail: str) -> JSONResponse:
    """A JSON error in the app's usual shape."""
    return JSONResponse(status_code=status, content={"code": code, "detail": detail})


def session_not_found() -> JSONResponse:
    """The 404 for an unknown session."""
    return error(404, "session_not_found", "No such session (it may have been deleted).")


def invalid_session_id() -> JSONResponse:
    """The 400 for a malformed session id."""
    return error(400, "invalid_session_id", "A session id is 32 lowercase hex digits.")


def invalid_turn_id() -> JSONResponse:
    """The 400 for a malformed turn id."""
    return error(400, "invalid_turn_id", "A turn id is 32 lowercase hex digits.")


def turn_not_found() -> JSONResponse:
    """The 404 for an unknown turn (or one of the wrong kind)."""
    return error(404, "turn_not_found", "No such jury turn in this session.")


def _session_json(session: Session) -> JSONResponse:
    return JSONResponse(content=session.model_dump(mode="json"))


def _set_synthesis_settings(turn_id: str, settings: dict[str, Any]) -> Any:
    def apply(session: Session) -> Session:
        turns = list(session.turns)
        for index, turn in enumerate(turns):
            if turn.id == turn_id and isinstance(turn, JuryTurn):
                turns[index] = turn.model_copy(update={"synthesis_settings": settings})
                return session.model_copy(update={"turns": tuple(turns)})
        raise LookupError(turn_id)

    return apply


def create_sessions_router(
    *,
    store: SessionStoreLike,
    settings: AppSettingsStore,
    allowed_hosts: Sequence[str] = DEFAULT_ALLOWED_HOSTS,
) -> APIRouter:
    """Create the sessions and app-settings router.

    Args:
        store: The session store.
        settings: The app settings store; saving notifies its listeners (the app
            switches session persistence there).
        allowed_hosts: Accepted ``Host`` names (without port); ``"*"`` accepts any.
    """
    hosts = frozenset(h.strip("[]").lower() for h in allowed_hosts)
    router = APIRouter()

    @router.get("/api/sessions", response_model=None)
    async def list_sessions(request: Request) -> JSONResponse:
        if (refused := check_request(request, hosts)) is not None:
            return refused
        rows = [r.model_dump(mode="json") for r in store.list()]
        return JSONResponse(content={"sessions": rows})

    @router.post("/api/sessions", response_model=None)
    async def create_session(request: Request, body: CreateSessionRequest) -> JSONResponse:
        if (refused := check_request(request, hosts)) is not None:
            return refused
        return _session_json(store.create(body.title.strip()))

    @router.delete("/api/sessions", response_model=None)
    async def delete_all_sessions(request: Request) -> JSONResponse:
        if (refused := check_request(request, hosts)) is not None:
            return refused
        count = await asyncio.to_thread(store.delete_all)
        return JSONResponse(content={"deleted": count})

    @router.get("/api/sessions/{session_id}", response_model=None)
    async def get_session(session_id: str, request: Request) -> JSONResponse:
        if (refused := check_request(request, hosts)) is not None:
            return refused
        if not is_session_id(session_id):
            return invalid_session_id()
        try:
            return _session_json(store.get(session_id))
        except SessionNotFoundError:
            return session_not_found()

    @router.post("/api/sessions/{session_id}/title", response_model=None)
    async def rename_session(session_id: str, request: Request, body: TitleRequest) -> JSONResponse:
        if (refused := check_request(request, hosts)) is not None:
            return refused
        if not is_session_id(session_id):
            return invalid_session_id()
        title = body.title.strip()
        try:
            session = store.update(session_id, lambda s: s.model_copy(update={"title": title}))
        except SessionNotFoundError:
            return session_not_found()
        return JSONResponse(content=summarize(session).model_dump(mode="json"))

    @router.post(
        "/api/sessions/{session_id}/turns/{turn_id}/synthesis-settings", response_model=None
    )
    async def save_synthesis_settings(
        session_id: str, turn_id: str, request: Request, body: SynthesisSettingsRequest
    ) -> JSONResponse:
        if (refused := check_request(request, hosts)) is not None:
            return refused
        if not (is_session_id(session_id) and is_session_id(turn_id)):
            return invalid_session_id() if not is_session_id(session_id) else invalid_turn_id()
        if len(json.dumps(body.settings)) > MAX_SYNTHESIS_SETTINGS_BYTES:
            return error(400, "too_large", "The synthesis settings are too large.")
        try:
            store.update(session_id, _set_synthesis_settings(turn_id, body.settings))
        except SessionNotFoundError:
            return session_not_found()
        except LookupError:
            return turn_not_found()
        return JSONResponse(content={"ok": True})

    @router.delete("/api/sessions/{session_id}", response_model=None)
    async def delete_session(session_id: str, request: Request) -> JSONResponse:
        if (refused := check_request(request, hosts)) is not None:
            return refused
        if not is_session_id(session_id):
            return invalid_session_id()
        return JSONResponse(content={"deleted": store.delete(session_id)})

    @router.get("/api/settings/app", response_model=None)
    async def get_app_settings(request: Request) -> JSONResponse:
        if (refused := check_request(request, hosts)) is not None:
            return refused
        return JSONResponse(
            content={
                "settings": settings.get().model_dump(mode="json"),
                "defaults": settings.defaults.model_dump(mode="json"),
            }
        )

    @router.put("/api/settings/app", response_model=None)
    async def put_app_settings(request: Request, body: AppSettings) -> JSONResponse:
        if (refused := check_request(request, hosts)) is not None:
            return refused
        try:
            saved = await asyncio.to_thread(settings.save, body)
        except OSError as exc:
            return error(500, "config_dir", f"Cannot save the settings: {exc.strerror}")
        return JSONResponse(content={"settings": saved.model_dump(mode="json")})

    return router


__all__ = [
    "create_sessions_router",
    "error",
    "invalid_session_id",
    "invalid_turn_id",
    "session_not_found",
    "turn_not_found",
]
