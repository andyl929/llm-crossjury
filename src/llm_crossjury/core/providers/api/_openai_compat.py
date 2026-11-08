"""Shared implementation for adapters that use the official ``openai`` SDK.

OpenAI and xAI both serve the **Responses API** (``POST /v1/responses``), and xAI
documents that its REST API "is compatible with the OpenAI REST API" and shows the
``openai`` SDK pointed at its base URL. One implementation therefore covers both; the
subclasses only set the vendor-specific knobs (system role name, base URL, how usage
is reported, whether input-token counting exists).

Sources (verified 2026-11-08):

* openai-python README, "The primary API for interacting with OpenAI models is the
  Responses API": https://github.com/openai/openai-python
* OpenAI text generation guide (recommends Responses over Chat Completions):
  https://developers.openai.com/api/docs/guides/text
* OpenAI structured outputs (``text.format`` with ``type: json_schema``):
  https://developers.openai.com/api/docs/guides/structured-outputs
* xAI REST API reference (OpenAI REST compatibility):
  https://docs.x.ai/developers/rest-api-reference
* xAI Responses reference: https://docs.x.ai/developers/rest-api-reference/inference/responses

Attachments (ADR 0007), verified 2026-11-08. Every attachment is sent inline in the
user message's ``content`` list, before the message text; no Files API is used:

* PDF: ``{"type": "input_file", "filename", "file_data":
  "data:application/pdf;base64,..."}``. ``detail`` is optional and defaults to
  ``auto``, so it is not sent
  (https://developers.openai.com/api/docs/guides/pdf-files).
* Image: ``{"type": "input_image", "image_url": "data:<media type>;base64,..."}``.
  ``detail`` is optional ("If you omit the parameter, it defaults to ``auto``"), so it
  is not sent (https://developers.openai.com/api/docs/guides/images-vision). xAI uses
  the same shape and its base64 example omits ``detail``
  (https://docs.x.ai/developers/model-capabilities/images/understanding).
* Token counting accepts the same image and file parts as ``responses.create``
  (https://developers.openai.com/api/docs/guides/token-counting), so
  ``count_input_tokens`` counts the rendered attachments.
* Anything not sent natively is inlined as an ``input_text`` part holding the locally
  extracted ``fallback_text`` in a delimited ``<attachment>`` block, with closing tags
  escaped. Attachment bytes, base64 and text never appear in errors or
  ``native_params_used``.

Security notes:

* The API key is read from the environment at call time and handed to a client that
  lives only for the duration of one call. It is never stored on the adapter.
* SDK exceptions are re-raised as :class:`ProviderError` with only the exception type,
  HTTP status and a vendor error code, and the original exception is **not** chained
  (``from None``). OpenAI's own messages can echo a masked prefix/suffix of the key
  ("Incorrect API key provided: ..."), which AGENTS.md rule 3 forbids surfacing.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import openai
from openai import AsyncOpenAI

from llm_crossjury.core.providers.api._common import ApiProviderBase
from llm_crossjury.core.providers.base import (
    Attachment,
    AttachmentKind,
    BatchHandle,
    BatchState,
    Capabilities,
    CapabilityNotSupportedError,
    CompletionRequest,
    CompletionResult,
    Message,
    ProviderError,
    ProviderStatus,
    ProviderUnavailableError,
    Role,
    Usage,
)
from llm_crossjury.core.registry.schema import ReasoningControlType

if TYPE_CHECKING:
    from llm_crossjury.core.registry.resolver import ModelRegistry

ClientFactory = Callable[[str, "str | None"], AsyncOpenAI]
"""Builds a client from ``(api_key, base_url)``. Replaced by a fake in tests."""

_SCHEMA_NAME = "response"
"""``text.format.name`` for native structured output. Any identifier is accepted."""

_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_REDACTED = "[REDACTED]"


def default_client_factory(api_key: str, base_url: str | None) -> AsyncOpenAI:
    """Create an ``AsyncOpenAI`` client for one call.

    Args:
        api_key: The key value, read from the environment just before the call.
        base_url: Vendor base URL, or ``None`` for the SDK default (OpenAI).

    Returns:
        A new async client. The caller closes it after the call.
    """
    return AsyncOpenAI(api_key=api_key, base_url=base_url)


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``base`` with ``override`` merged in recursively.

    Nested dicts are merged key by key; any other value in ``override`` replaces the
    one in ``base``. Neither input is modified.

    Args:
        base: Request kwargs built by the adapter.
        override: ``CompletionRequest.native_params``, sent verbatim.

    Returns:
        A new dict.
    """
    merged: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def format_instructions(schema: Mapping[str, Any]) -> str:
    """Return the fallback instructions for models without native structured output.

    The schema is serialized deterministically so identical requests yield identical
    bytes.

    Args:
        schema: Strict JSON schema from ``CompletionRequest.response_schema``.

    Returns:
        Text appended to the last user message.
    """
    rendered = json.dumps(schema, sort_keys=True, ensure_ascii=False, indent=2)
    return (
        "\n\nRespond with a single JSON object that conforms to the following JSON Schema. "
        "Output only the JSON object, with no surrounding text or code fences.\n\n"
        f"{rendered}"
    )


def fallback_block(attachment: Attachment) -> str:
    r"""Return the canonical delimited text block for an attachment (ADR 0007).

    Every ``</attachment>`` inside the extracted text is escaped as
    ``<\/attachment>`` so the content cannot close the block early.

    Args:
        attachment: An attachment whose ``fallback_text`` is set.

    Returns:
        The block, from the opening tag to the closing tag.
    """
    text = (attachment.fallback_text or "").replace("</attachment>", "<\\/attachment>")
    return (
        f'<attachment name="{attachment.filename}" type="{attachment.media_type}" '
        f'sha256="{attachment.sha256[:12]}">\n{text}\n</attachment>'
    )


def _data_url(media_type: str, data: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def _redact(text: str, secret: str | None) -> str:
    if secret:
        text = text.replace(secret, _REDACTED)
    return text


def _nonneg(value: int) -> int | None:
    return value if value >= 0 else None


def _int_attr(obj: object, name: str) -> int | None:
    value = getattr(obj, name, None) if obj is not None else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class OpenAICompatProvider(ApiProviderBase):
    """Responses-API adapter shared by OpenAI and xAI.

    Subclasses set ``name``, ``api_key_env`` and the class knobs below.

    Args:
        api_key_env: Override for the key's environment variable name.
        registry: Model registry; capabilities come only from its entries.
    """

    system_role: str = "system"
    """Role used for ``Role.SYSTEM`` messages in ``input``."""

    base_url: str | None = None
    """Vendor base URL; ``None`` uses the SDK default."""

    reasoning_in_output: bool = True
    """Whether ``usage.output_tokens`` includes ``reasoning_tokens`` (vendor default;
    a reported ``total_tokens`` overrides it, see :meth:`map_usage`)."""

    supports_input_token_count: bool = False
    """Whether ``responses.input_tokens.count`` is an official endpoint for the vendor."""

    documents_prompt_caching: bool = False
    """Whether the vendor officially documents prompt caching (automatic counts)."""

    never_native: frozenset[AttachmentKind] = frozenset()
    """Attachment kinds never sent natively, even if the registry lists them."""

    native_image_media_types: frozenset[str] = frozenset()
    """Image media types the vendor documents for ``input_image``. Images of other
    types use ``fallback_text`` or are refused."""

    def __init__(
        self,
        api_key_env: str | None = None,
        *,
        registry: ModelRegistry | None = None,
    ) -> None:
        super().__init__(api_key_env)
        self._registry = registry
        self._client_factory: ClientFactory = default_client_factory

    # ------------------------------------------------------------------ capabilities

    def capabilities(self, model: str) -> Capabilities:
        """Return capabilities from the registry entry for ``model``.

        Without a registry or an entry, only ``temperature`` is declared.
        ``input_modalities`` comes from the entry, minus :attr:`never_native`.

        Args:
            model: Concrete vendor model id.

        Returns:
            The declared capabilities.
        """
        entry = self._registry.get(self.name, model) if self._registry is not None else None
        if entry is None:
            return Capabilities(
                structured_output=False,
                caching=False,
                batch=False,
                temperature=True,
                reasoning=False,
            )
        return Capabilities(
            structured_output=entry.structured_output is True,
            caching=self.documents_prompt_caching,
            batch=False,
            temperature=True,
            reasoning=entry.reasoning_control.type
            in (ReasoningControlType.LEVELS, ReasoningControlType.BUDGET),
            input_modalities=frozenset(entry.input_modalities or ()) - self.never_native,
        )

    # ------------------------------------------------------------------ request mapping

    def render_attachment(
        self, attachment: Attachment, *, model: str, native: frozenset[AttachmentKind]
    ) -> dict[str, str]:
        """Render one attachment as a Responses ``input`` content part.

        ``DOCUMENT`` is never sent natively (ADR 0007). A PDF or image goes natively
        when its kind is in ``native`` (and, for images, its media type is in
        :attr:`native_image_media_types`); otherwise ``fallback_text`` is inlined.

        Args:
            attachment: The attachment.
            model: Concrete model id, for the error message.
            native: The model's native ``input_modalities``.

        Returns:
            An ``input_file``, ``input_image`` or ``input_text`` part.

        Raises:
            CapabilityNotSupportedError: the attachment cannot be sent natively and
                has no ``fallback_text``.
        """
        kind = attachment.kind
        if kind is AttachmentKind.PDF and kind in native:
            return {
                "type": "input_file",
                "filename": attachment.filename,
                "file_data": _data_url("application/pdf", attachment.data),
            }
        if (
            kind is AttachmentKind.IMAGE
            and kind in native
            and attachment.media_type in self.native_image_media_types
        ):
            return {
                "type": "input_image",
                "image_url": _data_url(attachment.media_type, attachment.data),
            }
        if attachment.fallback_text is not None:
            return {"type": "input_text", "text": fallback_block(attachment)}
        raise CapabilityNotSupportedError(
            f"{self.name}: model {model} cannot read attachment {attachment.filename!r} "
            f"({kind.value}, {attachment.media_type}) natively and it has no text fallback"
        )

    def build_input(
        self, request: CompletionRequest, *, native_schema: bool
    ) -> list[dict[str, Any]]:
        """Map the prompt onto Responses ``input`` messages.

        ``stable_prefix`` comes first, unchanged, so the vendor's automatic prefix
        caching can hit; no explicit cache marker exists or is needed. A user message
        with attachments gets a list of content parts: the attachments in their given
        order, then the text as an ``input_text`` part. If a schema is requested but
        not sent natively, format instructions are appended to the last user message's
        text (or a new user message if there is none).

        Args:
            request: The completion request.
            native_schema: Whether the schema goes into ``text.format`` instead.

        Returns:
            A list of ``{"role", "content"}`` message dicts.

        Raises:
            ProviderError: a system message appears in ``variable_suffix``, or a
                non-user message carries attachments.
            CapabilityNotSupportedError: an attachment can be sent neither natively
                nor as text.
        """
        if any(m.role is Role.SYSTEM for m in request.prompt.variable_suffix):
            raise ProviderError(f"{self.name}: system messages are only allowed in stable_prefix")
        messages: list[Message] = [*request.prompt.stable_prefix, *request.prompt.variable_suffix]
        if any(m.attachments and m.role is not Role.USER for m in messages):
            raise ProviderError(f"{self.name}: attachments are only allowed on user messages")
        native = self.capabilities(request.model).input_modalities
        items: list[dict[str, Any]] = []
        for m in messages:
            content: str | list[dict[str, str]] = m.content
            if m.attachments:
                content = [
                    *(
                        self.render_attachment(a, model=request.model, native=native)
                        for a in m.attachments
                    ),
                    {"type": "input_text", "text": m.content},
                ]
            role = self.system_role if m.role is Role.SYSTEM else m.role.value
            items.append({"role": role, "content": content})
        if request.response_schema is not None and not native_schema:
            extra = format_instructions(request.response_schema)
            for item in reversed(items):
                if item["role"] == Role.USER.value:
                    if isinstance(item["content"], list):
                        item["content"][-1]["text"] += extra
                    else:
                        item["content"] += extra
                    break
            else:
                items.append({"role": Role.USER.value, "content": extra.lstrip()})
        return items

    def build_kwargs(self, request: CompletionRequest) -> dict[str, Any]:
        """Build the ``responses.create`` kwargs, with ``native_params`` merged last.

        Args:
            request: The completion request.

        Returns:
            Keyword arguments for ``client.responses.create``.
        """
        native_schema = (
            request.response_schema is not None
            and self.capabilities(request.model).structured_output
        )
        kwargs: dict[str, Any] = {
            "model": request.model,
            "input": self.build_input(request, native_schema=native_schema),
        }
        if native_schema and request.response_schema is not None:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": _SCHEMA_NAME,
                    "schema": copy.deepcopy(request.response_schema),
                    "strict": True,
                }
            }
        if request.max_output_tokens is not None:
            kwargs["max_output_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.timeout_s is not None:
            kwargs["timeout"] = request.timeout_s
        # Privacy default: ask the vendor not to retain the response (the project's
        # store_content default is false). A member's ``native`` passthrough can override it.
        kwargs.setdefault("store", False)
        return deep_merge(kwargs, request.native_params)

    # ------------------------------------------------------------------ usage mapping

    def map_usage(self, usage: object | None) -> Usage:
        """Map Responses ``usage`` onto :class:`Usage`.

        * ``cached_input_tokens`` = ``input_tokens_details.cached_tokens``.
        * ``cache_write_tokens`` = ``input_tokens_details.cache_write_tokens`` when the
          vendor reports it, else ``None``.
        * ``input_tokens`` = ``input_tokens`` minus cached minus cache-write (both are
          included in ``input_tokens``); ``None`` if the cached count is unknown.
        * ``reasoning_tokens`` = ``output_tokens_details.reasoning_tokens``.
        * ``output_tokens`` (visible) = ``output_tokens`` minus reasoning when the
          vendor counts reasoning inside ``output_tokens``, else ``output_tokens``.
          If ``total_tokens`` is reported, it decides which reading applies
          (``input + output == total`` means inclusive; ``input + output + reasoning
          == total`` means disjoint); otherwise :attr:`reasoning_in_output` applies.

        Unknown values are ``None``, never 0. Inconsistent values (a negative
        difference) become ``None``.

        Args:
            usage: The SDK ``ResponseUsage`` object, or ``None``.

        Returns:
            The mapped usage.
        """
        if usage is None:
            return Usage(
                input_tokens=None,
                cached_input_tokens=None,
                cache_write_tokens=None,
                output_tokens=None,
                reasoning_tokens=None,
            )
        total_in = _int_attr(usage, "input_tokens")
        in_details = getattr(usage, "input_tokens_details", None)
        cached = _int_attr(in_details, "cached_tokens")
        cache_write = _int_attr(in_details, "cache_write_tokens")
        uncached: int | None = None
        if total_in is not None and cached is not None:
            uncached = _nonneg(total_in - cached - (cache_write or 0))

        total_out = _int_attr(usage, "output_tokens")
        reasoning = _int_attr(getattr(usage, "output_tokens_details", None), "reasoning_tokens")
        total = _int_attr(usage, "total_tokens")
        inclusive = self.reasoning_in_output
        if (
            total is not None
            and total_in is not None
            and total_out is not None
            and reasoning is not None
        ):
            if total_in + total_out == total:
                inclusive = True
            elif total_in + total_out + reasoning == total:
                inclusive = False
        visible: int | None
        if total_out is None:
            visible = None
        elif not inclusive:
            visible = total_out
        elif reasoning is None:
            visible = None
        else:
            visible = _nonneg(total_out - reasoning)
        return Usage(
            input_tokens=uncached,
            cached_input_tokens=cached,
            cache_write_tokens=cache_write,
            output_tokens=visible,
            reasoning_tokens=reasoning,
        )

    # ------------------------------------------------------------------ calls

    def _read_key(self) -> str:
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise ProviderUnavailableError(f"{self.api_key_env} not set")
        return key

    def _wrap_error(self, exc: openai.OpenAIError, key: str) -> ProviderError:
        parts = [f"{self.name} API error: {type(exc).__name__}"]
        if isinstance(exc, openai.APIStatusError):
            parts.append(f"(HTTP {exc.status_code})")
        code = getattr(exc, "code", None)
        if isinstance(code, str) and _SAFE_CODE.match(code):
            parts.append(f"code={code}")
        message = _redact(" ".join(parts), key)
        if isinstance(exc, openai.AuthenticationError):
            return ProviderUnavailableError(message)
        return ProviderError(message)

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Run one Responses API call.

        Args:
            request: The completion request.

        Returns:
            Text, mapped usage and call metadata. ``finish_reason`` is the vendor's
            ``incomplete_details.reason`` for incomplete responses (for example
            ``"max_output_tokens"``), otherwise the response ``status``.

        Raises:
            ProviderUnavailableError: the key is missing or rejected.
            ProviderError: any other SDK failure, a failed response, or a refusal.
        """
        key = self._read_key()
        kwargs = self.build_kwargs(request)
        started_at = datetime.now(UTC)
        t0 = time.monotonic()
        client = self._client_factory(key, self.base_url)
        try:
            response = await client.responses.create(**kwargs)
        except openai.OpenAIError as exc:
            raise self._wrap_error(exc, key) from None
        finally:
            await client.close()
        latency = time.monotonic() - t0

        status = getattr(response, "status", None)
        if status == "failed":
            code = getattr(getattr(response, "error", None), "code", None)
            detail = f" code={code}" if isinstance(code, str) and _SAFE_CODE.match(code) else ""
            raise ProviderError(_redact(f"{self.name}: response failed{detail}", key))
        if self._has_refusal(response):
            raise ProviderError(f"{self.name}: the model refused the request")

        incomplete = getattr(response, "incomplete_details", None)
        reason = getattr(incomplete, "reason", None)
        finish_reason = reason if isinstance(reason, str) else status
        reported_model = getattr(response, "model", None)
        return CompletionResult(
            text=response.output_text,
            usage=self.map_usage(getattr(response, "usage", None)),
            provider=self.name,
            model=reported_model
            if isinstance(reported_model, str) and reported_model
            else request.model,
            backend=self.backend,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            started_at=started_at,
            latency_s=latency,
            native_params_used=copy.deepcopy(request.native_params),
        )

    @staticmethod
    def _has_refusal(response: object) -> bool:
        for item in getattr(response, "output", None) or ():
            for part in getattr(item, "content", None) or ():
                if getattr(part, "type", None) == "refusal":
                    return True
        return False

    async def count_input_tokens(self, request: CompletionRequest) -> int | None:
        """Count input tokens with ``responses.input_tokens.count``, if supported.

        Sends ``model``, ``input`` and (for native structured output) ``text``; other
        ``native_params`` do not change the input count and are not sent.

        Args:
            request: The completion request.

        Returns:
            The vendor's input token count, or ``None`` when the vendor has no
            official counting endpoint.

        Raises:
            ProviderUnavailableError: the key is missing or rejected.
            ProviderError: any other SDK failure.
        """
        if not self.supports_input_token_count:
            return None
        key = self._read_key()
        full = self.build_kwargs(request)
        kwargs = {k: full[k] for k in ("model", "input", "text") if k in full}
        client = self._client_factory(key, self.base_url)
        try:
            result = await client.responses.input_tokens.count(**kwargs)
        except openai.OpenAIError as exc:
            raise self._wrap_error(exc, key) from None
        finally:
            await client.close()
        return _int_attr(result, "input_tokens")

    async def check(self) -> ProviderStatus:
        """Report key presence ("set"/"not set") and the SDK version.

        Makes no network call and spends no tokens. Never reveals any part of the key.

        Returns:
            The provider status.
        """
        is_set = bool(os.environ.get(self.api_key_env, ""))
        state = "set" if is_set else "not set"
        return ProviderStatus(
            provider=self.name,
            backend=self.backend,
            available=is_set,
            detail=f"{self.api_key_env} {state}; openai SDK {openai.__version__}",
            version=openai.__version__,
        )

    # ------------------------------------------------------------------ batch (not in MVP)

    async def submit_batch(self, requests: Sequence[CompletionRequest]) -> BatchHandle:
        """Not implemented in the MVP (``capabilities().batch`` is ``False``)."""
        raise NotImplementedError

    async def poll_batch(self, handle: BatchHandle) -> BatchState:
        """Not implemented in the MVP (``capabilities().batch`` is ``False``)."""
        raise NotImplementedError

    async def collect_batch(self, handle: BatchHandle) -> list[CompletionResult]:
        """Not implemented in the MVP (``capabilities().batch`` is ``False``)."""
        raise NotImplementedError
