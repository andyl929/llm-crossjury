"""Session data model: a conversation of chat turns and jury turns (ADR 0008).

A *session* is what the home page shows: an ordered list of turns. A turn is either

* a :class:`JuryTurn`: one crossjury round (step 1 answers from several models, plus
  the optional step-2 synthesis stages), or
* a :class:`ChatTurn`: one user message answered by a single model.

The two kinds interleave freely; that is the closed loop of ADR 0008: continue a jury
answer as a chat, then hand a later question back to the jury.

This module is a shared contract between the session store, the memory layer and the
web API. Change it only through ``docs/contract-issues.md``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from llm_crossjury.core.compare_service import CompareResult, MemberResult, Selection
from llm_crossjury.core.providers.base import Backend

SESSION_SCHEMA_VERSION = 1

TITLE_CHARS = 80
"""History-list titles are cut to this many characters."""

SYNTHESIS_STAGES = ("agreement", "finish", "resynthesize")
"""Keys of :attr:`JuryTurn.synthesis`: the latest result of each step-2 endpoint."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TurnStatus(StrEnum):
    PENDING = "pending"
    """Work is running on the server (the page may have been closed meanwhile)."""
    DONE = "done"
    ERROR = "error"
    """Failed; ``error`` says why. Also used for work interrupted by a server stop."""


class ModelRef(_Frozen):
    """One concrete model on one backend."""

    provider: str = Field(min_length=1)
    backend: Backend = Backend.API
    model: str = Field(min_length=1)
    """A registry ``model_id`` or an alias (``fast`` | ``balanced`` | ``best``)."""


class ContinuationRef(_Frozen):
    """Where a chat continues from: one member answer, or the synthesis, of a jury turn."""

    turn_id: str
    source: Literal["member", "synthesis"]
    member_index: int | None = None
    """Index into ``JuryTurn.step1.members`` when ``source == "member"``."""


class ChatTurn(_Frozen):
    """One user message answered by one model."""

    kind: Literal["chat"] = "chat"
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    created_at: datetime
    status: TurnStatus = TurnStatus.PENDING
    message: str
    """The user's text, as typed."""
    attachment_ids: tuple[str, ...] = ()
    model: ModelRef
    max_output_tokens: int | None = None
    """``None`` = the default (``DEFAULT_MAX_OUTPUT_TOKENS``, clamped to the model)."""
    continued_from: ContinuationRef | None = None
    """Set on a chat turn started with a "Continue" button on a jury turn."""
    reply: str | None = None
    resolved_model: str | None = None
    """The concrete model id that answered (aliases resolved)."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: Decimal | None = None
    """``None`` when unknown (every ``cli`` call)."""
    currency: str | None = None
    latency_s: float | None = None
    context_tokens: int | None = None
    """Estimated tokens of the context the memory layer sent (for the usage meter)."""
    error: str | None = None


class JuryTurn(_Frozen):
    """One crossjury round inside a session."""

    kind: Literal["jury"] = "jury"
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    created_at: datetime
    status: TurnStatus = TurnStatus.PENDING
    """Status of step 1. Step-2 results are stored as they arrive."""
    task: str
    """The user's question, as typed (without the case file)."""
    attachment_ids: tuple[str, ...] = ()
    selections: tuple[Selection, ...] = ()
    case_file: str | None = None
    """Conversation context given to the members when the jury is asked mid-session
    (built by the memory layer); ``None`` for a first turn."""
    step1: CompareResult | None = None
    members_done: dict[int, MemberResult] = Field(default_factory=dict)
    """Step-1 results that finished so far, by index into ``selections``, written as
    each member completes so the page can show answers before the slowest one."""
    retrying: tuple[int, ...] = ()
    """Indices of members being re-run on their own (per-member retry)."""
    synthesis_stale: bool = False
    """A member was re-run after step 2 produced results: they no longer match the
    current answers."""
    synthesis: dict[str, Any] = Field(default_factory=dict)
    """Latest JSON result of each step-2 endpoint, by stage name
    (:data:`SYNTHESIS_STAGES`), exactly as the endpoint returned it."""
    synthesis_settings: dict[str, Any] | None = None
    """The step-2 request settings the page used (chair, extra instructions, ...), so
    a reopened session can show and continue them. Opaque to the server."""
    error: str | None = None


Turn = Annotated[ChatTurn | JuryTurn, Field(discriminator="kind")]


class MemoryState(_Frozen):
    """Compressed memory of a session (maintained by ``core.memory``)."""

    summary: str = ""
    """Rolling summary of the turns before ``summarized_through``."""
    summarized_through: int = 0
    """Number of leading turns folded into ``summary`` (0 = none)."""
    facts: tuple[str, ...] = ()
    """Pinned decisions, constraints and conclusions, one per entry, newest last."""
    updated_at: datetime | None = None


class Session(_Frozen):
    """A conversation (see the module docstring)."""

    schema_version: int = SESSION_SCHEMA_VERSION
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    title: str = ""
    """Shown in the history list; the first question (truncated) when empty."""
    created_at: datetime
    updated_at: datetime
    turns: tuple[Turn, ...] = ()
    memory: MemoryState = Field(default_factory=MemoryState)


class SessionSummary(_Frozen):
    """One row of the history list."""

    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    turns: int
    pending: bool
    """Some turn is still running."""


def summarize(session: Session) -> SessionSummary:
    """The history-list row of ``session``."""
    title = session.title
    if not title and session.turns:
        first = session.turns[0]
        title = first.task if isinstance(first, JuryTurn) else first.message
    title = " ".join(title.split())
    return SessionSummary(
        id=session.id,
        title=title[:TITLE_CHARS] + ("..." if len(title) > TITLE_CHARS else ""),
        created_at=session.created_at,
        updated_at=session.updated_at,
        turns=len(session.turns),
        pending=any(t.status is TurnStatus.PENDING for t in session.turns),
    )


__all__ = [
    "SESSION_SCHEMA_VERSION",
    "SYNTHESIS_STAGES",
    "ChatTurn",
    "ContinuationRef",
    "JuryTurn",
    "MemoryState",
    "ModelRef",
    "Session",
    "SessionSummary",
    "Turn",
    "TurnStatus",
    "summarize",
]
