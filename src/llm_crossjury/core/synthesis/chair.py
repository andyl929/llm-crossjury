"""Stage 3 of the synthesis upgrade: the chair writes the final answer.

One chair call returns a ``Synthesis`` with ``final_text`` SET (the user chose
one-click synthesis): disputes are decided provisionally by the chair and recorded in
``disputed`` with ``chair_assessment`` explaining the provisional decision, so the UI
can flag them as "decided by AI". ``unique`` lists single-answer insights that the
chair did NOT put into ``final_text``. ``consensus`` lists the agreed points.

Re-synthesis: the user may rule on disputes (pick a position label) and on unique
insights (include / exclude). :func:`run_chair` is then called again with
``previous`` and ``rulings``; it is one more chair call (the stable prefix, holding
instructions, answers and review material, is unchanged so it can hit the cache).

The additional list shown under the final answer is built in Python
(:func:`build_additional_list`):

* disputed: ``Synthesis.disputed``;
* unique (not included): ``Synthesis.unique``;
* possibly wrong: review issues with severity ``critical`` or ``major``, merged per
  (target, quote) with the number of reviewers that flagged them.

INTERFACE FIXED by the integrator: implementations may add private helpers and
optional keyword arguments, but must not change these signatures or field names.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from llm_crossjury.core.attachments.prompt import with_attachments
from llm_crossjury.core.attachments.render import fallback_block
from llm_crossjury.core.cost.estimate import estimate_tokens
from llm_crossjury.core.prompting import load_template
from llm_crossjury.core.providers.base import Attachment, Prompt
from llm_crossjury.core.schemas._base import Severity
from llm_crossjury.core.schemas.agreement import ClaimGroup, ClaimRef
from llm_crossjury.core.schemas.export import strict_json_schema
from llm_crossjury.core.schemas.synthesis import DisputedPoint, Synthesis, UniqueInsight
from llm_crossjury.core.synthesis.agreement import AgreementOutcome
from llm_crossjury.core.synthesis.llm import StructuredOutputError, structured_call
from llm_crossjury.core.synthesis.review import ReviewOutcome
from llm_crossjury.core.synthesis.types import (
    AnonAnswer,
    CallFn,
    CallSpec,
    ModelChoice,
    PlannedCall,
    _Frozen,
)

STAGE = "chair"
PROMPT_ID = "synthesis/chair.v1"
MAX_OUTPUT_TOKENS = 8192
"""Output cap of the chair call (a product setting)."""

_MIN_POSITIONS = 2
_SERIOUS = (Severity.CRITICAL, Severity.MAJOR)
_SEVERITY_RANK = {
    Severity.CRITICAL: 0,
    Severity.MAJOR: 1,
    Severity.MINOR: 2,
    Severity.NIT: 3,
}
_REVIEW_ALLOWANCE = 0.6
"""Estimate: agreement plus review material, as a share of the answer tokens."""
_RULINGS_ALLOWANCE = 300
"""Estimate: tokens for the rulings directives of a re-synthesis."""
_MIN_OUTPUT_LOW = 300
_EDGE_CHARS = "\"'`" + "\u201c\u201d\u2018\u2019" + " .,;:!?"
"""Quote marks and punctuation ignored at the edges when matching issue quotes."""


@dataclass
class _Merged:
    quote: str
    problem: str
    severity: Severity
    reviewers: set[str] = field(default_factory=set)


class Ruling(_Frozen):
    """A user decision applied during re-synthesis."""

    kind: Literal["dispute", "unique"]
    target: str
    """``DisputedPoint.id`` for disputes; the index (as a string) into
    ``Synthesis.unique`` for unique insights."""
    decision: str
    """Disputes: the anonymous label whose position to adopt. Unique: ``"include"``."""


class FlaggedIssue(_Frozen):
    """A serious problem reviewers found in one answer."""

    target: str
    """Anonymous label of the answer."""
    quote: str
    problem: str
    severity: Severity
    flagged_by: int
    """Number of reviewers that reported an issue on this (target, quote)."""


class AdditionalList(_Frozen):
    """Material that did not make it into the final answer, for the user to inspect."""

    disputed: tuple[DisputedPoint, ...]
    unique: tuple[UniqueInsight, ...]
    possibly_wrong: tuple[FlaggedIssue, ...]


class ChairOutcome(_Frozen):
    """Result of the chair stage (JSON-serializable, sent to the UI)."""

    synthesis: Synthesis
    additional: AdditionalList


# --------------------------------------------------------------------------- prompt


def _distinct_labels(refs: Sequence[ClaimRef]) -> list[str]:
    return sorted({ref.label for ref in refs})


def _render_group(group: ClaimGroup) -> str:
    line = f"- {group.statement.strip()}"
    supporting = _distinct_labels(group.supporting)
    contradicting = _distinct_labels(group.contradicting)
    details = []
    if supporting:
        details.append("supported by " + ", ".join(supporting))
    if contradicting:
        details.append("contradicted by " + ", ".join(contradicting))
    return line + (f" ({'; '.join(details)})" if details else "")


def _render_agreement(agreement: AgreementOutcome | None) -> str:
    if agreement is None:
        return "No agreement analysis is available."
    parts: list[str] = []
    if agreement.ratio is not None:
        parts.append(f"Agreement ratio: {agreement.ratio:.2f}")
    sections = (
        ("Consensus statements", agreement.consensus),
        ("Disputed statements", agreement.disputed),
        ("Unique statements (raised by a single answer)", agreement.unique),
    )
    for title, groups in sections:
        body = "\n".join(_render_group(g) for g in groups) if groups else "- (none)"
        parts.append(f"#### {title}\n\n{body}")
    return "\n\n".join(parts)


def _render_reviews(reviews: ReviewOutcome | None) -> str:
    if reviews is None:
        return "No peer review was run."
    by_target: dict[str, list[str]] = {}
    for reviewer in sorted(reviews.reviews):
        for target_review in reviews.reviews[reviewer].reviews:
            lines = by_target.setdefault(target_review.target, [])
            for issue in target_review.issues:
                line = (
                    f'- [{issue.severity.value}] "{issue.quote.strip()}": {issue.problem.strip()}'
                )
                if issue.suggested_fix:
                    line += f" Suggested fix: {issue.suggested_fix.strip()}"
                lines.append(line)
    parts: list[str] = []
    for target in sorted(by_target):
        body = "\n".join(by_target[target]) or "- (no issues reported)"
        parts.append(f"#### Issues in {target}\n\n{body}")
    if not parts:
        parts.append("Reviewers reported no issues.")
    if reviews.ranking:
        ranked = []
        for position, item in enumerate(reviews.ranking, start=1):
            score = "n/a" if item.score is None else f"{item.score:.2f}"
            ranked.append(
                f"{position}. {item.label} (score {score}, "
                f"{item.informative_reviews} informative review(s))"
            )
        parts.append("#### Ranking (Borda count, best first)\n\n" + "\n".join(ranked))
    return "\n\n".join(parts)


def _render_material(
    answers: Sequence[AnonAnswer],
    task: str,
    agreement: AgreementOutcome | None,
    reviews: ReviewOutcome | None,
) -> str:
    answer_text = "\n\n".join(f"### {a.label}\n\n{a.text.strip()}" for a in answers)
    return (
        f"## Task\n\n{task.strip()}\n\n"
        f"## Answers\n\n{answer_text}\n\n"
        f"## Agreement analysis\n\n{_render_agreement(agreement)}\n\n"
        f"## Peer review\n\n{_render_reviews(reviews)}"
    )


def _render_rulings(previous: Synthesis, rulings: Sequence[Ruling]) -> list[str]:
    disputes = {d.id: d for d in previous.disputed}
    directives: list[str] = []
    for ruling in rulings:
        if ruling.kind == "dispute":
            point = disputes[ruling.target]
            stance = next(p.stance for p in point.positions if p.label == ruling.decision)
            directives.append(
                f"- For dispute {point.id} ({point.question.strip()}), adopt "
                f"{ruling.decision}'s position: {stance.strip()}"
            )
        else:
            index = int(ruling.target)
            insight = previous.unique[index]
            verb = "Include" if ruling.decision == "include" else "Do not include"
            directives.append(
                f"- {verb} unique insight {index + 1} (from {insight.label}): "
                f"{insight.insight.strip()}"
            )
    return directives


def _render_request(
    extra_instructions: str | None,
    previous: Synthesis | None,
    rulings: Sequence[Ruling],
) -> str:
    extra = (extra_instructions or "").strip()
    parts = [f"## User's extra instructions\n\n{extra or '(none)'}"]
    if previous is None:
        parts.append("## Request\n\nWrite the synthesis.")
        return "\n\n".join(parts)
    parts.append(
        "## Previous synthesis\n\n"
        "You produced the synthesis below earlier. Revise it: write a new complete "
        "synthesis that follows the user's rulings.\n\n"
        f"```json\n{previous.model_dump_json(indent=2)}\n```"
    )
    directives = _render_rulings(previous, rulings)
    body = "\n".join(directives) if directives else "(none)"
    parts.append(
        "## User's rulings\n\n"
        "The user decided the points below. These decisions are final: follow them in "
        "`final_text`. A dispute the user decided is no longer open, so leave it out of "
        "`disputed`; an insight the user asked to include is no longer listed in "
        "`unique`.\n\n" + body
    )
    return "\n\n".join(parts)


def _build_prompt(
    answers: Sequence[AnonAnswer],
    *,
    task: str,
    agreement: AgreementOutcome | None,
    reviews: ReviewOutcome | None,
    extra_instructions: str | None,
    previous: Synthesis | None,
    rulings: Sequence[Ruling],
    attachments: Sequence[Attachment] = (),
) -> Prompt:
    template = load_template(PROMPT_ID)
    prompt = template.render(
        prefix_values={"material": _render_material(answers, task, agreement, reviews)},
        suffix_values={"request": _render_request(extra_instructions, previous, rulings)},
    )
    # ADR 0007: the notice joins the system message and the files follow it in one
    # user message, all inside the stable prefix (sorted, so re-synthesis can hit the
    # cache).
    return with_attachments(prompt, attachments)


# --------------------------------------------------------------------------- validation


def _validate_rulings(previous: Synthesis | None, rulings: Sequence[Ruling]) -> None:
    if not rulings:
        return
    if previous is None:
        raise ValueError("rulings require the previous synthesis")
    disputes = {d.id: d for d in previous.disputed}
    seen: set[tuple[str, str]] = set()
    for ruling in rulings:
        key = (ruling.kind, ruling.target)
        if key in seen:
            raise ValueError(f"duplicate ruling for {ruling.kind} {ruling.target!r}")
        seen.add(key)
        if ruling.kind == "dispute":
            point = disputes.get(ruling.target)
            if point is None:
                raise ValueError(f"unknown dispute id {ruling.target!r}")
            if ruling.decision not in {p.label for p in point.positions}:
                raise ValueError(
                    f"label {ruling.decision!r} holds no position in dispute {ruling.target!r}"
                )
            continue
        if not ruling.target.isdigit() or int(ruling.target) >= len(previous.unique):
            raise ValueError(f"bad unique insight index {ruling.target!r}")
        if ruling.decision not in {"include", "exclude"}:
            raise ValueError(f"bad unique decision {ruling.decision!r}")


def _clean(synthesis: Synthesis, labels: set[str]) -> Synthesis:
    if synthesis.final_text is None or not synthesis.final_text.strip():
        raise StructuredOutputError("chair returned no final_text")
    disputed: list[DisputedPoint] = []
    used_ids: set[str] = set()
    for point in synthesis.disputed:
        positions = [p for p in point.positions if p.label in labels]
        if len(positions) < _MIN_POSITIONS:
            continue
        point_id = point.id.strip() or f"d{len(disputed) + 1}"
        if point_id in used_ids:
            suffix = 2
            while f"{point_id}-{suffix}" in used_ids:
                suffix += 1
            point_id = f"{point_id}-{suffix}"
        used_ids.add(point_id)
        disputed.append(point.model_copy(update={"id": point_id, "positions": positions}))
    return synthesis.model_copy(update={"disputed": disputed})


# --------------------------------------------------------------------------- public


def estimate_chair(
    answers: Sequence[AnonAnswer],
    chair: ModelChoice,
    *,
    task: str,
    with_reviews: bool,
    extra_instructions: str | None,
    resynthesis: bool = False,
    attachments: Sequence[Attachment] = (),
    attachment_tokens: int | None = None,
) -> list[PlannedCall]:
    """Planned calls for the estimate (exactly one chair call).

    Review material that does not exist yet is estimated from the answer lengths.

    Args:
        answers: The labelled answers.
        chair: The chair model.
        task: The user's task.
        with_reviews: Whether review material will be part of the prompt.
        extra_instructions: The user's extra instructions.
        resynthesis: Add the previous synthesis and the rulings to the input.
        attachments: Files the chair receives (ADR 0007).
        attachment_tokens: The caller's estimate of the attachments' input tokens
            (it knows whether they go native or as text). ``None`` counts the text
            form of every attachment that has one.
    """
    prompt = _build_prompt(
        answers,
        task=task,
        agreement=None,
        reviews=None,
        extra_instructions=extra_instructions,
        previous=None,
        rulings=(),
        attachments=attachments,
    )
    input_tokens = estimate_tokens(
        "\n".join(m.content for m in (*prompt.stable_prefix, *prompt.variable_suffix))
    )
    if attachment_tokens is None:
        attachment_tokens = sum(
            estimate_tokens(fallback_block(a)) for a in attachments if a.fallback_text is not None
        )
    input_tokens += attachment_tokens
    answer_tokens = [estimate_tokens(a.text) for a in answers]
    longest = max(answer_tokens, default=0)
    if with_reviews:
        input_tokens += math.ceil(sum(answer_tokens) * _REVIEW_ALLOWANCE)
    low = min(MAX_OUTPUT_TOKENS, max(_MIN_OUTPUT_LOW, math.ceil(longest * 0.8) + 200))
    high = min(
        MAX_OUTPUT_TOKENS,
        max(low, math.ceil(longest * 1.5) + 150 * len(answers) + 500),
    )
    if resynthesis:
        input_tokens += high + _RULINGS_ALLOWANCE
    return [
        PlannedCall(
            stage=STAGE,
            provider=chair.provider,
            model_id=chair.model_id,
            input_tokens=input_tokens,
            output_tokens_low=low,
            output_tokens_high=high,
        )
    ]


def _normalize_quote(quote: str) -> str:
    return " ".join(quote.split()).strip(_EDGE_CHARS).casefold()


def build_additional_list(synthesis: Synthesis, reviews: ReviewOutcome | None) -> AdditionalList:
    """Assemble the additional list (see the module docstring)."""
    merged: dict[tuple[str, str], _Merged] = {}
    if reviews is not None:
        for reviewer in sorted(reviews.reviews):
            for target_review in reviews.reviews[reviewer].reviews:
                for issue in target_review.issues:
                    if issue.severity not in _SERIOUS:
                        continue
                    key = (target_review.target, _normalize_quote(issue.quote))
                    entry = merged.setdefault(
                        key, _Merged(issue.quote, issue.problem, issue.severity)
                    )
                    entry.reviewers.add(reviewer)
                    if _SEVERITY_RANK[issue.severity] < _SEVERITY_RANK[entry.severity]:
                        entry.severity = issue.severity
    flagged = [
        FlaggedIssue(
            target=target,
            quote=entry.quote,
            problem=entry.problem,
            severity=entry.severity,
            flagged_by=len(entry.reviewers),
        )
        for (target, _), entry in merged.items()
    ]
    flagged.sort(key=lambda f: (_SEVERITY_RANK[f.severity], -f.flagged_by))
    return AdditionalList(
        disputed=tuple(synthesis.disputed),
        unique=tuple(synthesis.unique),
        possibly_wrong=tuple(flagged),
    )


async def run_chair(
    answers: Sequence[AnonAnswer],
    chair: ModelChoice,
    *,
    task: str,
    agreement: AgreementOutcome | None,
    reviews: ReviewOutcome | None,
    extra_instructions: str | None,
    call: CallFn,
    previous: Synthesis | None = None,
    rulings: Sequence[Ruling] = (),
    attachments: Sequence[Attachment] = (),
) -> ChairOutcome:
    """Run the chair call (via ``structured_call``) and build the additional list.

    ``attachments`` (ADR 0007) are placed in a user message inside the stable prefix,
    after the system message (which gains the attachment notice), in sha256 order.
    The ``CallFn`` adapts them to the chair's backend (text only for ``cli``).

    Raises:
        StructuredOutputError: if the output stays invalid or ``final_text`` is null.
        ValueError: if there are no answers or a ruling does not match ``previous``.
    """
    if not answers:
        raise ValueError("the chair needs at least one answer")
    _validate_rulings(previous, rulings)
    prompt = _build_prompt(
        answers,
        task=task,
        agreement=agreement,
        reviews=reviews,
        extra_instructions=extra_instructions,
        previous=previous,
        rulings=rulings,
        attachments=attachments,
    )
    spec = CallSpec(
        stage=STAGE,
        provider=chair.provider,
        model_id=chair.model_id,
        prompt=prompt,
        response_schema=strict_json_schema(Synthesis),
        max_output_tokens=MAX_OUTPUT_TOKENS,
        tag="resynthesis" if previous is not None else "synthesis",
    )
    parsed, _ = await structured_call(call, spec, Synthesis)
    synthesis = _clean(parsed, {a.label for a in answers})
    return ChairOutcome(synthesis=synthesis, additional=build_additional_list(synthesis, reviews))
