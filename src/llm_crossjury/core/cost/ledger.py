"""Run-time cost accounting.

Every completed call is recorded *before* its result is used. The budget check runs
before each call against ``spent + reserved``, where ``reserved`` is the high
estimate of calls already in flight; a call that could push the run (or the day)
over budget is not started (see ADR 0003). ``cli``-backend calls have unknown cost
but are still recorded with their count and wall-clock duration.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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


class LedgerError(Exception):
    """The ledger cannot honor a request (e.g. mixed currencies, negative amounts)."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    backend TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    cache_write_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    cost TEXT,
    currency TEXT NOT NULL,
    batched INTEGER NOT NULL,
    duration_s REAL,
    recorded_at TEXT NOT NULL,
    recorded_day TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_run ON calls (run_id);
CREATE INDEX IF NOT EXISTS calls_day ON calls (recorded_day);
CREATE INDEX IF NOT EXISTS calls_model ON calls (provider, model, reasoning);
"""
_SCHEMA_VERSION = 1


def _local_day(moment: datetime) -> date:
    """Local calendar day of ``moment`` (naive datetimes are taken as local time)."""
    return (moment.astimezone() if moment.tzinfo is not None else moment).date()


@dataclass(frozen=True)
class _Reservation:
    run_id: str
    amount: Decimal


class SqliteLedger:
    """:class:`Ledger` stored in SQLite under the user data directory.

    Never stores prompt or response text, regardless of ``store_content``: the table
    holds only the :class:`LedgerEntry` fields (ids, token counts, cost, duration).
    Costs are stored as ``TEXT`` and summed as :class:`~decimal.Decimal`. Known costs
    in one database must share a currency; recording another raises
    :class:`LedgerError`.

    Reservations for in-flight calls are held in memory by this instance (they belong
    to the running process and vanish with it). Every operation opens its own
    short-lived connection, so the instance may be used from worker threads.
    """

    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        """Open (and create if needed) the ledger database.

        Args:
            path: Database file, usually ``DataPaths.ledger``.
            clock: Returns the current time; used for "today" in :meth:`check`.
                Defaults to the local wall clock.
        """
        self._path = path
        self._clock = clock or datetime.now
        self._reservations: dict[str, _Reservation] = {}
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        try:
            with conn:  # commit on success, roll back on error
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _known_currencies(conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute("SELECT DISTINCT currency FROM calls WHERE cost IS NOT NULL")
        return {str(r[0]) for r in rows}

    def _costs(self, column: str, value: str) -> Decimal:
        # ``column`` is one of two fixed names chosen by this class, never user input.
        query = f"SELECT cost FROM calls WHERE cost IS NOT NULL AND {column} = ?"  # noqa: S608
        with self._connect() as conn:
            rows = conn.execute(query, (value,)).fetchall()
        return sum((Decimal(r[0]) for r in rows), Decimal(0))

    def record(self, entry: LedgerEntry) -> None:
        """Append ``entry``.

        Raises:
            LedgerError: if ``entry`` has a known cost in a different currency from the
                known costs already recorded.
        """
        u = entry.usage
        with self._connect() as conn:
            if entry.cost is not None:
                others = self._known_currencies(conn) - {entry.currency}
                if others:
                    raise LedgerError(
                        f"ledger holds costs in {sorted(others)}; refusing {entry.currency}"
                    )
            conn.execute(
                "INSERT INTO calls (run_id, stage, provider, model, backend, reasoning,"
                " input_tokens, cached_input_tokens, cache_write_tokens, output_tokens,"
                " reasoning_tokens, cost, currency, batched, duration_s, recorded_at,"
                " recorded_day) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.run_id,
                    entry.stage,
                    entry.provider,
                    entry.model,
                    entry.backend.value,
                    entry.reasoning.value,
                    u.input_tokens,
                    u.cached_input_tokens,
                    u.cache_write_tokens,
                    u.output_tokens,
                    u.reasoning_tokens,
                    None if entry.cost is None else str(entry.cost),
                    entry.currency,
                    int(entry.batched),
                    entry.duration_s,
                    entry.recorded_at.isoformat(),
                    _local_day(entry.recorded_at).isoformat(),
                ),
            )

    def spent(self, run_id: str) -> Decimal:
        """Known spend of ``run_id`` (unknown costs are not included)."""
        return self._costs("run_id", run_id)

    def spent_on(self, day: date) -> Decimal:
        """Known spend across all runs on a local calendar day."""
        return self._costs("recorded_day", day.isoformat())

    def reserve(self, run_id: str, amount: Decimal) -> str:
        """Reserve ``amount`` for an in-flight call; returns a reservation id.

        Raises:
            LedgerError: if ``amount`` is negative or not finite.
        """
        if not amount.is_finite() or amount < 0:
            raise LedgerError("a reservation must be a finite, non-negative amount")
        reservation_id = uuid.uuid4().hex
        with self._lock:
            self._reservations[reservation_id] = _Reservation(run_id, amount)
        return reservation_id

    def release(self, reservation_id: str) -> None:
        """Release a reservation. Releasing an unknown or released id is a no-op."""
        with self._lock:
            self._reservations.pop(reservation_id, None)

    def reserved(self, run_id: str | None = None) -> Decimal:
        """Sum of open reservations, for ``run_id`` or (``None``) for all runs."""
        with self._lock:
            amounts = [
                r.amount
                for r in self._reservations.values()
                if run_id is None or r.run_id == run_id
            ]
        return sum(amounts, Decimal(0))

    def check(self, run_id: str, next_call_high: Decimal, budget: Budget) -> None:
        """Raise :class:`BudgetExceededError` if ``spent + reserved + next_call_high``
        would exceed ``budget.run_limit`` for the run or ``budget.daily_limit`` for
        today. The run is checked first; reservations of all runs count for the day.

        Raises:
            BudgetExceededError: if either limit would be exceeded.
            LedgerError: if the ledger's known costs are in another currency.
        """
        with self._connect() as conn:
            others = self._known_currencies(conn) - {budget.currency}
        if others:
            raise LedgerError(
                f"budget is in {budget.currency} but the ledger holds {sorted(others)}"
            )
        spent = self.spent(run_id)
        reserved = self.reserved(run_id)
        if spent + reserved + next_call_high > budget.run_limit:
            raise BudgetExceededError("run", spent, reserved, budget.run_limit)
        spent_today = self.spent_on(_local_day(self._clock()))
        reserved_all = self.reserved()
        if spent_today + reserved_all + next_call_high > budget.daily_limit:
            raise BudgetExceededError("daily", spent_today, reserved_all, budget.daily_limit)

    def summary(self, run_id: str) -> RunCostSummary:
        """Aggregate one run.

        ``currency`` is that of the run's known costs, else of any recorded call, else
        ``""`` for a run with no calls. ``by_stage``/``by_provider`` list known spend
        in first-recorded order.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT stage, provider, backend, cost, currency, duration_s FROM calls"
                " WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        total = Decimal(0)
        unknown = cli_calls = 0
        cli_duration = 0.0
        by_stage: dict[str, Decimal] = {}
        by_provider: dict[str, Decimal] = {}
        currency = ""
        for stage, provider, backend, cost, row_currency, duration in rows:
            by_stage.setdefault(stage, Decimal(0))
            by_provider.setdefault(provider, Decimal(0))
            if backend == Backend.CLI.value:
                cli_calls += 1
                cli_duration += duration or 0.0
            if cost is None:
                unknown += 1
                currency = currency or row_currency
                continue
            amount = Decimal(cost)
            currency = row_currency
            total += amount
            by_stage[stage] += amount
            by_provider[provider] += amount
        return RunCostSummary(
            run_id=run_id,
            currency=currency,
            total_known=total,
            unknown_cost_calls=unknown,
            cli_calls=cli_calls,
            cli_duration_s=cli_duration,
            by_stage=tuple(by_stage.items()),
            by_provider=tuple(by_provider.items()),
        )

    def observed_reasoning_ratio(
        self, provider: str, model_id: str, level: ReasoningEffort, min_samples: int
    ) -> float | None:
        """Mean ``reasoning_tokens / output_tokens`` over recorded calls, or ``None``
        with fewer than ``min_samples`` complete observations (both counts known and
        ``output_tokens > 0``)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT reasoning_tokens, output_tokens FROM calls"
                " WHERE provider = ? AND model = ? AND reasoning = ?"
                " AND reasoning_tokens IS NOT NULL AND output_tokens > 0",
                (provider, model_id, level.value),
            ).fetchall()
        if not rows or len(rows) < min_samples:
            return None
        return float(sum(r / o for r, o in rows) / len(rows))


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
