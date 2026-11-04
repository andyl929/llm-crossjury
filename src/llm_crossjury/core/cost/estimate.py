"""Pre-run cost estimation.

The estimate is a range, not a point: output length is unknown before the run, and
cache hit rates are uncertain. ``cli``-backend calls are priced as ``None`` (the
vendor bills the user's subscription, not per token) and listed separately.

Reasoning tokens are estimated as ``visible output x multiplier[level]``. The
multiplier comes from, in order of precedence: the ledger-observed ratio for
(provider, model, level) once enough observations exist
(``Ledger.observed_reasoning_ratio``); the registry entry's
``reasoning_multipliers``; the global ``CostConfig.reasoning_multipliers``.

The estimate also lists every member's resolved model, backend and native reasoning
parameters, so the user sees exactly what will be called before confirming.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from llm_crossjury.core.cost.pricing import PricingTable
from llm_crossjury.core.providers.base import Backend, ReasoningEffort
from llm_crossjury.core.registry.resolver import ResolvedModel


class EstimateLine(BaseModel):
    """Estimated cost of one planned call (or a group of identical calls)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str
    provider: str
    model: str
    backend: Backend
    calls: int
    input_tokens: int
    cached_input_tokens: int
    """Expected cache hits, given the stable-prefix plan."""
    output_tokens_low: int
    output_tokens_high: int
    reasoning: ReasoningEffort
    reasoning_tokens_low: int
    reasoning_tokens_high: int
    cost_low: Decimal | None
    cost_high: Decimal | None
    """``None`` when unpriceable (cli backend or unpriced model)."""


class CostEstimate(BaseModel):
    """Estimate for a whole planned run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: str
    resolved: tuple[ResolvedModel, ...]
    """Every (member, role) resolution: model id, backend, native params, warnings."""
    lines: tuple[EstimateLine, ...]
    total_low: Decimal
    total_high: Decimal
    """Sum over priced lines only."""
    unpriced_calls: int
    assumptions: tuple[str, ...]
    """Human-readable assumptions (token heuristics, cache hit rate, topology, the
    reasoning multipliers used and whether they are configured or calibrated)."""

    def exceeds(self, budget: Decimal) -> bool:
        """Return ``True`` if ``total_high`` exceeds ``budget``."""
        raise NotImplementedError


class CallPlan(BaseModel):
    """One call the orchestrator intends to make, as seen by the estimator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str
    provider: str
    model: str
    """Resolved model id."""
    backend: Backend
    reasoning: ReasoningEffort
    """Effective level after resolution."""
    prefix_fingerprint: str
    prefix_tokens: int
    suffix_tokens: int
    expected_output_tokens_low: int
    expected_output_tokens_high: int
    batched: bool


class CostEstimator:
    """Turns a list of :class:`CallPlan` into a :class:`CostEstimate`.

    Calls sharing a ``prefix_fingerprint`` on a caching-capable provider are assumed
    to hit the cache after the first call (configurable hit rate).
    """

    def __init__(
        self,
        pricing: PricingTable,
        cache_hit_rate: float,
        reasoning_multipliers: dict[ReasoningEffort, float],
    ) -> None:
        raise NotImplementedError

    def estimate(self, plan: list[CallPlan], resolved: list[ResolvedModel]) -> CostEstimate:
        raise NotImplementedError
