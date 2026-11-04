"""Google Gemini via the official Google Gen AI SDK."""

from llm_crossjury.core.providers.api._common import ApiProviderBase


class GeminiApiProvider(ApiProviderBase):
    """Gemini models through the Gemini API.

    Structured output: the canonical strict schema may need a lossless dialect
    transform (see ADR 0001); that transform lives in this adapter.
    """

    name = "gemini"
    api_key_env = "GEMINI_API_KEY"
