"""SqliteLedger: Decimal storage, budgets, reservations, summaries, calibration."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from llm_crossjury.core.cost import (
    Budget,
    BudgetExceededError,
    LedgerEntry,
    LedgerError,
    SqliteLedger,
)
from llm_crossjury.core.providers.base import Backend, ReasoningEffort, Usage

from ._factories import usage

NOW = datetime(2026, 5, 4, 12, 0, 0)


def _entry(
    *,
    run_id: str = "r1",
    stage: str = "answer",
    provider: str = "acme",
    cost: str | None = "0.10",
    currency: str = "USD",
    backend: Backend = Backend.API,
    duration: float | None = None,
    at: datetime = NOW,
    u: Usage | None = None,
    reasoning: ReasoningEffort = ReasoningEffort.DEFAULT,
) -> LedgerEntry:
    return LedgerEntry(
        run_id=run_id,
        stage=stage,
        provider=provider,
        model="acme-model",
        backend=backend,
        reasoning=reasoning,
        usage=u or usage(inp=10, out=5),
        cost=None if cost is None else Decimal(cost),
        currency=currency,
        batched=False,
        duration_s=duration,
        recorded_at=at,
    )


def _budget(run: str = "1.00", daily: str = "5.00") -> Budget:
    return Budget(
        run_limit=Decimal(run), daily_limit=Decimal(daily), currency="USD", allow_unknown_cost=True
    )


@pytest.fixture
def ledger(tmp_path: Path) -> SqliteLedger:
    return SqliteLedger(tmp_path / "data" / "ledger.sqlite3", clock=lambda: NOW)


def test_spent_sums_exact_decimals(ledger: SqliteLedger) -> None:
    for _ in range(3):
        ledger.record(_entry(cost="0.1"))
    ledger.record(_entry(cost=None))
    ledger.record(_entry(run_id="r2", cost="9"))
    assert ledger.spent("r1") == Decimal("0.3")
    assert ledger.spent("unknown") == Decimal(0)


def test_persists_across_instances_as_text(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    SqliteLedger(path).record(_entry(cost="0.000018"))
    assert SqliteLedger(path).spent("r1") == Decimal("0.000018")
    with sqlite3.connect(path) as conn:
        (stored,) = conn.execute("SELECT typeof(cost) FROM calls").fetchone()
    conn.close()
    assert stored == "text"


def test_never_stores_text_columns(ledger: SqliteLedger, tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "data" / "ledger.sqlite3")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
    conn.close()
    assert not columns & {"prompt", "response", "text", "content"}


def test_spent_on_uses_local_day(ledger: SqliteLedger) -> None:
    ledger.record(_entry(at=NOW))
    ledger.record(_entry(at=NOW - timedelta(days=1), cost="2"))
    aware = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    ledger.record(_entry(at=aware, cost="0.05"))
    assert ledger.spent_on(date(2026, 5, 3)) == Decimal(2)
    assert ledger.spent_on(aware.astimezone().date()) >= Decimal("0.05")


def test_mixed_currencies_are_refused(ledger: SqliteLedger) -> None:
    ledger.record(_entry(currency="USD"))
    ledger.record(_entry(currency="EUR", cost=None))  # unknown cost: no amount to mix
    with pytest.raises(LedgerError):
        ledger.record(_entry(currency="EUR"))
    eur = Budget(
        run_limit=Decimal(1), daily_limit=Decimal(5), currency="EUR", allow_unknown_cost=False
    )
    with pytest.raises(LedgerError):
        ledger.check("r1", Decimal(0), eur)


def test_check_run_limit_includes_reservations(ledger: SqliteLedger) -> None:
    ledger.record(_entry(cost="0.50"))
    ledger.check("r1", Decimal("0.50"), _budget())  # exactly at the limit is allowed
    rid = ledger.reserve("r1", Decimal("0.30"))
    with pytest.raises(BudgetExceededError) as info:
        ledger.check("r1", Decimal("0.30"), _budget())
    assert info.value.scope == "run"
    assert info.value.spent == Decimal("0.50")
    assert info.value.reserved == Decimal("0.30")
    ledger.release(rid)
    ledger.release(rid)  # idempotent
    ledger.check("r1", Decimal("0.30"), _budget())


def test_check_daily_limit_spans_runs(ledger: SqliteLedger) -> None:
    ledger.record(_entry(run_id="earlier", cost="4.50"))
    ledger.record(_entry(run_id="yesterday", cost="100", at=NOW - timedelta(days=1)))
    ledger.reserve("other", Decimal("0.40"))
    with pytest.raises(BudgetExceededError) as info:
        ledger.check("r1", Decimal("0.20"), _budget())
    assert info.value.scope == "daily"
    assert info.value.reserved == Decimal("0.40")
    ledger.check("r1", Decimal("0.10"), _budget())


def test_reserve_rejects_negative(ledger: SqliteLedger) -> None:
    with pytest.raises(LedgerError):
        ledger.reserve("r1", Decimal("-1"))


def test_summary(ledger: SqliteLedger) -> None:
    ledger.record(_entry(stage="answer", provider="p1", cost="0.10"))
    ledger.record(_entry(stage="review", provider="p2", cost="0.25"))
    ledger.record(_entry(stage="answer", provider="p2", cost="0.05"))
    ledger.record(
        _entry(stage="answer", provider="p3", cost=None, backend=Backend.CLI, duration=2.5)
    )
    ledger.record(_entry(stage="review", provider="p3", cost=None, backend=Backend.CLI))
    ledger.record(_entry(run_id="r2", cost="7"))
    s = ledger.summary("r1")
    assert s.currency == "USD"
    assert s.total_known == Decimal("0.40")
    assert s.unknown_cost_calls == 2
    assert s.cli_calls == 2
    assert s.cli_duration_s == 2.5
    assert s.by_stage == (("answer", Decimal("0.15")), ("review", Decimal("0.25")))
    assert s.by_provider == (("p1", Decimal("0.10")), ("p2", Decimal("0.30")), ("p3", Decimal(0)))


def test_summary_of_empty_run(ledger: SqliteLedger) -> None:
    s = ledger.summary("none")
    assert s.total_known == Decimal(0)
    assert s.currency == ""


def test_observed_reasoning_ratio(ledger: SqliteLedger) -> None:
    high = ReasoningEffort.HIGH
    ledger.record(_entry(reasoning=high, u=usage(out=100, reasoning=200)))
    ledger.record(_entry(reasoning=high, u=usage(out=100, reasoning=None)))  # incomplete
    ledger.record(_entry(reasoning=high, u=usage(out=0, reasoning=50)))  # no output
    ledger.record(_entry(reasoning=ReasoningEffort.LOW, u=usage(out=10, reasoning=1)))
    assert ledger.observed_reasoning_ratio("acme", "acme-model", high, 2) is None
    ledger.record(_entry(reasoning=high, u=usage(out=50, reasoning=50)))
    assert ledger.observed_reasoning_ratio("acme", "acme-model", high, 2) == pytest.approx(1.5)
    assert ledger.observed_reasoning_ratio("acme", "other", high, 1) is None
