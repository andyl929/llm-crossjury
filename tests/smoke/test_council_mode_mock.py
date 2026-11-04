"""Smoke tests: empty Council-mode flows end to end with mock providers.

"Empty" means members return schema-valid but empty content and the user makes
empty decisions. The tests cover sequencing, structured-output plumbing, the
agreement summary at checkpoint A, pause/resume at both checkpoints, the
user-chosen skip-review path, and completion. No network, no real providers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal

import pytest
from pydantic import BaseModel

from llm_crossjury.core.cost.estimate import CallPlan
from llm_crossjury.core.modes import Mode
from llm_crossjury.core.orchestrator import (
    AdjudicationDecision,
    AgreementSummary,
    CheckpointKind,
    CheckpointStage,
    Completed,
    Paused,
    Pipeline,
    PruneDecision,
    RunContext,
    StageName,
)
from llm_crossjury.core.providers.base import (
    CompletionRequest,
    Message,
    Prompt,
    ReasoningEffort,
    Role,
)
from llm_crossjury.core.providers.mock import MockProvider
from llm_crossjury.core.schemas import (
    AgreementJudgment,
    PeerReview,
    Stage1Answer,
    Synthesis,
    strict_json_schema,
)

EMPTY_ANSWER = Stage1Answer(answer="", claims=[], assumptions=[], caveats=[])
EMPTY_AGREEMENT = AgreementJudgment(groups=[])
EMPTY_REVIEW = PeerReview(reviews=[], ranking_rationale="", ranking=[])
EMPTY_SYNTHESIS = Synthesis(consensus=[], disputed=[], unique=[], final_text=None)
LABELS = ("Response A", "Response B", "Response C", "Response D")


def _request(schema: type[BaseModel], effort: ReasoningEffort) -> CompletionRequest:
    return CompletionRequest(
        model="mock-model",
        prompt=Prompt(
            stable_prefix=(Message(role=Role.SYSTEM, content="shared instructions"),),
            variable_suffix=(Message(role=Role.USER, content="per-call input"),),
        ),
        response_schema=strict_json_schema(schema),
        temperature=None,
        max_output_tokens=None,
        reasoning_effort=effort,
        native_params={},
        cache_prefix=True,
        timeout_s=None,
        tags={},
    )


class _AnswerStage:
    name = StageName.ANSWER.value

    def __init__(self, members: dict[str, MockProvider]) -> None:
        self.members = members

    def plan(self, ctx: RunContext) -> Sequence[CallPlan]:
        return []

    async def run(self, ctx: RunContext) -> dict[str, Stage1Answer]:
        results = await asyncio.gather(
            *(
                p.complete(_request(Stage1Answer, ReasoningEffort.DEFAULT))
                for p in self.members.values()
            )
        )
        out: dict[str, Stage1Answer] = {}
        for label, member_id, result in zip(LABELS, self.members, results, strict=True):
            ctx.labels[label] = member_id
            out[label] = Stage1Answer.model_validate_json(result.text)
        return out


class _JsonStage:
    """One call to ``provider``, parsed as ``schema``."""

    def __init__(
        self, name: str, provider: MockProvider, schema: type[BaseModel], effort: ReasoningEffort
    ) -> None:
        self.name = name
        self.provider = provider
        self.schema = schema
        self.effort = effort

    def plan(self, ctx: RunContext) -> Sequence[CallPlan]:
        return []

    async def run(self, ctx: RunContext) -> BaseModel:
        result = await self.provider.complete(_request(self.schema, self.effort))
        return self.schema.model_validate_json(result.text)


def _agreement_summary(ctx: RunContext) -> AgreementSummary:
    return AgreementSummary(
        ratio=None,
        judge_model="mock-model",
        threshold=None,
        recommend_skip=False,
        review_cost_low=Decimal("0"),
        review_cost_high=Decimal("0"),
        currency="USD",
    )


def _disputes(ctx: RunContext) -> list[str]:
    draft = ctx.artifacts[StageName.DRAFT_SYNTHESIS.value]
    assert isinstance(draft, Synthesis)
    return [d.id for d in draft.disputed]


def _build() -> tuple[Pipeline, dict[str, MockProvider], MockProvider, MockProvider]:
    members = {
        m: MockProvider(m, lambda _r: EMPTY_ANSWER.model_dump_json())
        for m in ("claude", "openai", "gemini", "grok")
    }
    judge = MockProvider("judge", lambda _r: EMPTY_AGREEMENT.model_dump_json())
    reviewer = MockProvider("reviewer", lambda _r: EMPTY_REVIEW.model_dump_json())
    chair = MockProvider("chair", lambda _r: EMPTY_SYNTHESIS.model_dump_json())
    low = ReasoningEffort.LOW
    default = ReasoningEffort.DEFAULT
    pipeline = Pipeline(
        [
            _AnswerStage(members),
            _JsonStage(StageName.AGREEMENT.value, judge, AgreementJudgment, low),
            CheckpointStage(
                StageName.PRUNE.value,
                CheckpointKind.PRUNE_ANSWERS,
                lambda ctx: sorted(ctx.artifacts[StageName.ANSWER.value]),
                agreement=_agreement_summary,
                skip_review_to=StageName.DRAFT_SYNTHESIS.value,
            ),
            _JsonStage(StageName.REVIEW.value, reviewer, PeerReview, low),
            _JsonStage(StageName.DRAFT_SYNTHESIS.value, chair, Synthesis, default),
            CheckpointStage(
                StageName.ADJUDICATE.value, CheckpointKind.ADJUDICATE_DISPUTES, _disputes
            ),
            _JsonStage(StageName.FINAL_SYNTHESIS.value, chair, Synthesis, default),
        ]
    )
    return pipeline, members, reviewer, chair


async def _drive(pipeline: Pipeline, ctx: RunContext, *, skip_review: bool) -> None:
    outcome = await pipeline.run(ctx)
    assert isinstance(outcome, Paused)
    assert outcome.request.kind is CheckpointKind.PRUNE_ANSWERS
    assert outcome.request.candidates == LABELS
    assert outcome.request.agreement is not None
    assert outcome.request.agreement.recommend_skip is False

    outcome = await pipeline.resume(ctx, PruneDecision(drop=(), skip_review=skip_review))
    assert isinstance(outcome, Paused)
    assert outcome.request.kind is CheckpointKind.ADJUDICATE_DISPUTES
    assert outcome.request.candidates == ()

    outcome = await pipeline.resume(ctx, AdjudicationDecision(rulings=()))
    assert isinstance(outcome, Completed)


@pytest.mark.smoke
def test_empty_council_flow_with_checkpoints() -> None:
    pipeline, members, reviewer, chair = _build()
    ctx = RunContext(run_id="smoke-1", mode=Mode.COUNCIL, task="(empty)")

    asyncio.run(_drive(pipeline, ctx, skip_review=False))

    assert set(ctx.labels.values()) == set(members)
    assert ctx.artifacts[StageName.FINAL_SYNTHESIS.value] == EMPTY_SYNTHESIS
    assert all(len(p.requests) == 1 for p in members.values())
    assert members["claude"].requests[0].response_schema == strict_json_schema(Stage1Answer)
    assert reviewer.requests[0].reasoning_effort is ReasoningEffort.LOW
    assert len(chair.requests) == 2


@pytest.mark.smoke
def test_user_can_skip_review_at_checkpoint_a() -> None:
    pipeline, _members, reviewer, chair = _build()
    ctx = RunContext(run_id="smoke-2", mode=Mode.COUNCIL, task="(empty)")

    asyncio.run(_drive(pipeline, ctx, skip_review=True))

    assert reviewer.requests == []
    assert StageName.REVIEW.value not in ctx.artifacts
    assert len(chair.requests) == 2
