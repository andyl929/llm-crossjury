"""Stage 3 of the synthesis upgrade: the chair writes the final answer.

One chair call returns a ``Synthesis`` with ``final_text`` SET (the user chose
one-click synthesis): disputes are decided provisionally by the chair and recorded in
``disputed`` with ``chair_assessment`` explaining the provisional decision, so the UI
can flag them as "decided by AI". ``unique`` lists single-answer insights that the
chair did NOT put into ``final_text``. ``consensus`` lists the agreed points.

Re-synthesis: the user may rule on disputes (pick a position label) and on unique
insights (include / exclude). :func:`run_chair` is then called again with
``previous`` and ``rulings``; it is one more chair call (the stable prefix, holding
instructions, answers and review material, is unchanged so it can hit the cache).

The additional list shown under the final answer is built in Python
(:func:`build_additional_list`):

* disputed: ``Synthesis.disputed``;
* unique (not included): ``Synthesis.unique``;
* possibly wrong: review issues with severity ``critical`` or ``major``, merged per
  (target, quote) with the number of reviewers that flagged them.

INTERFACE FIXED by the integrator: implementations may add private helpers and
optional keyword arguments, but must not change these signatures or field names.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from llm_crossjury.core.schemas._base import Severity
from llm_crossjury.core.schemas.synthesis import DisputedPoint, Synthesis, UniqueInsight
from llm_crossjury.core.synthesis.agreement import AgreementOutcome
from llm_crossjury.core.synthesis.review import ReviewOutcome
from llm_crossjury.core.synthesis.types import (
    AnonAnswer,
    CallFn,
    ModelChoice,
    PlannedCall,
    _Frozen,
)

STAGE = "chair"
PROMPT_ID = "synthesis/chair.v1"


class Ruling(_Frozen):
    """A user decision applied during re-synthesis."""

    kind: Literal["dispute", "unique"]
    target: str
    """``DisputedPoint.id`` for disputes; the index (as a string) into
    ``Synthesis.unique`` for unique insights."""
    decision: str
    """Disputes: the anonymous label whose position to adopt. Unique: ``"include"``."""


class FlaggedIssue(_Frozen):
    """A serious problem reviewers found in one answer."""

    target: str
    """Anonymous label of the answer."""
    quote: str
    problem: str
    severity: Severity
    flagged_by: int
    """Number of reviewers that reported an issue on this (target, quote)."""


class AdditionalList(_Frozen):
    """Material that did not make it into the final answer, for the user to inspect."""

    disputed: tuple[DisputedPoint, ...]
    unique: tuple[UniqueInsight, ...]
    possibly_wrong: tuple[FlaggedIssue, ...]


class ChairOutcome(_Frozen):
    """Result of the chair stage (JSON-serializable, sent to the UI)."""

    synthesis: Synthesis
    additional: AdditionalList


def estimate_chair(
    answers: Sequence[AnonAnswer],
    chair: ModelChoice,
    *,
    task: str,
    with_reviews: bool,
    extra_instructions: str | None,
    resynthesis: bool = False,
) -> list[PlannedCall]:
    """Planned calls for the estimate (exactly one chair call).

    Review material that does not exist yet is estimated from the answer lengths.
    """
    raise NotImplementedError


def build_additional_list(synthesis: Synthesis, reviews: ReviewOutcome | None) -> AdditionalList:
    """Assemble the additional list (see the module docstring)."""
    raise NotImplementedError


async def run_chair(
    answers: Sequence[AnonAnswer],
    chair: ModelChoice,
    *,
    task: str,
    agreement: AgreementOutcome | None,
    reviews: ReviewOutcome | None,
    extra_instructions: str | None,
    call: CallFn,
    previous: Synthesis | None = None,
    rulings: Sequence[Ruling] = (),
) -> ChairOutcome:
    """Run the chair call (via ``structured_call``) and build the additional list.

    Raises:
        StructuredOutputError: if the output stays invalid or ``final_text`` is null.
    """
    raise NotImplementedError
