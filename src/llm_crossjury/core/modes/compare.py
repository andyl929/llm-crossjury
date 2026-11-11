"""Compare mode: independent answers side by side. No review, no synthesis.

The answer stage delegates to :mod:`llm_crossjury.core.compare_service`, which the web
UI also uses directly, so both paths share validation, budget checks and ledger
recording.
"""

from __future__ import annotations

from collections.abc import Sequence

from llm_crossjury.core.compare_service import (
    CompareDeps,
    CompareResult,
    Selection,
    execute_calls,
    prepare_calls,
)
from llm_crossjury.core.config import CrossjuryConfig
from llm_crossjury.core.cost.estimate import CallPlan
from llm_crossjury.core.orchestrator.context import RunContext
from llm_crossjury.core.orchestrator.pipeline import Pipeline
from llm_crossjury.core.orchestrator.stage import StageName
from llm_crossjury.core.providers.base import Backend, ReasoningEffort


class CompareAnswerStage:
    """Stage 1 of Compare: every selected member answers the task concurrently.

    Implements the :class:`~llm_crossjury.core.orchestrator.stage.Stage` protocol. The
    artifact is a :class:`~llm_crossjury.core.compare_service.CompareResult`.

    Args:
        selections: The members to call (one per provider).
        deps: Providers, resolver, pricing, estimator, ledger and budget.
    """

    name: str = StageName.ANSWER.value

    def __init__(self, selections: Sequence[Selection], deps: CompareDeps) -> None:
        self.selections = tuple(selections)
        self.deps = deps

    def plan(self, ctx: RunContext) -> Sequence[CallPlan]:
        """One call per selected member. No side effects."""
        return [call.plan for call in prepare_calls(ctx.task, self.selections, self.deps)]

    async def run(self, ctx: RunContext) -> CompareResult:
        """Call every member; a failing member becomes an error entry."""
        prepared = prepare_calls(ctx.task, self.selections, self.deps)
        return await execute_calls(ctx.run_id, prepared, self.deps)


def selections_from_config(config: CrossjuryConfig) -> tuple[Selection, ...]:
    """Map configured members onto compare selections.

    Raises:
        ValueError: a member uses a non-``api`` backend, a reasoning level other than
            ``default``, or native parameters (not supported by the compare MVP).
    """
    selections: list[Selection] = []
    for member in config.members:
        if member.backend is not Backend.API:
            raise ValueError(f"member {member.id!r}: compare mode supports the api backend only")
        if member.reasoning not in {None, ReasoningEffort.DEFAULT} or member.native:
            raise ValueError(
                f"member {member.id!r}: compare mode supports reasoning 'default' only"
            )
        selections.append(
            Selection(provider=member.provider, model=member.model, temperature=member.temperature)
        )
    return tuple(selections)


def build_compare_pipeline(
    config: CrossjuryConfig,
    *,
    deps: CompareDeps,
    selections: Sequence[Selection] | None = None,
) -> Pipeline:
    """Stages: ``answer`` only.

    Args:
        config: Run configuration; its members become the selections unless
            ``selections`` is given.
        deps: Compare dependencies (see ``compare_service.build_default_deps``).
        selections: Explicit selections, overriding ``config.members``.

    Note:
        The pipeline runs no run-level budget pre-check; callers estimate first
        (``compare_service.budget_status``). Each call is still checked against the
        ledger before it starts.
    """
    chosen = tuple(selections) if selections is not None else selections_from_config(config)
    return Pipeline([CompareAnswerStage(chosen, deps)])
