"""Claim agreement judgment, shown at checkpoint A.

CONTRACT FILE. Do not modify; report problems in docs/contract-issues.md.

A cheap judge model groups the stage-1 claims of all members by meaning. The
agreement ratio is computed in Python from the groups (see
llm_crossjury.core.orchestrator.agreement), never by the judge itself.
"""

from __future__ import annotations

from llm_crossjury.core.schemas._base import StrictModel


class ClaimRef(StrictModel):
    """A pointer to one claim of one anonymized answer."""

    label: str
    """Anonymous label of the answer, e.g. "Response A"."""

    claim_id: str
    """Claim id within that answer, e.g. "c2"."""


class ClaimGroup(StrictModel):
    """Claims that make the same point, plus claims that contradict it."""

    statement: str
    """The shared point, stated neutrally in one sentence."""

    supporting: list[ClaimRef]
    """Claims that assert this point."""

    contradicting: list[ClaimRef]
    """Claims that assert the opposite or an incompatible point."""


class AgreementJudgment(StrictModel):
    """The judge's grouping of all stage-1 claims."""

    groups: list[ClaimGroup]
    """Every input claim appears in exactly one group's supporting or contradicting list."""
