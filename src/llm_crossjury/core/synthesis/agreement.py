"""Stage 1 of the synthesis upgrade: segment answers, group segments by meaning.

Step-1 answers are free text, so there are no member-extracted claims. Instead each
answer is split locally into numbered segments (paragraphs and list items, see
:func:`segment_answer`) and ONE cheap judge call groups all segments of all answers
into an ``AgreementJudgment`` (``ClaimRef.claim_id`` = ``Segment.segment_id``).

Classification is done in Python, never by the judge:

* consensus: supported by >= 2 distinct labels, contradicted by none;
* disputed: at least one contradicting ref;
* unique: supported by exactly 1 label, contradicted by none.

``ratio`` is ``llm_crossjury.core.orchestrator.agreement.agreement_ratio`` over all
answer labels. Trivial segments (headings, very short fragments) may be dropped
before judging to save tokens; dropped segments simply do not appear in any group.

INTERFACE FIXED by the integrator: implementations may add private helpers and
optional keyword arguments, but must not change these signatures or field names.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from llm_crossjury.core.cost.estimate import estimate_tokens
from llm_crossjury.core.orchestrator.agreement import agreement_ratio
from llm_crossjury.core.prompting import load_template
from llm_crossjury.core.providers.base import Prompt
from llm_crossjury.core.schemas.agreement import AgreementJudgment, ClaimGroup, ClaimRef
from llm_crossjury.core.schemas.export import strict_json_schema
from llm_crossjury.core.synthesis.llm import structured_call
from llm_crossjury.core.synthesis.types import (
    AnonAnswer,
    CallFn,
    CallSpec,
    ModelChoice,
    PlannedCall,
    Segment,
    _Frozen,
)

STAGE = "agreement"
PROMPT_ID = "synthesis/agreement.v1"

MAX_OUTPUT_TOKENS = 16_000
"""Output cap of the judge call (product setting)."""

MIN_SEGMENT_WORDS = 4
"""Prose fragments with fewer words than this are dropped as trivial."""

# Output heuristics for the estimate (product settings). The judge echoes every
# segment ref (~15 tokens each) and writes one short statement per group. Low assumes
# heavily merged groups, high assumes roughly one group per segment.
_OUT_BASE_LOW = 40
_OUT_PER_SEGMENT_LOW = 20
_OUT_BASE_HIGH = 150
_OUT_PER_SEGMENT_HIGH = 60

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])\s+(.*)$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")
_RULE_RE = re.compile(r"^\s{0,3}(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,}|=+\s*)$")
_WIDE_CHAR_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")


class AgreementOutcome(_Frozen):
    """Result of the agreement stage (JSON-serializable, sent to the UI)."""

    segments: tuple[Segment, ...]
    judgment: AgreementJudgment
    consensus: tuple[ClaimGroup, ...]
    disputed: tuple[ClaimGroup, ...]
    unique: tuple[ClaimGroup, ...]
    ratio: float | None


def _word_count(text: str) -> int:
    """Count words; each CJK, kana or hangul character counts as one word."""
    wide = len(_WIDE_CHAR_RE.findall(text))
    return wide + len(_WIDE_CHAR_RE.sub(" ", text).split())


def _closes(line: str, fence: str) -> bool:
    match = _FENCE_RE.match(line)
    if match is None:
        return False
    marker = match.group(1)
    return marker[0] == fence[0] and len(marker) >= len(fence) and line.strip() == marker


def _raw_blocks(text: str) -> Iterator[tuple[str, bool]]:
    """Yield ``(block_text, is_code)`` in document order.

    Prose blocks end at blank lines, headings, rules and the start of a list item; their
    lines are joined with spaces. Fenced code blocks are yielded whole, fences
    included, with line breaks kept. Heading and rule lines themselves are skipped.
    """
    prose: list[str] = []
    code: list[str] = []
    fence: str | None = None

    def flush() -> Iterator[tuple[str, bool]]:
        if prose:
            yield " ".join(part.strip() for part in prose), False
            prose.clear()

    for line in text.splitlines():
        if fence is not None:
            code.append(line)
            if _closes(line, fence):
                yield "\n".join(code), True
                code, fence = [], None
            continue
        opening = _FENCE_RE.match(line)
        if opening:
            yield from flush()
            fence, code = opening.group(1), [line]
            continue
        if not line.strip() or _HEADING_RE.match(line) or _RULE_RE.match(line):
            yield from flush()
            continue
        item = _LIST_ITEM_RE.match(line)
        if item:
            yield from flush()
            prose.append(item.group(1))
            continue
        prose.append(line)
    if fence is not None:  # unterminated fence: keep the rest as one code block
        yield "\n".join(code), True
    yield from flush()


def segment_answer(answer: AnonAnswer) -> tuple[Segment, ...]:
    """Split one answer into numbered segments (``s1``, ``s2``, ...).

    Paragraphs (separated by blank lines) and Markdown list items become segments;
    a fenced code block stays one segment. Headings, horizontal rules and prose
    fragments shorter than :data:`MIN_SEGMENT_WORDS` words are dropped. Ids are
    consecutive over the kept segments, in order.

    Args:
        answer: The anonymous step-1 answer.

    Returns:
        The kept segments of this answer.
    """
    kept: list[str] = []
    for block, is_code in _raw_blocks(answer.text):
        text = block.rstrip() if is_code else block.strip()
        if not text or (not is_code and _word_count(text) < MIN_SEGMENT_WORDS):
            continue
        kept.append(text)
    return tuple(
        Segment(label=answer.label, segment_id=f"s{index}", text=text)
        for index, text in enumerate(kept, start=1)
    )


def _segment_all(answers: Sequence[AnonAnswer]) -> tuple[Segment, ...]:
    return tuple(segment for answer in answers for segment in segment_answer(answer))


def _render_segments(segments: Sequence[Segment]) -> str:
    if not segments:
        return "(no segments)"
    return "\n\n".join(f"{s.label} / {s.segment_id}: {s.text}" for s in segments)


def _spec_for(segments: Sequence[Segment], judge: ModelChoice, task: str) -> CallSpec:
    prompt = load_template(PROMPT_ID).render(
        suffix_values={"task": task, "segments": _render_segments(segments)}
    )
    return CallSpec(
        stage=STAGE,
        provider=judge.provider,
        model_id=judge.model_id,
        prompt=prompt,
        response_schema=strict_json_schema(AgreementJudgment),
        max_output_tokens=MAX_OUTPUT_TOKENS,
        tag="judge",
    )


def _prompt_text(prompt: Prompt) -> str:
    return "\n\n".join(m.content for m in (*prompt.stable_prefix, *prompt.variable_suffix))


def build_agreement_spec(
    answers: Sequence[AnonAnswer], judge: ModelChoice, *, task: str
) -> CallSpec:
    """Build the single judge call over all segments of all answers.

    Args:
        answers: The anonymous step-1 answers.
        judge: The judge model.
        task: The user's original task (rendered into the variable suffix).

    Returns:
        The call spec with a strict ``AgreementJudgment`` response schema.
    """
    return _spec_for(_segment_all(answers), judge, task)


def estimate_agreement(
    answers: Sequence[AnonAnswer], judge: ModelChoice, *, task: str
) -> list[PlannedCall]:
    """Planned calls for the estimate (exactly one judge call).

    Input tokens are :func:`estimate_tokens` over the rendered prompt. Output tokens
    are a heuristic proportional to the segment count, capped at
    :data:`MAX_OUTPUT_TOKENS`.

    Args:
        answers: The anonymous step-1 answers.
        judge: The judge model.
        task: The user's original task.

    Returns:
        A one-element list.
    """
    segments = _segment_all(answers)
    spec = _spec_for(segments, judge, task)
    count = len(segments)
    return [
        PlannedCall(
            stage=STAGE,
            provider=judge.provider,
            model_id=judge.model_id,
            input_tokens=estimate_tokens(_prompt_text(spec.prompt)),
            output_tokens_low=min(_OUT_BASE_LOW + _OUT_PER_SEGMENT_LOW * count, MAX_OUTPUT_TOKENS),
            output_tokens_high=min(
                _OUT_BASE_HIGH + _OUT_PER_SEGMENT_HIGH * count, MAX_OUTPUT_TOKENS
            ),
        )
    ]


def classify(judgment: AgreementJudgment, labels: Sequence[str]) -> AgreementOutcome:
    """Split groups into consensus / disputed / unique and compute the ratio.

    ``segments`` is left empty here; :func:`run_agreement` fills it. A group with no
    refs at all fits no category and appears in none of the three lists.

    Args:
        judgment: The (validated) judge grouping.
        labels: All answer labels, for the ratio.

    Returns:
        The classified outcome.
    """
    consensus: list[ClaimGroup] = []
    disputed: list[ClaimGroup] = []
    unique: list[ClaimGroup] = []
    for group in judgment.groups:
        supporters = {ref.label for ref in group.supporting}
        if group.contradicting:
            disputed.append(group)
        elif len(supporters) >= 2:  # noqa: PLR2004
            consensus.append(group)
        elif len(supporters) == 1:
            unique.append(group)
    return AgreementOutcome(
        segments=(),
        judgment=judgment,
        consensus=tuple(consensus),
        disputed=tuple(disputed),
        unique=tuple(unique),
        ratio=agreement_ratio(judgment, labels),
    )


def _validate(judgment: AgreementJudgment, segments: Sequence[Segment]) -> AgreementJudgment:
    """Drop unknown refs and repeated refs (first occurrence wins), then empty groups."""
    known = {(s.label, s.segment_id) for s in segments}
    seen: set[tuple[str, str]] = set()

    def keep(refs: Sequence[ClaimRef]) -> list[ClaimRef]:
        kept: list[ClaimRef] = []
        for ref in refs:
            key = (ref.label, ref.claim_id)
            if key in known and key not in seen:
                seen.add(key)
                kept.append(ref)
        return kept

    groups: list[ClaimGroup] = []
    for group in judgment.groups:
        supporting = keep(group.supporting)
        contradicting = keep(group.contradicting)
        if supporting or contradicting:
            groups.append(
                ClaimGroup(
                    statement=group.statement,
                    supporting=supporting,
                    contradicting=contradicting,
                )
            )
    return AgreementJudgment(groups=groups)


async def run_agreement(
    answers: Sequence[AnonAnswer], judge: ModelChoice, *, task: str, call: CallFn
) -> AgreementOutcome:
    """Segment, judge (via ``structured_call``), validate refs, classify.

    If no answer yields any segment, the judge is not called and the outcome has no
    groups (``ratio`` is ``None``).

    Args:
        answers: The anonymous step-1 answers.
        judge: The judge model.
        task: The user's original task.
        call: Executes the judge call.

    Returns:
        The outcome, with ``segments`` filled.

    Raises:
        StructuredOutputError: if the judge's reply is invalid after one retry.
    """
    segments = _segment_all(answers)
    if segments:
        raw, _results = await structured_call(
            call, _spec_for(segments, judge, task), AgreementJudgment
        )
        judgment = _validate(raw, segments)
    else:
        judgment = AgreementJudgment(groups=[])
    outcome = classify(judgment, [answer.label for answer in answers])
    return outcome.model_copy(update={"segments": segments})
