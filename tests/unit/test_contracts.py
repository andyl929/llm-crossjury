"""Shape checks for the provider contract and skeleton placeholders."""

from __future__ import annotations

import socket

import pytest
from pytest_socket import SocketConnectBlockedError

from llm_crossjury.core.providers.api import AnthropicApiProvider
from llm_crossjury.core.providers.base import Provider, Usage
from llm_crossjury.core.providers.cli import ClaudeCliProvider, GrokCliProvider
from llm_crossjury.core.providers.mock import MockProvider


def test_mock_provider_satisfies_protocol() -> None:
    assert isinstance(MockProvider("m", lambda _req: ""), Provider)


def test_adapters_satisfy_protocol_shape() -> None:
    assert isinstance(AnthropicApiProvider(), Provider)
    assert isinstance(ClaudeCliProvider(), Provider)


def test_usage_allows_unknown_fields() -> None:
    usage = Usage(
        input_tokens=None,
        cached_input_tokens=None,
        cache_write_tokens=None,
        output_tokens=None,
        reasoning_tokens=None,
    )
    assert usage.output_tokens is None


def test_grok_cli_is_skipped_with_reason() -> None:
    assert GrokCliProvider.skipped_reason


def test_network_is_blocked() -> None:
    # TEST-NET-3 address: never routable, and no DNS lookup is involved.
    with pytest.raises(SocketConnectBlockedError):
        socket.create_connection(("203.0.113.1", 80), timeout=0.1)
