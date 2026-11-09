"""Chat service: one user message in a session, answered by one model (ADR 0008).

The prompt comes from the memory layer (:mod:`llm_crossjury.core.memory.context`):
stable prefix (system prompt and the session's attachments), summary and facts,
retrieved snippets, recent turns verbatim, the new message. Everything else follows
compare mode, so the rules are the same as for ``/api/run``:

* the model is resolved and, on ``api``, priced (a missing price refuses the call);
  the output cap defaults to ``DEFAULT_MAX_OUTPUT_TOKENS`` clamped to the registry
  ``max_output``;
* attachments are delivered natively or as fallback text, exactly like a compare
  member's (a file the model cannot receive refuses the message);
* a ``cli`` call needs consent to unknown cost (:class:`ConsentRequiredError`); it is
  a stateless call whose transcript is the rendered prompt (ADR 0002);
* the high estimate is checked against the budget before the call, and the call goes
  through :func:`execute_budgeted_call` (ledger stage ``chat``).

Failures after the pre-checks (missing key, vendor error, budget stop) never raise
from :func:`run_chat`: they become a turn with ``status == "error"``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from llm_crossjury.core.attachments.build import BuiltAttachment
from llm_crossjury.core.attachments.limits import AttachmentError
from llm_crossjury.core.attachments.prompt import receipts, refusal_message
from llm_crossjury.core.compare_service import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_TIMEOUT_S,
    ESTIMATE_LOW_OUTPUT_DIVISOR,
    MAX_ATTACHMENTS,
    BudgetRefusedError,
    BudgetStatus,
    CompareDeps,
    SelectionError,
    budget_status,
    checked_price,
    cli_call_count,
    execute_budgeted_call,
    key_is_set,
    load_attachments,
    new_run_id,
    prefix_fingerprint,
    provider_names,
    reasoning_within_cap,
    require_consent,
)
from llm_crossjury.core.config import BudgetConfig
from llm_crossjury.core.cost.estimate import CallPlan, CostEstimate
from llm_crossjury.core.cost.ledger import BudgetExceededError
from llm_crossjury.core.memory.context import (
    BuiltContext,
    ContextBreakdown,
    ContextTarget,
    build_context,
)
from llm_crossjury.core.memory.index import MemoryIndex
from llm_crossjury.core.memory.picker import pick_memory_model
from llm_crossjury.core.memory.summarizer import plan_fold, update_memory
from llm_crossjury.core.memory.turns import find_jury_turn, session_attachment_ids
from llm_crossjury.core.providers.base import (
    Backend,
    CompletionRequest,
    ProviderError,
    ReasoningEffort,
)
from llm_crossjury.core.registry.resolver import ResolvedModel
from llm_crossjury.core.registry.resolver import Role as ModelRole
from llm_crossjury.core.registry.schema import Pricing
from llm_crossjury.core.sessions.interfaces import (
    AppSettings,
    SessionNotFoundError,
    SessionStoreLike,
)
from llm_crossjury.core.sessions.models import (
    ChatTurn,
    ContinuationRef,
    MemoryState,
    ModelRef,
    Session,
    TurnStatus,
)
from llm_crossjury.core.synthesis.llm import describe_result
from llm_crossjury.core.synthesis_service import make_call_fn

logger = logging.getLogger(__name__)

CHAT_STAGE = "chat"
"""Ledger stage of chat calls."""

MAX_MESSAGE_CHARS = 200_000
"""Product setting: longest chat message accepted."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChatRequest(_Frozen):
    """One chat message (``POST /api/chat`` and ``/api/chat/estimate``)."""

    message: str = Field(max_length=MAX_MESSAGE_CHARS)
    model: ModelRef
    attachment_ids: tuple[str, ...] = Field(default=(), max_length=MAX_ATTACHMENTS)
    """Files attached to this message (``POST /api/attachments``)."""
    max_output_tokens: int | None = Field(default=None, ge=1)
    """``None`` = ``DEFAULT_MAX_OUTPUT_TOKENS``, clamped to the model's maximum."""
    continued_from: ContinuationRef | None = None
    allow_unknown_cost: bool = False
    """Consent to a ``cli`` call of unknown cost."""
    confirmed: bool = False
    """The user confirmed a call whose high estimate exceeds ``confirm_above``."""


class ChatEstimate(_Frozen):
    """Cost and context of one chat message, before it is sent."""

    currency: str
    total_low: Decimal
    total_high: Decimal
    """Range of a priced (``api``) call; zero for ``cli``."""
    cli_calls: int
    budget: BudgetStatus
    model: str
    """The resolved model id."""
    max_output_tokens: int
    """The effective output cap."""
    context_tokens: int
    """Estimated input tokens of the whole context."""
    context: ContextBreakdown
    context_budget_tokens: int
    """The effective context budget (setting, capped by the model's window)."""
    verbatim_turns: int
    """Earlier turns sent verbatim."""
    omitted_turns: int
    """Earlier turns only reachable through the summary and retrieval."""
    warnings: tuple[str, ...]
    assumptions: tuple[str, ...]


@dataclass(frozen=True)
class PreparedChat:
    """A validated, resolved, priced chat call with its context."""

    resolved: ResolvedModel
    pricing: Pricing | None
    request: CompletionRequest
    plan: CallPlan
    context: BuiltContext
    estimate: CostEstimate


def deps_with_settings(deps: CompareDeps, settings: AppSettings) -> CompareDeps:
    """``deps`` with the budget limits of ``settings`` (they replace ``BudgetConfig``'s)."""
    budget = BudgetConfig(
        run_limit=settings.budget.run_limit,
        confirm_above=settings.budget.confirm_above,
        daily_limit=settings.budget.daily_limit,
        currency=deps.budget.currency,
        allow_unknown_cost=deps.budget.allow_unknown_cost,
    )
    return replace(deps, budget=budget)


def _validate(
    session: Session, req: ChatRequest, deps: CompareDeps, *, allow_empty: bool = False
) -> None:
    if not allow_empty and not req.message.strip():
        raise SelectionError("The message is empty.")
    ref = req.model
    if ref.provider not in provider_names(deps):
        raise SelectionError(f"Unknown provider {ref.provider!r}.")
    if (ref.provider, ref.backend) not in deps.providers:
        raise SelectionError(
            f"Provider {ref.provider!r} is not available on the {ref.backend.value} backend."
        )
    source = req.continued_from
    if source is None:
        return
    turn = find_jury_turn(session, source.turn_id)
    if turn is None:
        raise SelectionError("The continued turn is not a jury turn of this session.")
    if source.source == "member":
        members = turn.step1.members if turn.step1 is not None else ()
        index = source.member_index
        # While step 1 runs, a finished member can be continued from members_done.
        if index is None or not (0 <= index < len(members) or index in turn.members_done):
            raise SelectionError("The continued member answer does not exist.")


def _session_attachments(
    session: Session, new: Sequence[BuiltAttachment], exclude_turn_id: str | None, deps: CompareDeps
) -> list[BuiltAttachment]:
    """The session's earlier attachments (missing ones skipped) followed by ``new``."""
    have = {b.attachment.sha256 for b in new}
    older: list[BuiltAttachment] = []
    store = deps.attachment_store
    if store is not None:
        for attachment_id in session_attachment_ids(session, exclude_turn_id):
            if attachment_id in have:
                continue
            try:
                older.append(store.get(attachment_id))
            except (AttachmentError, OSError, ValueError):
                continue  # expired or removed: the conversation goes on without it
            have.add(attachment_id)
    return [*older, *new]


def prepare_chat(
    session: Session,
    req: ChatRequest,
    deps: CompareDeps,
    *,
    settings: AppSettings,
    index: MemoryIndex | None = None,
    exclude_turn_id: str | None = None,
    allow_empty: bool = False,
) -> PreparedChat:
    """Validate, resolve, price and build the context of one chat message.

    Args:
        session: The session (it may already hold the pending turn ``exclude_turn_id``).
        req: The message.
        deps: Compare dependencies (budget from ``settings`` applied by the caller).
        settings: App settings (context budget).
        index: Memory search index.
        exclude_turn_id: The pending turn being answered.
        allow_empty: Accept an empty message (an estimate of the context alone, for
            the meter shown before anything is typed).

    Raises:
        SelectionError: empty message (unless ``allow_empty``), unknown
            provider/backend or continuation, a ``cli`` model without ``cli_flags``, a
            file the model cannot receive, or a context that cannot fit.
        ResolutionError: the model cannot be resolved.
        PricingError: an ``api`` model without a verified price.
        AttachmentError: an attachment id is unknown or too large.
    """
    _validate(session, req, deps, allow_empty=allow_empty)
    ref = req.model
    resolved = deps.resolver.resolve(
        member_id=CHAT_STAGE,
        provider=ref.provider,
        model=ref.model,
        backend=ref.backend,
        role=ModelRole.ANSWERER,
        reasoning=ReasoningEffort.DEFAULT,
        native=None,
    )
    price = checked_price(ref.provider, resolved.model_id, ref.backend, deps)
    entry = deps.registry.get(ref.provider, resolved.model_id)
    cap = req.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS
    if entry is not None and entry.max_output is not None:
        cap = min(cap, entry.max_output)
    caps = deps.providers[(ref.provider, ref.backend)].capabilities(resolved.model_id)

    new = load_attachments(req.attachment_ids, deps)
    refused = [r for r in receipts(new, caps.input_modalities, ref.backend) if r.mode == "refused"]
    if refused:
        raise SelectionError(
            refusal_message(
                refused, provider=ref.provider, model=resolved.model_id, backend=ref.backend
            )
        )
    context = build_context(
        session,
        ContextTarget(
            provider=ref.provider,
            backend=ref.backend,
            model_id=resolved.model_id,
            context_window=entry.context_window if entry is not None else None,
            max_output_tokens=cap,
            input_modalities=caps.input_modalities,
        ),
        req.message,
        budget_tokens=settings.context_budget_tokens,
        count_tokens=deps.count_tokens,
        attachments=_session_attachments(session, new, exclude_turn_id, deps),
        index=index,
        continued_from=req.continued_from,
        exclude_turn_id=exclude_turn_id,
    )
    request = CompletionRequest(
        model=resolved.model_id,
        prompt=context.prompt,
        response_schema=None,
        temperature=None,
        max_output_tokens=cap,
        reasoning=resolved.effective_reasoning,
        native_params=dict(resolved.native_params),
        cache_prefix=True,
        timeout_s=DEFAULT_TIMEOUT_S,
        tags={"stage": CHAT_STAGE},
    )
    breakdown = context.breakdown
    plan = CallPlan(
        stage=CHAT_STAGE,
        provider=ref.provider,
        model=resolved.model_id,
        backend=ref.backend,
        reasoning=resolved.effective_reasoning,
        prefix_fingerprint=prefix_fingerprint(context.prompt),
        prefix_tokens=breakdown.prefix,
        suffix_tokens=breakdown.total - breakdown.prefix,
        expected_output_tokens_low=max(1, cap // ESTIMATE_LOW_OUTPUT_DIVISOR),
        expected_output_tokens_high=cap,
        batched=False,
        output_cap_includes_reasoning=reasoning_within_cap(ref.provider, ref.backend),
    )
    estimate = deps.estimator.estimate([plan], [resolved])
    return PreparedChat(
        resolved=resolved,
        pricing=price,
        request=request,
        plan=plan,
        context=context,
        estimate=estimate,
    )


def chat_estimate(prepared: PreparedChat, deps: CompareDeps) -> ChatEstimate:
    """The :class:`ChatEstimate` of a prepared call."""
    est = prepared.estimate
    status = budget_status(est, deps)
    context = prepared.context
    return ChatEstimate(
        currency=est.currency or deps.budget.currency,
        total_low=est.total_low,
        total_high=est.total_high,
        cli_calls=cli_call_count(est),
        budget=status,
        model=prepared.resolved.model_id,
        max_output_tokens=prepared.plan.expected_output_tokens_high,
        context_tokens=context.breakdown.total,
        context=context.breakdown,
        context_budget_tokens=context.budget_tokens,
        verbatim_turns=len(context.verbatim_turn_ids),
        omitted_turns=context.omitted_turns,
        warnings=tuple(w for r in est.resolved for w in r.warnings) + context.notes,
        assumptions=est.assumptions,
    )


def estimate_chat(
    session: Session,
    req: ChatRequest,
    deps: CompareDeps,
    *,
    settings: AppSettings,
    index: MemoryIndex | None = None,
    exclude_turn_id: str | None = None,
) -> ChatEstimate:
    """Estimate one chat message without any model call.

    Raises:
        See :func:`prepare_chat`.
    """
    prepared = prepare_chat(
        session, req, deps, settings=settings, index=index, exclude_turn_id=exclude_turn_id
    )
    return chat_estimate(prepared, deps)


def _failed(turn: ChatTurn, error: str, **fields: object) -> ChatTurn:
    return turn.model_copy(update={"status": TurnStatus.ERROR, "error": error, **fields})


async def run_chat(
    session: Session,
    req: ChatRequest,
    deps: CompareDeps,
    *,
    settings: AppSettings,
    turn: ChatTurn,
    index: MemoryIndex | None = None,
    run_id: str | None = None,
) -> ChatTurn:
    """Answer ``req`` and return ``turn`` completed (``done`` or ``error``).

    Args:
        session: The session (it may already hold ``turn`` as pending).
        req: The message.
        deps: Compare dependencies (budget from ``settings`` applied by the caller).
        settings: App settings (context budget).
        turn: The pending turn to complete.
        index: Memory search index.
        run_id: Ledger run id (default: a new one).

    Raises:
        SelectionError, ResolutionError, PricingError, AttachmentError: see
            :func:`prepare_chat`.
        ConsentRequiredError: a ``cli`` model without ``allow_unknown_cost``.
        BudgetRefusedError: the high estimate exceeds a budget limit. No call is made.
    """
    prepared = prepare_chat(
        session, req, deps, settings=settings, index=index, exclude_turn_id=turn.id
    )
    status = budget_status(prepared.estimate, deps)
    require_consent(status.cli_calls, req.allow_unknown_cost)
    if status.refused:
        raise BudgetRefusedError(status.refusal_reason or "Refused by the budget.", status)

    ref = req.model
    context_tokens = prepared.context.breakdown.total
    base = {"resolved_model": prepared.resolved.model_id, "context_tokens": context_tokens}
    key_env = deps.key_envs.get(ref.provider) if ref.backend is Backend.API else None
    if key_env is not None and not key_is_set(key_env):
        return _failed(
            turn, f"{key_env} is not set. Add it in Settings > API Keys (/settings).", **base
        )
    try:
        done = await execute_budgeted_call(
            run_id=run_id or new_run_id(),
            stage=CHAT_STAGE,
            provider=ref.provider,
            resolved=prepared.resolved,
            pricing=prepared.pricing,
            request=prepared.request,
            call_high=status.estimate_high,
            deps=deps,
        )
    except BudgetExceededError as exc:
        return _failed(turn, f"Not started: {exc.scope} budget would be exceeded.", **base)
    except ProviderError as exc:
        return _failed(turn, str(exc) or type(exc).__name__, **base)
    except Exception as exc:  # an adapter bug must not lose the turn
        # Only the type name: an unwrapped SDK message is not guaranteed secret-free.
        return _failed(turn, f"Unexpected error ({type(exc).__name__}).", **base)
    result = done.result
    currency = deps.budget.currency if prepared.pricing is None else prepared.pricing.currency
    fields = {
        **base,
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "cost": done.cost,
        "currency": currency,
        "latency_s": done.latency_s,
    }
    if not result.text.strip():
        return _failed(
            turn, f"The model returned an empty reply ({describe_result(result)}).", **fields
        )
    return turn.model_copy(
        update={"status": TurnStatus.DONE, "reply": result.text, "error": None, **fields}
    )


CliStatusFn = Callable[[], Awaitable[Mapping[str, bool]]]
"""Returns provider id -> whether its subscription CLI is available (signed in)."""


async def update_session_memory(
    session_id: str,
    sessions: SessionStoreLike,
    deps: CompareDeps,
    *,
    settings: AppSettings,
    cli_status: CliStatusFn | None = None,
    run_id: str | None = None,
) -> MemoryState | None:
    """Fold old turns of a session into its memory when needed, and store the result.

    Runs after a turn finished, in the background. The memory model comes from
    :func:`~llm_crossjury.core.memory.picker.pick_memory_model`; its calls go through
    the ledger under stage ``memory``. The new state is stored only if no other
    update changed the memory meanwhile.

    Returns:
        The stored memory state, or ``None`` (nothing to do, no usable memory model,
        a failed call, or the session is gone). Never raises (except on cancellation).
    """
    try:
        session = sessions.get(session_id)
        budget_tokens = settings.context_budget_tokens
        if plan_fold(session, budget_tokens, deps.count_tokens) is None:
            return None
        available = await cli_status() if cli_status is not None else {}
        model = pick_memory_model(deps, settings, cli_available=available)
        if model is None:
            logger.info("no usable memory model; summary skipped")
            return None
        call = make_call_fn(run_id or new_run_id(), deps, [], {model.provider: model.backend})
        new = await update_memory(
            session,
            call=call,
            provider=model.provider,
            model_id=model.model_id,
            budget_tokens=budget_tokens,
            count_tokens=deps.count_tokens,
        )
        if new is None:
            return None
        before = session.memory.summarized_through

        def apply(current: Session) -> Session:
            if current.memory.summarized_through != before:
                return current  # another update won the race
            return current.model_copy(update={"memory": new})

        stored = sessions.update(session_id, apply)
    except SessionNotFoundError:
        return None
    except Exception as exc:  # never raise into the chat path; the next turn retries
        logger.warning("session memory update failed (%s)", type(exc).__name__)
        return None
    return new if stored.memory == new else None


__all__ = [
    "CHAT_STAGE",
    "MAX_MESSAGE_CHARS",
    "ChatEstimate",
    "ChatRequest",
    "CliStatusFn",
    "PreparedChat",
    "chat_estimate",
    "deps_with_settings",
    "estimate_chat",
    "prepare_chat",
    "run_chat",
    "update_session_memory",
]
