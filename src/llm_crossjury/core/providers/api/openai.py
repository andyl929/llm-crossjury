"""OpenAI via the official ``openai`` SDK (Responses API).

Why the Responses API: OpenAI recommends it for new text generation work. The
openai-python README says "The primary API for interacting with OpenAI models is the
Responses API" (https://github.com/openai/openai-python), and the text generation
guide says "we recommend using the Responses API over the older Chat Completions API"
(https://developers.openai.com/api/docs/guides/text).

Verified 2026-11-08 against:

* Roles: the text guide passes instructions as ``developer`` role messages in
  ``input``, so ``Role.SYSTEM`` maps to ``developer``.
* Prompt caching is "enabled by default for supported OpenAI models" and works on
  exact prefixes, so ``cache_prefix`` needs no explicit marker. The adapter keeps
  ``stable_prefix`` first and byte-identical
  (https://developers.openai.com/api/docs/guides/prompt-caching).
* Usage: ``input_tokens_details.cached_tokens`` and ``cache_write_tokens`` are both
  included in ``input_tokens`` and do not overlap (prompt-caching guide). Reasoning
  tokens are reported in ``output_tokens_details.reasoning_tokens`` and billed as
  output tokens; the reasoning guide's usage example shows them inside
  ``output_tokens`` (https://developers.openai.com/api/docs/guides/reasoning).
* Incomplete responses have ``status="incomplete"`` and
  ``incomplete_details.reason`` (e.g. ``"max_output_tokens"``) (reasoning guide).
* Structured output: ``text.format = {"type": "json_schema", "name", "schema",
  "strict": true}``; refusals are ``refusal`` content parts
  (https://developers.openai.com/api/docs/guides/structured-outputs).
* Input token counting: ``POST /v1/responses/input_tokens``, exposed by the SDK as
  ``client.responses.input_tokens.count``. It accepts image and file parts "as you
  would for ``responses.create``", so attachments are counted
  (https://developers.openai.com/api/docs/guides/token-counting).
* Attachments (ADR 0007): PDFs go inline as ``input_file`` with a base64
  ``file_data`` data URL (https://developers.openai.com/api/docs/guides/pdf-files);
  images as ``input_image`` with a base64 data URL. Accepted image types: "PNG
  (``.png``), JPEG (``.jpeg`` or ``.jpg``), WEBP (``.webp``), and non-animated GIF
  (``.gif``)" (https://developers.openai.com/api/docs/guides/images-vision).
  ``detail`` defaults to ``auto`` for both and is not sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from llm_crossjury.core.providers.api._openai_compat import OpenAICompatProvider

if TYPE_CHECKING:
    from llm_crossjury.core.registry.resolver import ModelRegistry


class OpenAIApiProvider(OpenAICompatProvider):
    """OpenAI models through the OpenAI Responses API.

    Caching: automatic prefix caching on the vendor side; the adapter only keeps
    ``Prompt.stable_prefix`` first and byte-identical, so ``cache_prefix`` needs no
    explicit marker.

    Args:
        api_key_env: Override for the key's environment variable (default
            ``OPENAI_API_KEY``).
        registry: Model registry; capabilities come only from its entries.
    """

    name = "openai"
    api_key_env = "OPENAI_API_KEY"
    system_role = "developer"
    base_url = None
    reasoning_in_output = True
    supports_input_token_count = True
    documents_prompt_caching = True
    native_image_media_types = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})

    def __init__(
        self,
        api_key_env: str | None = None,
        *,
        registry: ModelRegistry | None = None,
    ) -> None:
        super().__init__(api_key_env, registry=registry)
