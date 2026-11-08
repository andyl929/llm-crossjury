"""Check an API key by listing the vendor's models. No tokens are spent.

Each check calls the official SDK's model-list endpoint with the key currently in the
environment (see :mod:`llm_crossjury.core.keys`):

* Anthropic: ``AsyncAnthropic(...).models.list(limit=1)`` (``GET /v1/models``).
* OpenAI: ``AsyncOpenAI(...).models.list()`` (``GET /v1/models``).
* Gemini: ``genai.Client(...).aio.models.list(config={"page_size": 1})``.
* xAI: the ``openai`` SDK with ``base_url="https://api.x.ai/v1"``, ``models.list()``.

Retries are disabled and a short timeout applies. 401/403 (and Gemini's
``API_KEY_INVALID``) mean the key is invalid; connection failures and timeouts are
network errors. Result messages are fixed texts: they never contain vendor messages,
and never any part of the key.

``google.genai`` is imported lazily inside the check (its first import emits a
SyntaxWarning, which the test suite treats as an error).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from llm_crossjury.core.keys import PROVIDER_KEY_ENVS

DEFAULT_TIMEOUT_S = 10.0
"""Product setting: per-check timeout."""

XAI_BASE_URL = "https://api.x.ai/v1"

KeyTestStatus = Literal["valid", "invalid", "network_error", "not_set", "error"]

ModelLister = Callable[[str, float], Awaitable[None]]
"""Lists the vendor's models with ``(api_key, timeout_s)``; raises on failure."""

_AUTH_STATUS = frozenset({401, 403})
_NETWORK_ERROR_NAMES = frozenset({"APIConnectionError", "APITimeoutError"})
_NETWORK_MODULES = ("httpx", "httpcore", "aiohttp")


class KeyTestResult(BaseModel):
    """Outcome of a key check. ``message`` never contains key material."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    status: KeyTestStatus
    message: str


# --------------------------------------------------------------------------- listers


async def _list_anthropic(api_key: str, timeout_s: float) -> None:
    from anthropic import AsyncAnthropic  # noqa: PLC0415 - optional SDK, lazy import

    client = AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=0)
    try:
        await client.models.list(limit=1)
    finally:
        await client.close()


async def _list_openai_compatible(api_key: str, timeout_s: float, base_url: str | None) -> None:
    from openai import AsyncOpenAI  # noqa: PLC0415

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=0)
    try:
        await client.models.list()
    finally:
        await client.close()


async def _list_openai(api_key: str, timeout_s: float) -> None:
    await _list_openai_compatible(api_key, timeout_s, None)


async def _list_xai(api_key: str, timeout_s: float) -> None:
    await _list_openai_compatible(api_key, timeout_s, XAI_BASE_URL)


async def _list_gemini(api_key: str, timeout_s: float) -> None:
    from google import genai  # noqa: PLC0415 - lazy: first import emits a SyntaxWarning
    from google.genai import types  # noqa: PLC0415

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=max(1, round(timeout_s * 1000))),  # ms
    )
    try:
        await client.aio.models.list(config={"page_size": 1})
    finally:
        await client.aio.aclose()


DEFAULT_LISTERS: dict[str, ModelLister] = {
    "anthropic": _list_anthropic,
    "openai": _list_openai,
    "gemini": _list_gemini,
    "xai": _list_xai,
}


# --------------------------------------------------------------------------- mapping


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _is_network_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    for cls in type(exc).__mro__:
        if cls.__name__ in _NETWORK_ERROR_NAMES:
            return True
        if cls.__module__.split(".", 1)[0] in _NETWORK_MODULES:
            return True
    return False


def _is_gemini_invalid_key(exc: BaseException, status: int) -> bool:
    if status != 400:  # noqa: PLR2004 - Gemini reports a bad key as 400 INVALID_ARGUMENT
        return False
    text = f"{getattr(exc, 'details', '')} {getattr(exc, 'message', '')}"
    return "API_KEY_INVALID" in text or "API key not valid" in text


def classify_error(provider: str, exc: BaseException) -> KeyTestResult:
    """Map an SDK exception to a sanitized result (vendor text is never copied)."""
    status = _status_code(exc)
    if status is not None:
        if status in _AUTH_STATUS or _is_gemini_invalid_key(exc, status):
            return KeyTestResult(
                provider=provider,
                status="invalid",
                message=f"The vendor rejected the key (HTTP {status}). Check or replace it.",
            )
        if status == 429:  # noqa: PLR2004
            return KeyTestResult(
                provider=provider,
                status="error",
                message="The vendor is rate limiting this key (HTTP 429). Try again later.",
            )
        return KeyTestResult(
            provider=provider,
            status="error",
            message=f"The vendor returned an error (HTTP {status}).",
        )
    if _is_network_error(exc):
        return KeyTestResult(
            provider=provider,
            status="network_error",
            message="Could not reach the vendor (network error or timeout).",
        )
    return KeyTestResult(
        provider=provider,
        status="error",
        message=f"The check failed ({type(exc).__name__}).",
    )


async def check_key(
    provider: str,
    *,
    listers: Mapping[str, ModelLister] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> KeyTestResult:
    """Check the provider's current key with a model-list call.

    Args:
        provider: A known provider id.
        listers: Provider id -> lister override (tests pass fakes).
        timeout_s: Timeout for the vendor call.

    Returns:
        ``valid``, ``invalid``, ``network_error``, ``not_set`` or ``error`` with a
        sanitized message.

    Raises:
        ValueError: unknown provider id.
    """
    env_var = PROVIDER_KEY_ENVS.get(provider)
    lister = (listers if listers is not None else DEFAULT_LISTERS).get(provider)
    if env_var is None or lister is None:
        raise ValueError("Unknown provider.")
    api_key = os.environ.get(env_var, "").strip()
    if not api_key:
        return KeyTestResult(provider=provider, status="not_set", message=f"{env_var} is not set.")
    try:
        await asyncio.wait_for(lister(api_key, timeout_s), timeout=timeout_s + 2)
    except Exception as exc:  # every failure becomes a sanitized result
        result = classify_error(provider, exc)
    else:
        result = KeyTestResult(
            provider=provider,
            status="valid",
            message="The key works: the vendor listed its models.",
        )
    if api_key in result.message:  # defense in depth; messages are fixed texts
        result = KeyTestResult(provider=provider, status=result.status, message="Check finished.")
    return result


test_key = check_key
"""Alias of :func:`check_key` (``test_key`` is the name used in the design notes)."""
test_key.__test__ = False  # type: ignore[attr-defined]  # keep pytest from collecting it

__all__ = ["DEFAULT_LISTERS", "KeyTestResult", "check_key", "classify_error", "test_key"]
