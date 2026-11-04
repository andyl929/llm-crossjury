"""Mutable state of one run, threaded through every stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from llm_crossjury.core.modes import Mode
from llm_crossjury.core.orchestrator.checkpoint import CheckpointKind, UserDecision


@dataclass
class RunContext:
    """State of a run.

    Attributes:
        run_id: Opaque id; also the key in the checkpoint store and ledger.
        mode: The mode being run.
        task: The user's task text (for Document Review, the instructions).
        artifacts: Stage outputs keyed by stage name.
        decisions: User decisions keyed by checkpoint kind.
        labels: Anonymous label -> member id. Never shown to members or the chair.
        cursor: Index of the next stage to execute.
        auto: ``--auto`` run: checkpoints are decided automatically (keep all, do not
            skip review, defer every dispute to the chair) and flagged as such.
    """

    run_id: str
    mode: Mode
    task: str
    artifacts: dict[str, Any] = field(default_factory=dict)
    decisions: dict[CheckpointKind, UserDecision] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    cursor: int = 0
    auto: bool = False

    def to_snapshot(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict for the checkpoint store.

        Snapshots always include the content needed to resume (temporary storage,
        deleted on completion or after the retention period); ``store_content``
        only governs the permanent run record.
        """
        raise NotImplementedError

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> RunContext:
        """Inverse of :meth:`to_snapshot`."""
        raise NotImplementedError
