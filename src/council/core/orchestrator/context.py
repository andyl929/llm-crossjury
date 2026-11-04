"""Mutable state of one run, threaded through every stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from council.core.modes import Mode
from council.core.orchestrator.checkpoint import CheckpointKind, UserDecision


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
    """

    run_id: str
    mode: Mode
    task: str
    artifacts: dict[str, Any] = field(default_factory=dict)
    decisions: dict[CheckpointKind, UserDecision] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    cursor: int = 0

    def to_snapshot(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict for the checkpoint store.

        Honors ``store_content``: when false, answer/review text is replaced by
        content hashes and the snapshot is resumable only within the same process.
        """
        raise NotImplementedError

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> RunContext:
        """Inverse of :meth:`to_snapshot`."""
        raise NotImplementedError
