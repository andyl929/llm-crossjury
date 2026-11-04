"""Human checkpoints: pause a run, hand a question to the user, resume with the answer.

Two checkpoints exist (SPEC section 4):

* ``PRUNE_ANSWERS`` (A): after stage 1, the user drops answers that should not go on
  to peer review and synthesis, and decides whether to skip peer review. The request
  carries the claim-agreement ratio and the estimated saving of skipping review;
  nothing is skipped automatically.
* ``ADJUDICATE_DISPUTES`` (B): after the draft synthesis, the user rules on each
  disputed point before the chair writes the final text.

These models are user-facing (CLI / later Web UI), not structured-output schemas, so
the strict-subset rules of ADR 0001 do not apply to them.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict


class CheckpointKind(StrEnum):
    PRUNE_ANSWERS = "prune_answers"
    ADJUDICATE_DISPUTES = "adjudicate_disputes"


class DecidedBy(StrEnum):
    """Who made a checkpoint decision. Shown in every output that depends on it."""

    USER = "user"
    AUTO = "auto"
    """Made under ``--auto``; outputs flag every auto-decided item."""


class AgreementSummary(BaseModel):
    """Claim agreement among stage-1 answers, for checkpoint A."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ratio: float | None
    """Share of claim groups supported by every kept answer with no contradiction;
    ``None`` if the measurement was disabled or failed."""
    judge_model: str | None
    threshold: float | None
    recommend_skip: bool
    """``ratio >= threshold`` (always ``False`` when either is ``None``)."""
    review_cost_low: Decimal | None
    review_cost_high: Decimal | None
    """Estimated cost of the peer-review stage, i.e. the saving if it is skipped."""
    currency: str


class CheckpointRequest(BaseModel):
    """What the user is asked at a checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    kind: CheckpointKind
    stage_name: str
    candidates: tuple[str, ...]
    """Anonymous labels (A) or dispute ids (B) the user decides on."""
    agreement: AgreementSummary | None
    """Checkpoint A only."""
    created_at: datetime


class PruneDecision(BaseModel):
    """Answer to checkpoint A."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[CheckpointKind.PRUNE_ANSWERS] = CheckpointKind.PRUNE_ANSWERS
    drop: tuple[str, ...]
    """Anonymous labels to remove. Empty means keep all."""
    skip_review: bool = False
    """Skip peer review and go straight to the draft synthesis."""
    decided_by: DecidedBy = DecidedBy.USER
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
    decided_by: DecidedBy = DecidedBy.USER
    """Under ``--auto`` every ruling is ``DEFER_TO_CHAIR`` with ``AUTO``."""


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

    def delete(self, run_id: str) -> None:
        """Remove a snapshot and its content (called when the run completes)."""
        ...

    def purge_expired(self, now: datetime, retention_days: int) -> list[str]:
        """Delete snapshots older than ``retention_days``; return their run ids."""
        ...


class FileCheckpointStore:
    """:class:`CheckpointStore` writing JSON files under the user data directory.

    Snapshots of unfinished runs include prompt/response content even when
    ``store_content`` is false, so a run can be resumed from another process. That
    content is temporary: it is deleted when the run completes and purged after
    ``checkpoint_retention_days`` (default 7) otherwise.
    """

    def __init__(self, root: Path) -> None:
        raise NotImplementedError

    def save(self, run_id: str, snapshot: dict[str, object], request: CheckpointRequest) -> None:
        raise NotImplementedError

    def load(self, run_id: str) -> tuple[dict[str, object], CheckpointRequest]:
        raise NotImplementedError

    def list_paused(self) -> list[CheckpointRequest]:
        raise NotImplementedError

    def delete(self, run_id: str) -> None:
        raise NotImplementedError

    def purge_expired(self, now: datetime, retention_days: int) -> list[str]:
        raise NotImplementedError
