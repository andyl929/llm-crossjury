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

Range bounds:

* **low**: ``expected_output_tokens_low``; calls after the first that share a
  ``prefix_fingerprint`` on the same ``api`` model read ``cache_hit_rate`` of the
  prefix from the cache.
* **high**: ``expected_output_tokens_high``; no cache hits at all.

In both bounds the first call of a shared prefix pays the cache-write rate for the
prefix. Input token counts in a :class:`CallPlan` come from vendor counting where
available and from :func:`estimate_tokens` otherwise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from llm_crossjury.core.cost.ledger import Ledger, compute_cost
from llm_crossjury.core.cost.pricing import PricingError, PricingTable
from llm_crossjury.core.providers.base import Backend, ReasoningEffort, Usage
from llm_crossjury.core.registry.resolver import ResolvedModel
from llm_crossjury.core.registry.schema import Pricing, ReasoningControlType

ASCII_CHARS_PER_TOKEN = 3
"""Heuristic used by :func:`estimate_tokens` for ASCII text."""


def estimate_tokens(text: str) -> int:
    """Estimate the token count of ``text`` without a vendor tokenizer.

    A deliberately conservative (over-counting) local heuristic, used when the vendor
    offers no counting facility (``Provider.count_input_tokens`` returns ``None``):
    one token per started group of three ASCII characters, plus one token per
    non-ASCII character. Common tokenizers average about four characters per token
    for English text and rarely less than one token per CJK character, so this
    usually overestimates.

    Args:
        text: The prompt text (all messages concatenated).

    Returns:
        The estimated number of tokens; ``0`` for an empty string.
    """
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)  # noqa: PLR2004
    return math.ceil(ascii_chars / ASCII_CHARS_PER_TOKEN) + (len(text) - ascii_chars)


class EstimateLine(BaseModel):
    """Estimated cost of one planned call (or a group of identical calls).

    Token fields are totals over the group's ``calls``. ``input_tokens`` counts all
    input tokens, including the expected cache hits in ``cached_input_tokens``.
    """

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
        return self.total_high > budget


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
    output_cap_includes_reasoning: bool = False
    """``True`` when ``expected_output_tokens_high`` is the call's hard output cap and the
    vendor counts reasoning tokens inside that cap (so visible plus reasoning output can
    never exceed it). ``False``: reasoning is estimated on top of the visible output."""


_LineKey = tuple[str, str, str, Backend, ReasoningEffort, bool]


@dataclass
class _Line:
    calls: int = 0
    input_tokens: int = 0
    cached: int = 0
    out_low: int = 0
    out_high: int = 0
    reasoning_low: int = 0
    reasoning_high: int = 0
    cost_low: Decimal | None = Decimal(0)
    cost_high: Decimal | None = Decimal(0)


class CostEstimator:
    """Turns a list of :class:`CallPlan` into a :class:`CostEstimate`.

    Calls sharing a ``prefix_fingerprint`` on a caching-capable provider are assumed
    to hit the cache after the first call (configurable hit rate). The estimator
    treats every ``api`` model as caching-capable for the low bound; the high bound
    assumes no hits, so it stays safe for providers without caching.

    ``api`` calls without a verified price, and every ``cli`` call, get
    ``cost_low``/``cost_high`` = ``None`` and count toward ``unpriced_calls``.
    ``mock`` calls cost zero.
    """

    def __init__(
        self,
        pricing: PricingTable,
        cache_hit_rate: float,
        reasoning_multipliers: dict[ReasoningEffort, float],
        *,
        ledger: Ledger | None = None,
        calibration_min_samples: int = 20,
        currency: str | None = None,
    ) -> None:
        """Create an estimator.

        Args:
            pricing: Verified prices (and, through its registry, per-model multipliers).
            cache_hit_rate: Share of a shared prefix read from the cache, ``0..1``.
            reasoning_multipliers: Global ``CostConfig.reasoning_multipliers``.
            ledger: If given, observed reasoning ratios take precedence once
                ``calibration_min_samples`` observations exist.
            calibration_min_samples: ``CostConfig.calibration_min_samples``.
            currency: The budget currency (``BudgetConfig.currency``). Prices in another
                currency raise :class:`PricingError`. If ``None``, the currency of the
                priced lines is used (``""`` if nothing is priced).

        Raises:
            ValueError: if ``cache_hit_rate`` is outside ``0..1``.
        """
        if not 0.0 <= cache_hit_rate <= 1.0:
            raise ValueError("cache_hit_rate must be between 0 and 1")
        self._pricing = pricing
        self._hit_rate = cache_hit_rate
        self._multipliers = dict(reasoning_multipliers)
        self._ledger = ledger
        self._min_samples = calibration_min_samples
        self._currency = currency

    def _multiplier(
        self, provider: str, model_id: str, level: ReasoningEffort
    ) -> tuple[float, str]:
        entry = self._pricing.registry.get(provider, model_id)
        if entry is not None and entry.reasoning_control.type is ReasoningControlType.NONE:
            return 0.0, "model has no reasoning control"
        if self._ledger is not None:
            observed = self._ledger.observed_reasoning_ratio(
                provider, model_id, level, self._min_samples
            )
            if observed is not None:
                return observed, "calibrated from the ledger"
        if entry is not None and entry.reasoning_multipliers is not None:
            override = entry.reasoning_multipliers.get(level)
            if override is not None:
                return override, "registry entry override"
        if level not in self._multipliers:
            raise ValueError(f"no reasoning multiplier configured for level '{level}'")
        return self._multipliers[level], "configured (uncalibrated)"

    def _price(self, call: CallPlan) -> Pricing | None:
        if call.backend is not Backend.API:
            return None
        try:
            return self._pricing.lookup(call.provider, call.model)
        except PricingError:
            return None

    def estimate(self, plan: list[CallPlan], resolved: list[ResolvedModel]) -> CostEstimate:
        """Estimate ``plan``.

        Args:
            plan: Every call the run intends to make, in execution order.
            resolved: Every (member, role) resolution, passed through for display.

        Raises:
            PricingError: if a price's currency differs from the estimate currency.
        """
        group_sizes: dict[tuple[str, str, str], int] = {}
        for call in plan:
            key = (call.provider, call.model, call.prefix_fingerprint)
            if call.backend is Backend.API:
                group_sizes[key] = group_sizes.get(key, 0) + 1
        seen_prefixes: set[tuple[str, str, str]] = set()

        lines: dict[_LineKey, _Line] = {}
        multiplier_notes: dict[str, None] = {}
        currency = self._currency
        mock_calls = 0
        for call in plan:
            line_key: _LineKey = (
                call.stage,
                call.provider,
                call.model,
                call.backend,
                call.reasoning,
                call.batched,
            )
            line = lines.setdefault(line_key, _Line())

            prefix_key = (call.provider, call.model, call.prefix_fingerprint)
            total_in = call.prefix_tokens + call.suffix_tokens
            written = cached = 0
            if group_sizes.get(prefix_key, 0) > 1:
                if prefix_key in seen_prefixes:
                    cached = round(call.prefix_tokens * self._hit_rate)
                else:
                    written = call.prefix_tokens
                seen_prefixes.add(prefix_key)

            mult, source = self._multiplier(call.provider, call.model, call.reasoning)
            multiplier_notes[
                f"{call.provider}/{call.model} at '{call.reasoning}': x{mult:g} ({source})"
            ] = None
            out_low, r_low, out_high, r_high = _output_bounds(call, mult)

            line.calls += 1
            line.input_tokens += total_in
            line.cached += cached
            line.out_low += out_low
            line.out_high += out_high
            line.reasoning_low += r_low
            line.reasoning_high += r_high

            if call.backend is Backend.MOCK:
                mock_calls += 1
                continue
            price = self._price(call)
            low: Decimal | None = None
            high: Decimal | None = None
            if price is not None:
                if currency is None:
                    currency = price.currency
                elif price.currency != currency:
                    raise PricingError(
                        f"{call.provider}/{call.model} is priced in {price.currency}, "
                        f"but the estimate is in {currency}"
                    )
                low = compute_cost(
                    _usage(
                        total_in - written - cached,
                        cached,
                        written,
                        out_low,
                        r_low,
                    ),
                    price,
                    batched=call.batched,
                )
                high = compute_cost(
                    _usage(total_in - written, 0, written, out_high, r_high),
                    price,
                    batched=call.batched,
                )
            line.cost_low = _add(line.cost_low, low)
            line.cost_high = _add(line.cost_high, high)

        estimate_lines = tuple(
            EstimateLine(
                stage=key[0],
                provider=key[1],
                model=key[2],
                backend=key[3],
                reasoning=key[4],
                calls=line.calls,
                input_tokens=line.input_tokens,
                cached_input_tokens=line.cached,
                output_tokens_low=line.out_low,
                output_tokens_high=line.out_high,
                reasoning_tokens_low=line.reasoning_low,
                reasoning_tokens_high=line.reasoning_high,
                cost_low=line.cost_low,
                cost_high=line.cost_high,
            )
            for key, line in lines.items()
        )
        priced = [ln for ln in estimate_lines if ln.cost_low is not None]
        unpriced_calls = sum(ln.calls for ln in estimate_lines if ln.cost_low is None)
        return CostEstimate(
            currency=currency or "",
            resolved=tuple(resolved),
            lines=estimate_lines,
            total_low=sum((ln.cost_low or Decimal(0) for ln in priced), Decimal(0)),
            total_high=sum((ln.cost_high or Decimal(0) for ln in priced), Decimal(0)),
            unpriced_calls=unpriced_calls,
            assumptions=self._assumptions(list(multiplier_notes), unpriced_calls, mock_calls, plan),
        )

    def _assumptions(
        self, multipliers: list[str], unpriced_calls: int, mock_calls: int, plan: list[CallPlan]
    ) -> tuple[str, ...]:
        notes = [
            "Input tokens come from vendor token counting where available, otherwise "
            "from the local estimate_tokens() heuristic (1 token per 3 ASCII characters, "
            "1 per non-ASCII character), which tends to overestimate.",
            "Output tokens use each stage's low/high bounds.",
            f"Cache: the first call of a shared prompt prefix on an api model pays the "
            f"cache-write rate; later calls read {self._hit_rate:.0%} of the prefix from "
            "the cache in the low estimate and nothing in the high estimate.",
            "Reasoning tokens = visible output x multiplier:",
            *(f"  {m}" for m in multipliers),
        ]
        if any(c.batched for c in plan):
            notes.append(
                "Batched calls use the registry batch_multiplier; a model without one "
                "is unpriced for batch calls."
            )
        if unpriced_calls:
            notes.append(
                f"{unpriced_calls} call(s) are unpriced (cli backend, or an api model "
                "without a verified registry price) and are excluded from the totals."
            )
        if mock_calls:
            notes.append(f"{mock_calls} mock-backend call(s) cost nothing.")
        return tuple(notes)


def _usage(inp: int, cached: int, written: int, out: int, reasoning: int) -> Usage:
    return Usage(
        input_tokens=inp,
        cached_input_tokens=cached,
        cache_write_tokens=written,
        output_tokens=out,
        reasoning_tokens=reasoning,
    )


def _add(total: Decimal | None, amount: Decimal | None) -> Decimal | None:
    if total is None or amount is None:
        return None
    return total + amount


def _split_within_cap(visible: int, reasoning: int, *, cap: int, mult: float) -> tuple[int, int]:
    """Fit visible + reasoning output into ``cap``, keeping their ratio (1 : ``mult``)."""
    if visible + reasoning <= cap:
        return visible, reasoning
    shown = math.ceil(cap / (1 + mult)) if mult > 0 else cap
    return shown, max(0, cap - shown)


def _output_bounds(call: CallPlan, mult: float) -> tuple[int, int, int, int]:
    """Return ``(visible_low, reasoning_low, visible_high, reasoning_high)`` for ``call``.

    Reasoning is ``visible x mult``; when the vendor counts reasoning inside the output
    cap, visible plus reasoning is fitted into ``expected_output_tokens_high``.
    """
    out_low, out_high = call.expected_output_tokens_low, call.expected_output_tokens_high
    r_low = math.ceil(out_low * mult)
    r_high = math.ceil(out_high * mult)
    if call.output_cap_includes_reasoning:
        cap = out_high
        out_low, r_low = _split_within_cap(out_low, r_low, cap=cap, mult=mult)
        out_high, r_high = _split_within_cap(out_high, r_high, cap=cap, mult=mult)
    return out_low, r_low, out_high, r_high
