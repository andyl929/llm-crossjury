"""Council mode: answer -> prune (A) -> review -> draft synthesis -> adjudicate (B) -> final."""

from __future__ import annotations

from collections.abc import Sequence

from llm_crossjury.core.config import CrossjuryConfig
from llm_crossjury.core.cost.estimate import CallPlan
from llm_crossjury.core.orchestrator.context import RunContext
from llm_crossjury.core.orchestrator.pipeline import Pipeline
from llm_crossjury.core.schemas.review import PeerReview
from llm_crossjury.core.schemas.stage1 import Stage1Answer
from llm_crossjury.core.schemas.synthesis import Synthesis


class AnswerStage:
    """Stage 1: every member answers independently, in parallel.

    Artifact: ``dict[label, Stage1Answer]`` keyed by freshly shuffled anonymous labels.
    """

    name = "answer"

    def plan(self, ctx: RunContext) -> Sequence[CallPlan]:
        raise NotImplementedError

    async def run(self, ctx: RunContext) -> dict[str, Stage1Answer]:
        raise NotImplementedError


class ReviewStage:
    """Stage 2: anonymous peer review under the configured topology.

    Artifact: ``list[PeerReview]``.
    """

    name = "review"

    def plan(self, ctx: RunContext) -> Sequence[CallPlan]:
        raise NotImplementedError

    async def run(self, ctx: RunContext) -> list[PeerReview]:
        raise NotImplementedError


class DraftSynthesisStage:
    """Stage 3: chair drafts consensus / disputed / unique; ``final_text`` is ``None``."""

    name = "draft_synthesis"

    def plan(self, ctx: RunContext) -> Sequence[CallPlan]:
        raise NotImplementedError

    async def run(self, ctx: RunContext) -> Synthesis:
        raise NotImplementedError


class FinalSynthesisStage:
    """Stage 4: chair writes ``final_text`` honoring the user's rulings."""

    name = "final_synthesis"

    def plan(self, ctx: RunContext) -> Sequence[CallPlan]:
        raise NotImplementedError

    async def run(self, ctx: RunContext) -> Synthesis:
        raise NotImplementedError


def build_council_pipeline(config: CrossjuryConfig) -> Pipeline:
    """Assemble the Council pipeline from configuration."""
    raise NotImplementedError
