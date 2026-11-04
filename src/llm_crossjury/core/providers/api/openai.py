"""OpenAI via the official ``openai`` SDK."""

from llm_crossjury.core.providers.api._common import ApiProviderBase


class OpenAIApiProvider(ApiProviderBase):
    """OpenAI models through the OpenAI API.

    Caching: relies on the vendor's prefix-based caching; the adapter only has to
    keep ``Prompt.stable_prefix`` first and byte-identical.
    """

    name = "openai"
    api_key_env = "OPENAI_API_KEY"
