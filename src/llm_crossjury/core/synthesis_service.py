"""AI synthesis service: step 2 of a Compare run (agreement, review, chair).

This module is the core API behind the synthesis panel of the web UI. It holds no web
or CLI code and reuses :class:`~llm_crossjury.core.compare_service.CompareDeps`.

The API is **stateless**: every request carries the step-1 material (the task and the
successful answers), the label seed, the user's settings and the outputs of earlier
stages. The server keeps no session state.

Flow of one "AI synthesize" click:

1. :func:`run_agreement_step` labels and (when enabled) scrubs the answers, then runs
   the cheap agreement judge. A new run id is returned; the client sends it back so
   every call of the click shares it (the per-run budget limit applies to all of
   them). When the agreement ratio is at least :data:`AGREEMENT_SKIP_THRESHOLD`, the
   result suggests skipping the review.
2. :func:`run_finish_step` runs the peer review (unless skipped) and then the chair.

:func:`run_resynthesis` re-runs only the chair with the user's rulings (a new run).

Model choices (maintainer decision):

* chair: chosen by the user; default is the step-1 model with the highest output
  price (:func:`default_chair`);
* agreement judge: the chair provider's ``fast`` alias, falling back to the chair model;
* reviewers: tier ``economy`` uses each answer provider's ``fast`` alias (falling back
  to the step-1 model), tier ``standard`` uses the step-1 models.

An alias "falls back" when it is unset or not reachable on the provider's backend: on
``api`` it needs a verified price, on ``cli`` a registry cli entry with ``cli_flags``.

Backends: every answer carries the backend that produced it (``api`` = API key,
``cli`` = the vendor CLI the user signed in to). Within one request each provider uses
one backend, so the judge, reviewers and chair of a provider go through the same
backend as its step-1 answer (the chair may name its backend if its provider gave no
answer). ``cli`` calls have unknown cost: a request that makes any needs
``allow_unknown_cost`` (:class:`~llm_crossjury.core.compare_service.ConsentRequiredError`
otherwise), and they are excluded from the budget limits but recorded in the ledger.

Budget: before any call of a request, the high estimate of the ``api`` calls the
request will run is checked against the run limit and today's remaining budget (SPEC
section 5); the agreement step also counts the chair, since a click always ends with
the chair. Each ``api`` call is then checked against the ledger before it starts
(:func:`~llm_crossjury.core.compare_service.execute_budgeted_call`).

Stage functions are always called through their modules (``agreement.run_agreement``
and so on), so tests can replace them.
"""

from __future__ import annotations

import random
import secrets
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from llm_crossjury.core.anonymize import scrub, scrub_enabled_for
from llm_crossjury.core.compare_service import (
    DEFAULT_TIMEOUT_S,
    BudgetRefusedError,
    BudgetStatus,
    CompareDeps,
    CompareError,
    SelectionError,
    budget_status,
    checked_price,
    cli_call_count,
    execute_budgeted_call,
    is_cli_usable,
    key_is_set,
    new_run_id,
    provider_names,
    require_consent,
)
from llm_crossjury.core.cost.estimate import CallPlan, CostEstimate
from llm_crossjury.core.cost.ledger import BudgetExceededError
from llm_crossjury.core.cost.pricing import PricingError
from llm_crossjury.core.providers.base import (
    Backend,
    CompletionRequest,
    CompletionResult,
    ProviderError,
    ProviderUnavailableError,
    ReasoningEffort,
    Usage,
)
from llm_crossjury.core.registry.resolver import ResolutionError, ResolvedModel
from llm_crossjury.core.registry.resolver import Role as ModelRole
from llm_crossjury.core.schemas.synthesis import Synthesis
from llm_crossjury.core.synthesis import agreement, chair, review
from llm_crossjury.core.synthesis.agreement import AgreementOutcome
from llm_crossjury.core.synthesis.chair import ChairOutcome, Ruling
from llm_crossjury.core.synthesis.llm import StructuredOutputError
from llm_crossjury.core.synthesis.review import ReviewOutcome
from llm_crossjury.core.synthesis.types import (
    AnonAnswer,
    CallFn,
    CallSpec,
    ModelChoice,
    PlannedCall,
)

AGREEMENT_SKIP_THRESHOLD = 0.8
"""Product setting: at or above this agreement ratio, the UI offers to skip review."""

MIN_ANSWERS = 2
MAX_ANSWERS = 26
MAX_SEED = 2**31 - 1
FAST_ALIAS = "fast"

ReviewTier = Literal["economy", "standard"]
StageId = Literal["agreement", "review", "chair"]

_STAGE_ROLES: dict[str, ModelRole] = {
    agreement.STAGE: ModelRole.AGREEMENT_JUDGE,
    review.STAGE: ModelRole.REVIEWER,
    chair.STAGE: ModelRole.CHAIRMAN,
}
_RUN_ID_PATTERN = r"^[0-9a-f]{32}$"


# --------------------------------------------------------------------------- errors


class SynthesisStageError(CompareError):
    """A stage failed after it started (vendor error, invalid output, budget stop).

    Attributes:
        stage: The failing stage.
        code: ``provider_error`` | ``invalid_output`` | ``budget_stopped``.
    """

    def __init__(self, stage: str, code: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code


# --------------------------------------------------------------------------- inputs


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StepOneAnswer(_Frozen):
    """A successful step-1 answer, posted back by the client."""

    provider: str = Field(min_length=1)
    backend: Backend = Backend.API
    """The backend that produced the answer; the provider's later calls use it too."""
    model: str = Field(min_length=1)
    """The resolved model id that produced the answer."""
    text: str = Field(min_length=1)


class SynthesisRequest(_Frozen):
    """The step-1 material and the user's step-2 settings (shared by every call)."""

    task: str
    answers: tuple[StepOneAnswer, ...]
    seed: int | None = Field(default=None, ge=0, le=MAX_SEED)
    """Label shuffle seed. ``None`` picks a new one (returned in every result)."""
    chair: ModelChoice | None = None
    """``None`` = :func:`default_chair`."""
    chair_backend: Backend | None = None
    """Backend of an explicit chair. ``None`` = the backend of that provider's answer,
    else ``api``. It must match the backend of that provider's answer, if any."""
    review_tier: ReviewTier = "economy"
    extra_instructions: str | None = None
    """Comparison criteria or a follow-up query for the reviewers and the chair."""
    allow_unknown_cost: bool = False
    """Consent to ``cli`` calls of unknown cost (required when any call uses ``cli``)."""


class FinishRequest(SynthesisRequest):
    """Input of :func:`run_finish_step`."""

    run_id: str | None = Field(default=None, pattern=_RUN_ID_PATTERN)
    """The run id returned by the agreement step (``None`` = a new run)."""
    agreement: AgreementOutcome | None = None
    skip_review: bool = False


class ResynthesisRequest(SynthesisRequest):
    """Input of :func:`estimate_resynthesis` and :func:`run_resynthesis`."""

    agreement: AgreementOutcome | None = None
    reviews: ReviewOutcome | None = None
    previous: Synthesis
    rulings: tuple[Ruling, ...] = ()


# --------------------------------------------------------------------------- outputs


class LabelInfo(_Frozen):
    """Which provider and model wrote the answer behind an anonymous label."""

    label: str
    provider: str
    model: str


class Reviewer(_Frozen):
    """The model reviewing on behalf of the answer ``label``."""

    label: str
    model: ModelChoice
    backend: Backend = Backend.API


class StageEstimate(_Frozen):
    """Estimate of one stage."""

    stage: StageId
    estimate: CostEstimate


class SynthesisEstimate(_Frozen):
    """Per-stage estimate of a full synthesis, and the models it would use."""

    seed: int
    currency: str
    chair: ModelChoice
    chair_backend: Backend
    """Backend of the chair and of the agreement judge."""
    judge: ModelChoice
    reviewers: tuple[Reviewer, ...]
    stages: tuple[StageEstimate, ...]
    total_low: Decimal
    total_high: Decimal
    """Range of the priced (``api``) calls only."""
    cli_calls: int
    """Calls on the ``cli`` backend (unknown cost, counted against the user's plan)."""
    budget: BudgetStatus
    """Verdict for the whole pipeline (agreement, review and chair)."""
    scrubbed: bool


class CallCost(_Frozen):
    """One completed call of a step."""

    stage: str
    provider: str
    backend: Backend
    model: str
    tag: str
    usage: Usage
    cost: Decimal | None
    """``None`` when unknown (every ``cli`` call)."""
    duration_s: float


class StageCost(_Frozen):
    """Spend of one stage in a step."""

    stage: str
    calls: int
    cost: Decimal
    """Sum of known costs."""
    unknown_cost_calls: int


class _StepResult(_Frozen):
    run_id: str
    seed: int
    currency: str
    labels: tuple[LabelInfo, ...]
    scrubbed: bool
    replacements: int
    """Number of model/vendor names replaced in the answers."""
    stage_costs: tuple[StageCost, ...]
    total_cost: Decimal
    calls: tuple[CallCost, ...]


class AgreementStepResult(_StepResult):
    """Result of :func:`run_agreement_step`."""

    agreement: AgreementOutcome
    threshold: float
    suggest_skip: bool
    """The ratio is at least :attr:`threshold`: offer to skip the review."""
    review_estimate: CostEstimate
    """Estimate of the review stage (what skipping saves)."""
    chair_estimate: CostEstimate


class FinishStepResult(_StepResult):
    """Result of :func:`run_finish_step`."""

    reviews: ReviewOutcome | None
    review_skipped: bool
    chair: ChairOutcome


class ResynthesisResult(_StepResult):
    """Result of :func:`run_resynthesis`."""

    chair: ChairOutcome


# --------------------------------------------------------------------------- planning


class SynthesisPlan(_Frozen):
    """The labelled answers and the models a request uses."""

    seed: int
    answers: tuple[AnonAnswer, ...]
    labels: tuple[LabelInfo, ...]
    scrubbed: bool
    replacements: int
    chair: ModelChoice
    judge: ModelChoice
    reviewers: dict[str, ModelChoice]
    """Label of the reviewer's own answer -> reviewing model."""
    backends: dict[str, Backend]
    """Provider id -> the backend every call of that provider uses."""
    task: str
    extra_instructions: str | None


def label_name(index: int) -> str:
    """Return the anonymous label for position ``index`` (``Response A``, ...)."""
    return f"Response {chr(ord('A') + index)}"


def _validate_request(req: SynthesisRequest, deps: CompareDeps) -> None:
    if not req.task.strip():
        raise SelectionError("The task is empty.")
    if len(req.answers) < MIN_ANSWERS:
        raise SelectionError(f"AI synthesis needs at least {MIN_ANSWERS} successful answers.")
    if len(req.answers) > MAX_ANSWERS:
        raise SelectionError(f"AI synthesis supports at most {MAX_ANSWERS} answers.")
    names = provider_names(deps)
    for answer in req.answers:
        if answer.provider not in names:
            raise SelectionError(f"Unknown provider {answer.provider!r}.")
        if (answer.provider, answer.backend) not in deps.providers:
            raise SelectionError(
                f"Provider {answer.provider!r} is not available on the "
                f"{answer.backend.value} backend."
            )
        if not answer.text.strip():
            raise SelectionError(f"The answer of {answer.provider!r} is empty.")


def _answer_backends(answers: Sequence[StepOneAnswer]) -> dict[str, Backend]:
    backends: dict[str, Backend] = {}
    for answer in answers:
        known = backends.setdefault(answer.provider, answer.backend)
        if known is not answer.backend:
            raise SelectionError(
                f"Provider {answer.provider!r} answered through two backends; "
                "AI synthesis needs one backend per provider."
            )
    return backends


def _reachable(provider: str, model_id: str, backend: Backend, deps: CompareDeps) -> bool:
    """Whether ``model_id`` can be called on ``backend`` (priced api, or a cli entry)."""
    if backend is Backend.CLI:
        return is_cli_usable(deps.registry.get(provider, model_id))
    try:
        checked_price(provider, model_id, backend, deps)
    except PricingError:
        return False
    return True


def _alias_or(provider: str, fallback: str, backend: Backend, deps: CompareDeps) -> str:
    """The provider's ``fast`` alias target, or ``fallback`` if the alias is unset or
    its model is not reachable on ``backend``."""
    try:
        target = deps.resolver.resolve_alias(provider, FAST_ALIAS)
    except ResolutionError:
        return fallback
    return target if _reachable(provider, target, backend, deps) else fallback


def default_chair(answers: Sequence[StepOneAnswer], deps: CompareDeps) -> ModelChoice:
    """Return the ``api`` step-1 model with the highest output price (first one on
    ties). When no answer came from a priced ``api`` model, return the first answer's
    ``cli`` model.

    Raises:
        SelectionError: if no answer model has a verified price or a cli entry.
    """
    best: tuple[Decimal, ModelChoice] | None = None
    for answer in answers:
        if answer.backend is Backend.CLI:
            continue
        try:
            price = checked_price(answer.provider, answer.model, answer.backend, deps)
        except PricingError:
            continue
        if price is not None and (best is None or price.output_per_mtok > best[0]):
            best = (
                price.output_per_mtok,
                ModelChoice(provider=answer.provider, model_id=answer.model),
            )
    if best is not None:
        return best[1]
    for answer in answers:
        if answer.backend is Backend.CLI and _reachable(
            answer.provider, answer.model, Backend.CLI, deps
        ):
            return ModelChoice(provider=answer.provider, model_id=answer.model)
    raise SelectionError("No step-1 model has a verified price; pick a chair model.")


def _chair(
    req: SynthesisRequest, backends: dict[str, Backend], deps: CompareDeps
) -> tuple[ModelChoice, Backend]:
    """Pick the chair and its backend; adds the chair provider to ``backends``."""
    if req.chair is None:
        choice = default_chair(req.answers, deps)
        return choice, backends.get(choice.provider, Backend.API)
    provider = req.chair.provider
    if provider not in provider_names(deps):
        raise SelectionError(f"Unknown chair provider {provider!r}.")
    answered = backends.get(provider)
    backend = req.chair_backend or answered or Backend.API
    if answered is not None and backend is not answered:
        raise SelectionError(
            f"The chair must use the {answered.value} backend, like the answer of {provider!r}."
        )
    if (provider, backend) not in deps.providers:
        raise SelectionError(f"Provider {provider!r} is not available on the {backend} backend.")
    model_id = deps.resolver.resolve_alias(provider, req.chair.model_id)
    checked_price(provider, model_id, backend, deps)
    backends[provider] = backend
    return ModelChoice(provider=provider, model_id=model_id), backend


def _anonymize(
    req: SynthesisRequest, seed: int, scrubbing: bool
) -> tuple[tuple[AnonAnswer, ...], tuple[LabelInfo, ...], tuple[StepOneAnswer, ...], int]:
    # Canonical order first, so the labels depend only on the seed and the answers,
    # never on the order the client posts them in.
    ordered = sorted(req.answers, key=lambda a: (a.provider, a.model, a.text))
    random.Random(seed).shuffle(ordered)  # noqa: S311 - not security sensitive
    anon: list[AnonAnswer] = []
    labels: list[LabelInfo] = []
    replacements = 0
    for index, answer in enumerate(ordered):
        label = label_name(index)
        text = answer.text
        if scrubbing:
            result = scrub(text)
            text, replacements = result.text, replacements + result.replacements
        anon.append(AnonAnswer(label=label, text=text))
        labels.append(LabelInfo(label=label, provider=answer.provider, model=answer.model))
    return tuple(anon), tuple(labels), tuple(ordered), replacements


def plan_synthesis(req: SynthesisRequest, deps: CompareDeps) -> SynthesisPlan:
    """Validate the request, label and scrub the answers, and pick the models.

    Raises:
        SelectionError: invalid task, answers or chair (or a chair on a backend that
            differs from its provider's answer, or a cli chair without cli_flags).
        ResolutionError: the chair model cannot be resolved.
        PricingError: an ``api`` chair model has no verified price.
    """
    _validate_request(req, deps)
    backends = _answer_backends(req.answers)
    seed = req.seed if req.seed is not None else secrets.randbelow(MAX_SEED + 1)
    extra = (req.extra_instructions or "").strip() or None
    scrubbing = scrub_enabled_for([req.task, extra or ""], True)
    answers, labels, ordered, replacements = _anonymize(req, seed, scrubbing)
    chair_model, chair_backend = _chair(req, backends, deps)
    judge = ModelChoice(
        provider=chair_model.provider,
        model_id=_alias_or(chair_model.provider, chair_model.model_id, chair_backend, deps),
    )
    reviewers: dict[str, ModelChoice] = {}
    for info, answer in zip(labels, ordered, strict=True):
        model_id = (
            _alias_or(answer.provider, answer.model, answer.backend, deps)
            if req.review_tier == "economy"
            else answer.model
        )
        reviewers[info.label] = ModelChoice(provider=answer.provider, model_id=model_id)
    return SynthesisPlan(
        seed=seed,
        answers=answers,
        labels=labels,
        scrubbed=scrubbing,
        replacements=replacements,
        chair=chair_model,
        judge=judge,
        reviewers=reviewers,
        backends=backends,
        task=req.task,
        extra_instructions=extra,
    )


# --------------------------------------------------------------------------- estimates


def _resolve(
    provider: str, model_id: str, stage: str, member: str, *, backend: Backend, deps: CompareDeps
) -> ResolvedModel:
    if (provider, backend) not in deps.providers:
        raise SelectionError(f"Unknown provider {provider!r} on the {backend.value} backend.")
    return deps.resolver.resolve(
        member_id=member,
        provider=provider,
        model=model_id,
        backend=backend,
        role=_STAGE_ROLES.get(stage, ModelRole.ANSWERER),
        reasoning=ReasoningEffort.DEFAULT,
        native=None,
    )


def _call_plan(planned: PlannedCall, resolved: ResolvedModel) -> CallPlan:
    return CallPlan(
        stage=planned.stage,
        provider=planned.provider,
        model=resolved.model_id,
        backend=resolved.backend,
        reasoning=resolved.effective_reasoning,
        # Unique per call: the estimate never assumes a cache discount (conservative).
        prefix_fingerprint=f"synthesis-{uuid.uuid4().hex}",
        prefix_tokens=planned.input_tokens,
        suffix_tokens=0,
        expected_output_tokens_low=planned.output_tokens_low,
        expected_output_tokens_high=planned.output_tokens_high,
        batched=False,
    )


def estimate_planned(
    planned: Sequence[PlannedCall],
    deps: CompareDeps,
    backends: Mapping[str, Backend] | None = None,
) -> CostEstimate:
    """Turn stage :class:`PlannedCall` objects into a :class:`CostEstimate`.

    Args:
        planned: The calls.
        deps: Compare dependencies.
        backends: Provider id -> backend (default ``api``). ``cli`` calls are
            unpriced lines in the estimate.

    Raises:
        SelectionError, ResolutionError, PricingError: a call cannot be resolved, an
            ``api`` call has no verified price, or a ``cli`` call has no cli entry.
    """
    chosen = backends or {}
    plans: list[CallPlan] = []
    resolved_all: list[ResolvedModel] = []
    for index, call in enumerate(planned):
        backend = chosen.get(call.provider, Backend.API)
        member = f"{call.stage}-{index}"
        resolved = _resolve(
            call.provider, call.model_id, call.stage, member, backend=backend, deps=deps
        )
        checked_price(call.provider, resolved.model_id, backend, deps)
        plans.append(_call_plan(call, resolved))
        resolved_all.append(resolved)
    return deps.estimator.estimate(plans, resolved_all)


def _agreement_calls(plan: SynthesisPlan) -> list[PlannedCall]:
    return agreement.estimate_agreement(plan.answers, plan.judge, task=plan.task)


def _review_calls(plan: SynthesisPlan, focus: AgreementOutcome | None) -> list[PlannedCall]:
    return review.estimate_reviews(
        plan.answers,
        plan.reviewers,
        task=plan.task,
        focus=focus,
        extra_instructions=plan.extra_instructions,
    )


def _chair_calls(
    plan: SynthesisPlan, *, with_reviews: bool, resynthesis: bool = False
) -> list[PlannedCall]:
    return chair.estimate_chair(
        plan.answers,
        plan.chair,
        task=plan.task,
        with_reviews=with_reviews,
        extra_instructions=plan.extra_instructions,
        resynthesis=resynthesis,
    )


def _refuse_if_over(
    planned: Sequence[PlannedCall], plan: SynthesisPlan, req: SynthesisRequest, deps: CompareDeps
) -> None:
    estimate = estimate_planned(planned, deps, plan.backends)
    require_consent(cli_call_count(estimate), req.allow_unknown_cost)
    status = budget_status(estimate, deps)
    if status.refused:
        raise BudgetRefusedError(status.refusal_reason or "Refused by the budget.", status)


def _reviewer_list(plan: SynthesisPlan) -> tuple[Reviewer, ...]:
    return tuple(
        Reviewer(label=label, model=model, backend=plan.backends.get(model.provider, Backend.API))
        for label, model in plan.reviewers.items()
    )


def estimate_synthesis(req: SynthesisRequest, deps: CompareDeps) -> SynthesisEstimate:
    """Estimate agreement, review and chair, without making any model call.

    Raises:
        SelectionError, ResolutionError, PricingError: see :func:`plan_synthesis`.
    """
    plan = plan_synthesis(req, deps)
    per_stage: list[tuple[StageId, list[PlannedCall]]] = [
        ("agreement", _agreement_calls(plan)),
        ("review", _review_calls(plan, None)),
        ("chair", _chair_calls(plan, with_reviews=True)),
    ]
    stages = tuple(
        StageEstimate(stage=stage, estimate=estimate_planned(calls, deps, plan.backends))
        for stage, calls in per_stage
    )
    total = estimate_planned([c for _, calls in per_stage for c in calls], deps, plan.backends)
    return SynthesisEstimate(
        seed=plan.seed,
        currency=total.currency or deps.budget.currency,
        chair=plan.chair,
        chair_backend=plan.backends.get(plan.chair.provider, Backend.API),
        judge=plan.judge,
        reviewers=_reviewer_list(plan),
        stages=stages,
        total_low=total.total_low,
        total_high=total.total_high,
        cli_calls=cli_call_count(total),
        budget=budget_status(total, deps),
        scrubbed=plan.scrubbed,
    )


def estimate_resynthesis(req: ResynthesisRequest, deps: CompareDeps) -> CostEstimate:
    """Estimate the single chair call of a re-synthesis.

    Raises:
        SelectionError, ResolutionError, PricingError: see :func:`plan_synthesis`.
    """
    plan = plan_synthesis(req, deps)
    return estimate_planned(
        _chair_calls(plan, with_reviews=req.reviews is not None, resynthesis=True),
        deps,
        plan.backends,
    )


# --------------------------------------------------------------------------- calls


def _call_input_text(spec: CallSpec) -> str:
    return "\n".join(m.content for m in (*spec.prompt.stable_prefix, *spec.prompt.variable_suffix))


def make_call_fn(
    run_id: str,
    deps: CompareDeps,
    records: list[CallCost],
    backends: Mapping[str, Backend] | None = None,
) -> CallFn:
    """Build the :data:`CallFn` the stages use.

    Each call is dispatched to its provider's backend (``backends``, default
    ``api``). It resolves its model (reasoning ``default``); an ``api`` call looks up
    the verified price, checks the ledger with the call's high estimate, reserves,
    calls, records and releases; a ``cli`` call (unknown cost, consent obtained by
    the caller) goes through the vendor CLI and is recorded with ``cost=None``. Every
    completed call is appended to ``records``.

    Raises (from the returned function):
        ProviderUnavailableError: the provider's key is not set (``api``), or the
            CLI is unavailable.
        ProviderError: the vendor call failed.
        BudgetExceededError: the call could exceed a budget limit; it is not started.
        ResolutionError, PricingError, SelectionError: the model is unusable.
    """
    chosen = dict(backends or {})

    async def call(spec: CallSpec) -> CompletionResult:
        backend = chosen.get(spec.provider, Backend.API)
        key_env = deps.key_envs.get(spec.provider) if backend is Backend.API else None
        if key_env is not None and not key_is_set(key_env):
            raise ProviderUnavailableError(f"{key_env} is not set. Add it to .env.")
        member = spec.tag or spec.stage
        resolved = _resolve(
            spec.provider, spec.model_id, spec.stage, member, backend=backend, deps=deps
        )
        price = checked_price(spec.provider, resolved.model_id, backend, deps)
        call_high = Decimal(0)
        if price is not None:
            planned = PlannedCall(
                stage=spec.stage,
                provider=spec.provider,
                model_id=resolved.model_id,
                input_tokens=deps.count_tokens(_call_input_text(spec)),
                output_tokens_low=spec.max_output_tokens,
                output_tokens_high=spec.max_output_tokens,
            )
            plan = _call_plan(planned, resolved)
            call_high = deps.estimator.estimate([plan], [resolved]).total_high
        tags = {"stage": spec.stage}
        if spec.tag:
            tags["tag"] = spec.tag
        request = CompletionRequest(
            model=resolved.model_id,
            prompt=spec.prompt,
            response_schema=spec.response_schema,
            temperature=None,
            max_output_tokens=spec.max_output_tokens,
            reasoning=resolved.effective_reasoning,
            native_params=dict(resolved.native_params),
            cache_prefix=True,
            timeout_s=DEFAULT_TIMEOUT_S,
            tags=tags,
        )
        done = await execute_budgeted_call(
            run_id=run_id,
            stage=spec.stage,
            provider=spec.provider,
            resolved=resolved,
            pricing=price,
            request=request,
            call_high=call_high,
            deps=deps,
        )
        records.append(
            CallCost(
                stage=spec.stage,
                provider=spec.provider,
                backend=backend,
                model=resolved.model_id,
                tag=spec.tag,
                usage=done.result.usage,
                cost=done.cost,
                duration_s=done.latency_s,
            )
        )
        return done.result

    return call


def _stage_error(stage: str, exc: Exception) -> SynthesisStageError:
    if isinstance(exc, BudgetExceededError):
        return SynthesisStageError(
            stage, "budget_stopped", f"Stopped before the {stage} stage: {exc.scope} budget."
        )
    if isinstance(exc, StructuredOutputError):
        return SynthesisStageError(
            stage, "invalid_output", f"The {stage} model returned unusable output: {exc}"
        )
    return SynthesisStageError(
        stage, "provider_error", f"The {stage} call failed: {exc or type(exc).__name__}"
    )


def _stage_costs(records: Sequence[CallCost]) -> tuple[StageCost, ...]:
    order: dict[str, list[CallCost]] = {}
    for record in records:
        order.setdefault(record.stage, []).append(record)
    return tuple(
        StageCost(
            stage=stage,
            calls=len(items),
            cost=sum((r.cost for r in items if r.cost is not None), Decimal(0)),
            unknown_cost_calls=sum(1 for r in items if r.cost is None),
        )
        for stage, items in order.items()
    )


def _step_fields(
    run_id: str, plan: SynthesisPlan, deps: CompareDeps, records: Sequence[CallCost]
) -> dict[str, Any]:
    stage_costs = _stage_costs(records)
    return {
        "run_id": run_id,
        "seed": plan.seed,
        "currency": deps.budget.currency,
        "labels": plan.labels,
        "scrubbed": plan.scrubbed,
        "replacements": plan.replacements,
        "stage_costs": stage_costs,
        "total_cost": sum((s.cost for s in stage_costs), Decimal(0)),
        "calls": tuple(records),
    }


_STAGE_FAILURES = (ProviderError, StructuredOutputError, BudgetExceededError)


# --------------------------------------------------------------------------- steps


async def run_agreement_step(req: SynthesisRequest, deps: CompareDeps) -> AgreementStepResult:
    """Start a synthesis: label the answers and measure their agreement.

    Raises:
        SelectionError, ResolutionError, PricingError: see :func:`plan_synthesis`.
        ConsentRequiredError: a ``cli`` call without ``allow_unknown_cost``.
        BudgetRefusedError: agreement plus chair could exceed a budget limit.
        SynthesisStageError: the judge call failed or its output stayed invalid.
    """
    plan = plan_synthesis(req, deps)
    agreement_calls = _agreement_calls(plan)
    _refuse_if_over([*agreement_calls, *_chair_calls(plan, with_reviews=False)], plan, req, deps)
    run_id = new_run_id()
    records: list[CallCost] = []
    try:
        outcome = await agreement.run_agreement(
            plan.answers,
            plan.judge,
            task=plan.task,
            call=make_call_fn(run_id, deps, records, plan.backends),
        )
    except _STAGE_FAILURES as exc:
        raise _stage_error(agreement.STAGE, exc) from None
    ratio = outcome.ratio
    return AgreementStepResult(
        **_step_fields(run_id, plan, deps, records),
        agreement=outcome,
        threshold=AGREEMENT_SKIP_THRESHOLD,
        suggest_skip=ratio is not None and ratio >= AGREEMENT_SKIP_THRESHOLD,
        review_estimate=estimate_planned(_review_calls(plan, outcome), deps, plan.backends),
        chair_estimate=estimate_planned(_chair_calls(plan, with_reviews=True), deps, plan.backends),
    )


async def run_finish_step(req: FinishRequest, deps: CompareDeps) -> FinishStepResult:
    """Run the peer review (unless ``skip_review``) and then the chair.

    Raises:
        SelectionError, ResolutionError, PricingError: see :func:`plan_synthesis`.
        ConsentRequiredError: a ``cli`` call without ``allow_unknown_cost``.
        BudgetRefusedError: review plus chair could exceed a budget limit.
        SynthesisStageError: the chair failed (or a budget stop before it). A failing
            reviewer does not fail the step.
    """
    plan = plan_synthesis(req, deps)
    with_reviews = not req.skip_review
    planned = _chair_calls(plan, with_reviews=with_reviews)
    if with_reviews:
        planned = [*_review_calls(plan, req.agreement), *planned]
    _refuse_if_over(planned, plan, req, deps)
    run_id = req.run_id or new_run_id()
    records: list[CallCost] = []
    call = make_call_fn(run_id, deps, records, plan.backends)
    reviews: ReviewOutcome | None = None
    if with_reviews:
        try:
            reviews = await review.run_reviews(
                plan.answers,
                plan.reviewers,
                task=plan.task,
                focus=req.agreement,
                extra_instructions=plan.extra_instructions,
                call=call,
                seed=plan.seed,
            )
        except _STAGE_FAILURES as exc:
            raise _stage_error(review.STAGE, exc) from None
    try:
        outcome = await chair.run_chair(
            plan.answers,
            plan.chair,
            task=plan.task,
            agreement=req.agreement,
            reviews=reviews,
            extra_instructions=plan.extra_instructions,
            call=call,
        )
    except _STAGE_FAILURES as exc:
        raise _stage_error(chair.STAGE, exc) from None
    return FinishStepResult(
        **_step_fields(run_id, plan, deps, records),
        reviews=reviews,
        review_skipped=not with_reviews,
        chair=outcome,
    )


def validate_rulings(rulings: Sequence[Ruling], previous: Synthesis) -> None:
    """Check that every ruling points at an existing dispute position or insight.

    Raises:
        SelectionError: an unknown dispute id or label, a bad insight index, or a
            duplicate ruling.
    """
    disputes = {d.id: {p.label for p in d.positions} for d in previous.disputed}
    seen: set[tuple[str, str]] = set()
    for ruling in rulings:
        key = (ruling.kind, ruling.target)
        if key in seen:
            raise SelectionError(f"Duplicate ruling for {ruling.kind} {ruling.target!r}.")
        seen.add(key)
        if ruling.kind == "dispute":
            labels = disputes.get(ruling.target)
            if labels is None:
                raise SelectionError(f"Unknown dispute {ruling.target!r}.")
            if ruling.decision not in labels:
                raise SelectionError(
                    f"Dispute {ruling.target!r} has no position from {ruling.decision!r}."
                )
        else:
            if not ruling.target.isdigit() or int(ruling.target) >= len(previous.unique):
                raise SelectionError(f"Unknown unique insight {ruling.target!r}.")
            if ruling.decision != "include":
                raise SelectionError("A unique-insight ruling must be 'include'.")


async def run_resynthesis(req: ResynthesisRequest, deps: CompareDeps) -> ResynthesisResult:
    """Re-run only the chair with the user's rulings (a new run id).

    Raises:
        SelectionError: invalid request or rulings.
        ResolutionError, PricingError: see :func:`plan_synthesis`.
        ConsentRequiredError: a ``cli`` chair without ``allow_unknown_cost``.
        BudgetRefusedError: the chair call could exceed a budget limit.
        SynthesisStageError: the chair call failed or its output stayed invalid.
    """
    plan = plan_synthesis(req, deps)
    validate_rulings(req.rulings, req.previous)
    _refuse_if_over(
        _chair_calls(plan, with_reviews=req.reviews is not None, resynthesis=True),
        plan,
        req,
        deps,
    )
    run_id = new_run_id()
    records: list[CallCost] = []
    try:
        outcome = await chair.run_chair(
            plan.answers,
            plan.chair,
            task=plan.task,
            agreement=req.agreement,
            reviews=req.reviews,
            extra_instructions=plan.extra_instructions,
            call=make_call_fn(run_id, deps, records, plan.backends),
            previous=req.previous,
            rulings=req.rulings,
        )
    except _STAGE_FAILURES as exc:
        raise _stage_error(chair.STAGE, exc) from None
    return ResynthesisResult(
        **_step_fields(run_id, plan, deps, records),
        chair=outcome,
    )


__all__ = [
    "AGREEMENT_SKIP_THRESHOLD",
    "AgreementStepResult",
    "CallCost",
    "FinishRequest",
    "FinishStepResult",
    "LabelInfo",
    "ResynthesisRequest",
    "ResynthesisResult",
    "Reviewer",
    "StageCost",
    "StageEstimate",
    "StepOneAnswer",
    "SynthesisEstimate",
    "SynthesisPlan",
    "SynthesisRequest",
    "SynthesisStageError",
    "default_chair",
    "estimate_planned",
    "estimate_resynthesis",
    "estimate_synthesis",
    "label_name",
    "make_call_fn",
    "plan_synthesis",
    "run_agreement_step",
    "run_finish_step",
    "run_resynthesis",
    "validate_rulings",
]
