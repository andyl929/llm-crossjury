"""Stage 2: anonymous peer review.

CONTRACT FILE. Do not modify; report problems in docs/contract-issues.md.

There are no numeric scores. A review is an evidence-anchored issue list per target
plus a ranking; rankings are aggregated across reviewers with a Borda count
(llm_crossjury.core.orchestrator.ranking). Evidence first, judgment last: within
each object the fields are ordered so that a model generating them in order must
quote and diagnose before it ranks.
"""

from __future__ import annotations

from llm_crossjury.core.schemas._base import Severity, StrictModel


class ReviewIssue(StrictModel):
    """One problem found in a reviewed response."""

    quote: str
    """Verbatim excerpt from the reviewed response that the issue is about."""

    claim_id: str | None
    """Id of the affected claim (or edit, in Document Review) in the reviewed response."""

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


class PeerReview(StrictModel):
    """Everything one reviewer says about the responses assigned to it."""

    reviews: list[TargetReview]
    """One entry per assigned target, in any order."""

    ranking_rationale: str
    """Why the ranking below follows from the issues and strengths above."""

    ranking: list[str]
    """Assigned target labels, best first. Must be a permutation of reviewed targets."""
