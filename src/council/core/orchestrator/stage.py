"""Stage abstractions."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from council.core.orchestrator.checkpoint import CheckpointKind

if TYPE_CHECKING:
    from council.core.cost.estimate import CallPlan
    from council.core.orchestrator.context import RunContext


class StageName(StrEnum):
    """Canonical stage names used as artifact keys and ledger tags."""

    ANSWER = "answer"
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
    """

    name: str
    kind: CheckpointKind
    candidates: Callable[[RunContext], Sequence[str]]
