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

from llm_crossjury.core.providers.base import Backend, ReasoningEffort, Usage
from llm_crossjury.core.registry.schema import Pricing


class LedgerEntry(BaseModel):
    """One completed (or failed but billed) call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    stage: str
    provider: str
    model: str
    backend: Backend
    reasoning: ReasoningEffort
    """Effective level; used to calibrate reasoning-token multipliers."""
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

    def observed_reasoning_ratio(
        self, provider: str, model_id: str, level: ReasoningEffort, min_samples: int
    ) -> float | None:
        """Mean ``reasoning_tokens / output_tokens`` over recorded calls, or ``None``
        with fewer than ``min_samples`` complete observations."""
        ...


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

    def observed_reasoning_ratio(
        self, provider: str, model_id: str, level: ReasoningEffort, min_samples: int
    ) -> float | None:
        raise NotImplementedError


def compute_cost(usage: Usage, price: Pricing, *, batched: bool) -> Decimal | None:
    """Compute the cost of ``usage`` under ``price``.

    Returns ``None`` if any usage field needed for the computation is unknown.

    All five usage fields carry a rate (with the documented fallbacks to the input or
    output rate), so all five are needed; ``None`` never means zero. A ``batched``
    call on a model without ``batch_multiplier`` also returns ``None``. The result is
    in ``price.currency`` and is not rounded.

    Args:
        usage: Token counts reported for the call.
        price: Verified price, e.g. from :meth:`PricingTable.lookup`.
        batched: Whether the call went through the vendor's batch API.
    """
    counts = (
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.cache_write_tokens,
        usage.output_tokens,
        usage.reasoning_tokens,
    )
    if any(c is None for c in counts):
        return None
    if batched and price.batch_multiplier is None:
        return None
    inp, cached, written, out, reasoning = (int(c or 0) for c in counts)
    total = (
        inp * price.input_per_mtok
        + cached * _or(price.cached_input_per_mtok, price.input_per_mtok)
        + written * _or(price.cache_write_per_mtok, price.input_per_mtok)
        + out * price.output_per_mtok
        + reasoning * _or(price.reasoning_per_mtok, price.output_per_mtok)
    ) / _MTOK
    if batched and price.batch_multiplier is not None:
        total *= price.batch_multiplier
    return total


_MTOK = Decimal(1_000_000)


def _or(rate: Decimal | None, fallback: Decimal) -> Decimal:
    return fallback if rate is None else rate
