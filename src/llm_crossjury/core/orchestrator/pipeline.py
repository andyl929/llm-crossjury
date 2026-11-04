"""The pipeline: runs stages in order, pauses at checkpoints, consults hooks.

``run``/``resume`` contain only the minimal control flow needed for the skeleton
smoke test (sequencing, pause, resume, hook actions). Persistence, concurrency
limits, retries, ledger wiring and estimation are implementation-phase work.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from llm_crossjury.core.cost.estimate import CostEstimate
from llm_crossjury.core.orchestrator.checkpoint import CheckpointRequest, UserDecision
from llm_crossjury.core.orchestrator.context import RunContext
from llm_crossjury.core.orchestrator.hooks import HookAction, PipelineHook
from llm_crossjury.core.orchestrator.stage import CheckpointStage, Stage


@dataclass(frozen=True)
class Completed:
    ctx: RunContext


@dataclass(frozen=True)
class Paused:
    ctx: RunContext
    request: CheckpointRequest


@dataclass(frozen=True)
class Stopped:
    ctx: RunContext
    reason: str | None


RunOutcome = Completed | Paused | Stopped


class PipelineError(Exception):
    """Invalid pipeline definition or resume input."""


class Pipeline:
    """An ordered list of stages and checkpoints.

    Args:
        stages: Model stages and checkpoint stages, in execution order. Names must
            be unique.
        hooks: Evaluated after each model stage, in order; the first non-CONTINUE
            decision wins.
    """

    def __init__(
        self,
        stages: Sequence[Stage | CheckpointStage],
        hooks: Sequence[PipelineHook] = (),
    ) -> None:
        names = [s.name for s in stages]
        if len(names) != len(set(names)):
            raise PipelineError("stage names must be unique")
        self.stages = tuple(stages)
        self.hooks = tuple(hooks)

    def estimate(self, ctx: RunContext) -> CostEstimate:
        """Estimate the cost of running from ``ctx.cursor`` to the end."""
        raise NotImplementedError

    async def run(self, ctx: RunContext) -> RunOutcome:
        """Run from ``ctx.cursor`` until completion, a checkpoint, or a stop."""
        while ctx.cursor < len(self.stages):
            stage = self.stages[ctx.cursor]
            if isinstance(stage, CheckpointStage):
                if stage.kind not in ctx.decisions:
                    request = CheckpointRequest(
                        run_id=ctx.run_id,
                        kind=stage.kind,
                        stage_name=stage.name,
                        candidates=tuple(stage.candidates(ctx)),
                        created_at=datetime.now(UTC),
                    )
                    return Paused(ctx, request)
                ctx.artifacts[stage.name] = ctx.decisions[stage.kind]
                ctx.cursor += 1
                continue

            ctx.artifacts[stage.name] = await stage.run(ctx)
            ctx.cursor += 1

            for hook in self.hooks:
                decision = hook.after_stage(stage.name, ctx)
                if decision.action is HookAction.ABORT:
                    return Stopped(ctx, decision.reason)
                if decision.action is HookAction.SKIP_TO:
                    ctx.cursor = self._index_of(decision.target)
                    break
        return Completed(ctx)

    async def resume(self, ctx: RunContext, decision: UserDecision) -> RunOutcome:
        """Record the user's decision for the pending checkpoint and continue.

        Raises:
            PipelineError: if the run is not paused at a checkpoint of that kind.
        """
        stage = self.stages[ctx.cursor] if ctx.cursor < len(self.stages) else None
        if not isinstance(stage, CheckpointStage) or stage.kind != decision.kind:
            raise PipelineError("run is not paused at a checkpoint of this kind")
        ctx.decisions[decision.kind] = decision
        return await self.run(ctx)

    def _index_of(self, name: str | None) -> int:
        for index, stage in enumerate(self.stages):
            if stage.name == name:
                return index
        raise PipelineError(f"unknown stage {name!r}")
