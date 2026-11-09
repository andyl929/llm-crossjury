"""Built-in defaults decided in SPEC (Decided items) must not drift silently."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml

from llm_crossjury.core.config import (
    AgreementConfig,
    BudgetConfig,
    CrossjuryConfig,
    DocumentConfig,
    MemberConfig,
    ReviewConfig,
    RolesConfig,
)
from llm_crossjury.core.providers.base import Backend, ReasoningEffort
from llm_crossjury.core.registry import ResolutionMode


def test_budget_defaults() -> None:
    budget = BudgetConfig()
    assert budget.run_limit == Decimal("5.00")
    assert budget.confirm_above == Decimal("1.00")
    assert budget.daily_limit == Decimal("15.00")
    assert budget.currency == "USD"
    assert budget.allow_unknown_cost is False


def test_review_and_agreement_defaults() -> None:
    assert ReviewConfig().topology is None  # automatic: full <= 3, ring >= 4
    assert ReviewConfig().ring_k is None  # automatic: 2; 1 = issues only, no ranking
    assert AgreementConfig().threshold is None  # decided in the evaluation phase


def test_role_reasoning_defaults() -> None:
    roles = RolesConfig()
    assert roles.answerer.reasoning is ReasoningEffort.DEFAULT
    assert roles.reviewer.reasoning is ReasoningEffort.LOW
    assert roles.agreement_judge.reasoning is ReasoningEffort.LOW
    assert roles.chairman.reasoning is ReasoningEffort.DEFAULT


def test_run_defaults() -> None:
    member = MemberConfig(id="m", provider="p", model="balanced", backend=Backend.API)
    config = CrossjuryConfig(members=(member,), chair=member)
    assert config.chairman_excluded is False
    assert config.store_content is False
    assert config.checkpoint_retention_days == 7
    assert config.output_language is None
    assert config.resolution is ResolutionMode.NORMAL
    assert config.anonymize.scrub_model_names is True
    assert config.models_path == Path("config/models.yaml")
    assert DocumentConfig().max_document_tokens == 50_000
    assert member.reasoning is None
    assert member.native is None


def test_example_config_is_valid() -> None:
    path = Path(__file__).resolve().parents[2] / "config" / "llm-crossjury.example.yaml"
    config = CrossjuryConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert config.budget == BudgetConfig()
    assert config.roles == RolesConfig()
