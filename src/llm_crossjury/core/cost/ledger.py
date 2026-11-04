"""Run-time cost accounting.

Every completed call is recorded *before* its result is used. The budget check runs
before each call against ``spent + reserved``, where ``reserved`` is the high
estimate of calls already in flight; a call that could push the run (or the day)
over budget is not started (see ADR 0003). ``cli``-backend calls have unknown cost
but are still recorded with their count and wall-clock duration.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from llm_crossjury.core.cost.pricing import PriceEntry
from llm_crossjury.core.providers.base import Backend, Usage


class LedgerEntry(BaseModel):
    """One completed (or failed but billed) call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    stage: str
    provider: str
    model: str
    backend: Backend
    usage: Usage
    cost: Decimal | None
    """``None`` when unknown (unknown usage fields, cli backend, unpriced model)."""
    currency: str
    batched: bool
    duration_s: float | None
    """Wall-clock duration of the call (always recorded for the cli backend)."""
    recorded_at: datetime


class Budget(BaseModel):
    """Spending limits in effect for a run (resolved from BudgetConfig)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_limit: Decimal
    daily_limit: Decimal
    currency: str
    allow_unknown_cost: bool
    """If ``False``, any call whose cost cannot be computed aborts the run."""


class BudgetExceededError(Exception):
    """Raised before a call that would exceed the budget. Carries no secrets."""

    def __init__(self, scope: str, spent: Decimal, reserved: Decimal, limit: Decimal) -> None:
        super().__init__(
            f"{scope} budget exceeded: spent={spent} reserved={reserved} limit={limit}"
        )
        self.scope = scope
        """``"run"`` or ``"daily"``."""
        self.spent = spent
        self.reserved = reserved
        self.limit = limit


class RunCostSummary(BaseModel):
    """Aggregates for ``llm-crossjury cost report``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    currency: str
    total_known: Decimal
    unknown_cost_calls: int
    cli_calls: int
    cli_duration_s: float
    by_stage: tuple[tuple[str, Decimal], ...]
    by_provider: tuple[tuple[str, Decimal], ...]


class Ledger(Protocol):
    """Append-only record of spend."""

    def record(self, entry: LedgerEntry) -> None: ...

    def spent(self, run_id: str) -> Decimal: ...

    def spent_on(self, day: date) -> Decimal:
        """Known spend across all runs on a local calendar day."""
        ...

    def reserve(self, run_id: str, amount: Decimal) -> str:
        """Reserve ``amount`` for an in-flight call; returns a reservation id."""
        ...

    def release(self, reservation_id: str) -> None: ...

    def check(self, run_id: str, next_call_high: Decimal, budget: Budget) -> None:
        """Raise :class:`BudgetExceededError` if ``spent + reserved + next_call_high``
        would exceed ``budget.run_limit`` for the run or ``budget.daily_limit`` for
        today."""
        ...

    def summary(self, run_id: str) -> RunCostSummary: ...


class SqliteLedger:
    """:class:`Ledger` stored in SQLite under the user data directory.

    Never stores prompt or response text, regardless of ``store_content``.
    """

    def __init__(self, path: Path) -> None:
        raise NotImplementedError

    def record(self, entry: LedgerEntry) -> None:
        raise NotImplementedError

    def spent(self, run_id: str) -> Decimal:
        raise NotImplementedError

    def spent_on(self, day: date) -> Decimal:
        raise NotImplementedError

    def reserve(self, run_id: str, amount: Decimal) -> str:
        raise NotImplementedError

    def release(self, reservation_id: str) -> None:
        raise NotImplementedError

    def check(self, run_id: str, next_call_high: Decimal, budget: Budget) -> None:
        raise NotImplementedError

    def summary(self, run_id: str) -> RunCostSummary:
        raise NotImplementedError


def compute_cost(usage: Usage, price: PriceEntry, *, batched: bool) -> Decimal | None:
    """Compute the cost of ``usage`` under ``price``.

    Returns ``None`` if any usage field needed for the computation is unknown.
    """
    raise NotImplementedError
