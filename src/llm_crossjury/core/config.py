"""Run configuration (``llm-crossjury.yaml``).

Model facts (ids, reasoning controls, limits, prices) live in the model registry
(``config/models.yaml``, ADR 0006), never here. Members name a model by alias
(``fast`` | ``balanced`` | ``best``) or by concrete id; the ModelResolver turns that
into a concrete model and native parameters. Budget defaults ARE built in (SPEC §5)
and printed at the start of every run.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from llm_crossjury.core.orchestrator.topology import ReviewTopology
from llm_crossjury.core.providers.base import Backend, ReasoningEffort
from llm_crossjury.core.registry.resolver import ResolutionMode, Role


class MemberConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    """Local id, e.g. ``"claude"``. Never shown to other models."""
    provider: str
    model: str
    """Alias (``fast`` | ``balanced`` | ``best``) of ``provider``, or a concrete model id."""
    backend: Backend
    temperature: float | None = None
    reasoning: ReasoningEffort | None = None
    """Overrides the role default for this member's answerer (or chairman) calls.
    ``None`` = use ``roles``."""
    native: dict[str, Any] | None = None
    """Vendor-native parameters passed through verbatim; merged last, so they win over
    the mapped reasoning value."""


class RoleDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reasoning: ReasoningEffort


class RolesConfig(BaseModel):
    """Per-role defaults. Reviewer and agreement-judge calls always use these; answerer
    and chairman calls use them unless the member sets ``reasoning``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    answerer: RoleDefaults = RoleDefaults(reasoning=ReasoningEffort.DEFAULT)
    reviewer: RoleDefaults = RoleDefaults(reasoning=ReasoningEffort.LOW)
    agreement_judge: RoleDefaults = RoleDefaults(reasoning=ReasoningEffort.LOW)
    chairman: RoleDefaults = RoleDefaults(reasoning=ReasoningEffort.DEFAULT)


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
    """Required to be ``True`` for any ``cli``-backend member. It does NOT admit api
    models without a verified registry price: those always refuse the run."""


class ReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    topology: ReviewTopology | None = None
    """``None`` = automatic: ``full`` for <= 3 reviewed answers, ``ring`` for >= 4."""
    ring_k: int | None = None
    """``None`` = 2. ``1`` is the "issues only, no ranking" mode: each reviewer sees one
    target, rankings are ignored and Borda aggregation is skipped."""
    judge: str | None = None
    """Member id for ``single_judge``."""


class AgreementConfig(BaseModel):
    """Claim-agreement measurement displayed at checkpoint A. Never skips by itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    judge: MemberConfig | None = None
    """A cheap model (e.g. a provider's ``fast`` alias); ``None`` disables the
    measurement."""
    threshold: float | None = None
    """At or above this ratio, checkpoint A *recommends* skipping review. The default is
    decided in the evaluation phase; ``None`` means no recommendation."""


def _default_multipliers() -> dict[ReasoningEffort, float]:
    return {
        ReasoningEffort.LOW: 0.5,
        ReasoningEffort.MEDIUM: 1.0,
        ReasoningEffort.HIGH: 2.0,
        ReasoningEffort.DEFAULT: 1.0,
    }


class CostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    use_prompt_cache: bool = True
    use_batch: bool = False
    """Batch APIs are cheaper but slow; off by default for interactive runs."""
    reasoning_multipliers: dict[ReasoningEffort, float] = _default_multipliers()
    """Estimated reasoning tokens = visible output tokens x multiplier. Provisional,
    uncalibrated defaults; replaced per (model, level) by ledger observations once
    ``calibration_min_samples`` calls are recorded."""
    calibration_min_samples: int = 20


class DocumentConfig(BaseModel):
    """Document Review input handling."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_document_tokens: int = 50_000
    """Hard limit (estimated). Larger documents are refused before any call."""
    markdown_chunk_depth: int = 2
    """Markdown ATX heading depth that starts a new chunk (2 = ``#`` and ``##``)."""


class AnonymizeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scrub_model_names: bool = True
    """Replace model/vendor names with ``[model]`` in member output. Automatically
    disabled for a run whose task input (task text or document) contains any of them."""


class CrossjuryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    members: tuple[MemberConfig, ...]
    chair: MemberConfig
    chairman_excluded: bool = False
    """``False`` (default): the chair may also be a member (it still only sees anonymous
    labels, including for its own answer). ``True``: any member with the chair's
    resolved (provider, model id, backend) is removed from the answer and review
    stages."""
    roles: RolesConfig = RolesConfig()
    review: ReviewConfig = ReviewConfig()
    agreement: AgreementConfig = AgreementConfig()
    budget: BudgetConfig = BudgetConfig()
    cost: CostConfig = CostConfig()
    document: DocumentConfig = DocumentConfig()
    anonymize: AnonymizeConfig = AnonymizeConfig()
    resolution: ResolutionMode = ResolutionMode.NORMAL
    """``strict``: an unsupported reasoning level is an error instead of a downgrade."""
    output_language: str | None = None
    """BCP 47 tag (e.g. ``"de"``). ``None``: follow the language of the task/document.
    Internal prompt templates are always English."""
    models_path: Path = Field(
        default=Path("config/models.yaml"),
        # Pin the exported default to POSIX form; str(Path) would use "\\" on Windows
        # and make the committed JSON Schema platform-dependent.
        json_schema_extra={"default": "config/models.yaml"},
    )
    store_content: bool = False
    """Persist prompt/response text in run records (see ARCHITECTURE §6)."""
    checkpoint_retention_days: int = 7
    """Paused-run content is deleted on completion or after this many days."""
    data_dir: Path | None = None
    """Override for the platformdirs user data directory."""


def load_config(path: Path) -> CrossjuryConfig:
    """Load and validate a YAML config file. Never reads secrets."""
    raise NotImplementedError


def resolve_topology(config: ReviewConfig, reviewed_answers: int) -> tuple[ReviewTopology, int]:
    """Return ``(topology, ring_k)``: the configured values, or the defaults
    (``full`` for <= 3 reviewed answers; ``ring`` with k = 2 for >= 4)."""
    raise NotImplementedError


def role_reasoning(config: CrossjuryConfig, member: MemberConfig, role: Role) -> ReasoningEffort:
    """Pick the reasoning level for ``member`` acting in ``role``.

    Answerer/chairman: ``member.reasoning`` if set, else the role default.
    Reviewer/agreement judge: always the role default.
    """
    raise NotImplementedError
