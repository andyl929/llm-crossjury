"""Turns as messages: how a stored turn is shown to a model (ADR 0008).

* A chat turn is the user's message plus the model's reply.
* A jury turn is the question as a user message, then, for the target model,
  (a) its own step-1 answer if it was a member, else (b) the synthesis final text if
  present, else (c) a compact digest of the member answers, labelled by model.
* A jury turn that a chat continues from shows the continued source (one member
  answer, or the synthesis) instead.

Without a target (summaries, the case file, the search index) the neutral form is used:
(b), else (c).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from llm_crossjury.core.providers.base import Message, Role
from llm_crossjury.core.sessions.models import (
    ChatTurn,
    ContinuationRef,
    JuryTurn,
    Session,
    Turn,
    TurnStatus,
)

DIGEST_CHARS_PER_MEMBER = 1_200
"""Product setting: characters of each member answer kept in a jury digest."""

SYNTHESIS_ORDER = ("resynthesize", "finish")
"""Synthesis stages that carry a final text, newest kind first."""

SYNTHESIS_HEADER = "[Synthesized answer of the jury]"
DIGEST_HEADER = "[Digest of the jury members' answers]"


@dataclass(frozen=True)
class TargetModel:
    """The model a context is built for: provider plus resolved model id."""

    provider: str
    model_id: str


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _final_text(result: Any) -> str | None:
    """The chair's final text in one stored step-2 result (``finish``/``resynthesize``).

    Falls back to the consensus statements when the chair wrote no final text.
    Unknown or malformed shapes give ``None``.
    """
    chair = _mapping((_mapping(result) or {}).get("chair"))
    synthesis = _mapping((chair or {}).get("synthesis"))
    if synthesis is None:
        return None
    final = synthesis.get("final_text")
    if isinstance(final, str) and final.strip():
        return final.strip()
    consensus = synthesis.get("consensus")
    if isinstance(consensus, list):
        points = [
            str(point["statement"]).strip()
            for point in consensus
            if isinstance(point, Mapping) and str(point.get("statement", "")).strip()
        ]
        if points:
            return "Points the jury agreed on:\n" + "\n".join(f"- {p}" for p in points)
    return None


def synthesis_final_text(turn: JuryTurn) -> str | None:
    """The synthesis final text of ``turn`` (re-synthesis preferred over finish)."""
    for stage in SYNTHESIS_ORDER:
        text = _final_text(turn.synthesis.get(stage))
        if text:
            return text
    return None


def member_answers(turn: JuryTurn) -> list[tuple[int, str, str, str]]:
    """``(index, provider, model, text)`` of every successful step-1 member."""
    if turn.step1 is None:
        return []
    return [
        (i, m.provider, m.model, m.text)
        for i, m in enumerate(turn.step1.members)
        if m.text is not None and m.text.strip()
    ]


def _clip(text: str, chars: int) -> str:
    text = text.strip()
    return text if len(text) <= chars else text[:chars].rstrip() + " [...]"


def jury_digest(turn: JuryTurn) -> str:
    """A compact digest of the member answers, labelled by model."""
    parts = [DIGEST_HEADER]
    for _, provider, model, text in member_answers(turn):
        parts.append(f"### {provider} / {model}\n{_clip(text, DIGEST_CHARS_PER_MEMBER)}")
    return "\n\n".join(parts)


def is_complete(turn: Turn) -> bool:
    """Whether a turn has content a model can see (a reply, or step-1 answers)."""
    if isinstance(turn, ChatTurn):
        return turn.status is TurnStatus.DONE and bool((turn.reply or "").strip())
    return bool(member_answers(turn))


def _continued_text(
    turn: JuryTurn, source: ContinuationRef, target: TargetModel | None
) -> str | None:
    if source.source == "synthesis":
        text = synthesis_final_text(turn)
        return f"{SYNTHESIS_HEADER}\n\n{text}" if text else None
    members = turn.step1.members if turn.step1 is not None else ()
    index = source.member_index
    if index is None:
        return None
    member = members[index] if 0 <= index < len(members) else turn.members_done.get(index)
    if member is None:
        return None
    if member.text is None or not member.text.strip():
        return None
    if target is not None and (member.provider, member.model) == (
        target.provider,
        target.model_id,
    ):
        return member.text.strip()
    return (
        f"[Answer of {member.provider} / {member.model} in the jury round]\n\n{member.text.strip()}"
    )


def jury_reply(
    turn: JuryTurn,
    target: TargetModel | None,
    continuation: ContinuationRef | None = None,
) -> str:
    """The assistant side of a jury turn for ``target`` (see the module docstring)."""
    if continuation is not None and continuation.turn_id == turn.id:
        text = _continued_text(turn, continuation, target)
        if text:
            return text
    if target is not None:
        for _, provider, model, text in member_answers(turn):
            if (provider, model) == (target.provider, target.model_id):
                return text.strip()
    final = synthesis_final_text(turn)
    if final:
        return f"{SYNTHESIS_HEADER}\n\n{final}"
    return jury_digest(turn)


def turn_messages(
    turn: Turn,
    target: TargetModel | None,
    continuation: ContinuationRef | None = None,
) -> tuple[Message, Message]:
    """The user and assistant messages of a complete turn (see :func:`is_complete`)."""
    if isinstance(turn, ChatTurn):
        return (
            Message(role=Role.USER, content=turn.message),
            Message(role=Role.ASSISTANT, content=(turn.reply or "").strip()),
        )
    return (
        Message(role=Role.USER, content=turn.task),
        Message(role=Role.ASSISTANT, content=jury_reply(turn, target, continuation)),
    )


def neutral_text(turn: Turn) -> str:
    """Model-neutral text of a complete turn, for summaries and the case file."""
    user, assistant = turn_messages(turn, None)
    if isinstance(turn, ChatTurn):
        who = f"Assistant ({turn.resolved_model or turn.model.model})"
        return f"User: {user.content}\n\n{who}: {assistant.content}"
    return f"User (asked the jury): {user.content}\n\nJury: {assistant.content}"


def index_text(turn: Turn) -> str | None:
    """Full searchable text of a complete turn (every member answer), else ``None``."""
    if not is_complete(turn):
        return None
    if isinstance(turn, ChatTurn):
        return neutral_text(turn)
    parts = [f"User (asked the jury): {turn.task}"]
    final = synthesis_final_text(turn)
    if final:
        parts.append(f"Jury synthesis: {final}")
    parts.extend(
        f"Answer of {provider} / {model}: {text.strip()}"
        for _, provider, model, text in member_answers(turn)
    )
    return "\n\n".join(parts)


def active_continuation(
    turns: Sequence[Turn], requested: ContinuationRef | None
) -> ContinuationRef | None:
    """The continuation a new chat message belongs to.

    The request's own, else that of the chat thread the message extends: the nearest
    earlier chat turn with one, looking back no further than the last jury turn.
    """
    if requested is not None:
        return requested
    for turn in reversed(turns):
        if isinstance(turn, JuryTurn):
            return None
        if turn.continued_from is not None:
            return turn.continued_from
    return None


def find_jury_turn(session: Session, turn_id: str) -> JuryTurn | None:
    """The jury turn ``turn_id`` of ``session``, if any."""
    for turn in session.turns:
        if isinstance(turn, JuryTurn) and turn.id == turn_id:
            return turn
    return None


def session_attachment_ids(session: Session, exclude_turn_id: str | None = None) -> list[str]:
    """Attachment ids of every turn, in first-use order, without duplicates."""
    ids: dict[str, None] = {}
    for turn in session.turns:
        if turn.id == exclude_turn_id:
            continue
        for attachment_id in turn.attachment_ids:
            ids.setdefault(attachment_id, None)
    return list(ids)


__all__ = [
    "DIGEST_CHARS_PER_MEMBER",
    "TargetModel",
    "active_continuation",
    "find_jury_turn",
    "index_text",
    "is_complete",
    "jury_digest",
    "jury_reply",
    "member_answers",
    "neutral_text",
    "session_attachment_ids",
    "synthesis_final_text",
    "turn_messages",
]
