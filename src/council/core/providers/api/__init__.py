"""``api`` backend: official vendor SDKs, authenticated by user-supplied API keys.

Keys are read from environment variables (loaded from ``.env``) at call time and are
never logged, printed, persisted, or included in exception messages. The vendor SDKs
are optional extras added in the implementation phase; this package must import
without them.
"""

from council.core.providers.api.anthropic import AnthropicApiProvider
from council.core.providers.api.gemini import GeminiApiProvider
from council.core.providers.api.openai import OpenAIApiProvider
from council.core.providers.api.xai import XaiApiProvider

__all__ = ["AnthropicApiProvider", "GeminiApiProvider", "OpenAIApiProvider", "XaiApiProvider"]
