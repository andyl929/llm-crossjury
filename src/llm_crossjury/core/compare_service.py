"""Compare-mode service: estimate and run one task across several providers.

This module is the shared core API behind the web UI (and, later, the CLI). It holds
no web or CLI code. Everything it needs comes in through :class:`CompareDeps`, so tests
can pass fakes; :func:`build_default_deps` is the single place with the real wiring.

Flow of :func:`run_compare`:

1. Validate the selections, resolve each model (reasoning is always ``default`` in
   this MVP), and look up its verified price. A missing price refuses the run.
2. Estimate the run and refuse it if the high estimate exceeds the run limit or the
   remaining daily budget (SPEC §5).
3. Call every member concurrently. Before each call the ledger is checked with that
   call's high estimate and the amount is reserved; every completed call is recorded
   with its computed cost. A failing member (missing key, vendor error, budget stop)
   becomes an error entry and never fails the whole run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from llm_crossjury.core.config import BudgetConfig, CostConfig
from llm_crossjury.core.cost.estimate import (
    CallPlan,
    CostEstimate,
    CostEstimator,
    estimate_tokens,
)
from llm_crossjury.core.cost.ledger import (
    Budget,
    BudgetExceededError,
    LedgerEntry,
    SqliteLedger,
    compute_cost,
)
from llm_crossjury.core.cost.pricing import PricingError, PricingTable
from llm_crossjury.core.orchestrator.stage import StageName
from llm_crossjury.core.providers.base import (
    Backend,
    CompletionRequest,
    CompletionResult,
    Message,
    Prompt,
    Provider,
    ProviderError,
    ReasoningEffort,
    Role,
    Usage,
)
from llm_crossjury.core.registry.resolver import (
    ModelRegistry,
    ModelResolver,
    ResolutionMode,
    ResolvedModel,
    resolve_models_path,
)
from llm_crossjury.core.registry.resolver import Role as ModelRole
from llm_crossjury.core.registry.schema import ModelEntry, Pricing
from llm_crossjury.core.storage import resolve_data_paths

PROVIDER_NAMES: tuple[str, ...] = ("anthropic", "openai", "gemini", "xai")
"""Provider ids offered in compare mode, in display order."""

ALIAS_NAMES: tuple[str, ...] = ("fast", "balanced", "best")
"""Portable alias names (SPEC glossary)."""

DEFAULT_MAX_OUTPUT_TOKENS = 4096
"""Product setting (not a model fact): default output cap per member call."""

DEFAULT_TIMEOUT_S = 300.0
"""Product setting: per-call timeout so a hung vendor call cannot block the run."""

ESTIMATE_LOW_OUTPUT_DIVISOR = 8
"""The low output estimate is ``max_output_tokens // 8``; the high one is the cap."""

PROMPT_ID = "compare/answer.v1"
"""Prompt template used for compare answers (``prompts/<id>.md``)."""

_PROMPT_VARIABLE = "{task}"


# --------------------------------------------------------------------------- errors


class CompareError(Exception):
    """Base class for compare-mode refusals. Messages never contain secrets."""


class SelectionError(CompareError, ValueError):
    """The task or the member selections are invalid."""


class BudgetRefusedError(CompareError):
    """The run is refused before any call because it could exceed a budget limit."""

    def __init__(self, message: str, status: BudgetStatus) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- data


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Selection(_Frozen):
    """One member picked by the user: a provider and a model, plus product settings.

    Reasoning is always ``default`` in this MVP.
    """

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    """A concrete registry ``model_id`` or an alias (``fast`` | ``balanced`` | ``best``)."""
    temperature: float | None = Field(default=None, ge=0)
    """``None`` = vendor default."""
    max_output_tokens: int = Field(default=DEFAULT_MAX_OUTPUT_TOKENS, ge=1)


class MemberResult(_Frozen):
    """Outcome of one member call. Exactly one of ``text`` and ``error`` is set."""

    provider: str
    model: str
    """Resolved model id."""
    requested_model: str
    """As selected (alias or id)."""
    text: str | None
    error: str | None
    usage: Usage | None
    cost: Decimal | None
    """``None`` when unknown (failed call, or usage the vendor did not report)."""
    currency: str
    latency_s: float | None


class CompareResult(_Frozen):
    """Result of one compare run."""

    run_id: str
    prompt_id: str
    currency: str
    members: tuple[MemberResult, ...]
    total_cost: Decimal
    """Sum of known member costs."""
    unknown_cost_members: int
    """Completed calls whose cost could not be computed."""


class BudgetStatus(_Frozen):
    """The effective budget compared with an estimate (SPEC §5)."""

    currency: str
    run_limit: Decimal
    confirm_above: Decimal
    daily_limit: Decimal
    spent_today: Decimal
    daily_remaining: Decimal
    estimate_low: Decimal
    estimate_high: Decimal
    needs_confirmation: bool
    """The high estimate exceeds ``confirm_above`` (and the run is not refused)."""
    refused: bool
    refusal_reason: str | None


# --------------------------------------------------------------------------- deps


class RegistryLike(Protocol):
    """The part of :class:`ModelRegistry` compare mode uses."""

    def get(self, provider: str, model_id: str) -> ModelEntry | None: ...

    def entries(self) -> tuple[ModelEntry, ...]: ...


class ResolverLike(Protocol):
    """The part of :class:`ModelResolver` compare mode uses."""

    def resolve_alias(self, provider: str, name: str) -> str: ...

    def resolve(
        self,
        *,
        member_id: str,
        provider: str,
        model: str,
        backend: Backend,
        role: ModelRole,
        reasoning: ReasoningEffort,
        native: dict[str, object] | None,
    ) -> ResolvedModel: ...


class PricingLike(Protocol):
    """The part of :class:`PricingTable` compare mode uses."""

    def lookup(self, provider: str, model_id: str) -> Pricing: ...


class EstimatorLike(Protocol):
    """The part of :class:`CostEstimator` compare mode uses."""

    def estimate(self, plan: list[CallPlan], resolved: list[ResolvedModel]) -> CostEstimate: ...


class LedgerLike(Protocol):
    """The part of :class:`~llm_crossjury.core.cost.ledger.Ledger` compare mode uses."""

    def record(self, entry: LedgerEntry) -> None: ...

    def spent_on(self, day: date) -> Decimal: ...

    def reserve(self, run_id: str, amount: Decimal) -> str: ...

    def release(self, reservation_id: str) -> None: ...

    def check(self, run_id: str, next_call_high: Decimal, budget: Budget) -> None: ...


CostFn = Callable[[Usage, Pricing], Decimal | None]
TokenCounter = Callable[[str], int]


def _real_compute_cost(usage: Usage, price: Pricing) -> Decimal | None:
    return compute_cost(usage, price, batched=False)


def default_prompts_dir() -> Path:
    """Locate the repository's ``prompts/`` directory relative to this package.

    Raises:
        FileNotFoundError: if no ``prompts/`` directory with the compare template is
            found above the package (for example in a wheel install).
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "prompts"
        if (candidate / f"{PROMPT_ID}.md").is_file():
            return candidate
    raise FileNotFoundError(f"prompt template {PROMPT_ID!r} not found")


@dataclass(frozen=True)
class CompareDeps:
    """Everything compare mode needs, injected so tests can use fakes.

    Attributes:
        providers: Provider objects by provider id.
        key_envs: Provider id -> name of the environment variable holding its key.
            Providers without an entry need no key (e.g. mocks).
        registry: Model registry (entries and per-model facts).
        resolver: Resolves aliases and models (ADR 0006).
        pricing: Verified price lookup.
        estimator: Turns call plans into a cost estimate.
        ledger: Spend ledger with per-run and daily checks.
        budget: Effective budget limits.
        compute_cost: Cost of one completed call from its usage and price.
        count_tokens: Local token heuristic for estimates.
        prompts_dir: Directory holding the prompt templates.
    """

    providers: Mapping[str, Provider]
    key_envs: Mapping[str, str]
    registry: RegistryLike
    resolver: ResolverLike
    pricing: PricingLike
    estimator: EstimatorLike
    ledger: LedgerLike
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    compute_cost: CostFn = _real_compute_cost
    count_tokens: TokenCounter = estimate_tokens
    prompts_dir: Path = field(default_factory=default_prompts_dir)


def key_is_set(env_var: str) -> bool:
    """Return whether ``env_var`` holds a non-empty value. Never exposes the value."""
    return bool(os.environ.get(env_var, "").strip())


def build_default_deps(base_dir: Path) -> CompareDeps:
    """Wire the real registry, pricing, estimator, ledger and API providers.

    Loads ``base_dir / ".env"`` into the environment (existing variables win) without
    printing anything.

    Args:
        base_dir: Directory holding ``.env`` and ``config/``.
    """
    # Imported here so that importing this module never requires the vendor SDKs.
    from dotenv import load_dotenv  # noqa: PLC0415

    from llm_crossjury.core.providers.api import (  # noqa: PLC0415
        AnthropicApiProvider,
        GeminiApiProvider,
        OpenAIApiProvider,
        XaiApiProvider,
    )

    load_dotenv(base_dir / ".env", override=False)
    registry = ModelRegistry.load(resolve_models_path(base_dir))
    pricing = PricingTable(registry)
    cost_config = CostConfig()
    estimator = CostEstimator(
        pricing,
        cache_hit_rate=0.0,  # one call per vendor: no shared cache entries
        reasoning_multipliers=dict(cost_config.reasoning_multipliers),
        currency=BudgetConfig().currency,
    )
    api_providers = (
        AnthropicApiProvider(registry=registry),
        OpenAIApiProvider(registry=registry),
        GeminiApiProvider(registry=registry),
        XaiApiProvider(registry=registry),
    )
    return CompareDeps(
        providers={p.name: p for p in api_providers},
        key_envs={p.name: p.api_key_env for p in api_providers},
        registry=registry,
        resolver=ModelResolver(registry, ResolutionMode.NORMAL),
        pricing=pricing,
        estimator=estimator,
        ledger=SqliteLedger(resolve_data_paths().ledger),
        budget=BudgetConfig(),
    )


# --------------------------------------------------------------------------- models


class UsableModel(_Frozen):
    """A model offered in the UI: a verified, priced ``api`` registry entry."""

    model_id: str
    aliases: tuple[str, ...]


def usable_models(provider: str, deps: CompareDeps) -> tuple[UsableModel, ...]:
    """Verified ``api`` entries of ``provider`` with a usable price, plus alias labels."""
    alias_map: dict[str, list[str]] = {}
    for alias in ALIAS_NAMES:
        try:
            target = deps.resolver.resolve_alias(provider, alias)
        except Exception:  # noqa: S112 - an unset alias is simply not offered
            continue
        if target != alias:
            alias_map.setdefault(target, []).append(alias)

    models: list[UsableModel] = []
    for entry in deps.registry.entries():
        if entry.provider != provider or Backend.API not in entry.backends:
            continue
        try:
            deps.pricing.lookup(provider, entry.model_id)
        except PricingError:
            continue
        models.append(
            UsableModel(model_id=entry.model_id, aliases=tuple(alias_map.get(entry.model_id, ())))
        )
    return tuple(models)


# --------------------------------------------------------------------------- prompts


@dataclass(frozen=True)
class PromptTemplate:
    """A parsed ``prompts/<id>.md`` file."""

    prompt_id: str
    stable_prefix: str
    variable_suffix: str

    def render(self, task: str) -> Prompt:
        """Build the :class:`Prompt`: the prefix as a system message, the suffix (with
        the task substituted) as a user message."""
        return Prompt(
            stable_prefix=(Message(role=Role.SYSTEM, content=self.stable_prefix),),
            variable_suffix=(
                Message(
                    role=Role.USER, content=self.variable_suffix.replace(_PROMPT_VARIABLE, task)
                ),
            ),
        )


@lru_cache(maxsize=8)
def load_prompt_template(path: Path) -> PromptTemplate:
    """Parse a prompt template: front matter, then ``## stable_prefix`` and
    ``## variable_suffix`` sections.

    Raises:
        ValueError: if the file does not follow the template conventions.
    """
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) != 3 or parts[0].strip():  # noqa: PLR2004
        raise ValueError(f"{path.name}: missing front matter")
    front, body = parts[1], parts[2]
    prompt_id = ""
    for line in front.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "id":
            prompt_id = value.strip()
    prefix_marker, suffix_marker = "## stable_prefix", "## variable_suffix"
    if prefix_marker not in body or suffix_marker not in body:
        raise ValueError(f"{path.name}: missing stable_prefix or variable_suffix section")
    _, _, rest = body.partition(prefix_marker)
    prefix, _, suffix = rest.partition(suffix_marker)
    if _PROMPT_VARIABLE not in suffix:
        raise ValueError(f"{path.name}: variable_suffix must contain {_PROMPT_VARIABLE}")
    return PromptTemplate(
        prompt_id=prompt_id, stable_prefix=prefix.strip(), variable_suffix=suffix.strip()
    )


def prefix_fingerprint(prompt: Prompt) -> str:
    """SHA-256 of the serialized stable prefix.

    Computed here because ``Prompt.prefix_fingerprint`` is unimplemented in the
    frozen contract.
    """
    payload = json.dumps(
        [m.model_dump(mode="json") for m in prompt.stable_prefix],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- planning


@dataclass(frozen=True)
class PreparedCall:
    """A validated, resolved and priced member call, ready to estimate or run."""

    selection: Selection
    resolved: ResolvedModel
    pricing: Pricing
    request: CompletionRequest
    plan: CallPlan


def _validate(task: str, selections: Sequence[Selection], deps: CompareDeps) -> None:
    if not task.strip():
        raise SelectionError("The task is empty.")
    if not selections:
        raise SelectionError("Select at least one provider.")
    seen: set[str] = set()
    for sel in selections:
        if sel.provider not in deps.providers:
            raise SelectionError(f"Unknown provider {sel.provider!r}.")
        if sel.provider in seen:
            raise SelectionError(f"Provider {sel.provider!r} is selected more than once.")
        seen.add(sel.provider)


def prepare_calls(
    task: str, selections: Sequence[Selection], deps: CompareDeps
) -> list[PreparedCall]:
    """Validate, resolve and price every selection, and build its request and plan.

    Raises:
        SelectionError: invalid task or selections.
        ResolutionError: a model cannot be resolved.
        PricingError: a model has no verified price, or a price currency differs
            from the budget currency.
    """
    _validate(task, selections, deps)
    template = load_prompt_template(deps.prompts_dir / f"{PROMPT_ID}.md")
    prompt = template.render(task)
    fingerprint = prefix_fingerprint(prompt)
    prefix_tokens = sum(deps.count_tokens(m.content) for m in prompt.stable_prefix)
    suffix_tokens = sum(deps.count_tokens(m.content) for m in prompt.variable_suffix)

    prepared: list[PreparedCall] = []
    for sel in selections:
        resolved = deps.resolver.resolve(
            member_id=sel.provider,
            provider=sel.provider,
            model=sel.model,
            backend=Backend.API,
            role=ModelRole.ANSWERER,
            reasoning=ReasoningEffort.DEFAULT,
            native=None,
        )
        price = deps.pricing.lookup(sel.provider, resolved.model_id)
        if price.currency != deps.budget.currency:
            raise PricingError(
                f"{sel.provider}/{resolved.model_id} is priced in {price.currency}, "
                f"but the budget is in {deps.budget.currency}."
            )
        entry = deps.registry.get(sel.provider, resolved.model_id)
        limit = entry.max_output if entry is not None else None
        if limit is not None and sel.max_output_tokens > limit:
            raise SelectionError(
                f"{sel.provider}/{resolved.model_id}: max output tokens "
                f"{sel.max_output_tokens} exceeds the model limit {limit}."
            )
        request = CompletionRequest(
            model=resolved.model_id,
            prompt=prompt,
            response_schema=None,
            temperature=sel.temperature,
            max_output_tokens=sel.max_output_tokens,
            reasoning=resolved.effective_reasoning,
            native_params=dict(resolved.native_params),
            cache_prefix=False,
            timeout_s=DEFAULT_TIMEOUT_S,
            tags={"stage": StageName.ANSWER.value, "member": sel.provider},
        )
        plan = CallPlan(
            stage=StageName.ANSWER.value,
            provider=sel.provider,
            model=resolved.model_id,
            backend=Backend.API,
            reasoning=resolved.effective_reasoning,
            prefix_fingerprint=fingerprint,
            prefix_tokens=prefix_tokens,
            suffix_tokens=suffix_tokens,
            expected_output_tokens_low=max(1, sel.max_output_tokens // ESTIMATE_LOW_OUTPUT_DIVISOR),
            expected_output_tokens_high=sel.max_output_tokens,
            batched=False,
        )
        prepared.append(
            PreparedCall(
                selection=sel, resolved=resolved, pricing=price, request=request, plan=plan
            )
        )
    return prepared


def _estimate_prepared(prepared: Sequence[PreparedCall], deps: CompareDeps) -> CostEstimate:
    return deps.estimator.estimate([p.plan for p in prepared], [p.resolved for p in prepared])


async def estimate_compare(
    task: str, selections: Sequence[Selection], deps: CompareDeps
) -> CostEstimate:
    """Estimate a compare run without making any model call.

    Raises:
        SelectionError, ResolutionError, PricingError: see :func:`prepare_calls`.
    """
    return _estimate_prepared(prepare_calls(task, selections, deps), deps)


def budget_status(estimate: CostEstimate, deps: CompareDeps) -> BudgetStatus:
    """Compare ``estimate`` with the budget and today's ledger spend."""
    budget = deps.budget
    spent_today = deps.ledger.spent_on(date.today())  # local calendar day (SPEC §5)
    remaining = max(Decimal(0), budget.daily_limit - spent_today)
    high = estimate.total_high
    cur = budget.currency
    reason: str | None = None
    if estimate.unpriced_calls:
        reason = f"{estimate.unpriced_calls} call(s) cannot be priced."
    elif high > budget.run_limit:
        reason = (
            f"The high estimate ({cur} {high}) exceeds the run limit ({cur} {budget.run_limit})."
        )
    elif high > remaining:
        reason = (
            f"The high estimate ({cur} {high}) exceeds today's remaining budget "
            f"({cur} {remaining} of {cur} {budget.daily_limit})."
        )
    return BudgetStatus(
        currency=cur,
        run_limit=budget.run_limit,
        confirm_above=budget.confirm_above,
        daily_limit=budget.daily_limit,
        spent_today=spent_today,
        daily_remaining=remaining,
        estimate_low=estimate.total_low,
        estimate_high=high,
        needs_confirmation=reason is None and high > budget.confirm_above,
        refused=reason is not None,
        refusal_reason=reason,
    )


# --------------------------------------------------------------------------- running


def _budget(deps: CompareDeps) -> Budget:
    return Budget(
        run_limit=deps.budget.run_limit,
        daily_limit=deps.budget.daily_limit,
        currency=deps.budget.currency,
        allow_unknown_cost=False,
    )


def _error_result(call: PreparedCall, currency: str, error: str) -> MemberResult:
    return MemberResult(
        provider=call.selection.provider,
        model=call.resolved.model_id,
        requested_model=call.selection.model,
        text=None,
        error=error,
        usage=None,
        cost=None,
        currency=currency,
        latency_s=None,
    )


@dataclass(frozen=True)
class BudgetedCallResult:
    """A completed call that was budget-checked and recorded in the ledger."""

    result: CompletionResult
    cost: Decimal | None
    latency_s: float


async def execute_budgeted_call(
    *,
    run_id: str,
    stage: str,
    provider: str,
    resolved: ResolvedModel,
    pricing: Pricing,
    request: CompletionRequest,
    call_high: Decimal,
    deps: CompareDeps,
) -> BudgetedCallResult:
    """Check the budget, reserve, call, record, release (SPEC section 5).

    The ledger check and the reservation happen without an ``await`` in between, so
    concurrent calls cannot both pass the check against the same headroom. The
    reservation is released only after the call is recorded, so spent + reserved
    never dips.

    Args:
        run_id: Run the call belongs to (per-run limit and ledger grouping).
        stage: Stage name recorded in the ledger.
        provider: Provider id (key into ``deps.providers``).
        resolved: The resolved model.
        pricing: Its verified price.
        request: The request to send; ``run_id`` is added to its tags.
        call_high: High cost estimate of this call, checked and reserved.
        deps: Compare dependencies.

    Raises:
        BudgetExceededError: the call could exceed a limit; it is not started.
        ProviderError: the vendor call failed (nothing is recorded).
        Exception: anything else the adapter raises, unchanged.
    """
    ledger = deps.ledger
    ledger.check(run_id, call_high, _budget(deps))
    reservation = ledger.reserve(run_id, call_high)
    try:
        tagged = request.model_copy(update={"tags": {**request.tags, "run_id": run_id}})
        started = time.perf_counter()
        result = await deps.providers[provider].complete(tagged)
        latency = time.perf_counter() - started
        cost = deps.compute_cost(result.usage, pricing)
        ledger.record(
            LedgerEntry(
                run_id=run_id,
                stage=stage,
                provider=provider,
                model=resolved.model_id,
                backend=result.backend,
                reasoning=resolved.effective_reasoning,
                usage=result.usage,
                cost=cost,
                currency=pricing.currency,
                batched=False,
                duration_s=latency,
                recorded_at=datetime.now(UTC),
            )
        )
        return BudgetedCallResult(result=result, cost=cost, latency_s=latency)
    finally:
        ledger.release(reservation)


async def _run_member(
    run_id: str, call: PreparedCall, call_high: Decimal, deps: CompareDeps
) -> MemberResult:
    sel = call.selection
    currency = deps.budget.currency
    key_env = deps.key_envs.get(sel.provider)
    if key_env is not None and not key_is_set(key_env):
        return _error_result(call, currency, f"{key_env} is not set. Add it to .env.")
    try:
        done = await execute_budgeted_call(
            run_id=run_id,
            stage=StageName.ANSWER.value,
            provider=sel.provider,
            resolved=call.resolved,
            pricing=call.pricing,
            request=call.request,
            call_high=call_high,
            deps=deps,
        )
    except BudgetExceededError as exc:
        return _error_result(call, currency, f"Not started: {exc.scope} budget would be exceeded.")
    except ProviderError as exc:
        return _error_result(call, currency, str(exc) or type(exc).__name__)
    except Exception as exc:  # an adapter bug must not fail the other members
        # Only the type name: an unwrapped SDK message is not guaranteed secret-free.
        return _error_result(call, currency, f"Unexpected error ({type(exc).__name__}).")
    return MemberResult(
        provider=sel.provider,
        model=call.resolved.model_id,
        requested_model=sel.model,
        text=done.result.text,
        error=None,
        usage=done.result.usage,
        cost=done.cost,
        currency=call.pricing.currency,
        latency_s=done.latency_s,
    )


async def execute_calls(
    run_id: str, prepared: Sequence[PreparedCall], deps: CompareDeps
) -> CompareResult:
    """Run prepared calls concurrently; each member's failure stays in its own entry.

    No run-level pre-check happens here (see :func:`run_compare`); each call is still
    checked against the ledger before it starts.
    """
    highs = [_estimate_prepared([call], deps).total_high for call in prepared]
    members = await asyncio.gather(
        *(_run_member(run_id, call, high, deps) for call, high in zip(prepared, highs, strict=True))
    )
    known = [m.cost for m in members if m.cost is not None]
    return CompareResult(
        run_id=run_id,
        prompt_id=PROMPT_ID,
        currency=deps.budget.currency,
        members=tuple(members),
        total_cost=sum(known, Decimal(0)),
        unknown_cost_members=sum(1 for m in members if m.error is None and m.cost is None),
    )


def new_run_id() -> str:
    """Return a fresh opaque run id."""
    return uuid.uuid4().hex


async def run_compare(
    task: str,
    selections: Sequence[Selection],
    deps: CompareDeps,
    *,
    run_id: str | None = None,
) -> CompareResult:
    """Estimate, check the budget, then run every selected member concurrently.

    Raises:
        SelectionError, ResolutionError, PricingError: see :func:`prepare_calls`.
        BudgetRefusedError: the high estimate exceeds the run limit or today's
            remaining budget. No call is made.
    """
    prepared = prepare_calls(task, selections, deps)
    status = budget_status(_estimate_prepared(prepared, deps), deps)
    if status.refused:
        raise BudgetRefusedError(status.refusal_reason or "Refused by the budget.", status)
    return await execute_calls(run_id or new_run_id(), prepared, deps)
