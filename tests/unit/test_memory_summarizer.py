"""Summaries: trigger, folding, failure tolerance, storing; memory model choice."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from llm_crossjury.core.chat_service import update_session_memory
from llm_crossjury.core.compare_service import CompareDeps
from llm_crossjury.core.memory.picker import MemoryModel, pick_memory_model
from llm_crossjury.core.memory.summarizer import (
    MEMORY_STAGE,
    FoldPlan,
    plan_fold,
    update_memory,
)
from llm_crossjury.core.providers.base import (
    Backend,
    CompletionRequest,
    CompletionResult,
    ProviderError,
    Usage,
)
from llm_crossjury.core.providers.mock import MockProvider
from llm_crossjury.core.sessions.interfaces import AppSettings
from llm_crossjury.core.sessions.models import MemoryState, ModelRef, Session, TurnStatus
from llm_crossjury.core.synthesis.types import CallSpec
from tests.unit._compare_fakes import (
    CLI_PROVIDERS,
    PROVIDERS,
    FakeCliProvider,
    FakeLedger,
    FakePricing,
    FakeResolver,
    make_deps,
    make_pricing,
)
from tests.unit._session_fakes import FakeSessionStore, chat_turn, make_session

BUDGET = 8_000
LONG = "x" * 3_000  # about 1,000 tokens with the local estimate
UPDATE = {"summary": "They planned a database.", "facts": ["Use PostgreSQL.", "Use PostgreSQL."]}


def _long_session(turns: int = 6, memory: MemoryState | None = None) -> Session:
    return make_session(
        [chat_turn(f"question {i}", f"{LONG} {i}") for i in range(turns)], memory=memory
    )


def _result(text: str) -> CompletionResult:
    return CompletionResult(
        text=text,
        usage=Usage(
            input_tokens=1,
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=1,
            reasoning_tokens=0,
        ),
        provider="openai",
        model="openai-model",
        backend=Backend.API,
        finish_reason="stop",
        started_at=datetime.now(UTC),
        latency_s=0.0,
        native_params_used={},
    )


# --------------------------------------------------------------------------- trigger


def test_no_summary_below_the_threshold() -> None:
    assert plan_fold(_long_session(3), BUDGET) is None
    assert plan_fold(make_session(), BUDGET) is None


def test_fold_takes_the_oldest_turns_and_keeps_the_last_two() -> None:
    plan = plan_fold(_long_session(6), BUDGET)
    assert plan == FoldPlan(start=0, end=4)


def test_fold_starts_after_summarized_turns_and_stops_at_pending() -> None:
    session = _long_session(8, memory=MemoryState(summary="s", summarized_through=2))
    pending = chat_turn("running", None, status=TurnStatus.PENDING)
    turns = list(session.turns)
    turns.insert(6, pending)
    plan = plan_fold(session.model_copy(update={"turns": tuple(turns)}), BUDGET)
    assert plan is not None
    assert plan.start == 2
    assert plan.end <= 6


# --------------------------------------------------------------------------- update


def test_update_memory_folds_and_dedupes_facts() -> None:
    specs: list[CallSpec] = []

    async def call(spec: CallSpec) -> CompletionResult:
        specs.append(spec)
        return _result(json.dumps(UPDATE))

    state = asyncio.run(
        update_memory(
            _long_session(6),
            call=call,
            provider="openai",
            model_id="openai-model",
            budget_tokens=BUDGET,
        )
    )
    assert state is not None
    assert state.summary == "They planned a database."
    assert state.facts == ("Use PostgreSQL.",)
    assert state.summarized_through == 4
    assert state.updated_at is not None
    assert specs[0].stage == MEMORY_STAGE
    assert specs[0].response_schema is not None
    rendered = specs[0].prompt.variable_suffix[0].content
    assert "question 0" in rendered
    assert "question 5" not in rendered


@pytest.mark.parametrize("behaviour", ["provider_error", "bad_json", "empty_summary", "unexpected"])
def test_update_memory_never_raises(behaviour: str) -> None:
    async def call(spec: CallSpec) -> CompletionResult:
        if behaviour == "provider_error":
            raise ProviderError("vendor down")
        if behaviour == "unexpected":
            raise RuntimeError("bug")
        if behaviour == "empty_summary":
            return _result(json.dumps({"summary": " ", "facts": []}))
        return _result("not json at all")

    state = asyncio.run(
        update_memory(
            _long_session(6),
            call=call,
            provider="openai",
            model_id="openai-model",
            budget_tokens=BUDGET,
        )
    )
    assert state is None


# --------------------------------------------------------------------------- picker


def _deps_with_fast(**kwargs: object) -> CompareDeps:
    deps = make_deps(**kwargs)  # type: ignore[arg-type]
    aliases: dict[tuple[str, str], str | None] = {}
    for p in PROVIDERS:
        aliases[(p, "balanced")] = f"{p}-model"
        aliases[(p, "fast")] = f"{p}-model"
    for p in CLI_PROVIDERS:
        aliases[(p, "fast")] = f"{p}-model"
    return replace(deps, resolver=FakeResolver(aliases))


def test_picker_prefers_the_configured_model() -> None:
    deps = _deps_with_fast()
    settings = AppSettings(memory_model=ModelRef(provider="gemini", model="gemini-model"))
    assert pick_memory_model(deps, settings) == MemoryModel("gemini", Backend.API, "gemini-model")


def test_picker_skips_an_unusable_configured_model() -> None:
    deps = _deps_with_fast()
    settings = AppSettings(memory_model=ModelRef(provider="gemini", model="gemini-unpriced"))
    picked = pick_memory_model(deps, settings)
    assert picked is not None
    assert picked.model_id != "gemini-unpriced"


def test_picker_uses_a_signed_in_cli_fast_alias_next() -> None:
    clis = {p: FakeCliProvider(p) for p in CLI_PROVIDERS}
    deps = _deps_with_fast(cli_providers=clis)
    aliases = dict(deps.resolver.aliases)  # type: ignore[attr-defined]
    aliases[("openai", "fast")] = "openai-cli"
    deps = replace(deps, resolver=FakeResolver(aliases))
    picked = pick_memory_model(deps, AppSettings(), cli_available={"openai": True})
    assert picked == MemoryModel("openai", Backend.CLI, "openai-cli")
    # Not signed in (or not checked): fall through to the cheapest keyed API model.
    fallback = pick_memory_model(deps, AppSettings(), cli_available={"openai": False})
    assert fallback is not None
    assert fallback.backend is Backend.API


def test_picker_takes_the_cheapest_keyed_fast_api_model(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = {p: f"{p.upper()}_API_KEY" for p in PROVIDERS}
    deps = _deps_with_fast(key_envs=keys)
    prices = {(p, f"{p}-model"): make_pricing() for p in PROVIDERS}
    cheap = make_pricing().model_copy(update={"input_per_mtok": Decimal("0.1")})
    prices[("xai", "xai-model")] = cheap
    deps = replace(deps, pricing=FakePricing(prices))
    assert pick_memory_model(deps, AppSettings()) is None  # no key set
    monkeypatch.setenv("GEMINI_API_KEY", "set")
    assert pick_memory_model(deps, AppSettings()) == MemoryModel(
        "gemini", Backend.API, "gemini-model"
    )
    monkeypatch.setenv("XAI_API_KEY", "set")
    assert pick_memory_model(deps, AppSettings()) == MemoryModel("xai", Backend.API, "xai-model")


def test_picker_returns_none_without_fast_aliases() -> None:
    assert pick_memory_model(make_deps(), AppSettings()) is None


# --------------------------------------------------------------------------- storing


def _memory_provider(name: str) -> MockProvider:
    def respond(req: CompletionRequest) -> str:
        assert req.tags["stage"] == MEMORY_STAGE
        return json.dumps(UPDATE)

    return MockProvider(name, respond)


def test_update_session_memory_stores_and_records_the_call() -> None:
    ledger = FakeLedger()
    deps = _deps_with_fast(providers={p: _memory_provider(p) for p in PROVIDERS}, ledger=ledger)
    store = FakeSessionStore()
    session = store.put(_long_session(6))
    settings = AppSettings(context_budget_tokens=BUDGET)
    state = asyncio.run(update_session_memory(session.id, store, deps, settings=settings))
    assert state is not None
    assert store.get(session.id).memory == state
    assert [e.stage for e in ledger.entries] == [MEMORY_STAGE]


def test_update_session_memory_skips_when_not_needed_or_no_model() -> None:
    store = FakeSessionStore()
    small = store.put(_long_session(2))
    big = store.put(_long_session(6))
    settings = AppSettings(context_budget_tokens=BUDGET)
    assert (
        asyncio.run(update_session_memory(small.id, store, _deps_with_fast(), settings=settings))
        is None
    )
    assert asyncio.run(update_session_memory(big.id, store, make_deps(), settings=settings)) is None
    assert store.get(big.id).memory == MemoryState()
    assert (
        asyncio.run(update_session_memory("0" * 32, store, make_deps(), settings=settings)) is None
    )


def test_update_session_memory_does_not_overwrite_a_newer_update() -> None:
    store = FakeSessionStore()
    session = store.put(_long_session(6))
    newer = MemoryState(summary="newer", summarized_through=1)

    def respond(req: CompletionRequest) -> str:
        store.put(store.get(session.id).model_copy(update={"memory": newer}))
        return json.dumps(UPDATE)

    deps = _deps_with_fast(providers={p: MockProvider(p, respond) for p in PROVIDERS})
    settings = AppSettings(context_budget_tokens=BUDGET)
    assert asyncio.run(update_session_memory(session.id, store, deps, settings=settings)) is None
    assert store.get(session.id).memory == newer
