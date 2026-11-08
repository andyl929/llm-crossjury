"""Subscription (cli) backend in the compare and synthesis services.

Uses fake CLI providers only (``FakeCliProvider``); no real CLI is ever run.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, cast

import pytest

from llm_crossjury.core.compare_service import (
    BudgetRefusedError,
    CompareDeps,
    ConsentRequiredError,
    Selection,
    SelectionError,
    budget_status,
    estimate_compare,
    run_compare,
    usable_models,
)
from llm_crossjury.core.config import BudgetConfig
from llm_crossjury.core.providers.base import Backend
from llm_crossjury.core.synthesis.types import ModelChoice
from llm_crossjury.core.synthesis_service import (
    FinishRequest,
    ResynthesisRequest,
    StepOneAnswer,
    SynthesisRequest,
    estimate_synthesis,
    plan_synthesis,
    run_agreement_step,
    run_finish_step,
    run_resynthesis,
)
from tests.unit._compare_fakes import (
    CLI_PROVIDERS,
    FIXED_CALL_COST,
    PER_OUTPUT_TOKEN,
    FakeCliProvider,
    FakeLedger,
    FakeResolver,
    make_deps,
    make_entry,
)
from tests.unit._synthesis_fakes import FakeStages, make_synthesis

TASK = "Which sorting algorithm should I use?"


def _cli_deps(**kwargs: Any) -> tuple[CompareDeps, dict[str, FakeCliProvider]]:
    clis = {p: FakeCliProvider(p) for p in CLI_PROVIDERS}
    return make_deps(cli_providers=clis, **kwargs), clis


def _mixed() -> list[Selection]:
    return [
        Selection(provider="anthropic", backend=Backend.CLI, model="anthropic-cli"),
        Selection(provider="openai", model="balanced"),
        Selection(provider="gemini", model="balanced"),
    ]


# --------------------------------------------------------------------------- models


def test_usable_models_per_backend() -> None:
    deps, _ = _cli_deps()
    assert [m.model_id for m in usable_models("anthropic", deps, Backend.CLI)] == ["anthropic-cli"]
    api_ids = [m.model_id for m in usable_models("anthropic", deps)]
    assert "anthropic-cli" not in api_ids
    assert "anthropic-model" in api_ids
    assert usable_models("gemini", deps, Backend.CLI) == ()  # no gemini cli provider


# --------------------------------------------------------------------------- compare


def test_mixed_estimate_prices_api_and_counts_cli() -> None:
    deps, _ = _cli_deps()
    est = asyncio.run(estimate_compare(TASK, _mixed(), deps))
    cli_lines = [line for line in est.lines if line.backend is Backend.CLI]
    assert len(cli_lines) == 1
    assert cli_lines[0].cost_high is None
    assert est.unpriced_calls == 1
    assert est.total_high == 2 * 4096 * PER_OUTPUT_TOKEN  # the two api calls only
    status = budget_status(est, deps)
    assert status.cli_calls == 1
    assert not status.refused


def test_cli_members_need_consent() -> None:
    deps, clis = _cli_deps()
    with pytest.raises(ConsentRequiredError) as info:
        asyncio.run(run_compare(TASK, _mixed(), deps))
    assert info.value.cli_calls == 1
    assert clis["anthropic"].requests == []
    assert cast(FakeLedger, deps.ledger).entries == []


def test_cli_call_is_recorded_with_unknown_cost_and_duration() -> None:
    ledger = FakeLedger()
    deps, clis = _cli_deps(ledger=ledger)
    result = asyncio.run(run_compare(TASK, _mixed(), deps, allow_unknown_cost=True))
    by = {m.provider: m for m in result.members}
    assert by["anthropic"].backend is Backend.CLI
    assert by["anthropic"].text == "anthropic cli answer"
    assert by["anthropic"].cost is None
    assert by["openai"].cost == FIXED_CALL_COST
    assert result.unknown_cost_members == 1
    assert result.total_cost == 2 * FIXED_CALL_COST
    entries = {e.provider: e for e in ledger.entries}
    assert entries["anthropic"].backend is Backend.CLI
    assert entries["anthropic"].cost is None
    assert entries["anthropic"].duration_s is not None
    assert entries["anthropic"].model == "anthropic-cli"
    assert entries["openai"].cost == FIXED_CALL_COST
    assert len(ledger.checks) == 2  # only the api calls are budget-checked
    assert ledger.reservations == {}
    assert len(clis["anthropic"].requests) == 1


def test_cli_member_needs_no_api_key() -> None:
    deps, _ = _cli_deps(key_envs={"anthropic": "ANTHROPIC_API_KEY"})
    sel = [Selection(provider="anthropic", backend=Backend.CLI, model="anthropic-cli")]
    result = asyncio.run(run_compare(TASK, sel, deps, allow_unknown_cost=True))
    assert result.members[0].error is None


def test_limits_apply_to_the_priced_part_only() -> None:
    # One api call (high 0.04096) fits a 0.05 run limit; the cli calls add nothing.
    deps, _ = _cli_deps(budget=BudgetConfig(run_limit=Decimal("0.05")))
    selections = [
        Selection(provider="anthropic", backend=Backend.CLI, model="anthropic-cli"),
        Selection(provider="xai", backend=Backend.CLI, model="xai-cli"),
        Selection(provider="openai", model="balanced"),
    ]
    result = asyncio.run(run_compare(TASK, selections, deps, allow_unknown_cost=True))
    assert [m.error for m in result.members] == [None, None, None]
    # Two api calls do not fit.
    selections.append(Selection(provider="gemini", model="balanced"))
    with pytest.raises(BudgetRefusedError):
        asyncio.run(run_compare(TASK, selections, deps, allow_unknown_cost=True))


def test_cli_call_runs_even_when_priced_budget_is_spent() -> None:
    deps, _ = _cli_deps(ledger=FakeLedger(spent_today=Decimal(5)))
    sel = [Selection(provider="anthropic", backend=Backend.CLI, model="anthropic-cli")]
    result = asyncio.run(run_compare(TASK, sel, deps, allow_unknown_cost=True))
    assert result.members[0].error is None


@pytest.mark.parametrize(
    ("selection", "match"),
    [
        (Selection(provider="gemini", backend=Backend.CLI, model="gemini-model"), "cli backend"),
        (
            Selection(provider="anthropic", backend=Backend.CLI, model="anthropic-model"),
            "cli_flags",
        ),
    ],
)
def test_invalid_cli_selections(selection: Selection, match: str) -> None:
    deps, _ = _cli_deps()
    with pytest.raises(SelectionError, match=match):
        asyncio.run(estimate_compare(TASK, [selection], deps))


def test_unavailable_cli_becomes_a_member_error() -> None:
    from llm_crossjury.core.providers.base import ProviderUnavailableError  # noqa: PLC0415

    def boom(req: object) -> str:
        raise ProviderUnavailableError("claude was not found on PATH")

    deps = make_deps(cli_providers={"anthropic": FakeCliProvider("anthropic", responder=boom)})
    sel = [Selection(provider="anthropic", backend=Backend.CLI, model="anthropic-cli")]
    result = asyncio.run(run_compare(TASK, sel, deps, allow_unknown_cost=True))
    assert result.members[0].error == "claude was not found on PATH"


# --------------------------------------------------------------------------- synthesis

CLI_ANSWERS = (
    StepOneAnswer(provider="anthropic", backend=Backend.CLI, model="anthropic-cli", text="A."),
    StepOneAnswer(provider="openai", backend=Backend.CLI, model="openai-cli", text="B."),
)
MIXED_ANSWERS = (
    StepOneAnswer(provider="anthropic", backend=Backend.CLI, model="anthropic-cli", text="A."),
    StepOneAnswer(provider="openai", model="openai-model", text="B."),
    StepOneAnswer(provider="gemini", model="gemini-model", text="C."),
)


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch) -> FakeStages:
    return FakeStages().install(monkeypatch)


def test_all_cli_synthesis_defaults_to_a_cli_chair(stages: FakeStages) -> None:
    deps, _ = _cli_deps()
    est = estimate_synthesis(SynthesisRequest(task=TASK, answers=CLI_ANSWERS, seed=1), deps)
    assert est.chair == ModelChoice(provider="anthropic", model_id="anthropic-cli")
    assert est.chair_backend is Backend.CLI
    assert est.total_high == 0
    assert est.cli_calls == 4  # judge, two reviewers, chair
    assert est.budget.cli_calls == 4
    assert not est.budget.refused
    assert all(r.backend is Backend.CLI for r in est.reviewers)


def test_mixed_synthesis_estimate(stages: FakeStages) -> None:
    deps, _ = _cli_deps()
    est = estimate_synthesis(SynthesisRequest(task=TASK, answers=MIXED_ANSWERS, seed=1), deps)
    assert est.chair == ModelChoice(provider="openai", model_id="openai-model")  # priced api
    assert est.cli_calls == 1  # the anthropic reviewer
    assert est.total_high > 0


def test_synthesis_with_cli_calls_needs_consent(stages: FakeStages) -> None:
    deps, clis = _cli_deps()
    with pytest.raises(ConsentRequiredError):
        asyncio.run(run_agreement_step(SynthesisRequest(task=TASK, answers=CLI_ANSWERS), deps))
    assert clis["anthropic"].requests == []


def test_cli_chair_goes_through_its_cli(stages: FakeStages) -> None:
    ledger = FakeLedger()
    deps, clis = _cli_deps(ledger=ledger)
    chair = ModelChoice(provider="anthropic", model_id="anthropic-cli")
    req = FinishRequest(
        task=TASK, answers=MIXED_ANSWERS, seed=1, chair=chair, allow_unknown_cost=True
    )
    result = asyncio.run(run_finish_step(req, deps))
    chair_calls = [c for c in result.calls if c.stage == "chair"]
    assert [(c.provider, c.backend, c.cost) for c in chair_calls] == [
        ("anthropic", Backend.CLI, None)
    ]
    assert len(clis["anthropic"].requests) == 2  # its review and the chair
    cli_entries = [e for e in ledger.entries if e.backend is Backend.CLI]
    assert len(cli_entries) == 2
    assert all(e.cost is None and e.duration_s is not None for e in cli_entries)
    by_stage = {s.stage: s for s in result.stage_costs}
    assert by_stage["chair"].unknown_cost_calls == 1
    assert len(ledger.checks) == 2  # the two api reviewers only


def test_resynthesis_with_cli_chair(stages: FakeStages) -> None:
    deps, _ = _cli_deps()
    labels = [f"Response {c}" for c in "AB"]
    req = ResynthesisRequest(
        task=TASK, answers=CLI_ANSWERS, seed=1, previous=make_synthesis(labels)
    )
    with pytest.raises(ConsentRequiredError):
        asyncio.run(run_resynthesis(req, deps))
    result = asyncio.run(run_resynthesis(req.model_copy(update={"allow_unknown_cost": True}), deps))
    assert [c.backend for c in result.calls] == [Backend.CLI]


def test_economy_fast_alias_only_if_reachable_on_the_same_backend() -> None:
    deps, _ = _cli_deps(
        extra_models=[
            make_entry("anthropic", "anthropic-fast"),  # api only
            make_entry("openai", "openai-fast-cli", backends=(Backend.API, Backend.CLI)),
        ]
    )
    resolver = cast(FakeResolver, deps.resolver)
    resolver.aliases[("anthropic", "fast")] = "anthropic-fast"
    resolver.aliases[("openai", "fast")] = "openai-fast-cli"
    plan = plan_synthesis(SynthesisRequest(task=TASK, answers=CLI_ANSWERS, seed=1), deps)
    by_provider = {m.provider: m.model_id for m in plan.reviewers.values()}
    assert by_provider == {"anthropic": "anthropic-cli", "openai": "openai-fast-cli"}
    assert plan.backends == {"anthropic": Backend.CLI, "openai": Backend.CLI}


def test_chair_backend_must_match_the_providers_answer() -> None:
    deps, _ = _cli_deps()
    req = SynthesisRequest(
        task=TASK,
        answers=MIXED_ANSWERS,
        chair=ModelChoice(provider="anthropic", model_id="anthropic-model"),
        chair_backend=Backend.API,
    )
    with pytest.raises(SelectionError, match="cli backend"):
        plan_synthesis(req, deps)


def test_explicit_cli_chair_for_a_provider_without_an_answer() -> None:
    deps, _ = _cli_deps()
    answers = MIXED_ANSWERS[1:]
    req = SynthesisRequest(
        task=TASK,
        answers=answers,
        chair=ModelChoice(provider="xai", model_id="xai-cli"),
        chair_backend=Backend.CLI,
    )
    plan = plan_synthesis(req, deps)
    assert plan.backends["xai"] is Backend.CLI
    assert plan.judge == ModelChoice(provider="xai", model_id="xai-cli")
