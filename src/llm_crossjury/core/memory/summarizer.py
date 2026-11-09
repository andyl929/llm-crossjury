"""Rolling summary and pinned facts of a session (ADR 0008, "Summaries").

After a turn finishes, when the history outside the recent layer (everything after
``summarized_through`` except the last :data:`MIN_RECENT_EXCHANGES` complete turns)
exceeds :data:`SUMMARY_TRIGGER_SHARE` of the context budget, the memory model folds
the oldest unsummarized turns into ``summary`` and rewrites ``facts``, until that
history is back under :data:`SUMMARY_TARGET_SHARE` (at most :data:`MAX_FOLD_TOKENS` of
turn text per call). The update never raises: a failure is logged (exception type
only) and the next finished turn tries again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from llm_crossjury.core.compare_service import TokenCounter
from llm_crossjury.core.cost.estimate import estimate_tokens
from llm_crossjury.core.memory.context import MIN_RECENT_EXCHANGES, truncate_to_tokens
from llm_crossjury.core.memory.turns import is_complete, neutral_text
from llm_crossjury.core.prompting import load_template
from llm_crossjury.core.schemas.export import strict_json_schema
from llm_crossjury.core.sessions.models import MemoryState, Session, TurnStatus
from llm_crossjury.core.synthesis.llm import structured_call
from llm_crossjury.core.synthesis.types import CallFn, CallSpec

logger = logging.getLogger(__name__)

MEMORY_STAGE = "memory"
"""Ledger stage of memory calls."""

PROMPT_ID = "memory/summarize.v1"

SUMMARY_TRIGGER_SHARE = 0.25
"""Summarize when the history outside the recent layer exceeds this budget share."""

SUMMARY_TARGET_SHARE = 0.10
"""Fold turns until the history outside the recent layer is under this share."""

MAX_FOLD_TOKENS = 60_000
"""At most this much turn text goes into one summary call."""

MEMORY_MAX_OUTPUT_TOKENS = 8_000
"""Output cap of a memory call (clamped to the model's maximum by the call function)."""

MAX_FACTS = 50
"""Pinned facts kept (newest last)."""

MAX_FACT_CHARS = 500


class MemoryUpdate(BaseModel):
    """Structured output of the memory model."""

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    summary: str
    """The updated rolling summary of the whole conversation so far."""
    facts: list[str]
    """The complete updated list of pinned facts, oldest first."""


@dataclass(frozen=True)
class FoldPlan:
    """Which turns a summary call folds: ``session.turns[start:end]``."""

    start: int
    end: int


def _outside_recent(session: Session) -> tuple[int, int]:
    """``[start, end)``: turns after ``summarized_through`` and before the recent layer,
    stopping at the first pending turn (its result is not known yet)."""
    start = min(session.memory.summarized_through, len(session.turns))
    complete = [i for i, t in enumerate(session.turns) if is_complete(t)]
    end = complete[-MIN_RECENT_EXCHANGES] if len(complete) >= MIN_RECENT_EXCHANGES else start
    for i in range(start, end):
        if session.turns[i].status is TurnStatus.PENDING:
            end = i
            break
    return start, max(start, end)


def _turn_tokens(session: Session, i: int, count: TokenCounter) -> int:
    turn = session.turns[i]
    return count(neutral_text(turn)) if is_complete(turn) else 0


def plan_fold(
    session: Session, budget_tokens: int, count_tokens: TokenCounter = estimate_tokens
) -> FoldPlan | None:
    """The turns to fold now, or ``None`` when no summary is needed."""
    start, end = _outside_recent(session)
    sizes = [_turn_tokens(session, i, count_tokens) for i in range(start, end)]
    total = sum(sizes)
    if total <= budget_tokens * SUMMARY_TRIGGER_SHARE:
        return None
    target = budget_tokens * SUMMARY_TARGET_SHARE
    folded = 0
    stop = start
    for size in sizes:
        if stop > start and (total <= target or folded + size > MAX_FOLD_TOKENS):
            break
        folded += size
        total -= size
        stop += 1
    return FoldPlan(start=start, end=stop)


def _turns_text(session: Session, plan: FoldPlan, count: TokenCounter) -> str:
    parts: list[str] = []
    for i in range(plan.start, plan.end):
        turn = session.turns[i]
        if not is_complete(turn):
            continue
        text = truncate_to_tokens(neutral_text(turn), MAX_FOLD_TOKENS, count)
        parts.append(f"[Turn {i + 1}]\n{text}")
    return "\n\n".join(parts) or "(no content)"


def build_summary_spec(
    session: Session,
    plan: FoldPlan,
    *,
    provider: str,
    model_id: str,
    count_tokens: TokenCounter = estimate_tokens,
) -> CallSpec:
    """The memory call that folds ``plan`` into the session's memory."""
    memory = session.memory
    prompt = load_template(PROMPT_ID).render(
        suffix_values={
            "summary": memory.summary.strip() or "(none yet)",
            "facts": "\n".join(f"- {f}" for f in memory.facts) or "(none yet)",
            "turns": _turns_text(session, plan, count_tokens),
        }
    )
    return CallSpec(
        stage=MEMORY_STAGE,
        provider=provider,
        model_id=model_id,
        prompt=prompt,
        response_schema=strict_json_schema(MemoryUpdate),
        max_output_tokens=MEMORY_MAX_OUTPUT_TOKENS,
        tag="summary",
    )


def _clean_facts(facts: list[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for fact in facts:
        text = " ".join(fact.split())[:MAX_FACT_CHARS]
        if text:
            seen.setdefault(text, None)
    return tuple(list(seen)[-MAX_FACTS:])


async def update_memory(
    session: Session,
    *,
    call: CallFn,
    provider: str,
    model_id: str,
    budget_tokens: int,
    count_tokens: TokenCounter = estimate_tokens,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> MemoryState | None:
    """Fold old turns into the memory when needed (see the module docstring).

    Args:
        session: The latest session.
        call: Executes the memory call (budget check, ledger with stage ``memory``).
        provider: Memory model provider.
        model_id: Memory model id.
        budget_tokens: ``AppSettings.context_budget_tokens``.
        count_tokens: Token estimator.
        now: Clock for ``updated_at``.

    Returns:
        The new memory state, or ``None`` when nothing was needed or the call failed.
        Never raises (except on cancellation).
    """
    try:
        plan = plan_fold(session, budget_tokens, count_tokens)
        if plan is None or plan.end <= plan.start:
            return None
        spec = build_summary_spec(
            session, plan, provider=provider, model_id=model_id, count_tokens=count_tokens
        )
        update, _ = await structured_call(call, spec, MemoryUpdate)
        summary = update.summary.strip()
        if not summary:
            logger.warning("memory model returned an empty summary; ignored")
            return None
        return MemoryState(
            summary=summary,
            summarized_through=plan.end,
            facts=_clean_facts(update.facts),
            updated_at=now(),
        )
    except Exception as exc:  # never raise into the chat path; the next turn retries
        logger.warning("memory update failed (%s); will retry later", type(exc).__name__)
        return None


__all__ = [
    "MAX_FOLD_TOKENS",
    "MEMORY_STAGE",
    "SUMMARY_TARGET_SHARE",
    "SUMMARY_TRIGGER_SHARE",
    "FoldPlan",
    "MemoryUpdate",
    "build_summary_spec",
    "plan_fold",
    "update_memory",
]
