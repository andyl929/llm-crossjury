"""Synthesis service: stateless steps with fake stages, budget, labels, scrubbing."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, cast

import pytest

from llm_crossjury.core.anonymize import REDACTION
from llm_crossjury.core.compare_service import BudgetRefusedError, CompareDeps, SelectionError
from llm_crossjury.core.config import BudgetConfig
from llm_crossjury.core.cost.pricing import PricingError
from llm_crossjury.core.providers.base import Backend, ProviderError
from llm_crossjury.core.synthesis.chair import Ruling
from llm_crossjury.core.synthesis.types import ModelChoice
from llm_crossjury.core.synthesis_service import (
    AGREEMENT_SKIP_THRESHOLD,
    FinishRequest,
    ResynthesisRequest,
    StepOneAnswer,
    SynthesisRequest,
    SynthesisStageError,
    default_chair,
    estimate_resynthesis,
    estimate_synthesis,
    plan_synthesis,
    run_agreement_step,
    run_finish_step,
    run_resynthesis,
)
from tests.unit._compare_fakes import (
    FakeLedger,
    FakePricing,
    FakeResolver,
    make_deps,
    make_entry,
    make_pricing,
    raising_provider,
)
from tests.unit._synthesis_fakes import FakeStages, make_synthesis

TASK = "Which sorting algorithm should I use?"
ANSWERS = (
    StepOneAnswer(provider="anthropic", model="anthropic-model", text="Claude says: quicksort."),
    StepOneAnswer(provider="openai", model="openai-model", text="Use merge sort, said GPT-4o."),
    StepOneAnswer(provider="gemini", model="gemini-model", text="Timsort is best."),
)


def _deps(**kwargs: Any) -> CompareDeps:
    """Fake deps: openai has the highest output price; every provider has a priced
    ``<p>-fast`` model behind its ``fast`` alias except xai."""
    fast = [make_entry(p, f"{p}-fast") for p in ("anthropic", "openai", "gemini")]
    deps = make_deps(extra_models=fast, **kwargs)
    pricing = cast(FakePricing, deps.pricing)
    resolver = cast(FakeResolver, deps.resolver)
    for p in ("anthropic", "openai", "gemini"):
        pricing.prices[(p, f"{p}-fast")] = make_pricing()
        resolver.aliases[(p, "fast")] = f"{p}-fast"
    pricing.prices[("openai", "openai-model")] = make_pricing().model_copy(
        update={"output_per_mtok": Decimal(10)}
    )
    return deps


def _req(**kwargs: Any) -> SynthesisRequest:
    return SynthesisRequest(task=TASK, answers=ANSWERS, seed=42, **kwargs)


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch) -> FakeStages:
    return FakeStages().install(monkeypatch)


# --------------------------------------------------------------------------- planning


def test_labels_are_stable_for_a_seed_and_independent_of_order() -> None:
    deps = _deps()
    first = plan_synthesis(_req(), deps)
    again = plan_synthesis(_req(), deps)
    shuffled = plan_synthesis(
        SynthesisRequest(task=TASK, answers=tuple(reversed(ANSWERS)), seed=42), deps
    )
    assert first.labels == again.labels == shuffled.labels
    assert [a.label for a in first.answers] == ["Response A", "Response B", "Response C"]
    assert {info.provider for info in first.labels} == {"anthropic", "openai", "gemini"}
    others = {plan_synthesis(_req().model_copy(update={"seed": s}), deps).labels for s in range(8)}
    assert len(others) > 1  # the seed really shuffles


def test_missing_seed_is_generated_and_returned() -> None:
    plan = plan_synthesis(SynthesisRequest(task=TASK, answers=ANSWERS), _deps())
    assert 0 <= plan.seed <= 2**31 - 1


def test_answers_are_scrubbed() -> None:
    plan = plan_synthesis(_req(), _deps())
    texts = " ".join(a.text for a in plan.answers)
    assert plan.scrubbed
    assert plan.replacements == 2
    assert "Claude" not in texts
    assert "GPT" not in texts
    assert REDACTION in texts


@pytest.mark.parametrize(
    ("task", "extra"),
    [("Compare Claude and Gemini on sorting.", None), (TASK, "Prefer what OpenAI recommends.")],
)
def test_scrubbing_is_off_when_the_task_names_a_vendor(task: str, extra: str | None) -> None:
    plan = plan_synthesis(
        SynthesisRequest(task=task, answers=ANSWERS, seed=1, extra_instructions=extra), _deps()
    )
    assert not plan.scrubbed
    assert plan.replacements == 0
    assert any("Claude" in a.text for a in plan.answers)


def test_default_chair_is_the_highest_output_price() -> None:
    assert default_chair(ANSWERS, _deps()) == ModelChoice(
        provider="openai", model_id="openai-model"
    )


def test_economy_tier_uses_fast_aliases_with_fallback() -> None:
    answers = (*ANSWERS[:2], StepOneAnswer(provider="xai", model="xai-model", text="Heapsort."))
    plan = plan_synthesis(SynthesisRequest(task=TASK, answers=answers, seed=3), _deps())
    by_provider = {m.provider: m.model_id for m in plan.reviewers.values()}
    assert by_provider == {
        "anthropic": "anthropic-fast",
        "openai": "openai-fast",
        "xai": "xai-model",  # fast alias unset: same model
    }
    assert plan.chair == ModelChoice(provider="openai", model_id="openai-model")
    assert plan.judge == ModelChoice(provider="openai", model_id="openai-fast")


def test_standard_tier_and_explicit_chair() -> None:
    chair = ModelChoice(provider="xai", model_id="xai-model")
    plan = plan_synthesis(_req(review_tier="standard", chair=chair), _deps())
    assert {(m.provider, m.model_id) for m in plan.reviewers.values()} == {
        (a.provider, a.model) for a in ANSWERS
    }
    assert plan.chair == chair
    assert plan.judge == chair  # xai has no fast alias


@pytest.mark.parametrize(
    ("request_kwargs", "error"),
    [
        ({"task": " ", "answers": ANSWERS}, SelectionError),
        ({"task": TASK, "answers": ANSWERS[:1]}, SelectionError),
        (
            {
                "task": TASK,
                "answers": (*ANSWERS[:1], StepOneAnswer(provider="x", model="m", text="t")),
            },
            SelectionError,
        ),
        (
            {
                "task": TASK,
                "answers": ANSWERS,
                "chair": ModelChoice(provider="xai", model_id="xai-unpriced"),
            },
            PricingError,
        ),
    ],
)
def test_invalid_requests_are_refused(
    request_kwargs: dict[str, Any], error: type[Exception]
) -> None:
    with pytest.raises(error):
        plan_synthesis(SynthesisRequest(**request_kwargs), _deps())


# --------------------------------------------------------------------------- estimates


def test_estimate_has_a_breakdown_per_stage(stages: FakeStages) -> None:
    est = estimate_synthesis(_req(), _deps())
    assert [s.stage for s in est.stages] == ["agreement", "review", "chair"]
    assert [sum(line.calls for line in s.estimate.lines) for s in est.stages] == [1, 3, 1]
    assert est.total_high == Decimal("0.05000")
    assert est.total_high == sum((s.estimate.total_high for s in est.stages), Decimal(0))
    assert not est.budget.refused
    assert est.chair == ModelChoice(provider="openai", model_id="openai-model")
    assert est.seed == 42


def test_estimate_uses_unique_fingerprints(
    monkeypatch: pytest.MonkeyPatch, stages: FakeStages
) -> None:
    deps = _deps()
    seen: list[str] = []
    original = deps.estimator.estimate

    def spy(plan: Any, resolved: Any) -> Any:
        seen.extend(p.prefix_fingerprint for p in plan)
        return original(plan, resolved)

    monkeypatch.setattr(deps.estimator, "estimate", spy)
    estimate_synthesis(_req(), deps)
    assert len(seen) == 10  # 5 calls per stage, then all together
    assert len(set(seen)) == len(seen)  # never shared: no cache discount


def test_estimate_resynthesis_is_one_chair_call(stages: FakeStages) -> None:
    deps = _deps()
    req = ResynthesisRequest(
        task=TASK, answers=ANSWERS, seed=42, previous=make_synthesis(["A", "B"])
    )
    est = estimate_resynthesis(req, deps)
    assert [line.stage for line in est.lines] == ["chair"]
    assert stages.seen["estimate_chair"][-1] == {"with_reviews": False, "resynthesis": True}


# --------------------------------------------------------------------------- steps


def test_stateless_flow_end_to_end(stages: FakeStages) -> None:
    ledger = FakeLedger()
    deps = _deps(ledger=ledger)
    first = asyncio.run(run_agreement_step(_req(extra_instructions="Focus on speed."), deps))
    assert first.agreement.ratio == 0.5
    assert not first.suggest_skip
    assert first.threshold == AGREEMENT_SKIP_THRESHOLD
    assert [s.stage for s in first.stage_costs] == ["agreement"]
    assert first.total_cost == Decimal("0.01")
    assert first.review_estimate.total_high > 0

    # The client posts everything back as JSON.
    finish = FinishRequest.model_validate(
        {
            **_req(extra_instructions="Focus on speed.").model_dump(mode="json"),
            "seed": first.seed,
            "run_id": first.run_id,
            "agreement": first.agreement.model_dump(mode="json"),
        }
    )
    second = asyncio.run(run_finish_step(finish, deps))
    assert second.run_id == first.run_id
    assert second.labels == first.labels
    assert second.reviews is not None
    assert not second.review_skipped
    assert [(s.stage, s.calls) for s in second.stage_costs] == [("review", 3), ("chair", 1)]
    assert second.total_cost == Decimal("0.04")
    assert second.chair.synthesis.final_text == "Final answer."
    assert stages.seen["run_reviews"][0]["focus"] == first.agreement
    assert stages.seen["run_reviews"][0]["extra_instructions"] == "Focus on speed."
    assert stages.seen["run_reviews"][0]["seed"] == 42
    assert stages.seen["run_chair"][0]["reviews"] == second.reviews

    # Every call was budget-checked, recorded under the same run id, and released.
    assert len(ledger.entries) == 5
    assert {e.run_id for e in ledger.entries} == {first.run_id}
    assert [e.stage for e in ledger.entries] == ["agreement", "review", "review", "review", "chair"]
    assert len(ledger.checks) == 5
    assert ledger.reservations == {}


def test_judge_sees_scrubbed_labelled_answers(stages: FakeStages) -> None:
    asyncio.run(run_agreement_step(_req(), _deps()))
    answers = stages.seen["run_agreement"][0]["answers"]
    assert all(a.label.startswith("Response ") for a in answers)
    assert not any("Claude" in a.text for a in answers)


def test_high_agreement_suggests_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeStages(ratio=0.85).install(monkeypatch)
    result = asyncio.run(run_agreement_step(_req(), _deps()))
    assert result.suggest_skip


def test_skip_review_is_honoured(stages: FakeStages) -> None:
    ledger = FakeLedger()
    finish = FinishRequest(task=TASK, answers=ANSWERS, seed=42, skip_review=True)
    result = asyncio.run(run_finish_step(finish, _deps(ledger=ledger)))
    assert result.review_skipped
    assert result.reviews is None
    assert "run_reviews" not in stages.seen
    assert stages.seen["run_chair"][0]["reviews"] is None
    assert [e.stage for e in ledger.entries] == ["chair"]


def test_rulings_are_passed_to_the_chair(stages: FakeStages) -> None:
    deps = _deps()
    labels = [info.label for info in plan_synthesis(_req(), deps).labels]
    previous = make_synthesis(labels)
    rulings = (
        Ruling(kind="dispute", target="d1", decision=labels[1]),
        Ruling(kind="unique", target="0", decision="include"),
    )
    req = ResynthesisRequest(
        task=TASK, answers=ANSWERS, seed=42, previous=previous, rulings=rulings
    )
    result = asyncio.run(run_resynthesis(req, deps))
    call = stages.seen["run_chair"][0]
    assert call["previous"] == previous
    assert call["rulings"] == rulings
    assert [(s.stage, s.calls) for s in result.stage_costs] == [("chair", 1)]


@pytest.mark.parametrize(
    "ruling",
    [
        Ruling(kind="dispute", target="d9", decision="Response A"),
        Ruling(kind="dispute", target="d1", decision="Response Z"),
        Ruling(kind="unique", target="5", decision="include"),
        Ruling(kind="unique", target="0", decision="exclude"),
    ],
)
def test_invalid_rulings_are_refused(stages: FakeStages, ruling: Ruling) -> None:
    req = ResynthesisRequest(
        task=TASK,
        answers=ANSWERS,
        seed=42,
        previous=make_synthesis(["Response A", "Response B"]),
        rulings=(ruling,),
    )
    with pytest.raises(SelectionError):
        asyncio.run(run_resynthesis(req, _deps()))
    assert "run_chair" not in stages.seen


def test_budget_refusal_before_any_call(stages: FakeStages) -> None:
    ledger = FakeLedger()
    deps = _deps(ledger=ledger, budget=BudgetConfig(run_limit=Decimal("0.015")))
    with pytest.raises(BudgetRefusedError) as info:
        asyncio.run(run_agreement_step(_req(), deps))  # agreement + chair = 0.02
    assert info.value.status.refused
    assert ledger.entries == []
    assert "run_agreement" not in stages.seen


def test_budget_refusal_counts_daily_remaining(stages: FakeStages) -> None:
    ledger = FakeLedger(spent_today=Decimal("4.995"))
    with pytest.raises(BudgetRefusedError):
        asyncio.run(
            run_finish_step(FinishRequest(task=TASK, answers=ANSWERS, seed=1), _deps(ledger=ledger))
        )
    assert ledger.entries == []


def test_provider_failure_becomes_a_stage_error(stages: FakeStages) -> None:
    deps = _deps()
    providers = dict(deps.providers)
    providers[("openai", Backend.API)] = raising_provider("openai", ProviderError("vendor down"))
    broken = CompareDeps(**{**deps.__dict__, "providers": providers})
    with pytest.raises(SynthesisStageError) as info:
        asyncio.run(run_agreement_step(_req(), broken))
    assert info.value.stage == "agreement"
    assert info.value.code == "provider_error"
    assert "vendor down" in str(info.value)


def test_missing_key_is_a_stage_error(monkeypatch: pytest.MonkeyPatch, stages: FakeStages) -> None:
    deps = _deps(key_envs={"openai": "OPENAI_API_KEY"})
    with pytest.raises(SynthesisStageError) as info:
        asyncio.run(run_agreement_step(_req(), deps))
    assert "OPENAI_API_KEY is not set" in str(info.value)
