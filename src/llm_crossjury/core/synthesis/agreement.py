"""Stage 1 of the synthesis upgrade: segment answers, group segments by meaning.

Step-1 answers are free text, so there are no member-extracted claims. Instead each
answer is split locally into numbered segments (paragraphs and list items, see
:func:`segment_answer`) and ONE cheap judge call groups all segments of all answers
into an ``AgreementJudgment`` (``ClaimRef.claim_id`` = ``Segment.segment_id``).

Classification is done in Python, never by the judge:

* consensus: supported by >= 2 distinct labels, contradicted by none;
* disputed: at least one contradicting ref;
* unique: supported by exactly 1 label, contradicted by none.

``ratio`` is ``llm_crossjury.core.orchestrator.agreement.agreement_ratio`` over all
answer labels. Trivial segments (headings, very short fragments) may be dropped
before judging to save tokens; dropped segments simply do not appear in any group.

INTERFACE FIXED by the integrator: implementations may add private helpers and
optional keyword arguments, but must not change these signatures or field names.
"""

from __future__ import annotations

from collections.abc import Sequence

from llm_crossjury.core.schemas.agreement import AgreementJudgment, ClaimGroup
from llm_crossjury.core.synthesis.types import (
    AnonAnswer,
    CallFn,
    CallSpec,
    ModelChoice,
    PlannedCall,
    Segment,
    _Frozen,
)

STAGE = "agreement"
PROMPT_ID = "synthesis/agreement.v1"


class AgreementOutcome(_Frozen):
    """Result of the agreement stage (JSON-serializable, sent to the UI)."""

    segments: tuple[Segment, ...]
    judgment: AgreementJudgment
    consensus: tuple[ClaimGroup, ...]
    disputed: tuple[ClaimGroup, ...]
    unique: tuple[ClaimGroup, ...]
    ratio: float | None


def segment_answer(answer: AnonAnswer) -> tuple[Segment, ...]:
    """Split one answer into numbered segments (``s1``, ``s2``, ...)."""
    raise NotImplementedError


def build_agreement_spec(
    answers: Sequence[AnonAnswer], judge: ModelChoice, *, task: str
) -> CallSpec:
    """Build the single judge call over all segments of all answers."""
    raise NotImplementedError


def estimate_agreement(
    answers: Sequence[AnonAnswer], judge: ModelChoice, *, task: str
) -> list[PlannedCall]:
    """Planned calls for the estimate (exactly one judge call)."""
    raise NotImplementedError


def classify(judgment: AgreementJudgment, labels: Sequence[str]) -> AgreementOutcome:
    """Split groups into consensus / disputed / unique and compute the ratio.

    ``segments`` is left empty here; :func:`run_agreement` fills it.
    """
    raise NotImplementedError


async def run_agreement(
    answers: Sequence[AnonAnswer], judge: ModelChoice, *, task: str, call: CallFn
) -> AgreementOutcome:
    """Segment, judge (via ``structured_call``), validate refs, classify."""
    raise NotImplementedError
