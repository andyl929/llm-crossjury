"""Stage abstractions."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from llm_crossjury.core.orchestrator.checkpoint import AgreementSummary, CheckpointKind

if TYPE_CHECKING:
    from llm_crossjury.core.cost.estimate import CallPlan
    from llm_crossjury.core.orchestrator.context import RunContext


class StageName(StrEnum):
    """Canonical stage names used as artifact keys and ledger tags."""

    ANSWER = "answer"
    AGREEMENT = "agreement"
    PRUNE = "prune"
    REVIEW = "review"
    DRAFT_SYNTHESIS = "draft_synthesis"
    ADJUDICATE = "adjudicate"
    FINAL_SYNTHESIS = "final_synthesis"


class Stage(Protocol):
    """A unit of model work.

    ``run`` stores nothing itself; the pipeline puts the return value into
    ``ctx.artifacts[name]``.
    """

    name: str

    def plan(self, ctx: RunContext) -> Sequence[CallPlan]:
        """Calls this stage would make, for the cost estimator. No side effects."""
        ...

    async def run(self, ctx: RunContext) -> object:
        """Execute the stage and return its artifact."""
        ...


@dataclass(frozen=True)
class CheckpointStage:
    """A pipeline position where the run pauses for a user decision.

    Attributes:
        name: Stage name (artifact key for the decision).
        kind: Which checkpoint this is.
        candidates: Computes the items the user decides on from the context.
        agreement: Checkpoint A only: builds the agreement summary from the context.
        skip_review_to: Checkpoint A only: stage to jump to when the user sets
            ``PruneDecision.skip_review``.
    """

    name: str
    kind: CheckpointKind
    candidates: Callable[[RunContext], Sequence[str]]
    agreement: Callable[[RunContext], AgreementSummary | None] | None = None
    skip_review_to: str | None = None
