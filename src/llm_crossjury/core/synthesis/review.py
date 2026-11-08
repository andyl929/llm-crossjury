"""Stage 2 of the synthesis upgrade: one round of anonymous peer review.

Each answer's own provider supplies one reviewer model (``reviewers`` maps the label
of the reviewer's *own* answer to the model that reviews; in the "economy" tier the
service passes that provider's ``fast`` model). Assignments come from
``llm_crossjury.core.orchestrator.topology.assign_reviews``: a reviewer never reviews
its own answer; ``FULL`` for <= 3 answers, ``RING`` with ``k = 2`` for >= 4.

Prompt caching: every reviewer's stable prefix holds the same instructions and ALL
anonymized answers (byte-identical across reviewers); the variable suffix names the
assigned targets, the focus points (disputed and unique statements from the agreement
stage, if any) and the user's extra instructions.

Rankings are aggregated with ``llm_crossjury.core.orchestrator.ranking.borda_aggregate``.
Invalid JSON or schema violations are retried once by ``structured_call``. A review
whose ranking is not a permutation of its targets is excluded from the Borda count
(its issues are still used).

INTERFACE FIXED by the integrator: implementations may add private helpers and
optional keyword arguments, but must not change these signatures or field names.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence

from llm_crossjury.core.cost.estimate import estimate_tokens
from llm_crossjury.core.orchestrator.ranking import borda_aggregate
from llm_crossjury.core.orchestrator.topology import (
    ReviewAssignment,
    ReviewTopology,
    assign_reviews,
)
from llm_crossjury.core.prompting import load_template
from llm_crossjury.core.providers.base import Message, Prompt, Role
from llm_crossjury.core.schemas.agreement import ClaimGroup, ClaimRef
from llm_crossjury.core.schemas.export import strict_json_schema
from llm_crossjury.core.schemas.review import PeerReview, TargetReview
from llm_crossjury.core.synthesis.agreement import AgreementOutcome
from llm_crossjury.core.synthesis.llm import structured_call
from llm_crossjury.core.synthesis.types import (
    AnonAnswer,
    CallFn,
    CallSpec,
    ModelChoice,
    PlannedCall,
    _Frozen,
)

STAGE = "review"
PROMPT_ID = "synthesis/review.v1"

MAX_OUTPUT_TOKENS = 3000
"""Output cap per reviewer call (product setting)."""

FULL_MAX_ANSWERS = 3
"""Up to this many answers every reviewer reviews every other one; above it, ``RING``."""

RING_K = 2
"""Targets per reviewer in ``RING``."""

_OUTPUT_LOW_BASE, _OUTPUT_LOW_PER_TARGET = 200, 350
_OUTPUT_HIGH_BASE, _OUTPUT_HIGH_PER_TARGET = 400, 900
_NO_FOCUS = (
    "No focus points were identified for your assigned responses. Review them in "
    "full, looking first for factual errors."
)
_NO_EXTRA = "None."


class RankedLabel(_Frozen):
    """Aggregated rank of one answer."""

    label: str
    score: float | None
    """Mean normalized Borda points (0..1); ``None`` if no informative review."""
    informative_reviews: int


class ReviewOutcome(_Frozen):
    """Result of the review stage (JSON-serializable, sent to the UI)."""

    reviews: dict[str, PeerReview]
    """Reviewer (label of its own answer) -> its review. Failed reviewers are absent."""
    ranking: tuple[RankedLabel, ...]
    """Best first; empty in "issues only" mode (every reviewer had one target)."""
    failed_reviewers: tuple[str, ...]
    """Reviewers whose call failed or whose output stayed invalid."""


def _ordered(answers: Sequence[AnonAnswer]) -> list[AnonAnswer]:
    return sorted(answers, key=lambda a: a.label)


def _assignments(
    answers: Sequence[AnonAnswer], reviewers: Mapping[str, ModelChoice], seed: int
) -> list[ReviewAssignment]:
    """Reviewer ids are the labels of their own answers; ``labels`` maps label to itself."""
    labels = [a.label for a in _ordered(answers)]
    members = [label for label in labels if label in reviewers]
    topology = ReviewTopology.FULL if len(labels) <= FULL_MAX_ANSWERS else ReviewTopology.RING
    return assign_reviews(
        members, {label: label for label in labels}, topology, ring_k=RING_K, seed=seed
    )


def _render_responses(answers: Sequence[AnonAnswer]) -> str:
    return "\n\n".join(f"### {a.label}\n\n{a.text.strip()}" for a in _ordered(answers))


def _labels(refs: Sequence[ClaimRef]) -> list[str]:
    return sorted({ref.label for ref in refs})


def _render_focus(focus: AgreementOutcome | None, targets: Sequence[str]) -> str:
    """Disputed and unique statements that involve at least one of ``targets``."""
    if focus is None:
        return _NO_FOCUS
    wanted = set(targets)
    lines: list[str] = []
    kinds: tuple[tuple[str, Sequence[ClaimGroup]], ...] = (
        ("disputed", focus.disputed),
        ("unique", focus.unique),
    )
    for kind, groups in kinds:
        for group in groups:
            supporting, contradicting = _labels(group.supporting), _labels(group.contradicting)
            if not wanted & {*supporting, *contradicting}:
                continue
            parts = [f"supported by {', '.join(supporting) or 'none'}"]
            if contradicting:
                parts.append(f"contradicted by {', '.join(contradicting)}")
            statement = " ".join(group.statement.split())
            lines.append(f"- [{kind}] {statement} ({'; '.join(parts)})")
    return "\n".join(lines) if lines else _NO_FOCUS


def _substitute(text: str, values: Mapping[str, str]) -> str:
    """Single-pass substitution, so a ``{name}`` inside a value is never expanded."""
    pattern = re.compile("|".join(re.escape("{" + name + "}") for name in values))
    return pattern.sub(lambda m: values[m.group(0)[1:-1]], text)


def _build_prompt(
    answers: Sequence[AnonAnswer],
    targets: Sequence[str],
    *,
    task: str,
    focus: AgreementOutcome | None,
    extra_instructions: str | None,
) -> Prompt:
    """Stable prefix: instructions, task and all answers (identical for every reviewer).

    Variable suffix: the shuffled targets, the focus points and the extra instructions.
    """
    template = load_template(PROMPT_ID)
    prefix = _substitute(
        template.stable_prefix,
        {"task": task.strip(), "responses": _render_responses(answers)},
    ).strip()
    suffix = _substitute(
        template.variable_suffix,
        {
            "targets": ", ".join(targets),
            "focus": _render_focus(focus, targets),
            "extra_instructions": (extra_instructions or "").strip() or _NO_EXTRA,
        },
    ).strip()
    return Prompt(
        stable_prefix=(Message(role=Role.SYSTEM, content=prefix),),
        variable_suffix=(Message(role=Role.USER, content=suffix),),
    )


def _build_spec(
    answers: Sequence[AnonAnswer],
    assignment: ReviewAssignment,
    model: ModelChoice,
    *,
    task: str,
    focus: AgreementOutcome | None,
    extra_instructions: str | None,
) -> CallSpec:
    prompt = _build_prompt(
        answers,
        assignment.targets,
        task=task,
        focus=focus,
        extra_instructions=extra_instructions,
    )
    return CallSpec(
        stage=STAGE,
        provider=model.provider,
        model_id=model.model_id,
        prompt=prompt,
        response_schema=strict_json_schema(PeerReview),
        max_output_tokens=MAX_OUTPUT_TOKENS,
        tag=assignment.reviewer,
    )


def _prompt_text(prompt: Prompt) -> str:
    return "\n\n".join(m.content for m in (*prompt.stable_prefix, *prompt.variable_suffix))


def estimate_reviews(
    answers: Sequence[AnonAnswer],
    reviewers: Mapping[str, ModelChoice],
    *,
    task: str,
    focus: AgreementOutcome | None,
    extra_instructions: str | None,
) -> list[PlannedCall]:
    """Planned calls for the estimate (one per reviewer with at least one target).

    Input tokens are estimated over the rendered prompt. Output tokens are a
    heuristic that grows with the number of targets, capped at
    :data:`MAX_OUTPUT_TOKENS`. Target order does not change the size, so the
    assignments use seed 0.
    """
    planned: list[PlannedCall] = []
    for assignment in _assignments(answers, reviewers, seed=0):
        model = reviewers[assignment.reviewer]
        spec = _build_spec(
            answers,
            assignment,
            model,
            task=task,
            focus=focus,
            extra_instructions=extra_instructions,
        )
        n = len(assignment.targets)
        planned.append(
            PlannedCall(
                stage=STAGE,
                provider=model.provider,
                model_id=model.model_id,
                input_tokens=estimate_tokens(_prompt_text(spec.prompt)),
                output_tokens_low=min(
                    _OUTPUT_LOW_BASE + _OUTPUT_LOW_PER_TARGET * n, MAX_OUTPUT_TOKENS
                ),
                output_tokens_high=min(
                    _OUTPUT_HIGH_BASE + _OUTPUT_HIGH_PER_TARGET * n, MAX_OUTPUT_TOKENS
                ),
            )
        )
    return planned


def _clean(review: PeerReview, targets: Sequence[str]) -> tuple[PeerReview, bool]:
    """Drop reviews of unassigned or repeated targets and check the ranking.

    Returns:
        The cleaned review and whether its ranking is a permutation of ``targets``.
        An invalid ranking is emptied so that nobody reads it later.
    """
    assigned = set(targets)
    seen: set[str] = set()
    kept: list[TargetReview] = []
    for target_review in review.reviews:
        if target_review.target in assigned and target_review.target not in seen:
            seen.add(target_review.target)
            kept.append(target_review)
    valid = len(review.ranking) == len(targets) and set(review.ranking) == assigned
    cleaned = PeerReview(
        reviews=kept,
        ranking_rationale=review.ranking_rationale,
        ranking=list(review.ranking) if valid else [],
    )
    return cleaned, valid


async def _review_one(call: CallFn, spec: CallSpec) -> PeerReview | None:
    """Run one reviewer; ``None`` if its call raised or its output stayed invalid."""
    try:
        review, _results = await structured_call(call, spec, PeerReview)
    except Exception:  # one failing reviewer must not fail the stage
        return None
    return review


def _ranking(
    rankable: Sequence[PeerReview], answers: Sequence[AnonAnswer]
) -> tuple[RankedLabel, ...]:
    results = borda_aggregate(rankable)
    if not any(r.informative_reviews for r in results):
        return ()
    ranked = [
        RankedLabel(label=r.label, score=r.score, informative_reviews=r.informative_reviews)
        for r in results
    ]
    present = {r.label for r in ranked}
    ranked.extend(
        RankedLabel(label=a.label, score=None, informative_reviews=0)
        for a in _ordered(answers)
        if a.label not in present
    )
    return tuple(ranked)


async def run_reviews(
    answers: Sequence[AnonAnswer],
    reviewers: Mapping[str, ModelChoice],
    *,
    task: str,
    focus: AgreementOutcome | None,
    extra_instructions: str | None,
    call: CallFn,
    seed: int,
) -> ReviewOutcome:
    """Assign, run reviewers concurrently, validate, aggregate rankings.

    One reviewer failing must not fail the stage. ``TargetReview`` entries for
    unassigned targets are dropped. A review whose ranking is not a permutation of
    its targets keeps its issues but is excluded from the Borda count, and its stored
    ``ranking`` is emptied. ``ranking`` is empty when no valid review ranked two or
    more targets ("issues only"); otherwise it lists every answer, with labels that
    no informative review ranked last and ``score=None``.
    """
    assignments = _assignments(answers, reviewers, seed)
    specs = [
        _build_spec(
            answers,
            assignment,
            reviewers[assignment.reviewer],
            task=task,
            focus=focus,
            extra_instructions=extra_instructions,
        )
        for assignment in assignments
    ]
    raw = await asyncio.gather(*(_review_one(call, spec) for spec in specs))
    reviews: dict[str, PeerReview] = {}
    rankable: list[PeerReview] = []
    failed: list[str] = []
    for assignment, review in zip(assignments, raw, strict=True):
        if review is None:
            failed.append(assignment.reviewer)
            continue
        cleaned, valid = _clean(review, assignment.targets)
        reviews[assignment.reviewer] = cleaned
        if valid:
            rankable.append(cleaned)
    return ReviewOutcome(
        reviews=reviews, ranking=_ranking(rankable, answers), failed_reviewers=tuple(failed)
    )
