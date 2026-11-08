"""PricingTable lookups (verified entries only) and compute_cost arithmetic."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from llm_crossjury.core.cost import PricingError, PricingTable
from llm_crossjury.core.cost.ledger import compute_cost
from llm_crossjury.core.providers.base import Backend

from ._factories import entry, price, registry, usage


@pytest.fixture
def table() -> PricingTable:
    return PricingTable(
        registry(
            entry("acme-priced", pricing=price()),
            entry("acme-unverified", pricing=price(), verified=False),
            entry("acme-unpriced"),
            entry("acme-cli", pricing=price(), backends=(Backend.CLI,)),
            entry("acme-bad", pricing=price(inp="-1")),
            entry("acme-eur", pricing=price(currency="eur")),
        )
    )


def test_lookup_returns_verified_price(table: PricingTable) -> None:
    assert table.lookup("acme", "acme-priced").input_per_mtok == Decimal(1)


@pytest.mark.parametrize(
    ("model_id", "reason"),
    [
        ("acme-missing", "not in the model registry"),
        ("acme-unverified", "unverified"),
        ("acme-unpriced", "no pricing"),
        ("acme-cli", "api backend"),
        ("acme-bad", "non-negative"),
        ("acme-eur", "ISO 4217"),
    ],
)
def test_lookup_refuses_without_fallback(table: PricingTable, model_id: str, reason: str) -> None:
    with pytest.raises(PricingError, match=reason):
        table.lookup("acme", model_id)


def test_stale_entries() -> None:
    table = PricingTable(
        registry(
            entry("old", pricing=price(), last_verified=date(2026, 1, 1)),
            entry("fresh", pricing=price(), last_verified=date(2026, 3, 1)),
            entry("unpriced-old", last_verified=date(2025, 1, 1)),
            entry("unverified", pricing=price(), verified=False),
        )
    )
    stale = table.stale_entries(date(2026, 3, 2), max_age_days=30)
    assert [e.model_id for e in stale] == ["old"]
    assert table.stale_entries(date(2026, 1, 31), max_age_days=30) == []


def test_compute_cost_all_rates() -> None:
    p = price(inp="2", out="10", cached="0.5", write="3", reasoning="20")
    u = usage(inp=1_000_000, cached=2_000_000, write=1_000_000, out=500_000, reasoning=100_000)
    # 2 + 1 + 3 + 5 + 2
    assert compute_cost(u, p, batched=False) == Decimal(13)


def test_compute_cost_fallback_rates() -> None:
    p = price(inp="2", out="10")
    u = usage(inp=1_000_000, cached=1_000_000, write=1_000_000, out=1_000_000, reasoning=1_000_000)
    assert compute_cost(u, p, batched=False) == Decimal(2 + 2 + 2 + 10 + 10)


def test_compute_cost_is_exact_decimal() -> None:
    p = price(inp="3", out="15")
    assert compute_cost(usage(inp=1, out=1), p, batched=False) == Decimal("0.000018")


def test_compute_cost_batch() -> None:
    u = usage(inp=1_000_000)
    assert compute_cost(u, price(inp="2", batch="0.5"), batched=True) == Decimal(1)
    assert compute_cost(u, price(inp="2", batch="0.5"), batched=False) == Decimal(2)
    assert compute_cost(u, price(inp="2"), batched=True) is None


@pytest.mark.parametrize("field", ["inp", "cached", "write", "out", "reasoning"])
def test_compute_cost_unknown_field_is_none(field: str) -> None:
    u = usage(**{field: None})
    assert compute_cost(u, price(), batched=False) is None
