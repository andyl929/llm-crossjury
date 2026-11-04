"""Provider contract.

CONTRACT FILE. Do not modify. If this contract is wrong or insufficient, record the
problem in ``docs/contract-issues.md`` and keep working against the current version.
Rationale: ``docs/adr/0001-provider-abstraction.md``.

Key ideas:

* A prompt is split into a **stable prefix** (system instructions, shared context,
  the document under review) and a **variable suffix** (the per-call part). Adapters
  that support prompt caching mark the end of the prefix as a cache breakpoint; the
  orchestrator arranges calls so that many calls share one prefix.
* Every completion returns text plus a :class:`Usage`. Any usage field may be
  ``None``, meaning *unknown* (typical for the ``cli`` backend). ``None`` never
  means zero.
* Capabilities are declared per model, because they vary across models of the same
  vendor. The orchestrator never assumes a capability that is not declared.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class Backend(StrEnum):
    """How a provider reaches the model."""

    API = "api"
    """Official vendor SDK, authenticated with a user-supplied API key from the environment."""

    CLI = "cli"
    """Official vendor CLI the user has signed in to themselves. Experimental."""

    MOCK = "mock"
    """Deterministic in-process provider. Tests only."""


class Role(StrEnum):
    """Chat message role."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Message(_Frozen):
    """A single chat message."""

    role: Role
    content: str


class Prompt(_Frozen):
    """A prompt split into a cacheable stable prefix and a per-call variable suffix.

    Invariants (validated by implementations, not here):

    * ``stable_prefix`` must be byte-identical across all calls that are meant to
      share a cache entry. It must not contain timestamps, run ids, anonymous labels
      or anything else that varies between calls.
    * ``variable_suffix`` is non-empty and holds everything that differs per call.
    * System messages may appear only in ``stable_prefix``.
    """

    stable_prefix: tuple[Message, ...]
    variable_suffix: tuple[Message, ...]

    def prefix_fingerprint(self) -> str:
        """Return a stable hash of ``stable_prefix``.

        Used by the cost estimator and the ledger to group calls that should hit
        the same cache entry. Must not depend on ``variable_suffix``.
        """
        raise NotImplementedError


class Capabilities(_Frozen):
    """What a provider can do for a given model.

    A ``False`` flag means the orchestrator must use a fallback (for example
    parse-and-validate instead of native structured output, or sequential calls
    instead of a batch).
    """

    structured_output: bool
    """Accepts a JSON schema and constrains output to it."""

    caching: bool
    """Supports prompt caching of ``Prompt.stable_prefix`` (explicit or automatic)."""

    batch: bool
    """Supports an asynchronous batch API (see :class:`BatchProvider`)."""

    temperature: bool
    """Honors a caller-supplied sampling temperature."""


class Usage(_Frozen):
    """Token usage of a single completion.

    Every field is ``int | None``; ``None`` means *unknown*, never zero. Counts are
    disjoint where the vendor reports them disjointly; adapters document how they
    map vendor fields onto these.
    """

    input_tokens: int | None
    """Uncached input tokens billed at the normal input rate."""

    cached_input_tokens: int | None
    """Input tokens served from a prompt cache (billed at the cached-read rate)."""

    cache_write_tokens: int | None
    """Input tokens written to a prompt cache (billed at the cache-write rate, if any)."""

    output_tokens: int | None
    """Visible output tokens."""

    reasoning_tokens: int | None
    """Hidden reasoning tokens, when the vendor reports them separately."""

    def is_complete(self) -> bool:
        """Return ``True`` if no field is unknown."""
        raise NotImplementedError


class CompletionRequest(_Frozen):
    """A single model call."""

    model: str
    """Vendor model identifier, taken from configuration (never hard-coded)."""

    prompt: Prompt

    response_schema: dict[str, Any] | None
    """Strict JSON schema the output must satisfy, or ``None`` for free text.

    Produced by ``council.core.schemas.export.strict_json_schema``. If the provider
    lacks ``structured_output``, the adapter appends format instructions and the
    caller validates the text afterwards.
    """

    temperature: float | None
    """Ignored (and recorded as such) when the provider lacks ``temperature``."""

    max_output_tokens: int | None

    cache_prefix: bool
    """Request caching of ``prompt.stable_prefix`` if supported."""

    timeout_s: float | None

    tags: dict[str, str]
    """Opaque labels for the ledger (run id, stage, anonymous label). Never sent to vendors."""


class CompletionResult(_Frozen):
    """The outcome of a single model call."""

    text: str
    usage: Usage
    provider: str
    model: str
    """Model identifier as reported by the vendor, if available; else the requested one."""
    backend: Backend
    finish_reason: str | None
    started_at: datetime
    latency_s: float | None


class ProviderStatus(_Frozen):
    """Result of ``Provider.check``.

    ``detail`` is human-readable and MUST NOT contain secrets, key fragments, token
    values or credential file paths.
    """

    provider: str
    backend: Backend
    available: bool
    detail: str
    version: str | None
    """CLI or SDK version string, when cheaply obtainable."""


class ProviderError(Exception):
    """Base class for provider failures. Messages must never include secrets."""


class ProviderUnavailableError(ProviderError):
    """The provider cannot be used at all (missing key, CLI not found, not signed in)."""


class CapabilityNotSupportedError(ProviderError):
    """The caller required a capability the provider did not declare."""


class BatchHandle(_Frozen):
    """Opaque reference to a submitted batch."""

    provider: str
    batch_id: str
    request_count: int
    submitted_at: datetime


class BatchState(StrEnum):
    """Lifecycle of a submitted batch."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@runtime_checkable
class Provider(Protocol):
    """A model vendor reachable through one backend.

    Implementations must be safe to call concurrently from one event loop.
    """

    name: str
    """Stable provider id, e.g. ``"anthropic"``, ``"openai"``, ``"gemini"``, ``"xai"``."""

    backend: Backend

    def capabilities(self, model: str) -> Capabilities:
        """Return the capabilities of ``model`` on this provider/backend."""
        ...

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Run one completion.

        Raises:
            ProviderUnavailableError: the provider cannot be used.
            CapabilityNotSupportedError: the request needs an undeclared capability
                that has no fallback.
            ProviderError: any other vendor failure (after the adapter's own retries).
        """
        ...

    async def count_input_tokens(self, request: CompletionRequest) -> int | None:
        """Return the input token count of ``request`` without running it.

        Returns ``None`` when the vendor offers no counting facility; the estimator
        then falls back to a local heuristic and widens its error bounds.
        """
        ...

    async def check(self) -> ProviderStatus:
        """Cheap readiness check. Must not spend tokens and must not reveal secrets."""
        ...


@runtime_checkable
class BatchProvider(Provider, Protocol):
    """A provider that also exposes an asynchronous batch API.

    Only providers whose ``capabilities(model).batch`` is ``True`` implement this.
    """

    async def submit_batch(self, requests: Sequence[CompletionRequest]) -> BatchHandle:
        """Submit requests as one batch; returns immediately."""
        ...

    async def poll_batch(self, handle: BatchHandle) -> BatchState:
        """Return the current state of a batch."""
        ...

    async def collect_batch(self, handle: BatchHandle) -> list[CompletionResult]:
        """Return results in the order of the submitted requests.

        Raises:
            ProviderError: if the batch is not completed.
        """
        ...
