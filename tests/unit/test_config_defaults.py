"""Built-in defaults decided in SPEC (Decided items) must not drift silently."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml

from llm_crossjury.core.config import (
    AgreementConfig,
    BudgetConfig,
    CrossjuryConfig,
    MemberConfig,
    ReviewConfig,
)
from llm_crossjury.core.providers.base import Backend, ReasoningEffort


def test_budget_defaults() -> None:
    budget = BudgetConfig()
    assert budget.run_limit == Decimal("1.00")
    assert budget.confirm_above == Decimal("0.25")
    assert budget.daily_limit == Decimal("5.00")
    assert budget.currency == "USD"
    assert budget.allow_unknown_cost is False


def test_review_and_agreement_defaults() -> None:
    assert ReviewConfig().topology is None  # automatic: full <= 3, ring(k=1) >= 4
    assert ReviewConfig().ring_k == 1
    assert ReviewConfig().reasoning_effort is ReasoningEffort.LOW
    assert AgreementConfig().reasoning_effort is ReasoningEffort.LOW
    assert AgreementConfig().threshold is None  # decided in the evaluation phase


def test_run_defaults() -> None:
    member = MemberConfig(id="m", provider="p", model="x", backend=Backend.API)
    config = CrossjuryConfig(members=(member,), chair=member)
    assert config.chairman_excluded is False
    assert config.store_content is False
    assert config.checkpoint_retention_days == 7
    assert config.output_language is None
    assert member.reasoning_effort is ReasoningEffort.DEFAULT


def test_example_config_is_valid() -> None:
    path = Path(__file__).resolve().parents[2] / "config" / "llm-crossjury.example.yaml"
    config = CrossjuryConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert config.budget == BudgetConfig()
