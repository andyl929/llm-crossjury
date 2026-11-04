"""Chair synthesis.

CONTRACT FILE. Do not modify; report problems in docs/contract-issues.md.

The chair produces a Synthesis twice:

1. **Draft** (before checkpoint B): final_text is None; disputed lists
   the points the user is asked to adjudicate.
2. **Final** (after checkpoint B): final_text is set and reflects the rulings.

The chair only ever sees anonymous labels.
"""

from __future__ import annotations

from council.core.schemas._base import StrictModel
from council.core.schemas.edits import Edit


class ConsensusPoint(StrictModel):
    """A point most or all members agree on."""

    statement: str
    supporting: list[str]
    """Anonymous labels of the members that support it."""


class Position(StrictModel):
    """One side of a dispute."""

    label: str
    """Anonymous label of the member holding this position."""

    stance: str
    """The position in one or two sentences."""

    argument: str
    """The strongest reasoning offered for it, from the member's answer or reviews."""


class DisputedPoint(StrictModel):
    """A question on which members materially disagree."""

    id: str
    """Unique within one Synthesis, e.g. "d1"."""

    question: str
    """The disagreement phrased as a neutral question."""

    positions: list[Position]
    """At least two, one per distinct stance (labels may repeat across stances only
    if a member was internally inconsistent)."""

    chair_assessment: str | None
    """The chair's own view, kept separate so the user can ignore it."""


class UniqueInsight(StrictModel):
    """Something only one member raised that the chair judges worth keeping."""

    label: str
    insight: str


class Synthesis(StrictModel):
    """Chair output for Council mode (and the narrative part of Document Review)."""

    consensus: list[ConsensusPoint]
    disputed: list[DisputedPoint]
    unique: list[UniqueInsight]
    final_text: str | None
    """None in the draft; the final answer in Markdown after adjudication."""


class DocumentSynthesis(StrictModel):
    """Chair output for Document Review mode.

    edits is the merged, de-duplicated edit list. In the draft, edits whose
    acceptance depends on a dispute are omitted from edits and represented in
    synthesis.disputed instead.
    """

    synthesis: Synthesis
    edits: list[Edit]
