"""Google Gemini via the official Google Gen AI SDK (``google-genai``).

Uses the ``generateContent`` endpoint through ``client.aio.models``.

Verified against (2026-11-08):

* SDK README: https://github.com/googleapis/python-genai
  (``genai.Client(api_key=...)``, ``await client.aio.models.generate_content(...)``,
  config passed as a dict, ``system_instruction``, ``max_output_tokens``,
  ``response_mime_type`` + ``response_json_schema``, ``count_tokens``,
  ``google.genai.errors.APIError`` with ``code``).
* generateContent reference: https://aistudio.google.com/api-reference/generate-content
  (formerly https://ai.google.dev/api/generate-content; ``UsageMetadata``:
  ``promptTokenCount`` includes cached content, ``cachedContentTokenCount``,
  ``candidatesTokenCount``, ``thoughtsTokenCount``; total = prompt + tool-use prompt
  + thoughts + candidates; ``FinishReason`` values).
* Caching: https://aistudio.google.com/docs/caching (formerly
  https://ai.google.dev/gemini-api/docs/caching; implicit caching is enabled by
  default for Gemini 2.5 and newer models).
* Tokens: https://aistudio.google.com/docs/tokens (``count_tokens`` counts input
  only and returns ``total_tokens``).
* Structured output: https://aistudio.google.com/docs/structured-output
  (type arrays such as ``["string", "null"]``, ``additionalProperties``,
  ``required`` and ``enum`` are supported).
* Text generation: https://aistudio.google.com/docs/text-generation (newer models
  ignore custom ``temperature``; it is passed through and ignored, not rejected).

SDK quirks (installed ``google-genai``, checked in its source):

* ``count_tokens`` in Gemini Developer API mode raises ``ValueError`` if
  ``config.system_instruction`` is set. System text is therefore counted as a
  leading user turn; the result is an estimate.
* ``HttpOptions.timeout`` is in **milliseconds**.
* Importing ``google.genai`` the first time (no ``.pyc`` yet) emits a
  ``SyntaxWarning`` from its ``types.py``, which pytest's ``-W error`` turns into a
  failure. The SDK is therefore imported lazily, only when a real client is built.
"""

from __future__ import annotations

import time
from typing import Any

from llm_crossjury.core.providers.api._common import (
    ApiProviderBase,
    deep_merge,
    sanitize_error,
    suffix_with_format_instructions,
    utc_now,
    validate_prompt,
)
from llm_crossjury.core.providers.base import (
    CompletionRequest,
    CompletionResult,
    Message,
    Role,
    Usage,
)

_ROLE_MAP = {Role.USER: "user", Role.ASSISTANT: "model"}


def _append_turn(contents: list[dict[str, Any]], role: str, text: str) -> None:
    part = {"text": text}
    if contents and contents[-1]["role"] == role:
        contents[-1]["parts"].append(part)
    else:
        contents.append({"role": role, "parts": [part]})


def _conversation(
    request: CompletionRequest, *, native_structured_output: bool
) -> tuple[list[str], list[dict[str, Any]]]:
    """Return ``(system_texts, contents)`` for ``request``.

    Consecutive turns with the same role are merged into one ``Content`` with
    several parts. Format instructions are appended to the last user message of the
    suffix when a schema is requested without native structured output.
    """
    prompt = request.prompt
    validate_prompt(prompt.stable_prefix, prompt.variable_suffix)
    suffix: tuple[Message, ...] = prompt.variable_suffix
    if request.response_schema is not None and not native_structured_output:
        suffix = suffix_with_format_instructions(suffix, request.response_schema)
    system_texts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in (*prompt.stable_prefix, *suffix):
        if message.role is Role.SYSTEM:
            system_texts.append(message.content)
        else:
            _append_turn(contents, _ROLE_MAP[message.role], message.content)
    return system_texts, contents


def build_generate_request(
    request: CompletionRequest, *, native_structured_output: bool
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build ``(contents, config)`` for ``generate_content``.

    Mapping (config is ``GenerateContentConfig`` in dict form):

    * System messages -> ``system_instruction`` (one part per message).
    * User/assistant messages -> ``contents`` with roles ``user``/``model``.
    * ``max_output_tokens`` and ``temperature`` are passed through when not ``None``
      (Gemini does not require ``max_output_tokens``).
    * ``response_schema`` -> ``response_mime_type: application/json`` +
      ``response_json_schema`` when ``native_structured_output``; otherwise format
      instructions are appended to the last user message.
    * Caching is implicit (vendor-side prefix caching); the stable prefix is simply
      kept first and unchanged. ``cache_prefix`` needs no request field.
    * ``timeout_s`` -> ``http_options.timeout`` in milliseconds.
    * ``native_params`` is deep-merged into the config last and wins.
    """
    system_texts, contents = _conversation(
        request, native_structured_output=native_structured_output
    )
    config: dict[str, Any] = {}
    if system_texts:
        config["system_instruction"] = {"parts": [{"text": t} for t in system_texts]}
    if request.max_output_tokens is not None:
        config["max_output_tokens"] = request.max_output_tokens
    if request.temperature is not None:
        config["temperature"] = request.temperature
    if request.response_schema is not None and native_structured_output:
        config["response_mime_type"] = "application/json"
        config["response_json_schema"] = request.response_schema
    if request.timeout_s is not None:
        config["http_options"] = {"timeout": max(1, round(request.timeout_s * 1000))}
    return contents, deep_merge(config, request.native_params)


def map_usage(metadata: Any) -> Usage:
    """Map Gemini ``usage_metadata`` onto :class:`Usage`.

    * ``input_tokens`` <- ``prompt_token_count - cached_content_token_count``
      (``prompt_token_count`` includes cached content).
    * ``cached_input_tokens`` <- ``cached_content_token_count``.
    * ``cache_write_tokens`` <- 0: implicit caching reports no write count and
      bills no separate write; written tokens are part of ``prompt_token_count``.
    * ``output_tokens`` <- ``candidates_token_count`` (excludes thoughts: the
      reference defines the total as prompt + tool-use prompt + thoughts +
      candidates).
    * ``reasoning_tokens`` <- ``thoughts_token_count``.

    The API omits zero-valued counters (proto3 JSON), so when ``usage_metadata`` is
    present a missing counter means 0. A missing ``usage_metadata``, or a missing
    ``prompt_token_count``, makes the affected fields ``None`` (unknown).
    ``tool_use_prompt_token_count`` is ignored (no tools are sent).
    """
    if metadata is None:
        return Usage(
            input_tokens=None,
            cached_input_tokens=None,
            cache_write_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
        )
    prompt = getattr(metadata, "prompt_token_count", None)
    cached = getattr(metadata, "cached_content_token_count", None) or 0
    if isinstance(prompt, int):
        input_tokens: int | None = max(prompt - cached, 0)
        cached_tokens: int | None = cached
        cache_write: int | None = 0
    else:
        input_tokens = cached_tokens = cache_write = None
    return Usage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        cache_write_tokens=cache_write,
        output_tokens=getattr(metadata, "candidates_token_count", None) or 0,
        reasoning_tokens=getattr(metadata, "thoughts_token_count", None) or 0,
    )


def _enum_text(value: Any) -> str | None:
    if value is None:
        return None
    inner = getattr(value, "value", value)
    return str(inner)


def _first_candidate(response: Any) -> Any:
    candidates = getattr(response, "candidates", None) or []
    return candidates[0] if candidates else None


def _response_text(response: Any) -> str:
    """Concatenate the first candidate's text parts, skipping thought summaries."""
    candidate = _first_candidate(response)
    content = getattr(candidate, "content", None) if candidate is not None else None
    parts = getattr(content, "parts", None) or []
    return "".join(
        part.text
        for part in parts
        if isinstance(getattr(part, "text", None), str)
        and getattr(part, "thought", None) is not True
    )


def _finish_reason(response: Any) -> str | None:
    candidate = _first_candidate(response)
    if candidate is not None:
        return _enum_text(getattr(candidate, "finish_reason", None))
    feedback = getattr(response, "prompt_feedback", None)
    block_reason = _enum_text(getattr(feedback, "block_reason", None))
    return f"BLOCKED: {block_reason}" if block_reason else None


class GeminiApiProvider(ApiProviderBase):
    """Gemini models through the Gemini Developer API (``generateContent``).

    * Caching: implicit, vendor-side prefix caching (documented for Gemini 2.5 and
      newer); the adapter only keeps ``Prompt.stable_prefix`` first and unchanged.
    * Structured output: native ``response_json_schema`` when the registry entry
      says ``structured_output: true``; the canonical strict schema is passed
      unchanged (Gemini accepts type arrays for nullables). Otherwise the
      format-instruction fallback.
    * Token counting: ``models.count_tokens`` (see the module docstring for the
      system-instruction quirk).
    * Batches: not implemented (MVP).
    """

    name = "gemini"
    api_key_env = "GEMINI_API_KEY"
    sdk_distribution = "google-genai"
    vendor_caching = True

    def _make_client(self, api_key: str) -> Any:
        """Create a ``genai.Client``. Tests replace this method.

        A fresh client per call keeps the key off the provider instance.
        """
        from google import genai  # noqa: PLC0415 - optional SDK, lazy import (see module doc)

        return genai.Client(api_key=api_key)

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Run one ``generate_content`` call. See :func:`build_generate_request` and
        :func:`map_usage` for the request and usage mapping."""
        native = self.capabilities(request.model).structured_output
        contents, config = build_generate_request(request, native_structured_output=native)
        api_key = self._api_key()
        started_at = utc_now()
        start = time.perf_counter()
        client = self._make_client(api_key)
        try:
            response = await client.aio.models.generate_content(
                model=request.model, contents=contents, config=config
            )
        except Exception as exc:
            raise sanitize_error(self.name, exc, api_key) from None
        finally:
            await client.aio.aclose()
        latency = time.perf_counter() - start
        model = getattr(response, "model_version", None)
        return CompletionResult(
            text=_response_text(response),
            usage=map_usage(getattr(response, "usage_metadata", None)),
            provider=self.name,
            model=model if isinstance(model, str) and model else request.model,
            backend=self.backend,
            finish_reason=_finish_reason(response),
            started_at=started_at,
            latency_s=latency,
            native_params_used=dict(request.native_params),
        )

    async def count_input_tokens(self, request: CompletionRequest) -> int | None:
        """Count input tokens with ``models.count_tokens`` (an estimate).

        The SDK rejects ``system_instruction`` in Developer API mode, so system
        messages are counted as a leading user turn. Generation config (and with it
        a native response schema) is not counted.
        """
        native = self.capabilities(request.model).structured_output
        system_texts, contents = _conversation(request, native_structured_output=native)
        if system_texts:
            contents = [{"role": "user", "parts": [{"text": t} for t in system_texts]}, *contents]
        config: dict[str, Any] | None = None
        if request.timeout_s is not None:
            config = {"http_options": {"timeout": max(1, round(request.timeout_s * 1000))}}
        api_key = self._api_key()
        client = self._make_client(api_key)
        try:
            response = await client.aio.models.count_tokens(
                model=request.model, contents=contents, config=config
            )
        except Exception as exc:
            raise sanitize_error(self.name, exc, api_key) from None
        finally:
            await client.aio.aclose()
        total = getattr(response, "total_tokens", None)
        return total if isinstance(total, int) else None
