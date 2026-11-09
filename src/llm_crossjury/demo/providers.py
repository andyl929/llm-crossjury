"""Simulated providers for demo mode. They never touch the network or a subprocess.

A :class:`DemoProvider` answers from the canned script
(:func:`llm_crossjury.demo.responder.respond`) after a short, fixed delay, so the UI's
progress states are visible. Capabilities (which attachment kinds a model accepts) are
taken from the real adapter of the same provider and backend, so attachments behave as
they would in a real run. Constructing a real adapter makes no call and reads no key.

Usage is believable rather than measured: about four characters per token for input
and output, plus a share of reasoning tokens per stage. ``api`` calls are then priced
by the real registry prices through the normal ledger flow. ``cli`` (subscription)
calls report unknown usage, exactly like the real CLI adapters.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime

from llm_crossjury.core.providers.base import (
    Backend,
    Capabilities,
    CompletionRequest,
    CompletionResult,
    Provider,
    ProviderStatus,
    Usage,
)
from llm_crossjury.demo import responder

CHARS_PER_TOKEN = 4
"""Rough English average used for the simulated token counts."""

ANSWER_DELAY_S: Mapping[str, float] = {
    "anthropic": 1.3,
    "openai": 1.5,
    "gemini": 0.9,
    "xai": 1.1,
}
"""Simulated step-1 latency per provider (seconds, before ``latency_scale``)."""

STAGE_DELAY_S: Mapping[str, float] = {"agreement": 0.7, "review": 1.0, "chair": 1.4}
"""Simulated latency of the synthesis stages."""

REASONING_SHARE: Mapping[str, float] = {
    "answer": 0.8,
    "agreement": 0.2,
    "review": 0.4,
    "chair": 0.6,
}
"""Reasoning tokens as a share of the visible output tokens, per stage."""

CHECK_DELAY_S = 0.2

Sleep = Callable[[float], Awaitable[None]]


def simulated_tokens(text: str) -> int:
    """Simulated token count of ``text`` (at least 1 for non-empty text)."""
    return 0 if not text else max(1, round(len(text) / CHARS_PER_TOKEN))


def _prompt_chars(request: CompletionRequest) -> str:
    messages = (*request.prompt.stable_prefix, *request.prompt.variable_suffix)
    return "\n".join(m.content for m in messages)


class DemoProvider:
    """A :class:`~llm_crossjury.core.providers.base.Provider` that plays the demo script.

    Args:
        name: Provider id (``anthropic``, ``openai``, ``gemini`` or ``xai``).
        backend: ``api`` or ``cli``.
        capabilities_from: The real adapter whose ``capabilities()`` this one reports.
        latency_scale: Multiplies every simulated delay (``0`` = no delay, for tests).
        product: Display name of the subscription CLI, for the status text.
        sleep: Awaitable sleep (injectable for tests).
    """

    def __init__(
        self,
        name: str,
        backend: Backend,
        *,
        capabilities_from: Provider,
        latency_scale: float = 1.0,
        product: str | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.name = name
        self.backend = backend
        self._source = capabilities_from
        self._scale = latency_scale
        self._product = product
        self._sleep = sleep
        self.calls = 0

    def capabilities(self, model: str) -> Capabilities:
        """The real adapter's capabilities for ``model``."""
        return self._source.capabilities(model)

    def _delay(self, request: CompletionRequest) -> float:
        stage = request.tags.get("stage", "answer")
        base = STAGE_DELAY_S.get(stage, ANSWER_DELAY_S.get(self.name, 1.0))
        return base * self._scale

    def _usage(self, request: CompletionRequest, text: str) -> Usage:
        if self.backend is Backend.CLI:
            return Usage(
                input_tokens=None,
                cached_input_tokens=None,
                cache_write_tokens=None,
                output_tokens=None,
                reasoning_tokens=None,
            )
        output = simulated_tokens(text)
        share = REASONING_SHARE.get(request.tags.get("stage", "answer"), 0.5)
        return Usage(
            input_tokens=simulated_tokens(_prompt_chars(request)),
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=output,
            reasoning_tokens=round(output * share),
        )

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Wait the simulated latency, then answer from the demo script."""
        self.calls += 1
        started = datetime.now(UTC)
        delay = self._delay(request)
        if delay > 0:
            await self._sleep(delay)
        text = responder.respond(self.name, request)
        return CompletionResult(
            text=text,
            usage=self._usage(request, text),
            provider=self.name,
            model=request.model,
            backend=self.backend,
            finish_reason="stop",
            started_at=started,
            latency_s=None if self.backend is Backend.CLI else delay,
            native_params_used=dict(request.native_params),
        )

    async def count_input_tokens(self, request: CompletionRequest) -> int | None:
        """No vendor counter in demo mode: the local estimate is used."""
        return None

    async def check(self) -> ProviderStatus:
        """Report the simulated CLI as found (nothing is executed)."""
        if self._scale > 0:
            await self._sleep(CHECK_DELAY_S * self._scale)
        product = self._product or self.name
        return ProviderStatus(
            provider=self.name,
            backend=self.backend,
            available=True,
            detail=f"Demo mode: {product} is simulated; nothing is executed.",
            version="demo",
        )


__all__ = ["DemoProvider", "simulated_tokens"]
