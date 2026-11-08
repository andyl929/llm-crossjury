"""Compare-mode service: estimate, budget checks, concurrent runs, failures."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from llm_crossjury.core.compare_service import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    PROMPT_ID,
    BudgetRefusedError,
    CompareResult,
    Selection,
    SelectionError,
    budget_status,
    default_prompts_dir,
    estimate_compare,
    execute_calls,
    key_is_set,
    load_prompt_template,
    prefix_fingerprint,
    prepare_calls,
    run_compare,
    usable_models,
)
from llm_crossjury.core.config import BudgetConfig, CrossjuryConfig, MemberConfig
from llm_crossjury.core.cost.pricing import PricingError
from llm_crossjury.core.modes import Mode
from llm_crossjury.core.modes.compare import build_compare_pipeline, selections_from_config
from llm_crossjury.core.orchestrator import Completed, RunContext
from llm_crossjury.core.providers.base import (
    Backend,
    ProviderError,
    ProviderUnavailableError,
    ReasoningEffort,
    Role,
)
from llm_crossjury.core.providers.mock import MockProvider
from tests.unit._compare_fakes import (
    FIXED_CALL_COST,
    PER_OUTPUT_TOKEN,
    PROVIDERS,
    FakeLedger,
    GatedProvider,
    echo_provider,
    make_deps,
    make_entry,
    raising_provider,
)

TASK = "Wie funktioniert {x} in Python?"


def _all(model: str = "balanced") -> list[Selection]:
    return [Selection(provider=p, model=model) for p in PROVIDERS]


# --------------------------------------------------------------------------- selection


def test_selection_defaults() -> None:
    sel = Selection(provider="openai", model="balanced")
    assert sel.max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS == 4096
    assert sel.temperature is None


@pytest.mark.parametrize(
    ("task", "selections", "message"),
    [
        ("   ", [Selection(provider="openai", model="balanced")], "empty"),
        ("task", [], "at least one"),
        ("task", [Selection(provider="nope", model="balanced")], "Unknown provider"),
        (
            "task",
            [Selection(provider="openai", model="balanced")] * 2,
            "more than once",
        ),
        (
            "task",
            [Selection(provider="openai", model="balanced", max_output_tokens=9000)],
            "exceeds the model limit",
        ),
    ],
)
def test_invalid_selections(task: str, selections: list[Selection], message: str) -> None:
    with pytest.raises(SelectionError, match=message):
        prepare_calls(task, selections, make_deps())


def test_unpriced_model_is_refused() -> None:
    with pytest.raises(PricingError):
        prepare_calls(TASK, [Selection(provider="openai", model="openai-unpriced")], make_deps())


# --------------------------------------------------------------------------- prompt


def test_prompt_template_follows_conventions() -> None:
    template = load_prompt_template(default_prompts_dir() / f"{PROMPT_ID}.md")
    assert template.prompt_id == PROMPT_ID
    assert "language" in template.stable_prefix
    prompt = template.render(TASK)
    assert prompt.stable_prefix[0].role is Role.SYSTEM
    assert prompt.variable_suffix[0].role is Role.USER
    assert prompt.variable_suffix[0].content == TASK  # braces in the task survive
    assert TASK not in prompt.stable_prefix[0].content


def test_prepared_calls_share_prefix_and_resolve_aliases() -> None:
    calls = prepare_calls(TASK, _all(), make_deps())
    assert [c.request.model for c in calls] == [f"{p}-model" for p in PROVIDERS]
    fingerprints = {c.plan.prefix_fingerprint for c in calls}
    assert fingerprints == {prefix_fingerprint(calls[0].request.prompt)}
    for call in calls:
        assert call.request.reasoning is ReasoningEffort.DEFAULT
        assert call.request.max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS
        assert call.plan.expected_output_tokens_high == DEFAULT_MAX_OUTPUT_TOKENS
        assert call.plan.expected_output_tokens_low < DEFAULT_MAX_OUTPUT_TOKENS


def test_fingerprint_ignores_suffix() -> None:
    deps = make_deps()
    a = prepare_calls("one", _all()[:1], deps)[0].request.prompt
    b = prepare_calls("two", _all()[:1], deps)[0].request.prompt
    assert prefix_fingerprint(a) == prefix_fingerprint(b)


# --------------------------------------------------------------------------- models


def test_usable_models_are_priced_api_entries_with_alias_labels() -> None:
    deps = make_deps(extra_models=[make_entry("openai", "openai-cli", backends=(Backend.CLI,))])
    models = usable_models("openai", deps)
    assert [(m.model_id, m.aliases) for m in models] == [("openai-model", ("balanced",))]


def test_key_is_set_reports_presence_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "  ")
    assert not key_is_set("OPENAI_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-value")
    assert key_is_set("OPENAI_API_KEY")


# --------------------------------------------------------------------------- estimate


def test_estimate_and_budget_status() -> None:
    deps = make_deps()
    estimate = asyncio.run(estimate_compare(TASK, _all(), deps))
    expected_high = 4 * DEFAULT_MAX_OUTPUT_TOKENS * PER_OUTPUT_TOKEN
    assert estimate.total_high == expected_high
    status = budget_status(estimate, deps)
    assert not status.refused
    assert not status.needs_confirmation
    assert status.daily_remaining == Decimal("5.00")


def test_budget_status_needs_confirmation() -> None:
    deps = make_deps(budget=BudgetConfig(confirm_above=Decimal("0.05")))
    status = budget_status(asyncio.run(estimate_compare(TASK, _all(), deps)), deps)
    assert status.needs_confirmation
    assert not status.refused


def test_budget_status_refuses_above_daily_remaining() -> None:
    deps = make_deps(ledger=FakeLedger(spent_today=Decimal("4.95")))
    status = budget_status(asyncio.run(estimate_compare(TASK, _all(), deps)), deps)
    assert status.refused
    assert status.refusal_reason is not None
    assert "remaining" in status.refusal_reason
    assert status.daily_remaining == Decimal("0.05")
    assert not status.needs_confirmation


# --------------------------------------------------------------------------- run


def test_run_all_members_succeed() -> None:
    ledger = FakeLedger()
    deps = make_deps(ledger=ledger)
    result = asyncio.run(run_compare(TASK, _all(), deps))
    assert [m.provider for m in result.members] == list(PROVIDERS)
    assert all(m.error is None and m.text and TASK in m.text for m in result.members)
    assert all(m.cost == FIXED_CALL_COST and m.currency == "USD" for m in result.members)
    assert result.total_cost == 4 * FIXED_CALL_COST
    assert len(ledger.entries) == 4
    assert {e.run_id for e in ledger.entries} == {result.run_id}
    assert all(e.stage == "answer" and e.cost == FIXED_CALL_COST for e in ledger.entries)
    assert ledger.reservations == {}
    assert ledger.checks == [DEFAULT_MAX_OUTPUT_TOKENS * PER_OUTPUT_TOKEN] * 4


def test_members_run_concurrently() -> None:
    # Each gated call waits until all four are in flight; sequential calls would time out.
    barrier = asyncio.Barrier(len(PROVIDERS))
    deps = make_deps(providers={p: GatedProvider(p, barrier) for p in PROVIDERS})
    result = asyncio.run(run_compare(TASK, _all(), deps))
    assert [m.error for m in result.members] == [None] * len(PROVIDERS)


def test_one_failure_does_not_break_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake-value")
    ledger = FakeLedger()
    deps = make_deps(
        ledger=ledger,
        providers={
            "anthropic": raising_provider("anthropic", ProviderError("vendor overloaded")),
            "openai": echo_provider("openai"),
            "gemini": echo_provider("gemini"),
            "xai": raising_provider("xai", RuntimeError("raw sdk text sk-should-not-leak")),
        },
        key_envs={"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"},
    )
    result = asyncio.run(run_compare(TASK, _all(), deps))
    by = {m.provider: m for m in result.members}
    assert by["anthropic"].error == "vendor overloaded"
    assert by["openai"].error == (
        "OPENAI_API_KEY is not set. Add it in Settings > API Keys (/settings)."
    )
    assert by["gemini"].text
    assert by["gemini"].error is None
    assert by["xai"].error == "Unexpected error (RuntimeError)."
    assert "should-not-leak" not in result.model_dump_json()
    assert [e.provider for e in ledger.entries] == ["gemini"]
    assert result.total_cost == FIXED_CALL_COST
    assert ledger.reservations == {}


def test_unavailable_provider_message() -> None:
    deps = make_deps(
        providers={
            **{p: echo_provider(p) for p in PROVIDERS},
            "openai": raising_provider("openai", ProviderUnavailableError("SDK not installed")),
        }
    )
    result = asyncio.run(run_compare(TASK, _all(), deps))
    assert [m.error for m in result.members] == [None, "SDK not installed", None, None]


def test_run_refused_before_any_call() -> None:
    providers = {p: echo_provider(p) for p in PROVIDERS}
    deps = make_deps(providers=providers, budget=BudgetConfig(run_limit=Decimal("0.05")))
    with pytest.raises(BudgetRefusedError, match="run limit") as info:
        asyncio.run(run_compare(TASK, _all(), deps))
    assert info.value.status.refused
    assert all(not p.requests for p in providers.values())


def test_per_call_budget_check_stops_calls_that_could_overshoot() -> None:
    # Each call reserves 0.04096; with a 0.10 run limit only two can be in flight.
    ledger = FakeLedger()
    barrier = asyncio.Barrier(2)
    deps = make_deps(
        ledger=ledger,
        budget=BudgetConfig(run_limit=Decimal("0.10")),
        providers={p: GatedProvider(p, barrier) for p in PROVIDERS},
    )
    prepared = prepare_calls(TASK, _all(), deps)
    result = asyncio.run(execute_calls("run-1", prepared, deps))
    errors = [m.error for m in result.members]
    assert errors[:2] == [None, None]
    assert errors[2:] == ["Not started: run budget would be exceeded."] * 2
    assert len(ledger.entries) == 2
    assert ledger.reservations == {}


def test_unknown_cost_is_counted() -> None:
    deps = make_deps(compute_cost=lambda usage, price: None)
    result = asyncio.run(run_compare(TASK, _all()[:2], deps))
    assert result.total_cost == Decimal(0)
    assert result.unknown_cost_members == 2


def test_request_carries_settings() -> None:
    provider = MockProvider("openai", lambda req: "ok")
    deps = make_deps(providers={**{p: echo_provider(p) for p in PROVIDERS}, "openai": provider})
    sel = Selection(provider="openai", model="openai-model", temperature=0.3, max_output_tokens=99)
    asyncio.run(run_compare(TASK, [sel], deps))
    (request,) = provider.requests
    assert request.temperature == pytest.approx(0.3)
    assert request.max_output_tokens == 99
    assert request.response_schema is None
    assert request.tags["stage"] == "answer"
    assert request.tags["run_id"]


# --------------------------------------------------------------------------- pipeline


def _config(*members: MemberConfig) -> CrossjuryConfig:
    return CrossjuryConfig(members=members, chair=members[0])


def test_compare_pipeline_runs_answer_stage() -> None:
    deps = make_deps()
    config = _config(
        MemberConfig(id="a", provider="anthropic", model="balanced", backend=Backend.API),
        MemberConfig(id="o", provider="openai", model="openai-model", backend=Backend.API),
    )
    pipeline = build_compare_pipeline(config, deps=deps)
    ctx = RunContext(run_id="run-x", mode=Mode.COMPARE, task=TASK)
    assert len(pipeline.stages[0].plan(ctx)) == 2  # type: ignore[union-attr]
    outcome = asyncio.run(pipeline.run(ctx))
    assert isinstance(outcome, Completed)
    artifact = outcome.ctx.artifacts["answer"]
    assert isinstance(artifact, CompareResult)
    assert artifact.run_id == "run-x"
    assert [m.model for m in artifact.members] == ["anthropic-model", "openai-model"]


@pytest.mark.parametrize(
    "member",
    [
        MemberConfig(id="c", provider="openai", model="balanced", backend=Backend.CLI),
        MemberConfig(
            id="r",
            provider="openai",
            model="balanced",
            backend=Backend.API,
            reasoning=ReasoningEffort.HIGH,
        ),
    ],
)
def test_selections_from_config_rejects_unsupported(member: MemberConfig) -> None:
    with pytest.raises(ValueError, match="compare mode supports"):
        selections_from_config(_config(member))
