"""OpenAI ``api`` adapter, with the SDK client replaced by a fake (no network).

The fakes and helpers here are reused by ``test_provider_xai.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import date
from typing import Any, TypeVar, cast

import httpx2
import openai
import pytest
from openai import AsyncOpenAI
from openai._models import construct_type
from openai.types.responses import Response

from llm_crossjury.core.providers.api import OpenAIApiProvider
from llm_crossjury.core.providers.api._openai_compat import (
    OpenAICompatProvider,
    deep_merge,
)
from llm_crossjury.core.providers.base import (
    Backend,
    CompletionRequest,
    Message,
    Prompt,
    Provider,
    ProviderError,
    ProviderUnavailableError,
    ReasoningEffort,
    Role,
)
from llm_crossjury.core.registry.resolver import ModelRegistry
from llm_crossjury.core.registry.schema import (
    EffortMapping,
    ModelEntry,
    ReasoningControl,
    ReasoningControlType,
)

T = TypeVar("T")

FAKE_KEY = "test-fake-key-0123456789abcdef"
MODEL = "fictional-model-1"


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


# --------------------------------------------------------------------------- fakes


class FakeInputTokens:
    def __init__(self, count: int) -> None:
        self.count_value = count
        self.calls: list[dict[str, Any]] = []

    async def count(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return construct_type(
            type_=openai.types.responses.InputTokenCountResponse,
            value={"input_tokens": self.count_value, "object": "response.input_tokens"},
        )


class FakeResponses:
    def __init__(self, response: object | None, error: Exception | None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.input_tokens = FakeInputTokens(42)

    async def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
        self.responses = FakeResponses(response, error)
        self.closed = False
        self.factory_args: tuple[str, str | None] | None = None

    async def close(self) -> None:
        self.closed = True


def install(provider: OpenAICompatProvider, client: FakeClient) -> None:
    """Replace the provider's client factory with one returning ``client``."""

    def factory(api_key: str, base_url: str | None) -> AsyncOpenAI:
        client.factory_args = (api_key, base_url)
        return cast(AsyncOpenAI, client)

    provider._client_factory = factory


def make_response(
    *,
    text: str = '{"ok": true}',
    status: str = "completed",
    usage: dict[str, Any] | None = None,
    model: str = MODEL,
    incomplete_reason: str | None = None,
    refusal: bool = False,
    error_code: str | None = None,
) -> Response:
    content: dict[str, Any] = (
        {"type": "refusal", "refusal": "no"}
        if refusal
        else {"type": "output_text", "text": text, "annotations": []}
    )
    value: dict[str, Any] = {
        "id": "resp_1",
        "created_at": 0,
        "model": model,
        "object": "response",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [content],
            }
        ],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "status": status,
        "usage": usage,
        "incomplete_details": (
            {"reason": incomplete_reason} if incomplete_reason is not None else None
        ),
        "error": {"code": error_code, "message": "x"} if error_code else None,
    }
    return cast(Response, construct_type(type_=Response, value=value))


class FakeRegistry:
    def __init__(self, entries: list[ModelEntry]) -> None:
        self._entries = {(e.provider, e.model_id): e for e in entries}

    def get(self, provider: str, model_id: str) -> ModelEntry | None:
        return self._entries.get((provider, model_id))


def make_entry(
    provider: str,
    *,
    structured_output: bool | None = True,
    control: ReasoningControlType = ReasoningControlType.LEVELS,
) -> ModelEntry:
    has_mapping = control in (ReasoningControlType.LEVELS, ReasoningControlType.BUDGET)
    return ModelEntry(
        provider=provider,
        model_id=MODEL,
        backends=(Backend.API,),
        reasoning_control=ReasoningControl(
            type=control,
            parameter="reasoning.effort" if has_mapping else None,
            allowed=("a", "b", "c") if has_mapping else None,
            mapping=EffortMapping(low="a", medium="b", high="c") if has_mapping else None,
        ),
        cli_flags=None,
        context_window=None,
        max_output=None,
        structured_output=structured_output,
        pricing=None,
        reasoning_multipliers=None,
        source_url=None,
        last_verified=date(2026, 1, 1),
        notes=None,
    )


def registry_for(*entries: ModelEntry) -> ModelRegistry:
    return cast(ModelRegistry, FakeRegistry(list(entries)))


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def make_request(**overrides: Any) -> CompletionRequest:
    fields: dict[str, Any] = {
        "model": MODEL,
        "prompt": Prompt(
            stable_prefix=(
                Message(role=Role.SYSTEM, content="sys"),
                Message(role=Role.USER, content="doc"),
            ),
            variable_suffix=(Message(role=Role.USER, content="question"),),
        ),
        "response_schema": None,
        "temperature": None,
        "max_output_tokens": None,
        "reasoning": ReasoningEffort.DEFAULT,
        "native_params": {},
        "cache_prefix": True,
        "timeout_s": None,
        "tags": {"run": "r1"},
    }
    fields.update(overrides)
    return CompletionRequest(**fields)


def status_error(cls: type[openai.APIStatusError], status: int, body: object) -> Exception:
    request = httpx2.Request("POST", "https://example.invalid/v1/responses")
    response = httpx2.Response(status, request=request)
    return cls(f"Incorrect API key provided: {FAKE_KEY}", response=response, body=body)


# --------------------------------------------------------------------------- tests


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)


def test_satisfies_protocol() -> None:
    assert isinstance(OpenAIApiProvider(), Provider)
    assert OpenAIApiProvider().name == "openai"


def test_capabilities_without_registry() -> None:
    caps = OpenAIApiProvider().capabilities(MODEL)
    assert caps.temperature is True
    assert not caps.structured_output
    assert not caps.caching
    assert not caps.batch
    assert not caps.reasoning


def test_capabilities_unknown_model() -> None:
    provider = OpenAIApiProvider(registry=registry_for(make_entry("openai")))
    caps = provider.capabilities("other-fictional-model")
    assert not caps.structured_output
    assert not caps.caching
    assert not caps.reasoning


def test_capabilities_from_entry() -> None:
    provider = OpenAIApiProvider(registry=registry_for(make_entry("openai")))
    caps = provider.capabilities(MODEL)
    assert caps.structured_output
    assert caps.caching
    assert caps.reasoning
    assert caps.temperature
    assert caps.batch is False


@pytest.mark.parametrize(
    ("control", "expected"),
    [
        (ReasoningControlType.LEVELS, True),
        (ReasoningControlType.BUDGET, True),
        (ReasoningControlType.ALWAYS_ON, False),
        (ReasoningControlType.NONE, False),
    ],
)
def test_capabilities_reasoning(control: ReasoningControlType, expected: bool) -> None:
    provider = OpenAIApiProvider(registry=registry_for(make_entry("openai", control=control)))
    assert provider.capabilities(MODEL).reasoning is expected


def test_capabilities_unverified_structured_output() -> None:
    entry = make_entry("openai", structured_output=None)
    assert (
        OpenAIApiProvider(registry=registry_for(entry)).capabilities(MODEL).structured_output
        is False
    )


def test_capabilities_ignore_other_provider_entry() -> None:
    provider = OpenAIApiProvider(registry=registry_for(make_entry("xai")))
    assert provider.capabilities(MODEL).caching is False


def test_missing_key_raises_unavailable() -> None:
    provider = OpenAIApiProvider()
    install(provider, FakeClient(make_response()))
    with pytest.raises(ProviderUnavailableError, match=r"^OPENAI_API_KEY not set$"):
        run(provider.complete(make_request()))


def test_empty_key_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(ProviderUnavailableError, match="not set"):
        run(OpenAIApiProvider().complete(make_request()))


def test_custom_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_OPENAI_KEY", FAKE_KEY)
    provider = OpenAIApiProvider("MY_OPENAI_KEY")
    client = FakeClient(make_response())
    install(provider, client)
    run(provider.complete(make_request()))
    assert client.factory_args == (FAKE_KEY, None)


@pytest.mark.usefixtures("key")
def test_complete_maps_prompt_and_result() -> None:
    provider = OpenAIApiProvider()
    usage = {
        "input_tokens": 100,
        "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 10},
        "output_tokens": 50,
        "output_tokens_details": {"reasoning_tokens": 20},
        "total_tokens": 150,
    }
    client = FakeClient(make_response(text="hello", usage=usage, model="fictional-model-1-x"))
    install(provider, client)
    result = run(provider.complete(make_request()))

    (call,) = client.responses.calls
    assert call == {
        "model": MODEL,
        "input": [
            {"role": "developer", "content": "sys"},
            {"role": "user", "content": "doc"},
            {"role": "user", "content": "question"},
        ],
        "store": False,
    }
    assert client.closed
    assert client.factory_args == (FAKE_KEY, None)
    assert result.text == "hello"
    assert result.provider == "openai"
    assert result.backend is Backend.API
    assert result.model == "fictional-model-1-x"
    assert result.finish_reason == "completed"
    assert result.started_at.tzinfo is not None
    assert result.latency_s is not None
    assert result.latency_s >= 0
    assert result.native_params_used == {}
    assert result.usage.input_tokens == 30
    assert result.usage.cached_input_tokens == 60
    assert result.usage.cache_write_tokens == 10
    assert result.usage.output_tokens == 30
    assert result.usage.reasoning_tokens == 20


@pytest.mark.usefixtures("key")
def test_complete_passes_optional_params_and_merges_native() -> None:
    provider = OpenAIApiProvider()
    client = FakeClient(make_response())
    install(provider, client)
    native = {"reasoning": {"effort": "fictional"}, "text": {"verbosity": "low"}}
    request = make_request(
        temperature=0.2, max_output_tokens=321, timeout_s=12.5, native_params=native
    )
    result = run(provider.complete(request))
    (call,) = client.responses.calls
    assert call["temperature"] == 0.2
    assert call["max_output_tokens"] == 321
    assert call["timeout"] == 12.5
    assert call["reasoning"] == {"effort": "fictional"}
    assert call["text"] == {"verbosity": "low"}
    assert result.native_params_used == native
    assert "tags" not in call
    assert "run" not in str(call)


@pytest.mark.usefixtures("key")
def test_no_defaults_invented() -> None:
    provider = OpenAIApiProvider()
    client = FakeClient(make_response())
    install(provider, client)
    run(provider.complete(make_request()))
    (call,) = client.responses.calls
    # ``store: False`` is the only adapter-side default: a privacy setting, not a
    # generation parameter (vendors otherwise retain responses).
    assert set(call) == {"model", "input", "store"}
    assert call["store"] is False


@pytest.mark.usefixtures("key")
def test_native_params_can_override_store() -> None:
    provider = OpenAIApiProvider()
    client = FakeClient(make_response())
    install(provider, client)
    run(provider.complete(make_request(native_params={"store": True})))
    (call,) = client.responses.calls
    assert call["store"] is True


@pytest.mark.usefixtures("key")
def test_native_structured_output() -> None:
    provider = OpenAIApiProvider(registry=registry_for(make_entry("openai")))
    client = FakeClient(make_response())
    install(provider, client)
    native = {"text": {"verbosity": "low"}}
    run(provider.complete(make_request(response_schema=SCHEMA, native_params=native)))
    (call,) = client.responses.calls
    assert call["text"] == {
        "format": {"type": "json_schema", "name": "response", "schema": SCHEMA, "strict": True},
        "verbosity": "low",
    }
    assert call["input"][-1] == {"role": "user", "content": "question"}


@pytest.mark.usefixtures("key")
def test_fallback_format_instructions() -> None:
    provider = OpenAIApiProvider()  # no registry: no structured_output
    client = FakeClient(make_response())
    install(provider, client)
    run(provider.complete(make_request(response_schema=SCHEMA)))
    (call,) = client.responses.calls
    assert "text" not in call
    last = call["input"][-1]
    assert last["role"] == "user"
    assert last["content"].startswith("question\n\n")
    assert '"additionalProperties": false' in last["content"]
    # The stable prefix is left untouched.
    assert call["input"][1] == {"role": "user", "content": "doc"}


@pytest.mark.usefixtures("key")
def test_fallback_appends_user_message_when_none() -> None:
    provider = OpenAIApiProvider()
    client = FakeClient(make_response())
    install(provider, client)
    prompt = Prompt(
        stable_prefix=(Message(role=Role.SYSTEM, content="sys"),),
        variable_suffix=(Message(role=Role.ASSISTANT, content="prior"),),
    )
    run(provider.complete(make_request(prompt=prompt, response_schema=SCHEMA)))
    (call,) = client.responses.calls
    assert call["input"][-1]["role"] == "user"
    assert call["input"][-1]["content"].startswith("Respond with a single JSON object")


@pytest.mark.usefixtures("key")
def test_system_message_in_suffix_rejected() -> None:
    provider = OpenAIApiProvider()
    install(provider, FakeClient(make_response()))
    prompt = Prompt(
        stable_prefix=(),
        variable_suffix=(Message(role=Role.SYSTEM, content="late"),),
    )
    with pytest.raises(ProviderError, match="stable_prefix"):
        run(provider.complete(make_request(prompt=prompt)))


@pytest.mark.usefixtures("key")
def test_incomplete_finish_reason() -> None:
    provider = OpenAIApiProvider()
    install(
        provider,
        FakeClient(make_response(status="incomplete", incomplete_reason="max_output_tokens")),
    )
    result = run(provider.complete(make_request()))
    assert result.finish_reason == "max_output_tokens"


@pytest.mark.usefixtures("key")
def test_failed_response_raises() -> None:
    provider = OpenAIApiProvider()
    install(provider, FakeClient(make_response(status="failed", error_code="server_error")))
    with pytest.raises(ProviderError, match="code=server_error"):
        run(provider.complete(make_request()))


@pytest.mark.usefixtures("key")
def test_refusal_raises() -> None:
    provider = OpenAIApiProvider()
    install(provider, FakeClient(make_response(refusal=True)))
    with pytest.raises(ProviderError, match="refused"):
        run(provider.complete(make_request()))


@pytest.mark.usefixtures("key")
def test_missing_usage_is_unknown() -> None:
    provider = OpenAIApiProvider()
    install(provider, FakeClient(make_response(usage=None)))
    usage = run(provider.complete(make_request())).usage
    assert usage.model_dump() == {
        "input_tokens": None,
        "cached_input_tokens": None,
        "cache_write_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
    }


def test_usage_partial_details_are_unknown() -> None:
    usage = OpenAIApiProvider().map_usage(
        construct_type(
            type_=openai.types.responses.ResponseUsage,
            value={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
    )
    assert usage.input_tokens is None
    assert usage.cached_input_tokens is None
    assert usage.cache_write_tokens is None
    assert usage.reasoning_tokens is None
    assert usage.output_tokens is None  # inclusive reasoning, count unknown


def test_usage_cache_write_absent_is_none() -> None:
    usage = OpenAIApiProvider().map_usage(
        construct_type(
            type_=openai.types.responses.ResponseUsage,
            value={
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 4},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 15,
            },
        )
    )
    assert usage.cache_write_tokens is None
    assert usage.input_tokens == 6
    assert usage.output_tokens == 5


def test_usage_total_overrides_default_reading() -> None:
    # Totals say reasoning is disjoint, although OpenAI's default is inclusive.
    usage = OpenAIApiProvider().map_usage(
        construct_type(
            type_=openai.types.responses.ResponseUsage,
            value={
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 7},
                "total_tokens": 22,
            },
        )
    )
    assert usage.output_tokens == 5
    assert usage.reasoning_tokens == 7


@pytest.mark.usefixtures("key")
@pytest.mark.parametrize(
    ("error", "kind", "fragment"),
    [
        (
            status_error(openai.AuthenticationError, 401, {"code": "invalid_api_key"}),
            ProviderUnavailableError,
            "AuthenticationError (HTTP 401) code=invalid_api_key",
        ),
        (
            status_error(openai.RateLimitError, 429, None),
            ProviderError,
            "RateLimitError (HTTP 429)",
        ),
        (
            openai.APIConnectionError(
                request=httpx2.Request("POST", "https://example.invalid/v1/responses")
            ),
            ProviderError,
            "APIConnectionError",
        ),
    ],
)
def test_sdk_errors_are_sanitized(
    error: Exception, kind: type[ProviderError], fragment: str
) -> None:
    provider = OpenAIApiProvider()
    client = FakeClient(error=error)
    install(provider, client)
    with pytest.raises(kind) as info:
        run(provider.complete(make_request()))
    message = str(info.value)
    assert fragment in message
    assert FAKE_KEY not in message
    assert FAKE_KEY[:6] not in message
    assert FAKE_KEY[-4:] not in message
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True
    assert client.closed


@pytest.mark.usefixtures("key")
def test_count_input_tokens() -> None:
    provider = OpenAIApiProvider(registry=registry_for(make_entry("openai")))
    client = FakeClient()
    install(provider, client)
    native = {"reasoning": {"effort": "fictional"}}
    request = make_request(response_schema=SCHEMA, max_output_tokens=9, native_params=native)
    assert run(provider.count_input_tokens(request)) == 42
    (call,) = client.responses.input_tokens.calls
    assert set(call) == {"model", "input", "text"}
    assert call["model"] == MODEL
    assert client.closed
    assert client.responses.calls == []


def test_count_input_tokens_without_key() -> None:
    with pytest.raises(ProviderUnavailableError):
        run(OpenAIApiProvider().count_input_tokens(make_request()))


def test_check_not_set() -> None:
    status = run(OpenAIApiProvider().check())
    assert status.available is False
    assert status.provider == "openai"
    assert status.backend is Backend.API
    assert "OPENAI_API_KEY not set" in status.detail
    assert status.version == openai.__version__


@pytest.mark.usefixtures("key")
def test_check_set_reveals_nothing() -> None:
    provider = OpenAIApiProvider()

    def no_client(api_key: str, base_url: str | None) -> AsyncOpenAI:
        raise AssertionError("check must not create a client")

    provider._client_factory = no_client
    status = run(provider.check())
    assert status.available is True
    assert "OPENAI_API_KEY set" in status.detail
    dumped = status.model_dump_json()
    assert FAKE_KEY not in dumped
    assert FAKE_KEY[:4] not in dumped
    assert FAKE_KEY[-4:] not in dumped
    assert str(len(FAKE_KEY)) not in dumped


def test_key_not_stored_on_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    provider = OpenAIApiProvider()
    client = FakeClient(make_response())
    install(provider, client)
    run(provider.complete(make_request()))
    assert FAKE_KEY not in repr(vars(provider))


def test_batch_not_implemented() -> None:
    provider = OpenAIApiProvider()
    with pytest.raises(NotImplementedError):
        run(provider.submit_batch([make_request()]))


def test_deep_merge() -> None:
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    override = {"a": {"c": 9, "e": {"f": 1}}, "d": {"x": 1}}
    merged = deep_merge(base, override)
    assert merged == {"a": {"b": 1, "c": 9, "e": {"f": 1}}, "d": {"x": 1}}
    assert base == {"a": {"b": 1, "c": 2}, "d": 3}
