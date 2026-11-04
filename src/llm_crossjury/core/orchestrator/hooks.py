"""Hooks evaluated after every model stage (ADR 0003).

There is deliberately no automatic early-stop hook: skipping peer review is a user
decision at checkpoint A, informed by the agreement ratio (see ``agreement``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from llm_crossjury.core.cost.ledger import Budget, Ledger
    from llm_crossjury.core.orchestrator.context import RunContext


class HookAction(StrEnum):
    CONTINUE = "continue"
    SKIP_TO = "skip_to"
    """Jump forward to ``HookDecision.target``."""
    ABORT = "abort"
    """Stop the run; artifacts produced so far are kept."""


@dataclass(frozen=True)
class HookDecision:
    action: HookAction
    target: str | None = None
    reason: str | None = None


CONTINUE = HookDecision(HookAction.CONTINUE)


class PipelineHook(Protocol):
    """Called by the pipeline after each non-checkpoint stage."""

    def after_stage(self, stage_name: str, ctx: RunContext) -> HookDecision: ...


class BudgetGuard:
    """Aborts the run when the ledger shows the budget is exhausted.

    Complements the per-call check in :meth:`Ledger.check`, which prevents starting
    a call that could overshoot.
    """

    def __init__(self, ledger: Ledger, budget: Budget) -> None:
        raise NotImplementedError

    def after_stage(self, stage_name: str, ctx: RunContext) -> HookDecision:
        raise NotImplementedError
