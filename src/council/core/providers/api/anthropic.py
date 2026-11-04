"""Anthropic (Claude) via the official ``anthropic`` SDK."""

from council.core.providers.api._common import ApiProviderBase


class AnthropicApiProvider(ApiProviderBase):
    """Claude models through the Anthropic API.

    Caching: the end of ``Prompt.stable_prefix`` maps to an explicit cache breakpoint.
    """

    name = "anthropic"
    api_key_env = "ANTHROPIC_API_KEY"
