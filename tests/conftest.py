"""Shared test configuration.

Network access is blocked by pytest-socket (``--allow-hosts=127.0.0.1,::1`` in
pyproject.toml); only loopback is permitted, which asyncio needs on Windows.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure no test can pick up a developer's real API keys."""
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
