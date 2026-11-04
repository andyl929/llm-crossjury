"""Deterministic in-process provider for tests.

This is test infrastructure, not business logic: it never touches the network and
returns whatever its ``responder`` produces.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from llm_crossjury.core.providers.base import (
    Backend,
    Capabilities,
    CompletionRequest,
    CompletionResult,
    ProviderStatus,
    Usage,
)

Responder = Callable[[CompletionRequest], str]


class MockProvider:
    """A :class:`~llm_crossjury.core.providers.base.Provider` backed by a callable.

    Args:
        name: Provider id reported in results.
        responder: Maps a request to the response text.
        capabilities: Capabilities reported for every model.
    """

    backend: Backend = Backend.MOCK

    def __init__(
        self,
        name: str,
        responder: Responder,
        capabilities: Capabilities | None = None,
    ) -> None:
        self.name = name
        self._responder = responder
        self._capabilities = capabilities or Capabilities(
            structured_output=True, caching=False, batch=False, temperature=True
        )
        self.requests: list[CompletionRequest] = []

    def capabilities(self, model: str) -> Capabilities:
        return self._capabilities

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        return CompletionResult(
            text=self._responder(request),
            usage=Usage(
                input_tokens=0,
                cached_input_tokens=0,
                cache_write_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
            ),
            provider=self.name,
            model=request.model,
            backend=self.backend,
            finish_reason="stop",
            started_at=datetime.now(UTC),
            latency_s=0.0,
        )

    async def count_input_tokens(self, request: CompletionRequest) -> int | None:
        return 0

    async def check(self) -> ProviderStatus:
        return ProviderStatus(
            provider=self.name,
            backend=self.backend,
            available=True,
            detail="mock provider",
            version=None,
        )
