"""Stage 2 of the synthesis upgrade: one round of anonymous peer review.

Each answer's own provider supplies one reviewer model (``reviewers`` maps the label
of the reviewer's *own* answer to the model that reviews; in the "economy" tier the
service passes that provider's ``fast`` model). Assignments come from
``llm_crossjury.core.orchestrator.topology.assign_reviews``: a reviewer never reviews
its own answer; ``FULL`` for <= 3 answers, ``RING`` with ``k = 2`` for >= 4.

Prompt caching: every reviewer's stable prefix holds the same instructions and ALL
anonymized answers (byte-identical across reviewers); the variable suffix names the
assigned targets, the focus points (disputed and unique statements from the agreement
stage, if any) and the user's extra instructions.

Rankings are aggregated with ``llm_crossjury.core.orchestrator.ranking.borda_aggregate``.
A review whose ranking is not a permutation of its targets is retried once via
``structured_call`` semantics, then dropped (its issues are still used if valid).

INTERFACE FIXED by the integrator: implementations may add private helpers and
optional keyword arguments, but must not change these signatures or field names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from llm_crossjury.core.schemas.review import PeerReview
from llm_crossjury.core.synthesis.agreement import AgreementOutcome
from llm_crossjury.core.synthesis.types import (
    AnonAnswer,
    CallFn,
    ModelChoice,
    PlannedCall,
    _Frozen,
)

STAGE = "review"
PROMPT_ID = "synthesis/review.v1"


class RankedLabel(_Frozen):
    """Aggregated rank of one answer."""

    label: str
    score: float | None
    """Mean normalized Borda points (0..1); ``None`` if no informative review."""
    informative_reviews: int


class ReviewOutcome(_Frozen):
    """Result of the review stage (JSON-serializable, sent to the UI)."""

    reviews: dict[str, PeerReview]
    """Reviewer (label of its own answer) -> its review. Failed reviewers are absent."""
    ranking: tuple[RankedLabel, ...]
    """Best first; empty in "issues only" mode (every reviewer had one target)."""
    failed_reviewers: tuple[str, ...]
    """Reviewers whose call failed or whose output stayed invalid."""


def estimate_reviews(
    answers: Sequence[AnonAnswer],
    reviewers: Mapping[str, ModelChoice],
    *,
    task: str,
    focus: AgreementOutcome | None,
    extra_instructions: str | None,
) -> list[PlannedCall]:
    """Planned calls for the estimate (one per reviewer with at least one target)."""
    raise NotImplementedError


async def run_reviews(
    answers: Sequence[AnonAnswer],
    reviewers: Mapping[str, ModelChoice],
    *,
    task: str,
    focus: AgreementOutcome | None,
    extra_instructions: str | None,
    call: CallFn,
    seed: int,
) -> ReviewOutcome:
    """Assign, run reviewers concurrently, validate, aggregate rankings.

    One reviewer failing must not fail the stage.
    """
    raise NotImplementedError
