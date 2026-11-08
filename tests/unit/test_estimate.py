"""CostEstimator ranges, cache assumptions, multipliers and unpriced lines."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from llm_crossjury.core.cost import (
    CallPlan,
    CostEstimate,
    CostEstimator,
    LedgerEntry,
    PricingError,
    PricingTable,
    SqliteLedger,
    estimate_tokens,
)
from llm_crossjury.core.providers.base import Backend, ReasoningEffort
from llm_crossjury.core.registry import (
    ModelResolver,
    ReasoningControlType,
    ResolutionMode,
    Role,
)

from ._factories import entry, price, registry, usage

M = 1_000_000
GLOBAL = {
    ReasoningEffort.LOW: 0.5,
    ReasoningEffort.MEDIUM: 1.0,
    ReasoningEffort.HIGH: 2.0,
    ReasoningEffort.DEFAULT: 1.0,
}


@pytest.fixture
def pricing() -> PricingTable:
    return PricingTable(
        registry(
            entry("acme-a", pricing=price(inp="1", out="4", cached="0.1", write="2")),
            entry("acme-unverified", pricing=price(), verified=False),
            entry("acme-over", pricing=price(), multipliers={ReasoningEffort.HIGH: 3.0}),
            entry("acme-flat", pricing=price(), control=ReasoningControlType.NONE),
        )
    )


def _call(
    model: str = "acme-a",
    *,
    backend: Backend = Backend.API,
    fp: str = "fp",
    stage: str = "answer",
    reasoning: ReasoningEffort = ReasoningEffort.DEFAULT,
    prefix: int = M,
    suffix: int = 0,
    low: int = M // 10,
    high: int = M // 5,
) -> CallPlan:
    return CallPlan(
        stage=stage,
        provider="acme",
        model=model,
        backend=backend,
        reasoning=reasoning,
        prefix_fingerprint=fp,
        prefix_tokens=prefix,
        suffix_tokens=suffix,
        expected_output_tokens_low=low,
        expected_output_tokens_high=high,
        batched=False,
    )


def test_range_with_shared_prefix_and_unpriced_lines(pricing: PricingTable) -> None:
    estimator = CostEstimator(pricing, 0.5, GLOBAL, currency="USD")
    plan = [
        _call(),
        _call(),
        _call("acme-cli-only", backend=Backend.CLI),
        _call("acme-unverified"),
        _call("acme-mock", backend=Backend.MOCK),
    ]
    est = estimator.estimate(plan, [])
    api, cli, unverified, mock = est.lines
    # call 1 writes the prefix: 2 + out 0.4 + reasoning 0.4 (low) / 2 + 0.8 + 0.8 (high)
    # call 2 reads half from cache: 0.05 + 0.5 + 0.8 (low) / 1 + 1.6 (high)
    assert api.calls == 2
    assert api.input_tokens == 2 * M
    assert api.cached_input_tokens == M // 2
    assert api.reasoning_tokens_low == 2 * M // 10
    assert api.cost_low == Decimal("4.15")
    assert api.cost_high == Decimal("6.2")
    assert cli.cost_low is None
    assert cli.cost_high is None
    assert unverified.cost_low is None
    assert mock.cost_low == Decimal(0)
    assert est.unpriced_calls == 2
    assert est.total_low == Decimal("4.15")
    assert est.total_high == Decimal("6.2")
    assert est.currency == "USD"
    assert any("estimate_tokens" in a for a in est.assumptions)
    assert any("2 call(s) are unpriced" in a for a in est.assumptions)
    assert est.exceeds(Decimal("6.19"))
    assert not est.exceeds(Decimal("6.2"))


def test_no_cache_without_sharing(pricing: PricingTable) -> None:
    est = CostEstimator(pricing, 1.0, GLOBAL).estimate(
        [_call(fp="a"), _call(fp="b", stage="review")], []
    )
    assert [ln.cached_input_tokens for ln in est.lines] == [0, 0]
    # 1M input at 1 + 0.4 output + 0.4 reasoning
    assert est.lines[0].cost_low == Decimal("1.8")
    assert est.currency == "USD"


def test_multiplier_precedence(pricing: PricingTable, tmp_path: Path) -> None:
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    ledger.record(
        LedgerEntry(
            run_id="r",
            stage="answer",
            provider="acme",
            model="acme-a",
            backend=Backend.API,
            reasoning=ReasoningEffort.HIGH,
            usage=usage(out=100, reasoning=150),
            cost=Decimal(0),
            currency="USD",
            batched=False,
            duration_s=None,
            recorded_at=datetime(2026, 1, 1),
        )
    )
    estimator = CostEstimator(pricing, 0.0, GLOBAL, ledger=ledger, calibration_min_samples=1)
    high = ReasoningEffort.HIGH
    plan = [
        _call("acme-a", reasoning=high, low=100, high=100),  # ledger: 1.5
        _call("acme-over", reasoning=high, low=100, high=100),  # registry override: 3.0
        _call("acme-over", reasoning=ReasoningEffort.LOW, low=100, high=100),  # global: 0.5
        _call("acme-flat", reasoning=ReasoningEffort.DEFAULT, low=100, high=100),  # none: 0
    ]
    est = estimator.estimate(plan, [])
    assert [ln.reasoning_tokens_high for ln in est.lines] == [150, 300, 50, 0]
    text = "\n".join(est.assumptions)
    assert "calibrated from the ledger" in text
    assert "registry entry override" in text
    assert "configured (uncalibrated)" in text


def test_currency_mismatch_is_refused(pricing: PricingTable) -> None:
    with pytest.raises(PricingError, match="EUR"):
        CostEstimator(pricing, 0.5, GLOBAL, currency="EUR").estimate([_call()], [])


def test_all_unpriced_plan(pricing: PricingTable) -> None:
    est = CostEstimator(pricing, 0.5, GLOBAL).estimate([_call("x", backend=Backend.CLI)], [])
    assert est.total_high == Decimal(0)
    assert est.unpriced_calls == 1
    assert est.currency == ""


def test_invalid_hit_rate(pricing: PricingTable) -> None:
    with pytest.raises(ValueError, match="cache_hit_rate"):
        CostEstimator(pricing, 1.5, GLOBAL)


def test_exceeds_compares_high_end() -> None:
    est = CostEstimate(
        currency="USD",
        resolved=(),
        lines=(),
        total_low=Decimal("0.1"),
        total_high=Decimal("0.3"),
        unpriced_calls=0,
        assumptions=(),
    )
    assert est.exceeds(Decimal("0.25"))
    assert not est.exceeds(Decimal("0.30"))


@pytest.mark.parametrize(
    ("text", "tokens"),
    [("", 0), ("a", 1), ("abc", 1), ("abcd", 2), ("\u65e5\u672c", 2), ("ab\u65e5", 2)],
)
def test_estimate_tokens(text: str, tokens: int) -> None:
    assert estimate_tokens(text) == tokens


def test_resolutions_are_carried(pricing: PricingTable) -> None:
    resolved = ModelResolver(pricing.registry, ResolutionMode.NORMAL).resolve(
        member_id="m1",
        provider="acme",
        model="acme-a",
        backend=Backend.API,
        role=Role.ANSWERER,
        reasoning=ReasoningEffort.HIGH,
        native={"x": {"y": 1}},
    )
    est = CostEstimator(pricing, 0.5, GLOBAL).estimate([_call()], [resolved])
    assert est.resolved == (resolved,)
    assert est.resolved[0].native_params == {"effort": "h", "x": {"y": 1}}
