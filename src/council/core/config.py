"""Run configuration (``config/council.yaml``).

Model identifiers come from configuration only; there are no built-in defaults.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from council.core.orchestrator.topology import ReviewTopology
from council.core.providers.base import Backend


class MemberConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    """Local id, e.g. ``"claude"``. Never shown to other models."""
    provider: str
    model: str
    backend: Backend
    temperature: float | None = None


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: Decimal
    currency: str = "USD"
    allow_unknown_cost: bool = False
    confirm_above: Decimal | None = None
    """Ask for confirmation when the high estimate exceeds this amount."""


class ReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    topology: ReviewTopology = ReviewTopology.FULL
    ring_k: int = 1
    judge: str | None = None


class CostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    use_prompt_cache: bool = True
    use_batch: bool = False
    """Batch APIs are cheaper but slow; off by default for interactive runs."""
    early_stop_agreement: float | None = None
    """If set, skip peer review when claim agreement is at or above this value."""


class CouncilConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    members: tuple[MemberConfig, ...]
    chair: MemberConfig
    review: ReviewConfig = ReviewConfig()
    budget: BudgetConfig
    cost: CostConfig = CostConfig()
    pricing_path: Path = Path("config/pricing.yaml")
    store_content: bool = False
    """Persist prompt/response text in run records and checkpoints (see ARCHITECTURE)."""
    data_dir: Path | None = None
    """Override for the platformdirs user data directory."""


def load_config(path: Path) -> CouncilConfig:
    """Load and validate a YAML config file. Never reads secrets."""
    raise NotImplementedError
