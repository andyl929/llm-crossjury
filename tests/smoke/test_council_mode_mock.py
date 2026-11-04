"""Smoke test: one empty Council flow end to end with mock providers.

"Empty" means members return schema-valid but empty content and the user makes
empty decisions at both checkpoints. It exercises sequencing, structured-output
plumbing, pause at checkpoint A, resume, pause at checkpoint B, resume, and
completion. No network, no real providers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from llm_crossjury.core.cost.estimate import CallPlan
from llm_crossjury.core.modes import Mode
from llm_crossjury.core.orchestrator import (
    AdjudicationDecision,
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
from llm_crossjury.core.schemas import PeerReview, Stage1Answer, Synthesis, strict_json_schema

EMPTY_ANSWER = Stage1Answer(answer="", claims=[], assumptions=[], caveats=[])
EMPTY_REVIEW = PeerReview(reviews=[], ranking=[], ranking_rationale="")
EMPTY_SYNTHESIS = Synthesis(consensus=[], disputed=[], unique=[], final_text=None)


def _request(model: str, schema: type[Stage1Answer | PeerReview | Synthesis]) -> CompletionRequest:
    return CompletionRequest(
        model=model,
        prompt=Prompt(
            stable_prefix=(Message(role=Role.SYSTEM, content="shared instructions"),),
            variable_suffix=(Message(role=Role.USER, content="per-call input"),),
        ),
        response_schema=strict_json_schema(schema),
        temperature=None,
        max_output_tokens=None,
        reasoning_effort=ReasoningEffort.DEFAULT,
        cache_prefix=True,
        timeout_s=None,
        tags={},
    )


class _MockAnswerStage:
    name = StageName.ANSWER.value

    def __init__(self, members: dict[str, MockProvider]) -> None:
        self.members = members

    def plan(self, ctx: RunContext) -> Sequence[CallPlan]:
        return []

    async def run(self, ctx: RunContext) -> dict[str, Stage1Answer]:
        results = await asyncio.gather(
            *(p.complete(_request("mock-model", Stage1Answer)) for p in self.members.values())
        )
        out: dict[str, Stage1Answer] = {}
        for i, (member_id, result) in enumerate(zip(self.members, results, strict=True)):
            label = f"Response {chr(ord('A') + i)}"
            ctx.labels[label] = member_id
            out[label] = Stage1Answer.model_validate_json(result.text)
        return out


class _MockReviewStage:
    name = StageName.REVIEW.value

    def __init__(self, reviewer: MockProvider) -> None:
        self.reviewer = reviewer

    def plan(self, ctx: RunContext) -> Sequence[CallPlan]:
        return []

    async def run(self, ctx: RunContext) -> list[PeerReview]:
        result = await self.reviewer.complete(_request("mock-model", PeerReview))
        return [PeerReview.model_validate_json(result.text)]


class _MockSynthesisStage:
    def __init__(self, name: str, chair: MockProvider) -> None:
        self.name = name
        self.chair = chair

    def plan(self, ctx: RunContext) -> Sequence[CallPlan]:
        return []

    async def run(self, ctx: RunContext) -> Synthesis:
        result = await self.chair.complete(_request("mock-model", Synthesis))
        return Synthesis.model_validate_json(result.text)


def _member(name: str) -> MockProvider:
    return MockProvider(name, lambda _req: EMPTY_ANSWER.model_dump_json())


@pytest.mark.smoke
def test_empty_council_flow_with_checkpoints() -> None:
    members = {m: _member(m) for m in ("claude", "openai", "gemini", "grok")}
    reviewer = MockProvider("reviewer", lambda _req: EMPTY_REVIEW.model_dump_json())
    chair = MockProvider("chair", lambda _req: EMPTY_SYNTHESIS.model_dump_json())

    pipeline = Pipeline(
        [
            _MockAnswerStage(members),
            CheckpointStage(
                StageName.PRUNE.value,
                CheckpointKind.PRUNE_ANSWERS,
                lambda ctx: sorted(ctx.artifacts[StageName.ANSWER.value]),
            ),
            _MockReviewStage(reviewer),
            _MockSynthesisStage(StageName.DRAFT_SYNTHESIS.value, chair),
            CheckpointStage(
                StageName.ADJUDICATE.value,
                CheckpointKind.ADJUDICATE_DISPUTES,
                lambda ctx: [d.id for d in ctx.artifacts[StageName.DRAFT_SYNTHESIS.value].disputed],
            ),
            _MockSynthesisStage(StageName.FINAL_SYNTHESIS.value, chair),
        ]
    )
    ctx = RunContext(run_id="smoke-1", mode=Mode.COUNCIL, task="(empty)")

    async def scenario() -> None:
        outcome = await pipeline.run(ctx)
        assert isinstance(outcome, Paused)
        assert outcome.request.kind is CheckpointKind.PRUNE_ANSWERS
        assert outcome.request.candidates == (
            "Response A",
            "Response B",
            "Response C",
            "Response D",
        )

        outcome = await pipeline.resume(ctx, PruneDecision(drop=()))
        assert isinstance(outcome, Paused)
        assert outcome.request.kind is CheckpointKind.ADJUDICATE_DISPUTES
        assert outcome.request.candidates == ()

        outcome = await pipeline.resume(ctx, AdjudicationDecision(rulings=()))
        assert isinstance(outcome, Completed)

    asyncio.run(scenario())

    assert set(ctx.labels.values()) == set(members)
    assert ctx.artifacts[StageName.FINAL_SYNTHESIS.value] == EMPTY_SYNTHESIS
    assert all(len(p.requests) == 1 for p in members.values())
    sent = members["claude"].requests[0]
    assert sent.response_schema == strict_json_schema(Stage1Answer)
    assert len(chair.requests) == 2
