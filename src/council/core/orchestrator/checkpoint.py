"""Human checkpoints: pause a run, hand a question to the user, resume with the answer.

Two checkpoints exist (SPEC section 4):

* ``PRUNE_ANSWERS`` (A): after stage 1, the user drops answers that should not go on
  to peer review and synthesis.
* ``ADJUDICATE_DISPUTES`` (B): after the draft synthesis, the user rules on each
  disputed point before the chair writes the final text.

These models are user-facing (CLI / later Web UI), not structured-output schemas, so
the strict-subset rules of ADR 0001 do not apply to them.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict


class CheckpointKind(StrEnum):
    PRUNE_ANSWERS = "prune_answers"
    ADJUDICATE_DISPUTES = "adjudicate_disputes"


class CheckpointRequest(BaseModel):
    """What the user is asked at a checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    kind: CheckpointKind
    stage_name: str
    candidates: tuple[str, ...]
    """Anonymous labels (A) or dispute ids (B) the user decides on."""
    created_at: datetime


class PruneDecision(BaseModel):
    """Answer to checkpoint A."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[CheckpointKind.PRUNE_ANSWERS] = CheckpointKind.PRUNE_ANSWERS
    drop: tuple[str, ...]
    """Anonymous labels to remove. Empty means keep all."""
    note: str | None = None


class RulingChoice(StrEnum):
    ADOPT_POSITION = "adopt_position"
    """Adopt the position held by ``label``."""
    CUSTOM = "custom"
    """Use the user's own text."""
    DEFER_TO_CHAIR = "defer_to_chair"
    """Let the chair decide, citing its assessment."""
    LEAVE_OPEN = "leave_open"
    """Report the dispute as unresolved in the final text."""


class Ruling(BaseModel):
    """The user's decision on one disputed point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dispute_id: str
    choice: RulingChoice
    label: str | None = None
    """Required iff ``choice`` is ``ADOPT_POSITION``."""
    text: str | None = None
    """Required iff ``choice`` is ``CUSTOM``."""


class AdjudicationDecision(BaseModel):
    """Answer to checkpoint B."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[CheckpointKind.ADJUDICATE_DISPUTES] = CheckpointKind.ADJUDICATE_DISPUTES
    rulings: tuple[Ruling, ...]
    note: str | None = None


UserDecision = PruneDecision | AdjudicationDecision


class CheckpointStore(Protocol):
    """Persists paused runs so they can be resumed in a later process."""

    def save(
        self, run_id: str, snapshot: dict[str, object], request: CheckpointRequest
    ) -> None: ...

    def load(self, run_id: str) -> tuple[dict[str, object], CheckpointRequest]: ...

    def list_paused(self) -> list[CheckpointRequest]: ...


class FileCheckpointStore:
    """:class:`CheckpointStore` writing JSON files under the user data directory."""

    def __init__(self, root: Path) -> None:
        raise NotImplementedError

    def save(self, run_id: str, snapshot: dict[str, object], request: CheckpointRequest) -> None:
        raise NotImplementedError

    def load(self, run_id: str) -> tuple[dict[str, object], CheckpointRequest]:
        raise NotImplementedError

    def list_paused(self) -> list[CheckpointRequest]:
        raise NotImplementedError
