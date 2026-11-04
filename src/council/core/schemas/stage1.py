"""Stage 1: an independent answer from one council member.

CONTRACT FILE. Do not modify; report problems in docs/contract-issues.md.
"""

from __future__ import annotations

from council.core.schemas._base import Confidence, StrictModel


class Claim(StrictModel):
    """An atomic, checkable statement extracted by the member from its own answer.

    Claims are what peer reviewers and the chair compare across members; they make
    agreement and disagreement detectable without diffing prose.
    """

    id: str
    """Member-local id, e.g. "c1". Unique within one Stage1Answer."""

    text: str
    """The claim as a single self-contained sentence."""

    confidence: Confidence

    support: str | None
    """Brief justification or source the member relies on; None if none."""


class Stage1Answer(StrictModel):
    """One member's independent answer to the task."""

    answer: str
    """The full answer in Markdown."""

    claims: list[Claim]
    """Key claims made in answer. Empty only for trivial tasks."""

    assumptions: list[str]
    """Assumptions the member made about an under-specified task."""

    caveats: list[str]
    """Known limitations of the answer."""
