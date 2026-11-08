"""Shared scaffolding for ``api`` adapters.

Everything here is vendor-neutral: API-key handling, registry-backed capabilities,
the structured-output fallback, prompt validation, deep merging of
``native_params`` and error sanitizing. Vendor SDKs are never imported here, so this
module (and ``check()``) works without them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib import metadata
from typing import Any, Protocol

from llm_crossjury.core.providers.base import (
    Backend,
    BatchHandle,
    BatchState,
    Capabilities,
    CompletionRequest,
    CompletionResult,
    Message,
    ProviderError,
    ProviderStatus,
    ProviderUnavailableError,
    Role,
)
from llm_crossjury.core.registry.schema import ModelEntry, ReasoningControlType

_REDACTED = "[REDACTED]"
_MAX_ERROR_DETAIL = 300


class ModelLookup(Protocol):
    """The part of :class:`~llm_crossjury.core.registry.resolver.ModelRegistry` that
    adapters need. Any object with this method (including test fakes) works."""

    def get(self, provider: str, model_id: str) -> ModelEntry | None:
        """Return the registry entry for ``(provider, model_id)``, or ``None``."""
        ...


def capabilities_from_entry(entry: ModelEntry | None, *, vendor_caching: bool) -> Capabilities:
    """Derive capabilities from a registry entry (model facts never live in code).

    Args:
        entry: The registry entry, or ``None`` if the model is not registered.
        vendor_caching: Whether the vendor officially documents prompt caching for
            its API (explicit or implicit). This is a vendor fact, not a model fact.

    Returns:
        ``structured_output`` iff the entry says ``True``; ``reasoning`` iff the
        control type is ``levels`` or ``budget``; ``temperature`` True; ``batch``
        False (MVP). Without an entry everything is False except ``temperature``.
    """
    if entry is None:
        return Capabilities(
            structured_output=False,
            caching=False,
            batch=False,
            temperature=True,
            reasoning=False,
        )
    return Capabilities(
        structured_output=entry.structured_output is True,
        caching=vendor_caching,
        batch=False,
        temperature=True,
        reasoning=entry.reasoning_control.type
        in (ReasoningControlType.LEVELS, ReasoningControlType.BUDGET),
    )


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``base`` with ``override`` merged in recursively; ``override`` wins.

    Nested dicts are merged key by key; any other value (lists included) is replaced.
    Neither input is mutated.
    """
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def format_instructions(schema: Mapping[str, Any]) -> str:
    """Plain-text output-format instructions for models without native structured
    output (the contract's fallback)."""
    return (
        "Respond with a single JSON object that conforms exactly to the JSON Schema "
        "below. Output only the JSON object: no Markdown code fences, no commentary "
        "before or after it. Include every property listed in the schema; use null "
        "where the schema allows it and no value applies.\n\n"
        "JSON Schema:\n" + json.dumps(schema, indent=2)
    )


def validate_prompt(stable_prefix: Sequence[Message], variable_suffix: Sequence[Message]) -> None:
    """Enforce the :class:`~llm_crossjury.core.providers.base.Prompt` invariants.

    Raises:
        ProviderError: if the suffix is empty or contains a system message.
    """
    if not variable_suffix:
        raise ProviderError("prompt.variable_suffix must not be empty")
    if any(m.role is Role.SYSTEM for m in variable_suffix):
        raise ProviderError("system messages may appear only in prompt.stable_prefix")


def suffix_with_format_instructions(
    variable_suffix: Sequence[Message], schema: Mapping[str, Any]
) -> tuple[Message, ...]:
    """Append format instructions to the last user message of the suffix.

    Only the suffix is touched, so the cacheable prefix stays byte-identical. If the
    suffix has no user message, a new user message carrying the instructions is
    appended.
    """
    messages = list(variable_suffix)
    instructions = format_instructions(schema)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role is Role.USER:
            messages[index] = Message(
                role=Role.USER, content=f"{message.content}\n\n{instructions}"
            )
            return tuple(messages)
    messages.append(Message(role=Role.USER, content=instructions))
    return tuple(messages)


def sdk_version(distribution: str) -> str | None:
    """Installed version of ``distribution`` without importing it, or ``None``."""
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def utc_now() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def sanitize_error(vendor: str, exc: BaseException, secret: str | None) -> ProviderError:
    """Wrap a vendor/SDK exception in a :class:`ProviderError` without secrets.

    The message carries the vendor name, the exception type, the HTTP status (if the
    SDK exposes one as ``status_code`` or ``code``) and a truncated description with
    every occurrence of ``secret`` replaced by ``[REDACTED]``. Callers raise the
    result ``from None`` so the original exception (and its repr) is not chained.
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "code", None)
    message = f"{vendor} API error: {type(exc).__name__}"
    if isinstance(status, int):
        message += f" (HTTP {status})"
    detail = str(exc)
    if secret:
        detail = detail.replace(secret, _REDACTED)
    detail = " ".join(detail.split())
    if len(detail) > _MAX_ERROR_DETAIL:
        detail = detail[:_MAX_ERROR_DETAIL] + "..."
    if detail:
        message += f": {detail}"
    return ProviderError(message)


class ApiProviderBase:
    """Common skeleton for SDK-backed providers.

    Subclasses set ``name``, ``api_key_env``, ``sdk_distribution`` and
    ``vendor_caching`` and implement :meth:`complete` and :meth:`count_input_tokens`.
    :meth:`capabilities` and :meth:`check` work as-is for any vendor; subclasses may
    override any method.

    Args:
        api_key_env: Name of the environment variable holding the key. The value is
            read at call time and is never stored on the instance.
        registry: Model registry used for capabilities. ``None`` means every model
            is unknown (all capabilities False except ``temperature``).
    """

    name: str = ""
    backend: Backend = Backend.API
    api_key_env: str = ""
    sdk_distribution: str = ""
    """PyPI distribution name of the vendor SDK, used by :meth:`check`."""
    vendor_caching: bool = False
    """Whether the vendor officially documents prompt caching for its API."""

    def __init__(
        self, api_key_env: str | None = None, *, registry: ModelLookup | None = None
    ) -> None:
        if api_key_env is not None:
            self.api_key_env = api_key_env
        self._registry = registry

    def _entry(self, model: str) -> ModelEntry | None:
        if self._registry is None:
            return None
        return self._registry.get(self.name, model)

    def _api_key(self) -> str:
        """Read the API key from the environment at call time.

        Raises:
            ProviderUnavailableError: if the variable is missing or empty.
        """
        value = os.environ.get(self.api_key_env, "")
        if not value.strip():
            raise ProviderUnavailableError(f"{self.api_key_env} not set")
        return value

    def _key_is_set(self) -> bool:
        return bool(os.environ.get(self.api_key_env, "").strip())

    def capabilities(self, model: str) -> Capabilities:
        """Capabilities of ``model`` from the registry (see :func:`capabilities_from_entry`)."""
        return capabilities_from_entry(self._entry(model), vendor_caching=self.vendor_caching)

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        raise NotImplementedError

    async def count_input_tokens(self, request: CompletionRequest) -> int | None:
        raise NotImplementedError

    async def check(self) -> ProviderStatus:
        """Report whether the key variable is set and which SDK version is installed.

        Spends no tokens and makes no network call (the SDK is not even imported).
        Never reveals any part of the key, not even its length or prefix: only
        "set" / "not set".
        """
        key_set = self._key_is_set()
        version = sdk_version(self.sdk_distribution) if self.sdk_distribution else None
        sdk_text = (
            f"{self.sdk_distribution} SDK {version}"
            if version
            else f"{self.sdk_distribution or 'vendor'} SDK not installed"
        )
        detail = f"{self.api_key_env} {'set' if key_set else 'not set'}; {sdk_text}"
        return ProviderStatus(
            provider=self.name,
            backend=self.backend,
            available=key_set and version is not None,
            detail=detail,
            version=version,
        )

    async def submit_batch(self, requests: Sequence[CompletionRequest]) -> BatchHandle:
        raise NotImplementedError

    async def poll_batch(self, handle: BatchHandle) -> BatchState:
        raise NotImplementedError

    async def collect_batch(self, handle: BatchHandle) -> list[CompletionResult]:
        raise NotImplementedError
