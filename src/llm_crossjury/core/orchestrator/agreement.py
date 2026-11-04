"""Claim agreement among stage-1 answers (shown at checkpoint A, never auto-acted on).

A cheap judge model (``AgreementConfig.judge``, reasoning effort LOW by default)
returns an ``AgreementJudgment`` grouping all claims. The ratio is computed here:

    ratio = |groups supported by every kept answer and contradicted by none|
            / |groups|

An empty set of groups yields ``None`` (nothing to compare).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from llm_crossjury.core.schemas.agreement import AgreementJudgment

if TYPE_CHECKING:
    from llm_crossjury.core.cost.estimate import CallPlan
    from llm_crossjury.core.orchestrator.context import RunContext


def agreement_ratio(judgment: AgreementJudgment, kept_labels: Sequence[str]) -> float | None:
    """Compute the agreement ratio as defined in the module docstring."""
    raise NotImplementedError


class AgreementStage:
    """Runs the judge over all stage-1 claims. Artifact: ``AgreementJudgment``.

    Skipped (artifact ``None``) when the agreement measurement is disabled, when no
    judge is configured, or when fewer than two answers exist.
    """

    name = "agreement"

    def plan(self, ctx: RunContext) -> Sequence[CallPlan]:
        raise NotImplementedError

    async def run(self, ctx: RunContext) -> AgreementJudgment | None:
        raise NotImplementedError
