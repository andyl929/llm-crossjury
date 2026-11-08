"""Fake synthesis stages for service and web tests.

They replace the stage functions (``agreement.run_agreement`` and so on) through
monkeypatching, so these tests never depend on the real stage implementations. Each
fake run makes its calls through the injected ``CallFn``, so the service's budget and
ledger flow is exercised.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from llm_crossjury.core.providers.base import Message, Prompt, Role
from llm_crossjury.core.schemas.agreement import AgreementJudgment, ClaimGroup, ClaimRef
from llm_crossjury.core.schemas.synthesis import (
    DisputedPoint,
    Position,
    Synthesis,
    UniqueInsight,
)
from llm_crossjury.core.synthesis import agreement, chair, review
from llm_crossjury.core.synthesis.agreement import AgreementOutcome
from llm_crossjury.core.synthesis.chair import AdditionalList, ChairOutcome, Ruling
from llm_crossjury.core.synthesis.review import RankedLabel, ReviewOutcome
from llm_crossjury.core.synthesis.types import (
    AnonAnswer,
    CallFn,
    CallSpec,
    ModelChoice,
    PlannedCall,
)

OUTPUT_HIGH = 1000
"""High output tokens per fake planned call (fake estimator: 0.01 per call)."""


def _spec(stage: str, model: ModelChoice, tag: str, content: str) -> CallSpec:
    return CallSpec(
        stage=stage,
        provider=model.provider,
        model_id=model.model_id,
        prompt=Prompt(
            stable_prefix=(Message(role=Role.SYSTEM, content="instructions"),),
            variable_suffix=(Message(role=Role.USER, content=content),),
        ),
        response_schema=None,
        max_output_tokens=OUTPUT_HIGH,
        tag=tag,
    )


def _planned(stage: str, model: ModelChoice) -> PlannedCall:
    return PlannedCall(
        stage=stage,
        provider=model.provider,
        model_id=model.model_id,
        input_tokens=100,
        output_tokens_low=OUTPUT_HIGH // 10,
        output_tokens_high=OUTPUT_HIGH,
    )


def make_synthesis(labels: Sequence[str], final_text: str = "Final answer.") -> Synthesis:
    """A chair output with one dispute between the first two labels and one insight."""
    return Synthesis(
        consensus=[],
        disputed=[
            DisputedPoint(
                id="d1",
                question="Which is right?",
                positions=[
                    Position(label=labels[0], stance="Yes", argument="Because."),
                    Position(label=labels[1], stance="No", argument="Because not."),
                ],
                chair_assessment="Yes seems better.",
            )
        ],
        unique=[UniqueInsight(label=labels[0], insight="A unique point.")],
        final_text=final_text,
    )


def make_agreement(labels: Sequence[str], ratio: float | None) -> AgreementOutcome:
    group = ClaimGroup(
        statement="Shared point.",
        supporting=[ClaimRef(label=label, claim_id="s1") for label in labels],
        contradicting=[],
    )
    return AgreementOutcome(
        segments=(),
        judgment=AgreementJudgment(groups=[group]),
        consensus=(group,),
        disputed=(),
        unique=(),
        ratio=ratio,
    )


@dataclass
class FakeStages:
    """Installs fake stage functions and records what they were called with."""

    ratio: float | None = 0.5
    final_text: str = "Final answer."
    seen: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def _see(self, name: str, **kwargs: Any) -> None:
        self.seen.setdefault(name, []).append(kwargs)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeStages:
        monkeypatch.setattr(agreement, "estimate_agreement", self.estimate_agreement)
        monkeypatch.setattr(agreement, "run_agreement", self.run_agreement)
        monkeypatch.setattr(review, "estimate_reviews", self.estimate_reviews)
        monkeypatch.setattr(review, "run_reviews", self.run_reviews)
        monkeypatch.setattr(chair, "estimate_chair", self.estimate_chair)
        monkeypatch.setattr(chair, "run_chair", self.run_chair)
        return self

    # agreement
    def estimate_agreement(
        self, answers: Sequence[AnonAnswer], judge: ModelChoice, *, task: str
    ) -> list[PlannedCall]:
        return [_planned(agreement.STAGE, judge)]

    async def run_agreement(
        self, answers: Sequence[AnonAnswer], judge: ModelChoice, *, task: str, call: CallFn
    ) -> AgreementOutcome:
        self._see("run_agreement", answers=tuple(answers), judge=judge, task=task)
        await call(_spec(agreement.STAGE, judge, "judge", answers[0].text))
        return make_agreement([a.label for a in answers], self.ratio)

    # review
    def estimate_reviews(
        self,
        answers: Sequence[AnonAnswer],
        reviewers: Mapping[str, ModelChoice],
        *,
        task: str,
        focus: AgreementOutcome | None,
        extra_instructions: str | None,
    ) -> list[PlannedCall]:
        return [_planned(review.STAGE, model) for model in reviewers.values()]

    async def run_reviews(
        self,
        answers: Sequence[AnonAnswer],
        reviewers: Mapping[str, ModelChoice],
        *,
        task: str,
        focus: AgreementOutcome | None,
        extra_instructions: str | None,
        call: CallFn,
        seed: int,
    ) -> ReviewOutcome:
        self._see(
            "run_reviews",
            reviewers=dict(reviewers),
            focus=focus,
            extra_instructions=extra_instructions,
            seed=seed,
        )
        for label, model in reviewers.items():
            await call(_spec(review.STAGE, model, label, "review"))
        return ReviewOutcome(
            reviews={},
            ranking=tuple(
                RankedLabel(label=a.label, score=1.0 - i / 10, informative_reviews=1)
                for i, a in enumerate(answers)
            ),
            failed_reviewers=(),
        )

    # chair
    def estimate_chair(
        self,
        answers: Sequence[AnonAnswer],
        chair_model: ModelChoice,
        *,
        task: str,
        with_reviews: bool,
        extra_instructions: str | None,
        resynthesis: bool = False,
    ) -> list[PlannedCall]:
        self._see("estimate_chair", with_reviews=with_reviews, resynthesis=resynthesis)
        return [_planned(chair.STAGE, chair_model)]

    async def run_chair(
        self,
        answers: Sequence[AnonAnswer],
        chair_model: ModelChoice,
        *,
        task: str,
        agreement: AgreementOutcome | None,
        reviews: ReviewOutcome | None,
        extra_instructions: str | None,
        call: CallFn,
        previous: Synthesis | None = None,
        rulings: Sequence[Ruling] = (),
    ) -> ChairOutcome:
        self._see(
            "run_chair",
            chair=chair_model,
            agreement=agreement,
            reviews=reviews,
            extra_instructions=extra_instructions,
            previous=previous,
            rulings=tuple(rulings),
        )
        await call(_spec(chair.STAGE, chair_model, "chair", "chair"))
        synthesis = make_synthesis([a.label for a in answers], self.final_text)
        return ChairOutcome(
            synthesis=synthesis,
            additional=AdditionalList(
                disputed=tuple(synthesis.disputed),
                unique=tuple(synthesis.unique),
                possibly_wrong=(),
            ),
        )
