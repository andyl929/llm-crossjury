"""xAI ``api`` adapter (OpenAI SDK against xAI's base URL), with a fake client."""

from __future__ import annotations

import openai
import pytest
from openai._models import construct_type

from llm_crossjury.core.providers.api import XaiApiProvider
from llm_crossjury.core.providers.api.xai import XAI_BASE_URL
from llm_crossjury.core.providers.base import (
    Backend,
    Provider,
    ProviderError,
    ProviderUnavailableError,
)
from llm_crossjury.core.registry.schema import ReasoningControlType
from tests.unit.test_provider_openai import (
    FAKE_KEY,
    MODEL,
    SCHEMA,
    FakeClient,
    install,
    make_entry,
    make_request,
    make_response,
    registry_for,
    run,
    status_error,
)


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", FAKE_KEY)


def test_satisfies_protocol() -> None:
    provider = XaiApiProvider()
    assert isinstance(provider, Provider)
    assert provider.name == "xai"
    assert provider.backend is Backend.API


def test_base_url_is_documented_value() -> None:
    assert XAI_BASE_URL == "https://api.x.ai/v1"


def test_capabilities() -> None:
    assert XaiApiProvider().capabilities(MODEL).caching is False
    provider = XaiApiProvider(
        registry=registry_for(make_entry("xai", control=ReasoningControlType.ALWAYS_ON))
    )
    caps = provider.capabilities(MODEL)
    assert caps.caching
    assert caps.structured_output
    assert caps.temperature
    assert caps.reasoning is False
    assert caps.batch is False


def test_missing_key() -> None:
    provider = XaiApiProvider()
    install(provider, FakeClient(make_response()))
    with pytest.raises(ProviderUnavailableError, match=r"^XAI_API_KEY not set$"):
        run(provider.complete(make_request()))


@pytest.mark.usefixtures("key")
def test_complete_uses_xai_base_url_and_system_role() -> None:
    provider = XaiApiProvider()
    usage = {
        "input_tokens": 32,
        "input_tokens_details": {"cached_tokens": 8},
        "output_tokens": 9,
        "output_tokens_details": {"reasoning_tokens": 110},
        "total_tokens": 151,
    }
    client = FakeClient(make_response(text="grok says", usage=usage))
    install(provider, client)
    result = run(provider.complete(make_request(temperature=0.5)))

    assert client.factory_args == (FAKE_KEY, XAI_BASE_URL)
    (call,) = client.responses.calls
    assert call["input"][0] == {"role": "system", "content": "sys"}
    assert call["temperature"] == 0.5
    assert client.closed
    assert result.text == "grok says"
    assert result.provider == "xai"
    # Example from the xAI Responses reference: reasoning is not inside output_tokens.
    assert result.usage.input_tokens == 24
    assert result.usage.cached_input_tokens == 8
    assert result.usage.cache_write_tokens is None
    assert result.usage.output_tokens == 9
    assert result.usage.reasoning_tokens == 110


def test_usage_without_total_uses_disjoint_default() -> None:
    usage = XaiApiProvider().map_usage(
        construct_type(
            type_=openai.types.responses.ResponseUsage,
            value={
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 3},
            },
        )
    )
    assert usage.output_tokens == 5
    assert usage.reasoning_tokens == 3


@pytest.mark.usefixtures("key")
def test_native_structured_output() -> None:
    provider = XaiApiProvider(registry=registry_for(make_entry("xai")))
    client = FakeClient(make_response())
    install(provider, client)
    run(provider.complete(make_request(response_schema=SCHEMA)))
    (call,) = client.responses.calls
    assert call["text"]["format"]["type"] == "json_schema"
    assert call["text"]["format"]["schema"] == SCHEMA


@pytest.mark.usefixtures("key")
def test_errors_are_sanitized() -> None:
    provider = XaiApiProvider()
    install(provider, FakeClient(error=status_error(openai.InternalServerError, 500, None)))
    with pytest.raises(ProviderError) as info:
        run(provider.complete(make_request()))
    assert "xai API error: InternalServerError (HTTP 500)" in str(info.value)
    assert FAKE_KEY not in str(info.value)


@pytest.mark.usefixtures("key")
def test_count_input_tokens_returns_none_without_network() -> None:
    provider = XaiApiProvider()
    client = FakeClient()
    install(provider, client)
    assert run(provider.count_input_tokens(make_request())) is None
    assert client.factory_args is None


def test_check() -> None:
    status = run(XaiApiProvider().check())
    assert status.available is False
    assert "XAI_API_KEY not set" in status.detail
    assert status.version == openai.__version__


@pytest.mark.usefixtures("key")
def test_check_set() -> None:
    status = run(XaiApiProvider().check())
    assert status.available is True
    assert "XAI_API_KEY set" in status.detail
    assert FAKE_KEY[:4] not in status.model_dump_json()
