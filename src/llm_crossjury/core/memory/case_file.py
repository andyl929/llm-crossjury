"""Case file: the conversation context for a jury question asked mid-session.

Every juror receives the same compact case file instead of the transcript, so the
context cost does not multiply by the transcript length (ADR 0008). It holds the
summary and facts, the best retrieved snippets for the question, and the last
exchange, capped at :data:`CASE_FILE_MAX_TOKENS`.
"""

from __future__ import annotations

import asyncio

from llm_crossjury.core.compare_service import TokenCounter
from llm_crossjury.core.cost.estimate import estimate_tokens
from llm_crossjury.core.memory.context import summary_text, truncate_to_tokens
from llm_crossjury.core.memory.index import MemoryIndex
from llm_crossjury.core.memory.turns import is_complete, neutral_text
from llm_crossjury.core.sessions.interfaces import CaseFileBuilder
from llm_crossjury.core.sessions.models import Session

CASE_FILE_MAX_TOKENS = 8_000
"""Upper bound of a case file (ADR 0008)."""

LAST_EXCHANGE_SHARE = 0.4
SUMMARY_SHARE = 0.35
CASE_FILE_SNIPPETS = 6

_SEPARATOR = "\n\n"
_FRAMING_TOKENS = 64


def _retrieved(
    session: Session,
    question: str,
    index: MemoryIndex | None,
    *,
    exclude: list[str],
    cap: int,
    count: TokenCounter,
) -> str:
    if index is None or not index.available or cap <= 0:
        return ""
    index.sync_session(session)
    hits = index.search(session.id, question, CASE_FILE_SNIPPETS, exclude_refs=exclude)
    header = "Earlier excerpts that may be relevant:"
    parts: list[str] = []
    used = count(header)
    for hit in hits:
        block = f"[{hit.label}]\n{hit.text}"
        tokens = count(block)
        if used + tokens <= cap:
            parts.append(block)
            used += tokens
    return _SEPARATOR.join([header, *parts]) if parts else ""


def build_case_file_sync(
    session: Session,
    question: str,
    *,
    index: MemoryIndex | None = None,
    count_tokens: TokenCounter = estimate_tokens,
    max_tokens: int = CASE_FILE_MAX_TOKENS,
) -> str | None:
    """Build the case file (``None`` when the session has no complete turn and no
    summary yet)."""
    count = count_tokens
    complete = [t for t in session.turns if is_complete(t)]
    memory = summary_text(session.memory)
    if not complete and not memory:
        return None
    sections: list[str] = []
    exclude: list[str] = []
    if memory:
        sections.append(truncate_to_tokens(memory, int(max_tokens * SUMMARY_SHARE), count))
    last = ""
    if complete:
        exclude.append(complete[-1].id)
        last = "Last exchange:\n" + truncate_to_tokens(
            neutral_text(complete[-1]), int(max_tokens * LAST_EXCHANGE_SHARE), count
        )
    used = sum(count(s) for s in sections) + count(last)
    retrieved = _retrieved(
        session,
        question,
        index,
        exclude=exclude,
        cap=max_tokens - used - _FRAMING_TOKENS,
        count=count,
    )
    if retrieved:
        sections.append(retrieved)
    if last:
        sections.append(last)
    text = _SEPARATOR.join(sections)
    return truncate_to_tokens(text, max_tokens, count)


def make_case_file_builder(
    index: MemoryIndex | None = None,
    *,
    count_tokens: TokenCounter = estimate_tokens,
    max_tokens: int = CASE_FILE_MAX_TOKENS,
) -> CaseFileBuilder:
    """A :data:`~llm_crossjury.core.sessions.interfaces.CaseFileBuilder` over ``index``.

    Args:
        index: The shared memory index (``None`` = no retrieved snippets).
        count_tokens: Token estimator.
        max_tokens: Cap of a case file.
    """

    async def build(session: Session, question: str) -> str | None:
        return await asyncio.to_thread(
            build_case_file_sync,
            session,
            question,
            index=index,
            count_tokens=count_tokens,
            max_tokens=max_tokens,
        )

    return build


__all__ = ["CASE_FILE_MAX_TOKENS", "build_case_file_sync", "make_case_file_builder"]
