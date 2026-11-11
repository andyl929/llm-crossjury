"""Compare-mode service: estimate and run one task across several providers.

This module is the shared core API behind the web UI (and, later, the CLI). It holds
no web or CLI code. Everything it needs comes in through :class:`CompareDeps`, so tests
can pass fakes; :func:`build_default_deps` is the single place with the real wiring.

Flow of :func:`run_compare`:

1. Validate the selections, resolve each model (reasoning is always ``default`` in
   this MVP), and look up its verified price. A missing price refuses an ``api``
   member. A ``cli`` member (the vendor CLI the user signed in to, "subscription")
   needs no price: its cost is unknown (ADR 0002, ADR 0003).
2. A run with any ``cli`` member needs the user's explicit consent
   (``allow_unknown_cost``), otherwise :class:`ConsentRequiredError` is raised.
3. Estimate the run and refuse it if the high estimate of the priced (``api``) part
   exceeds the run limit or the remaining daily budget (SPEC §5).
4. Call every member concurrently. Before each ``api`` call the ledger is checked with
   that call's high estimate and the amount is reserved; every completed call (``cli``
   calls too, with ``cost=None`` and their duration) is recorded. A failing member
   (missing key, CLI not found, vendor error, budget stop) becomes an error entry and
   never fails the whole run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from llm_crossjury.core.attachments.build import BuiltAttachment
from llm_crossjury.core.attachments.prompt import (
    AttachmentReceipt,
    DeliveryMode,
    add_derived_images,
    attachment_tokens,
    derived_map,
    inline_attachments,
    receipts,
    refusal_message,
    with_attachments,
)
from llm_crossjury.core.attachments.render import sort_attachments, validate_total
from llm_crossjury.core.attachments.store import AttachmentNotFoundError, AttachmentStore
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
from llm_crossjury.core.keys import load_all_keys
from llm_crossjury.core.orchestrator.stage import StageName
from llm_crossjury.core.providers.base import (
    AttachmentKind,
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

DEFAULT_MAX_OUTPUT_TOKENS = 32_000
"""Product setting (not a model fact): default output cap per member call. Research and
development answers are long, and reasoning models spend part of the cap on hidden
reasoning (Google recommends at least 32,768 for Gemini 4 Argon), so the default is
generous; it is clamped to the model's registry ``max_output`` and can be lowered per
card in the UI."""

REASONING_WITHIN_OUTPUT_CAP: frozenset[str] = frozenset({"anthropic", "openai", "gemini"})
"""API vendors whose output cap counts hidden reasoning tokens too, so visible plus
reasoning output never exceeds ``max_output_tokens`` (the estimate's worst case is the
cap itself). Anthropic: ``max_tokens`` includes thinking. OpenAI: ``max_output_tokens``
includes reasoning tokens. Gemini: ``max_output_tokens`` includes thought tokens
(https://ai.google.dev/gemini-api/docs/thinking). xAI is excluded: its cap covers only
the visible answer (https://docs.x.ai/developers/model-capabilities/text/best-practices)."""


def reasoning_within_cap(provider: str, backend: Backend) -> bool:
    """Whether ``provider`` on ``backend`` counts reasoning inside the output cap."""
    return backend is Backend.API and provider in REASONING_WITHIN_OUTPUT_CAP


DEFAULT_TIMEOUT_S = 900.0
"""Product setting: per-call timeout so a hung vendor call cannot block the run."""

ESTIMATE_LOW_OUTPUT_DIVISOR = 8
"""The low output estimate is ``max_output_tokens // 8``; the high one is the cap."""

PROMPT_ID = "compare/answer.v1"
"""Prompt template used for compare answers (``prompts/<id>.md``)."""

_PROMPT_VARIABLE = "{task}"

MAX_ATTACHMENTS = 20
"""Product setting: most files one run may carry."""

TOKEN_COUNT_TIMEOUT_S = 15.0
"""Vendor token counting that takes longer falls back to the local estimate."""

_TOKEN_COUNT_CACHE_MAX = 256


# --------------------------------------------------------------------------- errors


class CompareError(Exception):
    """Base class for compare-mode refusals. Messages never contain secrets."""


class SelectionError(CompareError, ValueError):
    """The task or the member selections are invalid."""


class ConsentRequiredError(CompareError):
    """A ``cli`` member was selected without consent to unknown cost (ADR 0003)."""

    def __init__(self, cli_calls: int) -> None:
        super().__init__(
            f"The run makes {cli_calls} subscription (CLI) call(s) with unknown cost. "
            "Confirm that the calls count against your plan's usage limits."
        )
        self.cli_calls = cli_calls


class BudgetRefusedError(CompareError):
    """The run is refused before any call because it could exceed a budget limit."""

    def __init__(self, message: str, status: BudgetStatus) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- data


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Selection(_Frozen):
    """One member picked by the user: a provider, a backend and a model, plus product
    settings.

    Reasoning is always ``default`` in this MVP.
    """

    provider: str = Field(min_length=1)
    backend: Backend = Backend.API
    """``api`` (API key) or ``cli`` (the vendor CLI the user signed in to)."""
    model: str = Field(min_length=1)
    """A concrete registry ``model_id`` or an alias (``fast`` | ``balanced`` | ``best``)."""
    temperature: float | None = Field(default=None, ge=0)
    """``None`` = vendor default."""
    max_output_tokens: int = Field(default=DEFAULT_MAX_OUTPUT_TOKENS, ge=1)


class MemberResult(_Frozen):
    """Outcome of one member call. Exactly one of ``text`` and ``error`` is set."""

    provider: str
    backend: Backend
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
    attachments: tuple[AttachmentReceipt, ...] = ()
    """How this member received each attached file (native, text, text+images or
    refused)."""


class AttachmentEstimateLine(_Frozen):
    """Estimated input tokens of one attachment for one member."""

    provider: str
    backend: Backend
    model: str
    id: str
    filename: str
    kind: AttachmentKind
    mode: DeliveryMode
    images: int = 0
    """Derived images sent with the text (``text+images``); included in ``tokens``."""
    tokens: int | None
    """Local estimate (``estimate_attachment_tokens``, plus the derived images as
    native images); ``None`` when refused."""
    vendor_counted: bool
    """The member's input total came from the vendor's token counter (which already
    includes the attachments); ``tokens`` is then shown for information only."""


class CompareEstimateDetails(_Frozen):
    """A compare estimate plus the per-attachment lines and member refusals."""

    estimate: CostEstimate
    attachments: tuple[AttachmentEstimateLine, ...]
    refusals: tuple[str, ...]
    """Members that will fail because they cannot receive an attachment."""


class CompareResult(_Frozen):
    """Result of one compare run."""

    run_id: str
    prompt_id: str
    currency: str
    members: tuple[MemberResult, ...]
    total_cost: Decimal
    """Sum of known member costs."""
    unknown_cost_members: int
    """Completed calls whose cost could not be computed (every ``cli`` call)."""


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
    """Range of the priced (``api``) calls only."""
    cli_calls: int = 0
    """Calls on the ``cli`` backend: unknown cost, counted against the user's plan."""
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


class AttachmentStoreLike(Protocol):
    """The part of :class:`AttachmentStore` the services and the upload API use."""

    def get(self, attachment_id: str) -> BuiltAttachment: ...

    def put(self, built: BuiltAttachment) -> str: ...

    def delete(self, attachment_id: str) -> bool: ...


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
        providers: Provider objects by ``(provider id, backend)``.
        key_envs: Provider id -> name of the environment variable holding its
            ``api`` key. Providers without an entry need no key (e.g. mocks). Never
            used for the ``cli`` backend (ADR 0002 rule 10).
        registry: Model registry (entries and per-model facts).
        resolver: Resolves aliases and models (ADR 0006).
        pricing: Verified price lookup.
        estimator: Turns call plans into a cost estimate.
        ledger: Spend ledger with per-run and daily checks.
        budget: Effective budget limits.
        compute_cost: Cost of one completed call from its usage and price.
        count_tokens: Local token heuristic for estimates.
        prompts_dir: Directory holding the prompt templates.
        attachment_store: Uploaded attachments by id (``None`` = attachments are not
            available).
        token_count_cache: Vendor token counts by request identity (bounded),
            so an estimate followed by a run counts once.
    """

    providers: Mapping[tuple[str, Backend], Provider]
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
    attachment_store: AttachmentStoreLike | None = None
    token_count_cache: dict[str, int | None] = field(default_factory=dict)


def provider_names(deps: CompareDeps) -> tuple[str, ...]:
    """Provider ids in ``deps.providers``, in first-seen order, without duplicates."""
    return tuple(dict.fromkeys(name for name, _ in deps.providers))


def provider_backends(provider: str, deps: CompareDeps) -> tuple[Backend, ...]:
    """Backends ``provider`` is reachable through, in ``deps.providers`` order."""
    return tuple(backend for name, backend in deps.providers if name == provider)


def key_is_set(env_var: str) -> bool:
    """Return whether ``env_var`` holds a non-empty value. Never exposes the value."""
    return bool(os.environ.get(env_var, "").strip())


def _cli_providers(registry: ModelRegistry) -> list[Provider]:
    """Build the supported CLI providers (``skipped_reason`` unset), never running them."""
    from llm_crossjury.core.providers.cli import (  # noqa: PLC0415
        ClaudeCliProvider,
        CodexCliProvider,
        GrokCliProvider,
    )

    built: list[Provider] = []
    for cls in (ClaudeCliProvider, CodexCliProvider, GrokCliProvider):
        if cls.skipped_reason is not None:
            continue
        # The adapters take ``(*, registry=None)``; the cast keeps this module
        # independent of the exact constructor signature of the CLI base class.
        factory = cast(Callable[..., Provider], cls)
        built.append(factory(registry=registry))
    return built


def build_default_deps(base_dir: Path) -> CompareDeps:
    """Wire the real registry, pricing, estimator, ledger, API and CLI providers.

    CLI providers are only constructed here; nothing is executed until a check or a
    call. Loads ``base_dir / ".env"`` and then ``.<provider>.key`` files into the environment
    (existing variables win) without printing anything.

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

    # Keys first (records each key's source: env, .env, key file, settings store),
    # then the rest of .env (non-key settings); existing variables always win.
    load_all_keys(
        base_dir,
        {
            "anthropic": AnthropicApiProvider.api_key_env,
            "openai": OpenAIApiProvider.api_key_env,
            "gemini": GeminiApiProvider.api_key_env,
            "xai": XaiApiProvider.api_key_env,
        },
    )
    load_dotenv(base_dir / ".env", override=False)
    from llm_crossjury.core.sessions.store import stored_attachment_ids  # noqa: PLC0415

    data = resolve_data_paths()
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
    providers: dict[tuple[str, Backend], Provider] = {
        (p.name, Backend.API): p for p in api_providers
    }
    for cli in _cli_providers(registry):
        providers[(cli.name, Backend.CLI)] = cli
    return CompareDeps(
        providers=providers,
        key_envs={p.name: p.api_key_env for p in api_providers},
        registry=registry,
        resolver=ModelResolver(registry, ResolutionMode.NORMAL),
        pricing=pricing,
        estimator=estimator,
        ledger=SqliteLedger(data.ledger),
        budget=BudgetConfig(),
        # Until the web app wires its session store in, the start-up purge keeps the
        # attachments of the sessions on disk.
        attachment_store=AttachmentStore(
            data.root / "attachments", keep=lambda: stored_attachment_ids(data.sessions)
        ),
    )


# --------------------------------------------------------------------------- models


class UsableModel(_Frozen):
    """A model offered in the UI for one backend."""

    model_id: str
    aliases: tuple[str, ...]


def is_cli_usable(entry: ModelEntry | None) -> bool:
    """Whether ``entry`` can be selected on the ``cli`` backend (listed, with flags)."""
    return entry is not None and Backend.CLI in entry.backends and entry.cli_flags is not None


def is_usable(provider: str, model_id: str, backend: Backend, deps: CompareDeps) -> bool:
    """Whether ``provider``/``model_id`` can be called on ``backend``.

    ``api``: the registry lists the backend and the model has a verified price.
    ``cli``: the registry lists the backend and has ``cli_flags``; no price needed.
    Other backends (``mock``, tests only) are usable when the provider is registered.
    """
    if (provider, backend) not in deps.providers:
        return False
    entry = deps.registry.get(provider, model_id)
    if backend is Backend.CLI:
        return is_cli_usable(entry)
    if backend is Backend.API:
        if entry is None or Backend.API not in entry.backends:
            return False
        try:
            deps.pricing.lookup(provider, model_id)
        except PricingError:
            return False
    return True


def usable_models(
    provider: str, deps: CompareDeps, backend: Backend = Backend.API
) -> tuple[UsableModel, ...]:
    """Usable entries of ``provider`` on ``backend`` (see :func:`is_usable`), with the
    aliases that point at them."""
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
        if entry.provider != provider or not is_usable(provider, entry.model_id, backend, deps):
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
    # Attachments are identified by their sha256; their raw bytes are not serialized.
    payload = json.dumps(
        [
            m.model_dump(mode="json", exclude={"attachments": {"__all__": {"data"}}})
            for m in prompt.stable_prefix
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- planning


@dataclass(frozen=True)
class PreparedCall:
    """A validated, resolved and (for ``api``) priced member call."""

    selection: Selection
    resolved: ResolvedModel
    pricing: Pricing | None
    """``None`` for the ``cli`` backend (unknown cost)."""
    request: CompletionRequest
    plan: CallPlan
    attachments: tuple[AttachmentReceipt, ...] = ()
    """How this member receives each attachment."""
    attachment_tokens: tuple[tuple[str, int | None], ...] = ()
    """Local token estimate per attachment id (``None`` = refused)."""
    refusal: str | None = None
    """Set when the member cannot receive an attachment: it fails without a call."""
    vendor_counted: bool = False
    """``plan`` input tokens came from the vendor's token counter."""


def _validate(task: str, selections: Sequence[Selection], deps: CompareDeps) -> None:
    if not task.strip():
        raise SelectionError("The task is empty.")
    if not selections:
        raise SelectionError("Select at least one provider.")
    seen: set[str] = set()
    names = provider_names(deps)
    for sel in selections:
        if sel.provider not in names:
            raise SelectionError(f"Unknown provider {sel.provider!r}.")
        if (sel.provider, sel.backend) not in deps.providers:
            raise SelectionError(
                f"Provider {sel.provider!r} is not available on the {sel.backend.value} backend."
            )
        if sel.provider in seen:
            raise SelectionError(f"Provider {sel.provider!r} is selected more than once.")
        seen.add(sel.provider)


def checked_price(
    provider: str, model_id: str, backend: Backend, deps: CompareDeps
) -> Pricing | None:
    """Return the verified price of an ``api`` model, or ``None`` for ``cli``.

    Raises:
        SelectionError: a ``cli`` model without a registry cli entry with ``cli_flags``.
        PricingError: an ``api`` model without a verified price, or a price in a
            currency other than the budget currency.
    """
    if backend is Backend.CLI:
        if not is_cli_usable(deps.registry.get(provider, model_id)):
            raise SelectionError(
                f"{provider}/{model_id} cannot be used through the CLI: the model "
                "registry has no cli entry with cli_flags for it."
            )
        return None
    price = deps.pricing.lookup(provider, model_id)
    if price.currency != deps.budget.currency:
        raise PricingError(
            f"{provider}/{model_id} is priced in {price.currency}, "
            f"but the budget is in {deps.budget.currency}."
        )
    return price


def load_attachments(
    attachment_ids: Sequence[str], deps: CompareDeps
) -> tuple[BuiltAttachment, ...]:
    """Load uploaded attachments by id, check the run total and sort them (ADR 0007).

    Duplicate ids are loaded once.

    Raises:
        SelectionError: too many files.
        AttachmentNotFoundError: an id is unknown or expired, or attachments are not
            available in this setup.
        AttachmentTooLargeError: the total exceeds the run limit.
    """
    ids = list(dict.fromkeys(attachment_ids))
    if not ids:
        return ()
    if len(ids) > MAX_ATTACHMENTS:
        raise SelectionError(f"Attach at most {MAX_ATTACHMENTS} files per run.")
    store = deps.attachment_store
    if store is None:
        raise AttachmentNotFoundError("attachments are not available in this setup")
    built = [store.get(attachment_id) for attachment_id in ids]
    validate_total(b.attachment for b in built)
    order = {a.sha256: i for i, a in enumerate(sort_attachments(b.attachment for b in built))}
    return tuple(sorted(built, key=lambda b: order[b.attachment.sha256]))


def prepare_calls(
    task: str,
    selections: Sequence[Selection],
    deps: CompareDeps,
    *,
    attachments: Sequence[BuiltAttachment] = (),
) -> list[PreparedCall]:
    """Validate, resolve and price every selection, and build its request and plan.

    With attachments (already loaded, see :func:`load_attachments`), the stable prefix
    is the system message (plus the attachment notice) and one user message carrying
    the files; the variable suffix is the task. ``cli`` members get the files' text
    inlined in that message. A member that cannot receive a file (an image without
    native support, or any image on ``cli``) gets a ``refusal`` and is not called.

    Raises:
        SelectionError: invalid task or selections, or a ``cli`` model without
            registry ``cli_flags``.
        ResolutionError: a model cannot be resolved.
        PricingError: an ``api`` model has no verified price, or a price currency
            differs from the budget currency.
    """
    _validate(task, selections, deps)
    template = load_prompt_template(deps.prompts_dir / f"{PROMPT_ID}.md")
    base_prompt = template.render(task)
    built = tuple(attachments)
    shared_prompt = with_attachments(base_prompt, [b.attachment for b in built])
    # Text of the prefix without attachment content; attachments are added per member.
    prefix_tokens = sum(deps.count_tokens(m.content) for m in shared_prompt.stable_prefix)
    suffix_tokens = sum(deps.count_tokens(m.content) for m in shared_prompt.variable_suffix)

    prepared: list[PreparedCall] = []
    for sel in selections:
        resolved = deps.resolver.resolve(
            member_id=sel.provider,
            provider=sel.provider,
            model=sel.model,
            backend=sel.backend,
            role=ModelRole.ANSWERER,
            reasoning=ReasoningEffort.DEFAULT,
            native=None,
        )
        price = checked_price(sel.provider, resolved.model_id, sel.backend, deps)
        entry = deps.registry.get(sel.provider, resolved.model_id)
        limit = entry.max_output if entry is not None else None
        if limit is not None and sel.max_output_tokens > limit:
            # Clamp rather than refuse: the generous default (or a large typed value)
            # simply becomes the model's own maximum.
            sel = sel.model_copy(update={"max_output_tokens": limit})  # noqa: PLW2901
        prompt, member_receipts, tokens_by_id, refusal = _member_prompt(
            base_prompt, shared_prompt, built, sel, resolved, deps
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
            backend=sel.backend,
            reasoning=resolved.effective_reasoning,
            prefix_fingerprint=prefix_fingerprint(prompt),
            prefix_tokens=prefix_tokens + sum(t or 0 for t in tokens_by_id.values()),
            suffix_tokens=suffix_tokens,
            expected_output_tokens_low=max(1, sel.max_output_tokens // ESTIMATE_LOW_OUTPUT_DIVISOR),
            expected_output_tokens_high=sel.max_output_tokens,
            batched=False,
            output_cap_includes_reasoning=reasoning_within_cap(sel.provider, sel.backend),
        )
        prepared.append(
            PreparedCall(
                selection=sel,
                resolved=resolved,
                pricing=price,
                request=request,
                plan=plan,
                attachments=member_receipts,
                attachment_tokens=tuple(tokens_by_id.items()),
                refusal=refusal,
            )
        )
    return prepared


def _member_prompt(  # noqa: PLR0917 - private helper of prepare_calls
    base_prompt: Prompt,
    shared_prompt: Prompt,
    built: Sequence[BuiltAttachment],
    sel: Selection,
    resolved: ResolvedModel,
    deps: CompareDeps,
) -> tuple[Prompt, tuple[AttachmentReceipt, ...], dict[str, int | None], str | None]:
    """The prompt, attachment receipts, token estimates and refusal of one member."""
    if not built:
        return shared_prompt, (), {}, None
    caps = deps.providers[(sel.provider, sel.backend)].capabilities(resolved.model_id)
    member_receipts = receipts(built, caps.input_modalities, sel.backend)
    tokens_by_id = attachment_tokens(
        built,
        provider=sel.provider,
        input_modalities=caps.input_modalities,
        backend=sel.backend,
    )
    refused = [r for r in member_receipts if r.mode == "refused"]
    refusal = (
        refusal_message(
            refused, provider=sel.provider, model=resolved.model_id, backend=sel.backend
        )
        if refused
        else None
    )
    derived = derived_map(built)
    if sel.backend is not Backend.CLI:
        # Smart page images: a member that reads images but not a file's kind gets
        # the file's derived images next to its text (unchanged prompt otherwise).
        prompt = add_derived_images(shared_prompt, derived, caps.input_modalities)
        return prompt, member_receipts, tokens_by_id, refusal
    # cli adapters read only the attachments their capabilities declare: keep those,
    # inline the rest as text (refused files are left out; a refused member is never
    # called anyway).
    kept = {r.id for r in member_receipts if r.mode != "refused"}
    usable = [b.attachment for b in built if b.attachment.sha256 in kept]
    prompt = (
        inline_attachments(
            add_derived_images(
                with_attachments(base_prompt, usable), derived, caps.input_modalities
            ),
            caps.input_modalities,
        )
        if usable
        else base_prompt
    )
    return prompt, member_receipts, tokens_by_id, refusal


def _count_key(call: PreparedCall) -> str:
    suffix = json.dumps([m.content for m in call.request.prompt.variable_suffix])
    raw = json.dumps(
        [
            call.selection.provider,
            call.selection.backend.value,
            call.resolved.model_id,
            call.plan.prefix_fingerprint,
            suffix,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _vendor_count(call: PreparedCall, deps: CompareDeps) -> int | None:
    key = _count_key(call)
    if key in deps.token_count_cache:
        return deps.token_count_cache[key]
    provider = deps.providers[(call.selection.provider, call.selection.backend)]
    try:
        count = await asyncio.wait_for(
            provider.count_input_tokens(call.request), timeout=TOKEN_COUNT_TIMEOUT_S
        )
    except Exception:  # counting is best effort: fall back to the local estimate
        return None
    if len(deps.token_count_cache) >= _TOKEN_COUNT_CACHE_MAX:
        deps.token_count_cache.clear()
    deps.token_count_cache[key] = count
    return count


async def apply_vendor_counts(
    prepared: Sequence[PreparedCall], deps: CompareDeps
) -> list[PreparedCall]:
    """Replace local input estimates with vendor token counts where available.

    Only calls that carry attachments are counted (the local heuristic is weakest
    there, and plain-text estimates stay free of vendor calls). Only ``api`` members
    with their key set and no refusal are counted; a count that fails, times out or
    returns ``None`` keeps the local estimate. ``api`` adapters count attachments in
    the same native or text form they send them.
    """

    async def one(call: PreparedCall) -> PreparedCall:
        sel = call.selection
        if not call.attachments or call.refusal is not None or sel.backend is not Backend.API:
            return call
        key_env = deps.key_envs.get(sel.provider)
        if key_env is not None and not key_is_set(key_env):
            return call
        count = await _vendor_count(call, deps)
        if count is None:
            return call
        plan = call.plan.model_copy(
            update={"prefix_tokens": max(0, count - call.plan.suffix_tokens)}
        )
        return replace(call, plan=plan, vendor_counted=True)

    return list(await asyncio.gather(*(one(call) for call in prepared)))


def _estimate_prepared(prepared: Sequence[PreparedCall], deps: CompareDeps) -> CostEstimate:
    return deps.estimator.estimate([p.plan for p in prepared], [p.resolved for p in prepared])


async def prepare_compare(
    task: str,
    selections: Sequence[Selection],
    deps: CompareDeps,
    *,
    attachment_ids: Sequence[str] = (),
) -> list[PreparedCall]:
    """Load the attachments, prepare every call and apply vendor token counts.

    Raises:
        SelectionError, ResolutionError, PricingError: see :func:`prepare_calls`.
        AttachmentError: see :func:`load_attachments`.
    """
    built = load_attachments(attachment_ids, deps)
    prepared = prepare_calls(task, selections, deps, attachments=built)
    return await apply_vendor_counts(prepared, deps)


def _details(prepared: Sequence[PreparedCall], deps: CompareDeps) -> CompareEstimateDetails:
    lines: list[AttachmentEstimateLine] = []
    for call in prepared:
        tokens = dict(call.attachment_tokens)
        for receipt in call.attachments:
            lines.append(
                AttachmentEstimateLine(
                    provider=call.selection.provider,
                    backend=call.selection.backend,
                    model=call.resolved.model_id,
                    id=receipt.id,
                    filename=receipt.filename,
                    kind=receipt.kind,
                    mode=receipt.mode,
                    images=receipt.images,
                    tokens=tokens.get(receipt.id),
                    vendor_counted=call.vendor_counted,
                )
            )
    return CompareEstimateDetails(
        estimate=_estimate_prepared(prepared, deps),
        attachments=tuple(lines),
        refusals=tuple(
            f"{call.selection.provider}: {call.refusal}"
            for call in prepared
            if call.refusal is not None
        ),
    )


async def estimate_compare_details(
    task: str,
    selections: Sequence[Selection],
    deps: CompareDeps,
    *,
    attachment_ids: Sequence[str] = (),
) -> CompareEstimateDetails:
    """Estimate a compare run, with per-attachment lines, without any model call.

    Vendor token counting (free, no completion) may be used for calls that carry
    attachments (see :func:`apply_vendor_counts`).

    Raises:
        SelectionError, ResolutionError, PricingError: see :func:`prepare_calls`.
        AttachmentError: see :func:`load_attachments`.
    """
    prepared = await prepare_compare(task, selections, deps, attachment_ids=attachment_ids)
    return _details(prepared, deps)


async def estimate_compare(
    task: str,
    selections: Sequence[Selection],
    deps: CompareDeps,
    *,
    attachment_ids: Sequence[str] = (),
) -> CostEstimate:
    """Estimate a compare run without making any model call.

    Raises:
        SelectionError, ResolutionError, PricingError: see :func:`prepare_calls`.
        AttachmentError: see :func:`load_attachments`.
    """
    details = await estimate_compare_details(task, selections, deps, attachment_ids=attachment_ids)
    return details.estimate


def cli_call_count(estimate: CostEstimate) -> int:
    """Number of planned calls on the ``cli`` backend (unknown cost)."""
    return sum(line.calls for line in estimate.lines if line.backend is Backend.CLI)


def budget_status(estimate: CostEstimate, deps: CompareDeps) -> BudgetStatus:
    """Compare ``estimate`` with the budget and today's ledger spend.

    The limits apply to the priced part only. Unpriced ``cli`` calls are allowed
    (they need consent, checked separately) and counted in ``cli_calls``; any other
    unpriced call refuses the run.
    """
    budget = deps.budget
    spent_today = deps.ledger.spent_on(date.today())  # local calendar day (SPEC §5)
    remaining = max(Decimal(0), budget.daily_limit - spent_today)
    high = estimate.total_high
    cur = budget.currency
    unpriced = sum(
        line.calls
        for line in estimate.lines
        if line.cost_high is None and line.backend is not Backend.CLI
    )
    reason: str | None = None
    if unpriced:
        reason = f"{unpriced} call(s) cannot be priced."
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
        cli_calls=cli_call_count(estimate),
        needs_confirmation=reason is None and high > budget.confirm_above,
        refused=reason is not None,
        refusal_reason=reason,
    )


# --------------------------------------------------------------------------- running


def _budget(deps: CompareDeps) -> Budget:
    # Only api calls are checked against the ledger; their cost is always known.
    return Budget(
        run_limit=deps.budget.run_limit,
        daily_limit=deps.budget.daily_limit,
        currency=deps.budget.currency,
        allow_unknown_cost=False,
    )


def require_consent(cli_calls: int, allow_unknown_cost: bool) -> None:
    """Refuse ``cli`` calls without consent to unknown cost (ADR 0003).

    Raises:
        ConsentRequiredError: ``cli_calls > 0`` and ``allow_unknown_cost`` is false.
    """
    if cli_calls and not allow_unknown_cost:
        raise ConsentRequiredError(cli_calls)


def _error_result(call: PreparedCall, currency: str, error: str) -> MemberResult:
    return MemberResult(
        provider=call.selection.provider,
        backend=call.selection.backend,
        model=call.resolved.model_id,
        requested_model=call.selection.model,
        text=None,
        error=error,
        usage=None,
        cost=None,
        currency=currency,
        latency_s=None,
        attachments=call.attachments,
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
    pricing: Pricing | None,
    request: CompletionRequest,
    call_high: Decimal,
    deps: CompareDeps,
) -> BudgetedCallResult:
    """Check the budget, reserve, call, record, release (SPEC section 5).

    The ledger check and the reservation happen without an ``await`` in between, so
    concurrent calls cannot both pass the check against the same headroom. The
    reservation is released only after the call is recorded, so spent + reserved
    never dips.

    A ``cli`` call (``resolved.backend``) has unknown cost: it is neither checked nor
    reserved (the limits apply to priced spend only; the caller must have obtained
    consent), and it is recorded with ``cost=None`` and its duration.

    Args:
        run_id: Run the call belongs to (per-run limit and ledger grouping).
        stage: Stage name recorded in the ledger.
        provider: Provider id; with ``resolved.backend`` the key into ``deps.providers``.
        resolved: The resolved model.
        pricing: Its verified price; ``None`` only for the ``cli`` backend.
        request: The request to send; ``run_id`` is added to its tags.
        call_high: High cost estimate of this call, checked and reserved.
        deps: Compare dependencies.

    Raises:
        BudgetExceededError: the call could exceed a limit; it is not started.
        ProviderError: the vendor call failed (nothing is recorded).
        Exception: anything else the adapter raises, unchanged.
    """
    ledger = deps.ledger
    backend = resolved.backend
    if pricing is None and backend is not Backend.CLI:
        raise ValueError(f"{provider}/{resolved.model_id}: a {backend} call needs a price")
    reservation: str | None = None
    if backend is not Backend.CLI:
        ledger.check(run_id, call_high, _budget(deps))
        reservation = ledger.reserve(run_id, call_high)
    try:
        tagged = request.model_copy(update={"tags": {**request.tags, "run_id": run_id}})
        started = time.perf_counter()
        result = await deps.providers[(provider, backend)].complete(tagged)
        latency = time.perf_counter() - started
        cost = None if pricing is None else deps.compute_cost(result.usage, pricing)
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
                currency=deps.budget.currency if pricing is None else pricing.currency,
                batched=False,
                duration_s=latency,
                recorded_at=datetime.now(UTC),
            )
        )
        return BudgetedCallResult(result=result, cost=cost, latency_s=latency)
    finally:
        if reservation is not None:
            ledger.release(reservation)


async def _run_member(
    run_id: str, call: PreparedCall, call_high: Decimal, deps: CompareDeps
) -> MemberResult:
    sel = call.selection
    currency = deps.budget.currency
    if call.refusal is not None:
        return _error_result(call, currency, call.refusal)
    key_env = deps.key_envs.get(sel.provider) if sel.backend is Backend.API else None
    if key_env is not None and not key_is_set(key_env):
        return _error_result(
            call, currency, f"{key_env} is not set. Add it in Settings > API Keys (/settings)."
        )
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
        backend=sel.backend,
        model=call.resolved.model_id,
        requested_model=sel.model,
        text=done.result.text,
        error=None,
        usage=done.result.usage,
        cost=done.cost,
        currency=currency if call.pricing is None else call.pricing.currency,
        latency_s=done.latency_s,
        attachments=call.attachments,
    )


MemberCallback = Callable[[int, MemberResult], None]
"""Called with ``(index, result)`` as each member finishes (index into the selections)."""

_log = logging.getLogger(__name__)


def _notify(on_member: MemberCallback | None, index: int, member: MemberResult) -> None:
    if on_member is None:
        return
    try:
        on_member(index, member)
    except Exception as exc:  # a progress hook must never fail the member
        _log.debug("member callback failed (%s)", type(exc).__name__)


def compare_result(run_id: str, currency: str, members: Sequence[MemberResult]) -> CompareResult:
    """A :class:`CompareResult` of ``members`` with its totals computed.

    Args:
        run_id: Run id of the result.
        currency: Budget currency.
        members: Member results, in selection order.
    """
    known = [m.cost for m in members if m.cost is not None]
    return CompareResult(
        run_id=run_id,
        prompt_id=PROMPT_ID,
        currency=currency,
        members=tuple(members),
        total_cost=sum(known, Decimal(0)),
        unknown_cost_members=sum(1 for m in members if m.error is None and m.cost is None),
    )


def replace_member(result: CompareResult, index: int, member: MemberResult) -> CompareResult:
    """``result`` with member ``index`` replaced and the totals recomputed (a retry).

    Raises:
        IndexError: ``index`` is out of range.
    """
    if not 0 <= index < len(result.members):
        raise IndexError(index)
    members = list(result.members)
    members[index] = member
    return compare_result(result.run_id, result.currency, members)


async def execute_calls(
    run_id: str,
    prepared: Sequence[PreparedCall],
    deps: CompareDeps,
    *,
    allow_unknown_cost: bool = False,
    on_member: MemberCallback | None = None,
) -> CompareResult:
    """Run prepared calls concurrently; each member's failure stays in its own entry.

    No run-level budget pre-check happens here (see :func:`run_compare`); each ``api``
    call is still checked against the ledger before it starts.

    Args:
        run_id: Run id of every call.
        prepared: The calls, in selection order.
        deps: Compare dependencies.
        allow_unknown_cost: Consent to ``cli`` calls of unknown cost.
        on_member: Called with ``(index, result)`` as soon as each member finishes,
            in completion order (an exception it raises is logged and ignored). The
            returned members keep the selection order.

    Raises:
        ConsentRequiredError: a ``cli`` call without ``allow_unknown_cost``.
    """
    require_consent(
        sum(1 for call in prepared if call.selection.backend is Backend.CLI), allow_unknown_cost
    )
    highs = [_estimate_prepared([call], deps).total_high for call in prepared]

    async def one(index: int, call: PreparedCall, high: Decimal) -> MemberResult:
        member = await _run_member(run_id, call, high, deps)
        _notify(on_member, index, member)
        return member

    members = await asyncio.gather(
        *(
            one(index, call, high)
            for index, (call, high) in enumerate(zip(prepared, highs, strict=True))
        )
    )
    return compare_result(run_id, deps.budget.currency, members)


def new_run_id() -> str:
    """Return a fresh opaque run id."""
    return uuid.uuid4().hex


async def run_compare(
    task: str,
    selections: Sequence[Selection],
    deps: CompareDeps,
    *,
    run_id: str | None = None,
    allow_unknown_cost: bool = False,
    attachment_ids: Sequence[str] = (),
    on_member: MemberCallback | None = None,
) -> CompareResult:
    """Check consent, estimate, check the budget, then run every member concurrently.

    Args:
        task: The user's task.
        selections: The members.
        deps: Compare dependencies.
        run_id: Run id to use (default: a new one).
        allow_unknown_cost: The user consented to ``cli`` calls of unknown cost that
            count against their plan's usage limits. Required for any ``cli`` member.
        attachment_ids: Uploaded attachments sent to every member (ADR 0007).
        on_member: Progress hook, see :func:`execute_calls`.

    Raises:
        SelectionError, ResolutionError, PricingError: see :func:`prepare_calls`.
        AttachmentError: see :func:`load_attachments`.
        ConsentRequiredError: a ``cli`` member without ``allow_unknown_cost``.
        BudgetRefusedError: the high estimate of the priced part exceeds the run
            limit or today's remaining budget. No call is made.
    """
    prepared = await prepare_compare(task, selections, deps, attachment_ids=attachment_ids)
    estimate = _estimate_prepared(prepared, deps)
    require_consent(cli_call_count(estimate), allow_unknown_cost)
    status = budget_status(estimate, deps)
    if status.refused:
        raise BudgetRefusedError(status.refusal_reason or "Refused by the budget.", status)
    return await execute_calls(
        run_id or new_run_id(),
        prepared,
        deps,
        allow_unknown_cost=allow_unknown_cost,
        on_member=on_member,
    )
