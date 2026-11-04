"""Shared scaffolding for ``api`` adapters."""

from __future__ import annotations

from collections.abc import Sequence

from llm_crossjury.core.providers.base import (
    Backend,
    BatchHandle,
    BatchState,
    Capabilities,
    CompletionRequest,
    CompletionResult,
    ProviderStatus,
)


class ApiProviderBase:
    """Common skeleton for SDK-backed providers.

    Subclasses set ``name`` and ``api_key_env``. Capabilities are resolved per model
    in the implementation phase from verified vendor documentation; they are not
    assumed here.

    Args:
        api_key_env: Name of the environment variable holding the key. The value is
            read lazily and is never stored on the instance in a printable form.
    """

    name: str = ""
    backend: Backend = Backend.API
    api_key_env: str = ""

    def __init__(self, api_key_env: str | None = None) -> None:
        if api_key_env is not None:
            self.api_key_env = api_key_env

    def capabilities(self, model: str) -> Capabilities:
        raise NotImplementedError

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        raise NotImplementedError

    async def count_input_tokens(self, request: CompletionRequest) -> int | None:
        raise NotImplementedError

    async def check(self) -> ProviderStatus:
        """Report whether the key variable is set and the SDK is importable.

        Must not reveal any part of the key (not even its length or prefix).
        """
        raise NotImplementedError

    async def submit_batch(self, requests: Sequence[CompletionRequest]) -> BatchHandle:
        raise NotImplementedError

    async def poll_batch(self, handle: BatchHandle) -> BatchState:
        raise NotImplementedError

    async def collect_batch(self, handle: BatchHandle) -> list[CompletionResult]:
        raise NotImplementedError
