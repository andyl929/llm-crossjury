"""Agreement stage of the synthesis upgrade: segmentation, judge call, classification."""

from __future__ import annotations

import pytest

from llm_crossjury.core.orchestrator.agreement import agreement_ratio
from llm_crossjury.core.schemas.agreement import AgreementJudgment, ClaimGroup, ClaimRef

A, B, C = "Response A", "Response B", "Response C"


def _ref(label: str, claim_id: str) -> ClaimRef:
    return ClaimRef(label=label, claim_id=claim_id)


def _group(
    statement: str,
    supporting: list[ClaimRef],
    contradicting: list[ClaimRef] | None = None,
) -> ClaimGroup:
    return ClaimGroup(statement=statement, supporting=supporting, contradicting=contradicting or [])


def test_ratio_none_for_no_groups() -> None:
    assert agreement_ratio(AgreementJudgment(groups=[]), [A, B]) is None


def test_ratio_counts_groups_backed_by_every_label_and_uncontradicted() -> None:
    judgment = AgreementJudgment(
        groups=[
            _group("all agree", [_ref(A, "s1"), _ref(B, "s1")]),
            _group("only A", [_ref(A, "s2")]),
            _group("all but contradicted", [_ref(A, "s3"), _ref(B, "s2")], [_ref(B, "s3")]),
            _group("all again", [_ref(B, "s4"), _ref(A, "s4"), _ref(A, "s5")]),
        ]
    )
    assert agreement_ratio(judgment, [A, B]) == pytest.approx(0.5)
    assert agreement_ratio(judgment, [A, B, C]) == pytest.approx(0.0)
