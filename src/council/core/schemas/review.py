"""Stage 2: anonymous peer review.

CONTRACT FILE. Do not modify; report problems in docs/contract-issues.md.

Evidence first, judgment last: within each object the fields are ordered so that a
model generating them in order must quote and diagnose before it scores and ranks.
"""

from __future__ import annotations

from council.core.schemas._base import Severity, StrictModel


class ReviewIssue(StrictModel):
    """One problem found in a reviewed response."""

    quote: str
    """Verbatim excerpt from the reviewed response that the issue is about."""

    claim_id: str | None
    """Id of the affected claim in the reviewed response, if any."""

    problem: str
    """What is wrong, stated concretely."""

    severity: Severity

    suggested_fix: str | None


class TargetReview(StrictModel):
    """A review of one anonymized response."""

    target: str
    """Anonymous label of the reviewed response, e.g. "Response B"."""

    issues: list[ReviewIssue]
    """Problems, each anchored in a verbatim quote. May be empty."""

    strengths: list[str]
    """What the response does well."""

    score_rationale: str
    """Why the score follows from the issues and strengths above."""

    score: int
    """Overall quality on a 1-10 scale (range validated after parsing)."""


class PeerReview(StrictModel):
    """Everything one reviewer says about the responses assigned to it."""

    reviews: list[TargetReview]
    """One entry per assigned target, in any order."""

    ranking: list[str]
    """Assigned target labels, best first. Must be a permutation of reviewed targets."""

    ranking_rationale: str
