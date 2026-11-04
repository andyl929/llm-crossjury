"""Run configuration (``config/llm-crossjury.yaml``).

Model identifiers come from configuration only; there are no built-in defaults for
them. Budget defaults ARE built in (SPEC section 5) and printed at the start of every
run. Prices are never part of this file (see config/pricing.yaml).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from llm_crossjury.core.orchestrator.topology import ReviewTopology
from llm_crossjury.core.providers.base import Backend, ReasoningEffort


class MemberConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    """Local id, e.g. ``"claude"``. Never shown to other models."""
    provider: str
    model: str
    backend: Backend
    temperature: float | None = None
    reasoning_effort: ReasoningEffort = ReasoningEffort.DEFAULT
    """Effort for this member's stage-1 answer (and chair calls, for the chair)."""


class BudgetConfig(BaseModel):
    """Spending limits. Defaults are deliberately small and always printed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_limit: Decimal = Decimal("1.00")
    """Hard cap per run."""
    confirm_above: Decimal = Decimal("0.25")
    """Ask for confirmation when the high estimate exceeds this amount."""
    daily_limit: Decimal = Decimal("5.00")
    """Hard cap on the ledger total per local calendar day, across runs."""
    currency: str = "USD"
    allow_unknown_cost: bool = False
    """Required to be ``True`` for any ``cli``-backend member. It does NOT allow api
    models missing from the price table: those always refuse the run."""


class ReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    topology: ReviewTopology | None = None
    """``None`` = automatic: ``full`` for <= 3 reviewed answers, ``ring`` (k=1) for >= 4."""
    ring_k: int = 1
    judge: str | None = None
    """Member id for ``single_judge``."""
    reasoning_effort: ReasoningEffort = ReasoningEffort.LOW


class AgreementConfig(BaseModel):
    """Claim-agreement measurement displayed at checkpoint A. Never skips by itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    judge: MemberConfig | None = None
    """A cheap model; ``None`` disables the measurement (checkpoint A shows no ratio)."""
    reasoning_effort: ReasoningEffort = ReasoningEffort.LOW
    threshold: float | None = None
    """At or above this ratio, checkpoint A *recommends* skipping review. The default
    is decided in the evaluation phase; ``None`` means no recommendation."""


class CostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    use_prompt_cache: bool = True
    use_batch: bool = False
    """Batch APIs are cheaper but slow; off by default for interactive runs."""


class DocumentConfig(BaseModel):
    """Document Review input handling."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_document_tokens: int = 50_000
    """Hard limit (estimated tokens). Larger documents are refused before any call.
    Provisional default, see SPEC Open Questions."""
    markdown_chunk_depth: int = 2
    """Markdown ATX heading depth that starts a new chunk (2 = ``#`` and ``##``)."""


class CrossjuryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    members: tuple[MemberConfig, ...]
    chair: MemberConfig
    chairman_excluded: bool = False
    """``False`` (default): the chair may also be a member (it still only sees anonymous
    labels, including for its own answer). ``True``: any member with the chair's
    (provider, model, backend) is removed from the answer and review stages."""
    review: ReviewConfig = ReviewConfig()
    agreement: AgreementConfig = AgreementConfig()
    budget: BudgetConfig = BudgetConfig()
    cost: CostConfig = CostConfig()
    document: DocumentConfig = DocumentConfig()
    output_language: str | None = None
    """BCP 47 tag (e.g. ``"de"``). ``None``: follow the language of the task/document.
    Internal prompt templates are always English."""
    pricing_path: Path = Path("config/pricing.yaml")
    store_content: bool = False
    """Persist prompt/response text in run records (see ARCHITECTURE section 6)."""
    checkpoint_retention_days: int = 7
    """Paused-run content is deleted on completion or after this many days."""
    data_dir: Path | None = None
    """Override for the platformdirs user data directory."""


def load_config(path: Path) -> CrossjuryConfig:
    """Load and validate a YAML config file. Never reads secrets."""
    raise NotImplementedError


def resolve_topology(config: ReviewConfig, reviewed_answers: int) -> ReviewTopology:
    """Return the configured topology, or the automatic default for ``reviewed_answers``."""
    raise NotImplementedError
