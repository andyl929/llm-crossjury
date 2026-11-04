"""``api`` backend: official vendor SDKs, authenticated by user-supplied API keys.

Keys are read from environment variables (loaded from ``.env``) at call time and are
never logged, printed, persisted, or included in exception messages. The vendor SDKs
are optional extras added in the implementation phase; this package must import
without them.
"""

from llm_crossjury.core.providers.api.anthropic import AnthropicApiProvider
from llm_crossjury.core.providers.api.gemini import GeminiApiProvider
from llm_crossjury.core.providers.api.openai import OpenAIApiProvider
from llm_crossjury.core.providers.api.xai import XaiApiProvider

__all__ = ["AnthropicApiProvider", "GeminiApiProvider", "OpenAIApiProvider", "XaiApiProvider"]
