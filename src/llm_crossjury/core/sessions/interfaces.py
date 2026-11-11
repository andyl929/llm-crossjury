"""Interfaces between the session store, the task runner, app settings and the memory
layer (ADR 0008). Shared contract: change it only through ``docs/contract-issues.md``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from llm_crossjury.core.sessions.models import ModelRef, Session, SessionSummary


class SessionNotFoundError(LookupError):
    """No session with that id."""


class SessionStoreLike(Protocol):
    """Sessions by id. Every method is synchronous and safe to call from the event loop.

    ``update`` is the only way to change a stored session: it applies ``fn`` to the
    latest version under a per-session lock, bumps ``updated_at``, saves (atomically,
    owner-only) and returns the new version.
    """

    def create(self, title: str = "") -> Session: ...
    def get(self, session_id: str) -> Session: ...
    def list(self) -> list[SessionSummary]: ...
    def update(self, session_id: str, fn: Callable[[Session], Session]) -> Session: ...
    def delete(self, session_id: str) -> bool: ...
    def delete_all(self) -> int: ...
    def attachment_ids(self) -> frozenset[str]:
        """Every attachment id referenced by a stored session (kept past the TTL)."""
        ...


class TaskRunnerLike(Protocol):
    """Runs work detached from the HTTP request that started it.

    ``run`` schedules ``coro`` as a task the runner keeps a strong reference to, and
    awaits it under ``asyncio.shield``: when the client disconnects (page closed or
    navigated away), the request is cancelled but the task finishes, and its own code
    is responsible for writing the result into the session.
    """

    async def run(self, key: str, coro: Coroutine[Any, Any, Any]) -> Any: ...
    def running(self, key: str) -> bool: ...


class BudgetSettings(BaseModel):
    """User-editable spending limits (Settings page); override ``BudgetConfig``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_limit: Decimal = Field(default=Decimal("5.00"), gt=0)
    confirm_above: Decimal = Field(default=Decimal("1.00"), ge=0)
    daily_limit: Decimal = Field(default=Decimal("15.00"), gt=0)


class AppSettings(BaseModel):
    """Settings stored in ``<user config dir>/llm-crossjury/settings.json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    save_history: bool = True
    """Write sessions to disk. Off: sessions live in memory until the server stops."""
    memory_model: ModelRef | None = None
    """Model that writes memory summaries; ``None`` = automatic (see ``core.memory``)."""
    context_budget_tokens: int = Field(default=120_000, ge=8_000)
    """Upper bound on the context the memory layer builds for one call."""


SettingsGetter = Callable[[], AppSettings]
CaseFileBuilder = Callable[[Session, str], Awaitable[str | None]]
"""Builds the conversation context for a jury question asked mid-session
(``None`` = no context needed, e.g. the first turn)."""


__all__ = [
    "AppSettings",
    "BudgetSettings",
    "CaseFileBuilder",
    "SessionNotFoundError",
    "SessionStoreLike",
    "SettingsGetter",
    "TaskRunnerLike",
]
