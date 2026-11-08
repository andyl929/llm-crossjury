"""Gemini ``api`` adapter, with the SDK client faked at the adapter boundary.

The real ``google.genai`` package is never imported here (see the adapter's module
docstring for why).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC
from enum import Enum
from types import SimpleNamespace
from typing import Any

import pytest

from llm_crossjury.core.providers.api.gemini import GeminiApiProvider, map_usage
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

FAKE_KEY = "fake-gemini-key-abcdefghij"
MODEL = "fictional-gemini-model"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": ["string", "null"]}},
    "required": ["answer"],
    "additionalProperties": False,
}


class FakeFinishReason(Enum):
    STOP = "STOP"


def _entry(
    *, structured: bool | None, control: ReasoningControlType = ReasoningControlType.LEVELS
) -> ModelEntry:
    has_mapping = control in (ReasoningControlType.LEVELS, ReasoningControlType.BUDGET)
    return ModelEntry(
        provider="gemini",
        model_id=MODEL,
        backends=(),
        reasoning_control=ReasoningControl(
            type=control,
            parameter="thinking_config.thinking_level" if has_mapping else None,
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


def _metadata(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "prompt_token_count": 1000,
        "cached_content_token_count": 800,
        "candidates_token_count": 40,
        "thoughts_token_count": 60,
        "total_token_count": 1100,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _response(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "candidates": [
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(text="thinking summary", thought=True),
                        SimpleNamespace(text="Hello ", thought=None),
                        SimpleNamespace(text="world", thought=None),
                    ]
                ),
                finish_reason=FakeFinishReason.STOP,
            )
        ],
        "model_version": "fictional-gemini-model-001",
        "usage_metadata": _metadata(),
        "prompt_feedback": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@dataclass
class FakeModels:
    response: Any = None
    count: Any = None
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    count_calls: list[dict[str, Any]] = field(default_factory=list)

    async def generate_content(self, **kwargs: Any) -> Any:
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
class FakeAio:
    models: FakeModels
    closed: int = 0

    async def aclose(self) -> None:
        self.closed += 1


@dataclass
class FakeClient:
    aio: FakeAio


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    client = FakeClient(
        aio=FakeAio(models=FakeModels(response=_response(), count=SimpleNamespace(total_tokens=77)))
    )

    def make_client(self: GeminiApiProvider, api_key: str) -> FakeClient:
        return client

    monkeypatch.setattr(GeminiApiProvider, "_make_client", make_client)
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    return client


def _request(**overrides: Any) -> CompletionRequest:
    values: dict[str, Any] = {
        "model": MODEL,
        "prompt": Prompt(
            stable_prefix=(
                Message(role=Role.SYSTEM, content="sys one"),
                Message(role=Role.SYSTEM, content="sys two"),
                Message(role=Role.USER, content="shared document"),
            ),
            variable_suffix=(
                Message(role=Role.ASSISTANT, content="ack"),
                Message(role=Role.USER, content="question"),
            ),
        ),
        "response_schema": None,
        "temperature": None,
        "max_output_tokens": None,
        "reasoning": ReasoningEffort.DEFAULT,
        "native_params": {},
        "cache_prefix": True,
        "timeout_s": None,
        "tags": {},
    }
    values.update(overrides)
    return CompletionRequest(**values)


def _provider(*entries: ModelEntry) -> GeminiApiProvider:
    return GeminiApiProvider(registry=FakeRegistry(*entries) if entries else None)


# ----------------------------------------------------------------------------- protocol


def test_satisfies_provider_protocol() -> None:
    assert isinstance(GeminiApiProvider(), Provider)
    assert GeminiApiProvider("ALT_KEY").api_key_env == "ALT_KEY"


# ------------------------------------------------------------------------- capabilities


def test_capabilities_without_registry() -> None:
    caps = GeminiApiProvider().capabilities(MODEL)
    assert caps.model_dump() == {
        "structured_output": False,
        "caching": False,
        "batch": False,
        "temperature": True,
        "reasoning": False,
        "input_modalities": frozenset(),
    }


def test_capabilities_from_registry_entry() -> None:
    caps = _provider(_entry(structured=True)).capabilities(MODEL)
    assert caps.model_dump() == {
        "structured_output": True,
        "caching": True,
        "batch": False,
        "temperature": True,
        "reasoning": True,
        "input_modalities": frozenset(),
    }
    caps = _provider(_entry(structured=None, control=ReasoningControlType.NONE)).capabilities(MODEL)
    assert caps.structured_output is False
    assert caps.reasoning is False


# ----------------------------------------------------------------------------- complete


def test_complete_maps_request_and_result(fake: FakeClient) -> None:
    result = asyncio.run(
        _provider().complete(_request(temperature=0.3, max_output_tokens=512, timeout_s=2.5))
    )
    (sent,) = fake.aio.models.calls
    assert sent["model"] == MODEL
    assert sent["contents"] == [
        {"role": "user", "parts": [{"text": "shared document"}]},
        {"role": "model", "parts": [{"text": "ack"}]},
        {"role": "user", "parts": [{"text": "question"}]},
    ]
    assert sent["config"] == {
        "system_instruction": {"parts": [{"text": "sys one"}, {"text": "sys two"}]},
        "max_output_tokens": 512,
        "temperature": 0.3,
        "http_options": {"timeout": 2500},
    }
    assert fake.aio.closed == 1

    assert result.text == "Hello world"
    assert result.model == "fictional-gemini-model-001"
    assert result.provider == "gemini"
    assert result.finish_reason == "STOP"
    assert result.started_at.tzinfo is UTC
    assert result.latency_s is not None
    assert result.usage.model_dump() == {
        "input_tokens": 200,
        "cached_input_tokens": 800,
        "cache_write_tokens": 0,
        "output_tokens": 40,
        "reasoning_tokens": 60,
    }


def test_optional_fields_are_omitted(fake: FakeClient) -> None:
    prompt = Prompt(stable_prefix=(), variable_suffix=(Message(role=Role.USER, content="hi"),))
    asyncio.run(_provider().complete(_request(prompt=prompt)))
    assert fake.aio.models.calls[0]["config"] == {}


def test_consecutive_turns_are_merged(fake: FakeClient) -> None:
    prompt = Prompt(
        stable_prefix=(Message(role=Role.USER, content="doc"),),
        variable_suffix=(Message(role=Role.USER, content="q"),),
    )
    asyncio.run(_provider().complete(_request(prompt=prompt)))
    assert fake.aio.models.calls[0]["contents"] == [
        {"role": "user", "parts": [{"text": "doc"}, {"text": "q"}]}
    ]


def test_native_structured_output_and_native_params(fake: FakeClient) -> None:
    native = {"thinking_config": {"thinking_level": "high"}, "temperature": 1.0}
    request = _request(response_schema=SCHEMA, native_params=native, temperature=0.1)
    result = asyncio.run(_provider(_entry(structured=True)).complete(request))
    config = fake.aio.models.calls[0]["config"]
    assert config["response_mime_type"] == "application/json"
    assert config["response_json_schema"] == SCHEMA
    assert config["thinking_config"] == {"thinking_level": "high"}
    assert config["temperature"] == 1.0  # native_params win
    assert result.native_params_used == native
    assert fake.aio.models.calls[0]["contents"][-1]["parts"][-1]["text"] == "question"


def test_schema_fallback(fake: FakeClient) -> None:
    asyncio.run(_provider(_entry(structured=False)).complete(_request(response_schema=SCHEMA)))
    sent = fake.aio.models.calls[0]
    assert "response_json_schema" not in sent["config"]
    text = sent["contents"][-1]["parts"][-1]["text"]
    assert text.startswith("question\n\n")
    assert '"additionalProperties": false' in text


def test_blocked_prompt_reports_block_reason(fake: FakeClient) -> None:
    fake.aio.models.response = _response(
        candidates=[],
        usage_metadata=_metadata(candidates_token_count=None, thoughts_token_count=None),
        prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
    )
    result = asyncio.run(_provider().complete(_request()))
    assert result.text == ""
    assert result.finish_reason == "BLOCKED: SAFETY"
    assert result.model == "fictional-gemini-model-001"


@pytest.mark.parametrize("value", [None, ""])
def test_missing_key_raises_unavailable(
    fake: FakeClient, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("GEMINI_API_KEY", value)
    with pytest.raises(ProviderUnavailableError, match=r"^GEMINI_API_KEY not set$"):
        asyncio.run(_provider().complete(_request()))
    assert fake.aio.models.calls == []


class FakeApiError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code} {message}")
        self.code = code


def test_sdk_errors_are_wrapped_and_redacted(fake: FakeClient) -> None:
    fake.aio.models.error = FakeApiError(400, f"API key not valid: key={FAKE_KEY}")
    with pytest.raises(ProviderError) as info:
        asyncio.run(_provider().complete(_request()))
    message = str(info.value)
    assert FAKE_KEY not in message
    assert "[REDACTED]" in message
    assert "HTTP 400" in message
    assert "FakeApiError" in message
    assert info.value.__cause__ is None
    assert fake.aio.closed == 1


def test_system_message_in_suffix_is_rejected(fake: FakeClient) -> None:
    prompt = Prompt(stable_prefix=(), variable_suffix=(Message(role=Role.SYSTEM, content="x"),))
    with pytest.raises(ProviderError, match="stable_prefix"):
        asyncio.run(_provider().complete(_request(prompt=prompt)))


# ------------------------------------------------------------------------------- usage


def test_usage_omitted_counters_mean_zero() -> None:
    usage = map_usage(
        _metadata(cached_content_token_count=None, thoughts_token_count=None, prompt_token_count=9)
    )
    assert usage.model_dump() == {
        "input_tokens": 9,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 40,
        "reasoning_tokens": 0,
    }


def test_usage_unknown_when_missing() -> None:
    assert set(map_usage(None).model_dump().values()) == {None}
    usage = map_usage(_metadata(prompt_token_count=None))
    assert usage.input_tokens is None
    assert usage.cached_input_tokens is None
    assert usage.cache_write_tokens is None


# ------------------------------------------------------------------------ count tokens


def test_count_input_tokens_puts_system_text_first(fake: FakeClient) -> None:
    count = asyncio.run(_provider().count_input_tokens(_request()))
    assert count == 77
    (sent,) = fake.aio.models.count_calls
    assert sent["model"] == MODEL
    assert sent["config"] is None
    assert sent["contents"][0] == {
        "role": "user",
        "parts": [{"text": "sys one"}, {"text": "sys two"}],
    }
    assert sent["contents"][1] == {"role": "user", "parts": [{"text": "shared document"}]}
    assert fake.aio.models.calls == []
    assert fake.aio.closed == 1


def test_count_input_tokens_wraps_errors(fake: FakeClient) -> None:
    fake.aio.models.error = FakeApiError(503, "unavailable")
    with pytest.raises(ProviderError, match="HTTP 503"):
        asyncio.run(_provider().count_input_tokens(_request(timeout_s=1.0)))
    assert fake.aio.models.count_calls[0]["config"] == {"http_options": {"timeout": 1000}}


# ------------------------------------------------------------------------------- check


def test_check_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_client(self: GeminiApiProvider, api_key: str) -> None:
        raise AssertionError("check must not create a client")

    monkeypatch.setattr(GeminiApiProvider, "_make_client", no_client)
    status = asyncio.run(GeminiApiProvider().check())
    assert status.available is False
    assert "GEMINI_API_KEY not set" in status.detail
    assert status.version is not None
    assert "google-genai SDK" in status.detail


def test_check_with_key_never_reveals_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    status = asyncio.run(GeminiApiProvider().check())
    assert status.available is True
    assert "GEMINI_API_KEY set" in status.detail
    dumped = status.model_dump_json()
    for fragment in (FAKE_KEY, FAKE_KEY[:6], FAKE_KEY[-4:]):
        assert fragment not in dumped
