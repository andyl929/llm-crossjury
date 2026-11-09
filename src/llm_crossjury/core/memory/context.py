"""Layered context for one model call in a session (ADR 0008, "Memory layer").

Four layers, within ``context_budget_tokens`` and within the model's context window
minus its output cap:

0. **Stable prefix**: the system prompt and the session's attachments (identical
   across turns, so vendor prompt caching applies). An attachment too large for its
   share of the budget is chunked into the search index and served through layer 2.
1. **Summary**: ``MemoryState.summary`` and ``facts`` (turns before
   ``summarized_through``).
2. **Retrieved**: the best FTS5 snippets for the new message from older turns and
   large attachments, leaving out turns layer 3 already holds.
3. **Recent**: the last turns verbatim, newest kept first. The last
   :data:`MIN_RECENT_EXCHANGES` exchanges and the continued jury turn are always kept
   (shortened if even they do not fit); older verbatim turns are dropped oldest first
   and are then only reachable through layers 1 and 2.

Layers 1 and 2 travel in one user message (a ``<conversation_memory>`` block) at the
start of the variable suffix, then layer 3, then the new message.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from pydantic import BaseModel, ConfigDict, Field

from llm_crossjury.core.attachments.build import BuiltAttachment
from llm_crossjury.core.attachments.limits import MAX_TOTAL_BYTES
from llm_crossjury.core.attachments.prompt import (
    add_derived_images,
    attachment_tokens,
    derived_map,
    inline_attachments,
    with_attachments,
)
from llm_crossjury.core.compare_service import SelectionError, TokenCounter
from llm_crossjury.core.cost.estimate import estimate_tokens
from llm_crossjury.core.memory.index import MemoryIndex
from llm_crossjury.core.memory.turns import (
    TargetModel,
    active_continuation,
    is_complete,
    turn_messages,
)
from llm_crossjury.core.prompting import load_template
from llm_crossjury.core.providers.base import AttachmentKind, Backend, Message, Prompt, Role
from llm_crossjury.core.sessions.models import ContinuationRef, MemoryState, Session

SYSTEM_PROMPT_ID = "chat/answer.v1"
"""Prompt template whose stable prefix is the chat system prompt."""

MIN_RECENT_EXCHANGES = 2
"""The last exchanges that are always sent verbatim."""

SUMMARY_SHARE = 0.15
"""At most this share of the context budget goes to the summary and facts."""

RETRIEVED_SHARE = 0.15
"""At most this share of the context budget goes to retrieved snippets."""

ATTACHMENT_SHARE = 0.5
"""Attachments larger than this share of the budget (together) move to retrieval,
largest first."""

RETRIEVAL_K = 8
"""Snippets asked from the index per call."""

OVERHEAD_TOKENS = 64
"""Safety margin for message framing and per-part rounding of the token estimate."""

MEMORY_OPEN = "<conversation_memory>"
MEMORY_CLOSE = "</conversation_memory>"
_TRUNCATION_MARK = " [...]"


class ContextOverflowError(SelectionError):
    """The new message and the stable prefix alone exceed the context budget."""


class ContextBreakdown(BaseModel):
    """Estimated input tokens per layer (local heuristic, see ``estimate_tokens``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prefix: int = Field(ge=0)
    """System prompt and attachments (layer 0)."""
    summary: int = Field(ge=0)
    """Summary and facts (layer 1), including the memory block framing."""
    retrieved: int = Field(ge=0)
    """Retrieved snippets (layer 2)."""
    recent: int = Field(ge=0)
    """Recent turns verbatim (layer 3)."""
    message: int = Field(ge=0)
    """The new user message."""

    @property
    def total(self) -> int:
        """Sum of every layer."""
        return self.prefix + self.summary + self.retrieved + self.recent + self.message


@dataclass(frozen=True)
class ContextTarget:
    """Facts about the model a context is built for.

    Attributes:
        provider: Provider id.
        backend: ``api`` or ``cli`` (``cli`` prompts get attachments inlined as text).
        model_id: Resolved registry model id.
        context_window: Registry context window, ``None`` when unknown.
        max_output_tokens: The call's output cap (reserved out of the window).
        input_modalities: Attachment kinds the model reads natively.
    """

    provider: str
    backend: Backend
    model_id: str
    context_window: int | None
    max_output_tokens: int
    input_modalities: frozenset[AttachmentKind] = frozenset()


@dataclass(frozen=True)
class BuiltContext:
    """A prompt assembled by :func:`build_context`.

    Attributes:
        prompt: The prompt to send.
        breakdown: Estimated tokens per layer.
        budget_tokens: The effective budget (setting, capped by the model window).
        verbatim_turn_ids: Turns sent verbatim (layer 3), oldest first.
        omitted_turns: Complete turns not sent verbatim.
        attachments: Attachments kept in the stable prefix.
        retrieval_attachments: Attachment ids served through retrieval instead.
        notes: Human-readable notes (attachments moved or left out).
    """

    prompt: Prompt
    breakdown: ContextBreakdown
    budget_tokens: int
    verbatim_turn_ids: tuple[str, ...]
    omitted_turns: int
    attachments: tuple[BuiltAttachment, ...]
    retrieval_attachments: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class _Exchange:
    index: int
    turn_id: str
    messages: tuple[Message, Message]
    tokens: int
    pinned: bool


def available_tokens(budget_tokens: int, target: ContextTarget) -> int:
    """The context budget for ``target``: the setting, capped by the model's context
    window minus its output cap (when the registry knows the window)."""
    available = budget_tokens
    if target.context_window:
        room = target.context_window - target.max_output_tokens
        if room > 0:
            available = min(available, room)
    return available


def truncate_to_tokens(text: str, max_tokens: int, count: TokenCounter) -> str:
    """The longest head of ``text`` (plus a ``[...]`` mark) within ``max_tokens``."""
    if count(text) <= max_tokens:
        return text
    if max_tokens <= count(_TRUNCATION_MARK):
        return ""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count(text[:mid] + _TRUNCATION_MARK) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + _TRUNCATION_MARK if lo else ""


def escape_memory(text: str) -> str:
    """Keep text from closing the memory block early."""
    return text.replace(MEMORY_CLOSE, "<\\/conversation_memory>")


def summary_text(memory: MemoryState) -> str:
    """Layer 1 text: the summary and the pinned facts (``""`` when both are empty)."""
    parts: list[str] = []
    if memory.summary.strip():
        parts.append(f"Summary of the earlier conversation:\n{memory.summary.strip()}")
    if memory.facts:
        parts.append("Pinned facts:\n" + "\n".join(f"- {fact}" for fact in memory.facts))
    return "\n\n".join(parts)


def _fit_messages(messages: Sequence[Message], cap: int, count: TokenCounter) -> list[Message]:
    """Shorten ``messages`` to fit ``cap`` tokens together, longest ones first."""
    sizes = [count(m.content) for m in messages]
    out = list(messages)
    left = max(0, cap)
    order = sorted(range(len(messages)), key=lambda i: sizes[i])
    for position, i in enumerate(order):
        share = left // (len(order) - position)
        if sizes[i] <= share:
            left -= sizes[i]
            continue
        content = truncate_to_tokens(messages[i].content, share, count) or _TRUNCATION_MARK.strip()
        out[i] = Message(role=messages[i].role, content=content)
        left -= count(content)
    return out


@dataclass(frozen=True)
class _Layer0:
    kept: tuple[BuiltAttachment, ...]
    tokens: int
    moved: tuple[str, ...]
    notes: tuple[str, ...]


def _place_attachments(
    session_id: str,
    built: Sequence[BuiltAttachment],
    target: ContextTarget,
    available: int,
    index: MemoryIndex | None,
) -> _Layer0:
    """Keep attachments in the prefix while they fit their share; move the largest
    text ones to retrieval (or leave them out) when they do not."""
    if not built:
        return _Layer0((), 0, (), ())
    tokens = attachment_tokens(
        built,
        provider=target.provider,
        input_modalities=target.input_modalities,
        backend=target.backend,
    )
    notes: list[str] = []
    kept: list[BuiltAttachment] = []
    for item in built:
        if tokens.get(item.attachment.sha256) is None:
            notes.append(f"{item.attachment.filename} is left out: this model cannot read it.")
        else:
            kept.append(item)
    cap = int(available * ATTACHMENT_SHARE)
    moved: list[str] = []

    def size() -> int:
        return sum(tokens.get(b.attachment.sha256) or 0 for b in kept)

    def size_bytes() -> int:
        return sum(len(b.attachment.data) for b in kept)

    while kept and (size() > cap or size_bytes() > MAX_TOTAL_BYTES):
        largest = max(kept, key=lambda b: tokens.get(b.attachment.sha256) or 0)
        kept.remove(largest)
        att = largest.attachment
        if att.fallback_text is not None and index is not None and index.available:
            index.index_attachment(session_id, att.sha256, att.filename, att.fallback_text)
            moved.append(att.sha256)
            notes.append(
                f"{att.filename} is too large for the context: relevant parts are "
                "retrieved by search instead."
            )
        else:
            notes.append(f"{att.filename} is left out: it is too large for the context.")
    return _Layer0(tuple(kept), size(), tuple(moved), tuple(notes))


def _exchanges(
    session: Session,
    target: TargetModel,
    continuation: ContinuationRef | None,
    exclude_turn_id: str | None,
    count: TokenCounter,
) -> list[_Exchange]:
    out: list[_Exchange] = []
    for index, turn in enumerate(session.turns):
        if turn.id == exclude_turn_id or not is_complete(turn):
            continue
        messages = turn_messages(turn, target, continuation)
        out.append(
            _Exchange(
                index=index,
                turn_id=turn.id,
                messages=messages,
                tokens=sum(count(m.content) for m in messages),
                pinned=continuation is not None and continuation.turn_id == turn.id,
            )
        )
    return out


def _select_recent(
    exchanges: Sequence[_Exchange],
    *,
    room: int,
    reserve: int,
    summarized_through: int,
    count: TokenCounter,
) -> tuple[list[_Exchange], int]:
    """Pick layer 3: mandatory exchanges (shortened to ``room`` if needed), then older
    ones newest first while they fit ``room - reserve``. Returns them oldest first and
    their token total."""
    mandatory_ids = {e.turn_id for e in exchanges[-MIN_RECENT_EXCHANGES:]}
    mandatory_ids |= {e.turn_id for e in exchanges if e.pinned}
    mandatory = [e for e in exchanges if e.turn_id in mandatory_ids]
    used = sum(e.tokens for e in mandatory)
    if used > room:
        flat = [m for e in mandatory for m in e.messages]
        fitted = _fit_messages(flat, room, count)
        mandatory = [
            replace(
                e,
                messages=(fitted[2 * i], fitted[2 * i + 1]),
                tokens=count(fitted[2 * i].content) + count(fitted[2 * i + 1].content),
            )
            for i, e in enumerate(mandatory)
        ]
        used = sum(e.tokens for e in mandatory)
    chosen = {e.turn_id: e for e in mandatory}
    left = room - reserve - used
    for e in reversed(exchanges):
        if e.turn_id in chosen:
            continue
        if e.index < summarized_through or e.tokens > left:
            break
        chosen[e.turn_id] = e
        left -= e.tokens
        used += e.tokens
    return sorted(chosen.values(), key=lambda e: e.index), used


def _retrieved_text(
    session: Session,
    message: str,
    index: MemoryIndex,
    *,
    cap: int,
    exclude: Sequence[str],
    count: TokenCounter,
) -> str:
    index.sync_session(session)
    hits = index.search(session.id, message, RETRIEVAL_K, exclude_refs=exclude)
    header = "Excerpts from earlier in the conversation that may be relevant:"
    parts: list[str] = []
    used = count(header)
    for hit in hits:
        block = f"[{hit.label}]\n{hit.text}"
        tokens = count(block)
        if used + tokens > cap:
            continue
        parts.append(block)
        used += tokens
    return "\n\n".join([header, *parts]) if parts else ""


def build_context(
    session: Session,
    target: ContextTarget,
    message: str,
    *,
    budget_tokens: int,
    count_tokens: TokenCounter = estimate_tokens,
    attachments: Sequence[BuiltAttachment] = (),
    index: MemoryIndex | None = None,
    continued_from: ContinuationRef | None = None,
    exclude_turn_id: str | None = None,
    system_prompt: str | None = None,
) -> BuiltContext:
    """Assemble the layered prompt for one call (see the module docstring).

    Args:
        session: The session (may contain the pending turn being answered).
        target: The model the prompt is for.
        message: The new user message.
        budget_tokens: ``AppSettings.context_budget_tokens``.
        count_tokens: Token estimator.
        attachments: Every attachment of the session plus the new message's, loaded.
        index: Search index (``None`` = no retrieval; large attachments are left out).
        continued_from: The continuation of the new message, if any.
        exclude_turn_id: The pending turn being answered (never part of the history).
        system_prompt: Override of the chat system prompt.

    Raises:
        ContextOverflowError: the prefix and the new message alone exceed the budget.
    """
    count = count_tokens
    available = available_tokens(budget_tokens, target)
    system = (
        system_prompt
        if system_prompt is not None
        else load_template(SYSTEM_PROMPT_ID).stable_prefix
    )
    layer0 = _place_attachments(session.id, attachments, target, available, index)
    prefix_prompt = with_attachments(
        Prompt(stable_prefix=(Message(role=Role.SYSTEM, content=system),), variable_suffix=()),
        [b.attachment for b in layer0.kept],
    )
    prefix_tokens = sum(count(m.content) for m in prefix_prompt.stable_prefix) + layer0.tokens
    message_tokens = count(message)
    room = available - OVERHEAD_TOKENS - prefix_tokens - message_tokens
    if room < 0:
        raise ContextOverflowError(
            f"The message and attachments (about {prefix_tokens + message_tokens} tokens) "
            f"do not fit the context budget of {available} tokens."
        )

    model = TargetModel(provider=target.provider, model_id=target.model_id)
    turns = [t for t in session.turns if t.id != exclude_turn_id]
    continuation = active_continuation(turns, continued_from)
    exchanges = _exchanges(session, model, continuation, exclude_turn_id, count)

    memory_summary = summary_text(session.memory)
    retrieval_on = index is not None and index.available
    summary_cap = min(int(available * SUMMARY_SHARE), room)
    memory_summary = truncate_to_tokens(memory_summary, summary_cap, count)
    summary_tokens = count(memory_summary) if memory_summary else 0
    reserve = int(available * RETRIEVED_SHARE) if retrieval_on else 0
    recent, recent_tokens = _select_recent(
        exchanges,
        room=max(0, room - summary_tokens),
        reserve=reserve,
        summarized_through=session.memory.summarized_through,
        count=count,
    )
    verbatim = [e.turn_id for e in recent]

    retrieved = ""
    retrieval_cap = min(reserve, room - summary_tokens - recent_tokens)
    has_older = len(recent) < len(exchanges) or bool(layer0.moved)
    if index is not None and retrieval_on and retrieval_cap > 0 and has_older:
        retrieved = _retrieved_text(
            session, message, index, cap=retrieval_cap, exclude=verbatim, count=count
        )

    suffix: list[Message] = []
    summary_part = 0
    retrieved_part = 0
    if memory_summary or retrieved:
        body = "\n\n".join(p for p in (memory_summary, retrieved) if p)
        memory_block = f"{MEMORY_OPEN}\n{escape_memory(body)}\n{MEMORY_CLOSE}"
        suffix.append(Message(role=Role.USER, content=memory_block))
        retrieved_part = count(retrieved) if retrieved else 0
        summary_part = max(0, count(memory_block) - retrieved_part)
    for e in recent:
        suffix.extend(e.messages)
    suffix.append(Message(role=Role.USER, content=message))

    prompt = Prompt(stable_prefix=prefix_prompt.stable_prefix, variable_suffix=tuple(suffix))
    prompt = add_derived_images(prompt, derived_map(layer0.kept), target.input_modalities)
    if target.backend is Backend.CLI:
        prompt = inline_attachments(prompt, target.input_modalities)
    return BuiltContext(
        prompt=prompt,
        breakdown=ContextBreakdown(
            prefix=prefix_tokens,
            summary=summary_part,
            retrieved=retrieved_part,
            recent=recent_tokens,
            message=message_tokens,
        ),
        budget_tokens=available,
        verbatim_turn_ids=tuple(verbatim),
        omitted_turns=len(exchanges) - len(recent),
        attachments=layer0.kept,
        retrieval_attachments=layer0.moved,
        notes=layer0.notes,
    )


__all__ = [
    "ATTACHMENT_SHARE",
    "MIN_RECENT_EXCHANGES",
    "RETRIEVED_SHARE",
    "SUMMARY_SHARE",
    "BuiltContext",
    "ContextBreakdown",
    "ContextOverflowError",
    "ContextTarget",
    "available_tokens",
    "build_context",
    "summary_text",
    "truncate_to_tokens",
]
