"""Fakes for compare-mode tests: registry, resolver, pricing, estimator, ledger.

All model ids and prices here are fictitious test data.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from llm_crossjury.core.compare_service import CompareDeps
from llm_crossjury.core.config import BudgetConfig
from llm_crossjury.core.cost.estimate import CallPlan, CostEstimate, EstimateLine
from llm_crossjury.core.cost.ledger import Budget, BudgetExceededError, LedgerEntry
from llm_crossjury.core.cost.pricing import PricingError
from llm_crossjury.core.providers.base import (
    Backend,
    Capabilities,
    CompletionRequest,
    CompletionResult,
    Provider,
    ProviderStatus,
    ReasoningEffort,
    Usage,
)
from llm_crossjury.core.providers.mock import MockProvider
from llm_crossjury.core.registry.resolver import ResolutionError, ResolvedModel, Role
from llm_crossjury.core.registry.schema import (
    CliFlags,
    ModelEntry,
    Pricing,
    ReasoningControl,
    ReasoningControlType,
)

PROVIDERS = ("anthropic", "openai", "gemini", "xai")
CLI_PROVIDERS = ("anthropic", "openai", "xai")
"""Providers with a fake subscription (cli) backend in :func:`make_deps`."""
PER_OUTPUT_TOKEN = Decimal("0.00001")
"""Fake estimator rate: cost = output tokens x this (high = 4096 tokens -> 0.04096)."""
FIXED_CALL_COST = Decimal("0.01")


def make_pricing(currency: str = "USD") -> Pricing:
    return Pricing(
        currency=currency,
        input_per_mtok=Decimal(1),
        output_per_mtok=Decimal(2),
        cached_input_per_mtok=None,
        cache_write_per_mtok=None,
        reasoning_per_mtok=None,
        batch_multiplier=None,
    )


def make_entry(
    provider: str,
    model_id: str,
    *,
    backends: tuple[Backend, ...] = (Backend.API,),
    max_output: int | None = None,
) -> ModelEntry:
    cli_flags = (
        CliFlags(model=("--model", "{model}"), reasoning=None) if Backend.CLI in backends else None
    )
    return ModelEntry(
        provider=provider,
        model_id=model_id,
        backends=backends,
        reasoning_control=ReasoningControl(
            type=ReasoningControlType.NONE, parameter=None, allowed=None, mapping=None
        ),
        cli_flags=cli_flags,
        context_window=None,
        max_output=max_output,
        structured_output=None,
        pricing=None,
        reasoning_multipliers=None,
        source_url=None,
        last_verified=None,
        notes="test entry",
    )


@dataclass
class FakeRegistry:
    models: list[ModelEntry]

    def get(self, provider: str, model_id: str) -> ModelEntry | None:
        for entry in self.models:
            if entry.provider == provider and entry.model_id == model_id:
                return entry
        return None

    def entries(self) -> tuple[ModelEntry, ...]:
        return tuple(self.models)


@dataclass
class FakeResolver:
    aliases: dict[tuple[str, str], str | None]

    def resolve_alias(self, provider: str, name: str) -> str:
        if (provider, name) in self.aliases:
            target = self.aliases[(provider, name)]
            if target is None:
                raise ResolutionError(f"alias {name} unset")
            return target
        return name

    def resolve(
        self,
        *,
        member_id: str,
        provider: str,
        model: str,
        backend: Backend,
        role: Role,
        reasoning: ReasoningEffort,
        native: dict[str, object] | None,
    ) -> ResolvedModel:
        model_id = self.resolve_alias(provider, model)
        return ResolvedModel(
            member_id=member_id,
            role=role,
            provider=provider,
            requested_model=model,
            model_id=model_id,
            backend=backend,
            registered=True,
            requested_reasoning=reasoning,
            effective_reasoning=reasoning,
            native_reasoning=None,
            native_params={},
            cli_args=(),
            warnings=(),
        )


@dataclass
class FakePricing:
    prices: dict[tuple[str, str], Pricing]

    def lookup(self, provider: str, model_id: str) -> Pricing:
        try:
            return self.prices[(provider, model_id)]
        except KeyError:
            raise PricingError(f"no verified price for {provider}/{model_id}") from None


class FakeEstimator:
    """Prices every non-cli call by output tokens; cli calls are unpriced (as the real
    estimator does)."""

    def estimate(self, plan: list[CallPlan], resolved: list[ResolvedModel]) -> CostEstimate:
        lines = tuple(
            EstimateLine(
                stage=p.stage,
                provider=p.provider,
                model=p.model,
                backend=p.backend,
                calls=1,
                input_tokens=p.prefix_tokens + p.suffix_tokens,
                cached_input_tokens=0,
                output_tokens_low=p.expected_output_tokens_low,
                output_tokens_high=p.expected_output_tokens_high,
                reasoning=p.reasoning,
                reasoning_tokens_low=0,
                reasoning_tokens_high=0,
                cost_low=None
                if p.backend is Backend.CLI
                else p.expected_output_tokens_low * PER_OUTPUT_TOKEN,
                cost_high=None
                if p.backend is Backend.CLI
                else p.expected_output_tokens_high * PER_OUTPUT_TOKEN,
            )
            for p in plan
        )
        return CostEstimate(
            currency="USD",
            resolved=tuple(resolved),
            lines=lines,
            total_low=sum((line.cost_low or Decimal(0) for line in lines), Decimal(0)),
            total_high=sum((line.cost_high or Decimal(0) for line in lines), Decimal(0)),
            unpriced_calls=sum(1 for line in lines if line.cost_low is None),
            assumptions=("fake estimator",),
        )


@dataclass
class FakeLedger:
    spent_today: Decimal = Decimal(0)
    entries: list[LedgerEntry] = field(default_factory=list)
    reservations: dict[str, tuple[str, Decimal]] = field(default_factory=dict)
    checks: list[Decimal] = field(default_factory=list)

    def _run_spent(self, run_id: str) -> Decimal:
        return sum((e.cost or Decimal(0) for e in self.entries if e.run_id == run_id), Decimal(0))

    def _reserved(self, run_id: str | None = None) -> Decimal:
        return sum(
            (amt for rid, amt in self.reservations.values() if run_id in {None, rid}), Decimal(0)
        )

    def record(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)

    def spent_on(self, day: date) -> Decimal:
        return self.spent_today + sum((e.cost or Decimal(0) for e in self.entries), Decimal(0))

    def reserve(self, run_id: str, amount: Decimal) -> str:
        rid = f"r{len(self.reservations)}-{run_id}-{amount}"
        self.reservations[rid] = (run_id, amount)
        return rid

    def release(self, reservation_id: str) -> None:
        del self.reservations[reservation_id]

    def check(self, run_id: str, next_call_high: Decimal, budget: Budget) -> None:
        self.checks.append(next_call_high)
        run_spent, run_reserved = self._run_spent(run_id), self._reserved(run_id)
        if run_spent + run_reserved + next_call_high > budget.run_limit:
            raise BudgetExceededError("run", run_spent, run_reserved, budget.run_limit)
        day = self.spent_on(date.today())
        if day + self._reserved() + next_call_high > budget.daily_limit:
            raise BudgetExceededError("daily", day, self._reserved(), budget.daily_limit)


def echo_provider(name: str) -> MockProvider:
    return MockProvider(
        name, lambda req: f"{name} answer to: {req.prompt.variable_suffix[0].content}"
    )


class GatedProvider(MockProvider):
    """Waits at ``barrier`` before answering, so the call stays in flight until every
    other gated call has started (proves concurrency, exercises reservations)."""

    def __init__(self, name: str, barrier: asyncio.Barrier) -> None:
        super().__init__(name, lambda req: f"{name} answer")
        self._barrier = barrier

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        await asyncio.wait_for(self._barrier.wait(), timeout=5)
        await asyncio.sleep(0)  # the last arrival must yield too
        return await super().complete(request)


class FakeCliProvider(MockProvider):
    """A subscription (``cli``) provider: reports unknown usage, like a real CLI.

    Args:
        name: Provider id.
        available: What :meth:`check` reports.
        detail: The check detail text.
        check_error: If set, :meth:`check` raises it.
        responder: Response text factory (default: an echo).
        capabilities: Reported capabilities (default: the mock's, no native files).
    """

    backend = Backend.CLI

    def __init__(
        self,
        name: str,
        *,
        available: bool = True,
        detail: str = "fake cli 1.0",
        check_error: Exception | None = None,
        responder: Callable[[CompletionRequest], str] | None = None,
        capabilities: Capabilities | None = None,
    ) -> None:
        super().__init__(name, responder or (lambda req: f"{name} cli answer"), capabilities)
        self.available = available
        self.detail = detail
        self.check_error = check_error
        self.checks = 0

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        result = await super().complete(request)
        unknown = Usage(
            input_tokens=None,
            cached_input_tokens=None,
            cache_write_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
        )
        return result.model_copy(update={"usage": unknown, "latency_s": None})

    async def check(self) -> ProviderStatus:
        self.checks += 1
        if self.check_error is not None:
            raise self.check_error
        return ProviderStatus(
            provider=self.name,
            backend=Backend.CLI,
            available=self.available,
            detail=self.detail,
            version="1.0" if self.available else None,
        )


def raising_provider(name: str, exc: Exception) -> MockProvider:
    def responder(req: CompletionRequest) -> str:
        raise exc

    return MockProvider(name, responder)


def fixed_cost(usage: Usage, price: Pricing) -> Decimal | None:
    return FIXED_CALL_COST


def make_deps(
    *,
    providers: Mapping[str, Provider] | None = None,
    cli_providers: Mapping[str, Provider] | None = None,
    key_envs: Mapping[str, str] | None = None,
    budget: BudgetConfig | None = None,
    ledger: FakeLedger | None = None,
    compute_cost: Callable[[Usage, Pricing], Decimal | None] = fixed_cost,
    extra_models: list[ModelEntry] | None = None,
) -> CompareDeps:
    """Deps with one priced model per provider (``<provider>-model``, alias ``balanced``)
    plus one unpriced model per provider (``<provider>-unpriced``) and one cli-only
    model per :data:`CLI_PROVIDERS` entry (``<provider>-cli``, unpriced).

    ``providers`` are keyed by name and registered on the ``api`` backend;
    ``cli_providers`` (default: none) on the ``cli`` backend.
    """
    # max_output 4096: the generous default cap (32000) is clamped to it, so amounts stay
    # easy to reason about (high = 4096 output tokens).
    models = [make_entry(p, f"{p}-model", max_output=4096) for p in PROVIDERS]
    models += [make_entry(p, f"{p}-unpriced") for p in PROVIDERS]
    models += [make_entry(p, f"{p}-cli", backends=(Backend.CLI,)) for p in CLI_PROVIDERS]
    models += extra_models or []
    aliases: dict[tuple[str, str], str | None] = {}
    for p in PROVIDERS:
        aliases[(p, "balanced")] = f"{p}-model"
        aliases[(p, "fast")] = None
    api = providers if providers is not None else {p: echo_provider(p) for p in PROVIDERS}
    keyed: dict[tuple[str, Backend], Provider] = {(n, Backend.API): p for n, p in api.items()}
    for name, provider in (cli_providers or {}).items():
        keyed[(name, Backend.CLI)] = provider
    return CompareDeps(
        providers=keyed,
        key_envs=key_envs or {},
        registry=FakeRegistry(models),
        resolver=FakeResolver(aliases),
        pricing=FakePricing({(p, f"{p}-model"): make_pricing() for p in PROVIDERS}),
        estimator=FakeEstimator(),
        ledger=ledger if ledger is not None else FakeLedger(),
        budget=budget or BudgetConfig(),
        compute_cost=compute_cost,
    )
