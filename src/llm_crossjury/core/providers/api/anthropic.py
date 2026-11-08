"""Anthropic (Claude) via the official ``anthropic`` SDK (API version 2026-10-21).

Verified against (2026-11-08):

* Python SDK: https://platform.claude.com/docs/en/api/sdks/python
  (``AsyncAnthropic(api_key=...)``, ``await client.messages.create(...)``, per-request
  ``timeout``, ``await client.close()``, ``anthropic.__version__``, error classes with
  ``status_code``).
* Messages API: https://platform.claude.com/docs/en/api/messages/create
  (``max_tokens`` is required, ``system`` is a string or a list of text blocks,
  ``stop_reason``, ``usage`` incl. ``output_tokens_details.thinking_tokens``).
* Prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
  (``"cache_control": {"type": "ephemeral"}`` on the last content block of the
  cached prefix; ``input_tokens`` counts only tokens after the last breakpoint).
* Token counting: https://platform.claude.com/docs/en/build-with-claude/token-counting
  (``client.messages.count_tokens(...)`` returns ``input_tokens``; free of charge).
* Structured outputs: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
  (GA; ``output_config={"format": {"type": "json_schema", "schema": ...}}``; the JSON
  is returned in the text content block).
* API version migration: https://platform.claude.com/docs/en/api/migration-2026-10-21
  (SDK >= 2.0 uses API version ``2026-10-21``, in which ``temperature``, ``top_p``
  and ``top_k`` are removed and return HTTP 400).
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
    Capabilities,
    CompletionRequest,
    CompletionResult,
    Message,
    ProviderError,
    Role,
    Usage,
)

_CACHE_BREAKPOINT: dict[str, str] = {"type": "ephemeral"}

# Request keys accepted by ``messages.count_tokens`` (installed SDK signature and
# https://platform.claude.com/docs/en/api/messages/count_tokens). Other
# ``native_params`` keys (e.g. ``max_tokens``, ``metadata``) only affect generation.
_COUNT_TOKENS_KEYS = frozenset(
    {
        "model",
        "messages",
        "system",
        "thinking",
        "tools",
        "tool_choice",
        "output_config",
        "cache_control",
        "speed",
    }
)


def _text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def build_messages_params(
    request: CompletionRequest, *, native_structured_output: bool, for_counting: bool = False
) -> dict[str, Any]:
    """Build the ``messages.create`` kwargs for ``request`` (without ``timeout``).

    Mapping:

    * System messages (only allowed in ``stable_prefix``) become ``system`` text
      blocks, in order.
    * All other messages become ``messages``; consecutive messages with the same
      role are merged into one message with several text blocks.
    * If ``cache_prefix`` is set, the last content block that comes from
      ``stable_prefix`` gets ``cache_control: {"type": "ephemeral"}``: the last
      prefix message block if the prefix has non-system messages, otherwise the last
      system block.
    * ``response_schema`` goes to ``output_config.format`` when
      ``native_structured_output``; otherwise format instructions are appended to
      the last user message of the suffix.
    * ``temperature`` is never sent: API version 2026-10-21 rejects it.
    * ``native_params`` is deep-merged last and wins.

    Args:
        request: The completion request.
        native_structured_output: Send ``response_schema`` natively.
        for_counting: Build params for ``count_tokens``: ``max_tokens`` is neither
            required nor included.

    Raises:
        ProviderError: if ``max_output_tokens`` is ``None`` when generating
            (Anthropic requires ``max_tokens``) or the prompt violates its invariants.
    """
    prompt = request.prompt
    validate_prompt(prompt.stable_prefix, prompt.variable_suffix)
    if request.max_output_tokens is None and not for_counting:
        raise ProviderError("anthropic requires max_output_tokens (max_tokens); none was given")

    suffix: tuple[Message, ...] = prompt.variable_suffix
    if request.response_schema is not None and not native_structured_output:
        suffix = suffix_with_format_instructions(suffix, request.response_schema)

    system_blocks: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    last_prefix_block: dict[str, Any] | None = None

    def add(message: Message) -> dict[str, Any]:
        block = _text_block(message.content)
        if messages and messages[-1]["role"] == message.role.value:
            messages[-1]["content"].append(block)
        else:
            messages.append({"role": message.role.value, "content": [block]})
        return block

    last_system_block: dict[str, Any] | None = None
    for message in prompt.stable_prefix:
        if message.role is Role.SYSTEM:
            last_system_block = _text_block(message.content)
            system_blocks.append(last_system_block)
        else:
            last_prefix_block = add(message)
    for message in suffix:
        add(message)

    if request.cache_prefix:
        breakpoint_block = last_prefix_block or last_system_block
        if breakpoint_block is not None:
            breakpoint_block["cache_control"] = dict(_CACHE_BREAKPOINT)

    params: dict[str, Any] = {"model": request.model, "messages": messages}
    if not for_counting:
        params["max_tokens"] = request.max_output_tokens
    if system_blocks:
        params["system"] = system_blocks
    if request.response_schema is not None and native_structured_output:
        params["output_config"] = {
            "format": {"type": "json_schema", "schema": request.response_schema}
        }
    return deep_merge(params, request.native_params)


def map_usage(usage: Any) -> Usage:
    """Map an Anthropic ``usage`` object onto :class:`Usage`.

    * ``input_tokens`` <- ``usage.input_tokens`` (tokens after the last cache
      breakpoint, i.e. uncached input).
    * ``cached_input_tokens`` <- ``usage.cache_read_input_tokens``.
    * ``cache_write_tokens`` <- ``usage.cache_creation_input_tokens``.
      The docs define total input as the sum of these three fields, so a ``null``
      cache field (no caching involved) is mapped to 0, not unknown.
    * ``output_tokens`` <- ``usage.output_tokens - output_tokens_details.thinking_tokens``
      (``output_tokens`` is the inclusive billing total).
    * ``reasoning_tokens`` <- ``output_tokens_details.thinking_tokens``.
      If ``output_tokens_details`` is absent, ``reasoning_tokens`` is ``None``
      (unknown) and ``output_tokens`` is the inclusive total.

    A missing ``usage`` object maps to all ``None``.
    """
    if usage is None:
        return Usage(
            input_tokens=None,
            cached_input_tokens=None,
            cache_write_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
        )
    input_tokens = getattr(usage, "input_tokens", None)
    output_total = getattr(usage, "output_tokens", None)
    details = getattr(usage, "output_tokens_details", None)
    thinking = getattr(details, "thinking_tokens", None) if details is not None else None
    if isinstance(output_total, int) and isinstance(thinking, int):
        visible: int | None = max(output_total - thinking, 0)
    else:
        visible = output_total if isinstance(output_total, int) else None
    if input_tokens is None:
        cached: int | None = None
        written: int | None = None
    else:
        cached = getattr(usage, "cache_read_input_tokens", None) or 0
        written = getattr(usage, "cache_creation_input_tokens", None) or 0
    return Usage(
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        cache_write_tokens=written,
        output_tokens=visible,
        reasoning_tokens=thinking if isinstance(thinking, int) else None,
    )


def _response_text(response: Any) -> str:
    return "".join(
        block.text
        for block in (getattr(response, "content", None) or [])
        if getattr(block, "type", None) == "text"
    )


class AnthropicApiProvider(ApiProviderBase):
    """Claude models through the Anthropic Messages API.

    * Caching: the end of ``Prompt.stable_prefix`` maps to an explicit
      ``cache_control`` breakpoint when ``cache_prefix`` is set.
    * Temperature: not supported. The installed SDK (>= 2.0) speaks API version
      2026-10-21, which rejects ``temperature`` with HTTP 400, so
      ``capabilities().temperature`` is False and the value is never sent.
    * Structured output: native ``output_config.format`` when the registry entry
      says ``structured_output: true``, else the format-instruction fallback.
    * Token counting: the free ``messages.count_tokens`` endpoint.
    * Batches: not implemented (MVP).
    """

    name = "anthropic"
    api_key_env = "ANTHROPIC_API_KEY"
    sdk_distribution = "anthropic"
    vendor_caching = True

    def _make_client(self, api_key: str) -> Any:
        """Create an ``AsyncAnthropic`` client. Tests replace this method.

        A fresh client per call keeps the key off the provider instance.
        """
        from anthropic import AsyncAnthropic  # noqa: PLC0415 - optional SDK, lazy import

        return AsyncAnthropic(api_key=api_key)

    def capabilities(self, model: str) -> Capabilities:
        """Registry capabilities with ``temperature`` False (removed in API 2026-10-21)."""
        return super().capabilities(model).model_copy(update={"temperature": False})

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Run one ``messages.create`` call. See :func:`build_messages_params` and
        :func:`map_usage` for the request and usage mapping."""
        native = self.capabilities(request.model).structured_output
        params = build_messages_params(request, native_structured_output=native)
        api_key = self._api_key()
        started_at = utc_now()
        start = time.perf_counter()
        client = self._make_client(api_key)
        try:
            kwargs = dict(params)
            if request.timeout_s is not None:
                kwargs["timeout"] = request.timeout_s
            response = await client.messages.create(**kwargs)
        except ProviderError:
            raise
        except Exception as exc:
            raise sanitize_error(self.name, exc, api_key) from None
        finally:
            await client.close()
        latency = time.perf_counter() - start
        model = getattr(response, "model", None)
        return CompletionResult(
            text=_response_text(response),
            usage=map_usage(getattr(response, "usage", None)),
            provider=self.name,
            model=model if isinstance(model, str) and model else request.model,
            backend=self.backend,
            finish_reason=getattr(response, "stop_reason", None),
            started_at=started_at,
            latency_s=latency,
            native_params_used=dict(request.native_params),
        )

    async def count_input_tokens(self, request: CompletionRequest) -> int | None:
        """Count input tokens with ``messages.count_tokens`` (free; an estimate).

        Uses the same request mapping as :meth:`complete`, restricted to the keys the
        counting endpoint accepts.
        """
        native = self.capabilities(request.model).structured_output
        params = build_messages_params(request, native_structured_output=native, for_counting=True)
        count_params = {k: v for k, v in params.items() if k in _COUNT_TOKENS_KEYS}
        api_key = self._api_key()
        client = self._make_client(api_key)
        try:
            kwargs = dict(count_params)
            if request.timeout_s is not None:
                kwargs["timeout"] = request.timeout_s
            response = await client.messages.count_tokens(**kwargs)
        except ProviderError:
            raise
        except Exception as exc:
            raise sanitize_error(self.name, exc, api_key) from None
        finally:
            await client.close()
        count = getattr(response, "input_tokens", None)
        return count if isinstance(count, int) else None
