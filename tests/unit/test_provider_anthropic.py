"""Anthropic ``api`` adapter, with the SDK client faked at the adapter boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC
from types import SimpleNamespace
from typing import Any

import pytest

from llm_crossjury.core.providers.api._common import deep_merge
from llm_crossjury.core.providers.api.anthropic import AnthropicApiProvider, map_usage
from llm_crossjury.core.providers.base import (
    CompletionRequest,
    Message,
    Prompt,
    Provider,
    ProviderError,
    ProviderUnavailableError,
    ReasoningEffort,
    Role,
)
from llm_crossjury.core.registry.schema import (
    EffortMapping,
    ModelEntry,
    ReasoningControl,
    ReasoningControlType,
)

FAKE_KEY = "fake-test-key-0123456789"
MODEL = "fictional-claude-model"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _entry(
    *, structured: bool | None, control: ReasoningControlType = ReasoningControlType.LEVELS
) -> ModelEntry:
    has_mapping = control in (ReasoningControlType.LEVELS, ReasoningControlType.BUDGET)
    return ModelEntry(
        provider="anthropic",
        model_id=MODEL,
        backends=(),
        reasoning_control=ReasoningControl(
            type=control,
            parameter="output_config.effort" if has_mapping else None,
            allowed=("low", "high") if has_mapping else None,
            mapping=EffortMapping(low="low", medium=None, high="high") if has_mapping else None,
        ),
        cli_flags=None,
        context_window=None,
        max_output=None,
        structured_output=structured,
        pricing=None,
        reasoning_multipliers=None,
        source_url=None,
        last_verified=None,
        notes=None,
    )


class FakeRegistry:
    def __init__(self, *entries: ModelEntry) -> None:
        self._entries = {(e.provider, e.model_id): e for e in entries}

    def get(self, provider: str, model_id: str) -> ModelEntry | None:
        return self._entries.get((provider, model_id))


def _usage(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "input_tokens": 12,
        "cache_read_input_tokens": 300,
        "cache_creation_input_tokens": 40,
        "output_tokens": 50,
        "output_tokens_details": SimpleNamespace(thinking_tokens=20),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _message_response(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "content": [
            SimpleNamespace(type="thinking", thinking="hidden"),
            SimpleNamespace(type="text", text="Hello "),
            SimpleNamespace(type="text", text="world"),
        ],
        "model": "fictional-claude-model-20990101",
        "stop_reason": "end_turn",
        "usage": _usage(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@dataclass
class FakeMessages:
    response: Any = None
    count: Any = None
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    count_calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response

    async def count_tokens(self, **kwargs: Any) -> Any:
        self.count_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.count


@dataclass
class FakeClient:
    messages: FakeMessages
    closed: int = 0

    async def close(self) -> None:
        self.closed += 1


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    client = FakeClient(
        messages=FakeMessages(response=_message_response(), count=SimpleNamespace(input_tokens=123))
    )
    keys_seen: list[str] = []

    def make_client(self: AnthropicApiProvider, api_key: str) -> FakeClient:
        keys_seen.append(api_key)
        return client

    monkeypatch.setattr(AnthropicApiProvider, "_make_client", make_client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    return client


def _request(**overrides: Any) -> CompletionRequest:
    values: dict[str, Any] = {
        "model": MODEL,
        "prompt": Prompt(
            stable_prefix=(
                Message(role=Role.SYSTEM, content="sys one"),
                Message(role=Role.SYSTEM, content="sys two"),
            ),
            variable_suffix=(Message(role=Role.USER, content="question"),),
        ),
        "response_schema": None,
        "temperature": None,
        "max_output_tokens": 256,
        "reasoning": ReasoningEffort.DEFAULT,
        "native_params": {},
        "cache_prefix": False,
        "timeout_s": None,
        "tags": {"run": "r1"},
    }
    values.update(overrides)
    return CompletionRequest(**values)


def _provider(*entries: ModelEntry) -> AnthropicApiProvider:
    return AnthropicApiProvider(registry=FakeRegistry(*entries) if entries else None)


# ----------------------------------------------------------------------------- protocol


def test_satisfies_provider_protocol() -> None:
    assert isinstance(AnthropicApiProvider(), Provider)
    assert isinstance(AnthropicApiProvider("OTHER_KEY", registry=FakeRegistry()), Provider)


def test_custom_key_env_is_used() -> None:
    assert AnthropicApiProvider("OTHER_KEY").api_key_env == "OTHER_KEY"
    assert AnthropicApiProvider().api_key_env == "ANTHROPIC_API_KEY"


# ------------------------------------------------------------------------- capabilities


def test_capabilities_without_registry_are_all_false() -> None:
    caps = AnthropicApiProvider().capabilities(MODEL)
    assert caps.model_dump() == {
        "structured_output": False,
        "caching": False,
        "batch": False,
        "temperature": False,
        "reasoning": False,
        "input_modalities": frozenset(),
    }


def test_capabilities_from_registry_entry() -> None:
    caps = _provider(_entry(structured=True)).capabilities(MODEL)
    assert caps.structured_output is True
    assert caps.reasoning is True
    assert caps.caching is True
    assert caps.batch is False
    assert caps.temperature is False  # removed in API version 2026-10-21


def test_capabilities_unknown_model_and_unverified_flags() -> None:
    provider = _provider(_entry(structured=None, control=ReasoningControlType.ALWAYS_ON))
    caps = provider.capabilities(MODEL)
    assert caps.structured_output is False
    assert caps.reasoning is False
    assert provider.capabilities("other-model").caching is False


def test_budget_control_counts_as_reasoning() -> None:
    caps = _provider(_entry(structured=False, control=ReasoningControlType.BUDGET)).capabilities(
        MODEL
    )
    assert caps.reasoning is True


# ----------------------------------------------------------------------------- complete


def test_complete_maps_request_and_result(fake: FakeClient) -> None:
    result = asyncio.run(_provider().complete(_request(temperature=0.2, timeout_s=30.0)))

    (sent,) = fake.messages.calls
    assert sent["model"] == MODEL
    assert sent["max_tokens"] == 256
    assert sent["system"] == [
        {"type": "text", "text": "sys one"},
        {"type": "text", "text": "sys two"},
    ]
    assert sent["messages"] == [{"role": "user", "content": [{"type": "text", "text": "question"}]}]
    assert sent["timeout"] == 30.0
    assert "temperature" not in sent
    assert "output_config" not in sent
    assert fake.closed == 1

    assert result.text == "Hello world"
    assert result.model == "fictional-claude-model-20990101"
    assert result.provider == "anthropic"
    assert result.finish_reason == "end_turn"
    assert result.started_at.tzinfo is UTC
    assert result.latency_s is not None
    assert result.latency_s >= 0
    assert result.native_params_used == {}
    assert result.usage.model_dump() == {
        "input_tokens": 12,
        "cached_input_tokens": 300,
        "cache_write_tokens": 40,
        "output_tokens": 30,
        "reasoning_tokens": 20,
    }


def test_cache_breakpoint_on_last_system_block(fake: FakeClient) -> None:
    asyncio.run(_provider().complete(_request(cache_prefix=True)))
    sent = fake.messages.calls[0]
    assert "cache_control" not in sent["system"][0]
    assert sent["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in sent["messages"][0]["content"][0]


def test_cache_breakpoint_on_last_prefix_message(fake: FakeClient) -> None:
    prompt = Prompt(
        stable_prefix=(
            Message(role=Role.SYSTEM, content="sys"),
            Message(role=Role.USER, content="shared document"),
        ),
        variable_suffix=(Message(role=Role.USER, content="chunk 3"),),
    )
    asyncio.run(_provider().complete(_request(prompt=prompt, cache_prefix=True)))
    sent = fake.messages.calls[0]
    assert "cache_control" not in sent["system"][0]
    # Consecutive user turns are merged into one message; the breakpoint stays on
    # the block that ends the stable prefix.
    assert sent["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "shared document",
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": "chunk 3"},
            ],
        }
    ]


def test_no_cache_breakpoint_without_cache_prefix(fake: FakeClient) -> None:
    asyncio.run(_provider().complete(_request(cache_prefix=False)))
    sent = fake.messages.calls[0]
    assert all("cache_control" not in block for block in sent["system"])


def test_native_params_are_deep_merged_and_recorded(fake: FakeClient) -> None:
    native = {"output_config": {"effort": "high"}, "thinking": {"type": "adaptive"}}
    request = _request(response_schema=SCHEMA, native_params=native)
    result = asyncio.run(_provider(_entry(structured=True)).complete(request))
    sent = fake.messages.calls[0]
    assert sent["output_config"] == {
        "format": {"type": "json_schema", "schema": SCHEMA},
        "effort": "high",
    }
    assert sent["thinking"] == {"type": "adaptive"}
    assert result.native_params_used == native
    # Native structured output: no format instructions in the prompt.
    assert sent["messages"][0]["content"][0]["text"] == "question"


def test_schema_fallback_appends_instructions_to_last_user_message(fake: FakeClient) -> None:
    asyncio.run(_provider(_entry(structured=False)).complete(_request(response_schema=SCHEMA)))
    sent = fake.messages.calls[0]
    assert "output_config" not in sent
    text = sent["messages"][-1]["content"][-1]["text"]
    assert text.startswith("question\n\n")
    assert '"additionalProperties": false' in text
    assert "JSON" in text


def test_missing_max_output_tokens_raises_without_calling(fake: FakeClient) -> None:
    with pytest.raises(ProviderError, match="max_output_tokens"):
        asyncio.run(_provider().complete(_request(max_output_tokens=None)))
    assert fake.messages.calls == []


def test_system_message_in_suffix_is_rejected(fake: FakeClient) -> None:
    prompt = Prompt(
        stable_prefix=(),
        variable_suffix=(Message(role=Role.SYSTEM, content="late system"),),
    )
    with pytest.raises(ProviderError, match="stable_prefix"):
        asyncio.run(_provider().complete(_request(prompt=prompt)))


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_key_raises_unavailable(
    fake: FakeClient, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", value)
    with pytest.raises(ProviderUnavailableError, match=r"^ANTHROPIC_API_KEY not set$"):
        asyncio.run(_provider().complete(_request()))
    assert fake.messages.calls == []


class FakeStatusError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_sdk_errors_are_wrapped_and_redacted(fake: FakeClient) -> None:
    fake.messages.error = FakeStatusError(f"invalid x-api-key {FAKE_KEY} rejected", 401)
    with pytest.raises(ProviderError) as info:
        asyncio.run(_provider().complete(_request()))
    message = str(info.value)
    assert FAKE_KEY not in message
    assert "[REDACTED]" in message
    assert "FakeStatusError" in message
    assert "HTTP 401" in message
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True
    assert type(info.value) is ProviderError
    assert fake.closed == 1


def test_key_is_not_stored_on_instance(fake: FakeClient) -> None:
    provider = _provider()
    asyncio.run(provider.complete(_request()))
    assert FAKE_KEY not in repr(vars(provider))


# ------------------------------------------------------------------------------- usage


def test_usage_without_output_details_has_unknown_reasoning() -> None:
    usage = map_usage(
        _usage(
            output_tokens_details=None,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=None,
        )
    )
    assert usage.output_tokens == 50
    assert usage.reasoning_tokens is None
    assert usage.cached_input_tokens == 0
    assert usage.cache_write_tokens == 0


def test_missing_usage_is_unknown() -> None:
    assert set(map_usage(None).model_dump().values()) == {None}


# ------------------------------------------------------------------------ count tokens


def test_count_input_tokens(fake: FakeClient) -> None:
    count = asyncio.run(
        _provider().count_input_tokens(
            _request(max_output_tokens=None, cache_prefix=True, native_params={"metadata": {}})
        )
    )
    assert count == 123
    (sent,) = fake.messages.count_calls
    assert "max_tokens" not in sent
    assert "metadata" not in sent
    assert sent["model"] == MODEL
    assert sent["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert fake.messages.calls == []
    assert fake.closed == 1


def test_count_input_tokens_wraps_errors(fake: FakeClient) -> None:
    fake.messages.error = FakeStatusError("boom", 500)
    with pytest.raises(ProviderError, match="HTTP 500"):
        asyncio.run(_provider().count_input_tokens(_request()))


# ------------------------------------------------------------------------------- check


def test_check_reports_not_set_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_client(self: AnthropicApiProvider, api_key: str) -> None:
        raise AssertionError("check must not create a client")

    monkeypatch.setattr(AnthropicApiProvider, "_make_client", no_client)
    status = asyncio.run(AnthropicApiProvider().check())
    assert status.available is False
    assert status.provider == "anthropic"
    assert "ANTHROPIC_API_KEY not set" in status.detail
    assert status.version is not None
    assert status.version in status.detail


def test_check_reports_set_and_never_reveals_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    status = asyncio.run(AnthropicApiProvider().check())
    assert status.available is True
    assert "ANTHROPIC_API_KEY set" in status.detail
    dumped = status.model_dump_json()
    for fragment in (FAKE_KEY, FAKE_KEY[:6], FAKE_KEY[-4:], str(len(FAKE_KEY))):
        assert fragment not in dumped


# --------------------------------------------------------------------------- deep merge


def test_deep_merge_does_not_mutate_inputs() -> None:
    base = {"a": {"b": 1, "c": 2}, "d": [1]}
    override = {"a": {"c": 3}, "d": [2]}
    assert deep_merge(base, override) == {"a": {"b": 1, "c": 3}, "d": [2]}
    assert base == {"a": {"b": 1, "c": 2}, "d": [1]}
