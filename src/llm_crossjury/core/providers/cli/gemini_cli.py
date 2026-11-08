"""Gemini via the official ``gemini`` CLI: skipped in v1 (ADR 0002, S6).

Verified 2026-11-08:

* https://github.com/google-gemini/gemini-cli/discussions/27274 (maintainer
  announcement, 2026-05-19): "On June 18, 2026, Gemini CLI will stop serving requests
  for Google AI Pro and Ultra", and the same applies to free use; "These tiers are
  now supported via Antigravity CLI". Gemini CLI stays available only with paid API
  keys, which the ``cli`` backend never forwards (ADR 0002, rules 4 and 10).
* https://antigravity.google/docs/faq/: under consumer plans, "Wrapping ``agy``
  inside third-party coding agents or applications, or automating account rotation,
  is not supported".

So no consumer-plan CLI from Google can back a ``cli`` member. Gemini users use the
``api`` backend with an AI Studio key.
"""

from __future__ import annotations

from pathlib import Path

from llm_crossjury.core.providers.base import (
    Capabilities,
    CompletionRequest,
    CompletionResult,
    ProviderStatus,
    ProviderUnavailableError,
)
from llm_crossjury.core.providers.cli._subprocess import CliInvocation, CliProviderBase

GEMINI_CLI_SKIPPED_REASON = (
    "S6: Google consumer plans no longer work with Gemini CLI since 2026-06-18 "
    "(https://github.com/google-gemini/gemini-cli/discussions/27274), and wrapping "
    "its successor `agy` in third-party applications is not supported under consumer "
    "plans (https://antigravity.google/docs/faq/). Use the api backend with an AI "
    "Studio key instead."
)


class GeminiCliProvider(CliProviderBase):
    """Skipped Gemini CLI adapter: every call raises ``ProviderUnavailableError``.

    See :data:`GEMINI_CLI_SKIPPED_REASON`. The executable is never launched, not
    even for a version check.
    """

    name = "gemini"
    command = "gemini"
    skipped_reason: str | None = GEMINI_CLI_SKIPPED_REASON

    def _unavailable(self) -> ProviderUnavailableError:
        return ProviderUnavailableError(
            f"gemini cli backend is not supported: {self.skipped_reason}"
        )

    def capabilities(self, model: str) -> Capabilities:
        """Nothing is supported."""
        return Capabilities(
            structured_output=False,
            caching=False,
            batch=False,
            temperature=False,
            reasoning=False,
        )

    def build_invocation(self, request: CompletionRequest, cwd: Path) -> CliInvocation:
        """Always raises: the CLI is skipped.

        Raises:
            ProviderUnavailableError: always.
        """
        raise self._unavailable()

    def parse_output(self, stdout: str, request: CompletionRequest) -> CompletionResult:
        """Always raises: the CLI is skipped.

        Raises:
            ProviderUnavailableError: always.
        """
        raise self._unavailable()

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Always raises: the CLI is skipped.

        Raises:
            ProviderUnavailableError: always, with :data:`GEMINI_CLI_SKIPPED_REASON`.
        """
        raise self._unavailable()

    async def count_input_tokens(self, request: CompletionRequest) -> int | None:
        """Always raises: the CLI is skipped.

        Raises:
            ProviderUnavailableError: always.
        """
        raise self._unavailable()

    async def check(self) -> ProviderStatus:
        """Always raises without launching anything: the CLI is skipped.

        Raises:
            ProviderUnavailableError: always, with :data:`GEMINI_CLI_SKIPPED_REASON`.
        """
        raise self._unavailable()
