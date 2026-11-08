"""xAI (Grok) through the official ``openai`` SDK pointed at xAI's API.

xAI documents that its REST API "is compatible with the OpenAI REST API"
(https://docs.x.ai/developers/rest-api-reference), and its text generation guide
shows the ``openai`` SDK with ``base_url="https://api.x.ai/v1"``. The same guide
says "The Responses API is the preferred way of interacting with our models via API"
(https://docs.x.ai/developers/model-capabilities/text/generate-text), and Chat
Completions "is offered as a legacy endpoint"
(https://docs.x.ai/docs/guides/chat-completions). So this adapter reuses the
OpenAI Responses implementation.

Verified 2026-11-08 against:

* Roles: xAI's examples send ``system`` role messages in ``input``.
* Prompt caching is automatic for shared starting messages
  (https://docs.x.ai/developers/advanced-api-usage/prompt-caching); ``cache_prefix``
  needs no explicit marker. Cached tokens are reported in
  ``usage.input_tokens_details.cached_tokens`` and are part of the prompt tokens; no
  cache-write count is reported
  (https://docs.x.ai/developers/advanced-api-usage/prompt-caching/usage-and-pricing).
* Usage: the Responses reference example reports ``output_tokens: 9`` with
  ``reasoning_tokens: 110`` and ``total_tokens: 151`` for 32 input tokens, so
  reasoning tokens are *not* part of ``output_tokens``
  (https://docs.x.ai/developers/rest-api-reference/inference/responses).
  ``total_tokens`` still decides per response (see ``map_usage``).
* ``temperature`` and ``max_output_tokens`` are accepted (Responses reference).
* Structured output uses ``text.format`` with ``type: json_schema``
  (https://docs.x.ai/developers/model-capabilities/text/structured-outputs).
* Input token counting: the Responses reference documents no
  ``/v1/responses/input_tokens`` endpoint, so ``count_input_tokens`` returns ``None``.
* xAI recommends a long client timeout for reasoning models; the adapter passes
  ``timeout_s`` through and does not invent a default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from llm_crossjury.core.providers.api._openai_compat import OpenAICompatProvider

if TYPE_CHECKING:
    from llm_crossjury.core.registry.resolver import ModelRegistry

XAI_BASE_URL = "https://api.x.ai/v1"
"""xAI API base URL for the OpenAI SDK, as documented in
https://docs.x.ai/developers/model-capabilities/text/generate-text."""


class XaiApiProvider(OpenAICompatProvider):
    """Grok models through the xAI Responses API.

    Caching: automatic prefix caching on the vendor side; the adapter keeps
    ``Prompt.stable_prefix`` first and byte-identical.

    Args:
        api_key_env: Override for the key's environment variable (default
            ``XAI_API_KEY``).
        registry: Model registry; capabilities come only from its entries.
    """

    name = "xai"
    api_key_env = "XAI_API_KEY"
    system_role = "system"
    base_url = XAI_BASE_URL
    reasoning_in_output = False
    supports_input_token_count = False
    documents_prompt_caching = True

    def __init__(
        self,
        api_key_env: str | None = None,
        *,
        registry: ModelRegistry | None = None,
    ) -> None:
        super().__init__(api_key_env, registry=registry)
