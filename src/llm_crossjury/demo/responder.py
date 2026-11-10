"""Demo responder: answers every request from the canned script, chosen by its schema.

* No response schema: the provider's canned step-1 answer (whatever the task says), or,
  for a chat message (stage ``chat``), the canned follow-up reply.
* ``AgreementJudgment``: groups the segments listed in the prompt by their canned topic.
* ``PeerReview``: canned issues and strengths for each assigned target, and a ranking.
* ``Synthesis``: the canned chair output (consensus, disputes, unique insights and the
  final answer), with a re-synthesis applying the user's rulings.

Anonymous labels are shuffled per run, so the responder never assumes a label. It reads
the labels from the prompt and recognizes each answer by its canned text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from functools import cache

from llm_crossjury.core.providers.base import CompletionRequest
from llm_crossjury.core.schemas.agreement import AgreementJudgment, ClaimGroup, ClaimRef
from llm_crossjury.core.schemas.review import PeerReview, ReviewIssue, TargetReview
from llm_crossjury.core.schemas.synthesis import (
    ConsensusPoint,
    DisputedPoint,
    Position,
    Synthesis,
    UniqueInsight,
)
from llm_crossjury.core.synthesis.agreement import segment_answer
from llm_crossjury.core.synthesis.types import AnonAnswer
from llm_crossjury.demo import script

_SEGMENT_LINE = re.compile(r"^(Response [A-Z]+) / (s\d+): (.*)$", re.MULTILINE)
_ANSWER_HEADING = re.compile(r"^### (Response [A-Z]+)[ \t]*$", re.MULTILINE)
_SECTION_END = re.compile(r"^#{1,3} ", re.MULTILINE)
_TARGETS = re.compile(r"^Review these responses, and only these: (.+)$", re.MULTILINE)
_DISPUTE_RULING = re.compile(
    r"^- For dispute (\S+) \(.*\), adopt (Response [A-Z]+)'s position: (.*)$"
)
_UNIQUE_RULING = re.compile(
    r"^- (Include|Do not include) unique insight (\d+) \(from (Response [A-Z]+)\): (.*)$"
)
_FINGERPRINT_CHARS = 80
_CHAT_STAGE = "chat"
"""``tags["stage"]`` of a chat call (``core.chat_service.CHAT_STAGE``)."""
_MIN_SUPPORTERS = 2


def _normalize(text: str) -> str:
    return " ".join(text.split())


@cache
def _segment_topics() -> dict[str, tuple[str, str, script.Stance]]:
    """Normalized segment text -> (provider, topic key, stance) for every canned answer.

    Raises:
        ValueError: if a canned answer does not split into one segment per block.
    """
    index: dict[str, tuple[str, str, script.Stance]] = {}
    for provider, blocks in script.ANSWERS.items():
        segments = segment_answer(AnonAnswer(label="x", text=script.answer_text(provider)))
        if len(segments) != len(blocks):
            raise ValueError(f"demo answer {provider!r}: blocks and segments differ")
        for segment, block in zip(segments, blocks, strict=True):
            index[_normalize(segment.text)] = (provider, block.topic, block.stance)
    return index


def _provider_of(text: str) -> str | None:
    """Recognize a canned answer by its opening text."""
    head = _normalize(text)[:_FINGERPRINT_CHARS]
    for provider in script.ANSWERS:
        if _normalize(script.answer_text(provider))[:_FINGERPRINT_CHARS] == head:
            return provider
    return None


def _prompt_text(request: CompletionRequest) -> str:
    messages = (*request.prompt.stable_prefix, *request.prompt.variable_suffix)
    return "\n\n".join(m.content for m in messages)


def _answer_sections(text: str) -> dict[str, str]:
    """Label -> answer text for every ``### Response X`` section in ``text``."""
    sections: dict[str, str] = {}
    for match in _ANSWER_HEADING.finditer(text):
        rest = text[match.end() :]
        end = _SECTION_END.search(rest)
        sections[match.group(1)] = (rest[: end.start()] if end else rest).strip()
    return sections


def _labels_by_provider(text: str) -> dict[str, str]:
    """Provider -> anonymous label, for the canned answers found in ``text``."""
    found: dict[str, str] = {}
    for label, body in _answer_sections(text).items():
        provider = _provider_of(body)
        if provider is not None and provider not in found:
            found[provider] = label
    return found


# --------------------------------------------------------------------------- answer


def answer(provider: str) -> str:
    """The canned step-1 answer of ``provider`` (a short note for unknown providers)."""
    if provider in script.ANSWERS:
        return script.answer_text(provider)
    return "Demo mode has no canned answer for this provider."


# --------------------------------------------------------------------------- agreement


def agreement(text: str) -> AgreementJudgment:
    """Group the segments listed in an agreement prompt by their canned topic.

    Segments the script does not know become single-answer groups of their own.
    """
    known = _segment_topics()
    support: dict[str, list[ClaimRef]] = {}
    contra: dict[str, list[ClaimRef]] = {}
    extra: list[ClaimGroup] = []
    for label, segment_id, body in _SEGMENT_LINE.findall(text):
        ref = ClaimRef(label=label, claim_id=segment_id)
        hit = known.get(_normalize(body))
        if hit is None:
            extra.append(ClaimGroup(statement=body[:160], supporting=[ref], contradicting=[]))
            continue
        _, key, stance = hit
        (support if stance == "support" else contra).setdefault(key, []).append(ref)
    groups: list[ClaimGroup] = []
    for topic in script.TOPICS:
        pro, con = support.get(topic.key, []), contra.get(topic.key, [])
        if pro:
            groups.append(ClaimGroup(statement=topic.statement, supporting=pro, contradicting=con))
        elif con:
            statement = topic.counter or topic.statement
            groups.append(ClaimGroup(statement=statement, supporting=con, contradicting=[]))
    return AgreementJudgment(groups=[*groups, *extra])


# --------------------------------------------------------------------------- review


def review(text: str) -> PeerReview:
    """Canned reviews of the targets named in a review prompt, ranked by quality."""
    match = _TARGETS.search(text)
    targets = [t.strip() for t in match.group(1).split(",")] if match else []
    providers = {label: provider for provider, label in _labels_by_provider(text).items()}
    reviews: list[TargetReview] = []
    for target in targets:
        notes = script.REVIEW_NOTES.get(providers.get(target, ""))
        if notes is None:
            reviews.append(TargetReview(target=target, issues=[], strengths=["Clear answer."]))
            continue
        reviews.append(
            TargetReview(
                target=target,
                issues=[
                    ReviewIssue(
                        quote=issue.quote,
                        claim_id=None,
                        problem=issue.problem,
                        severity=issue.severity,
                        suggested_fix=issue.suggested_fix,
                    )
                    for issue in notes.issues
                ],
                strengths=list(notes.strengths),
            )
        )
    order = {provider: rank for rank, provider in enumerate(script.QUALITY_ORDER)}
    ranking = sorted(
        targets, key=lambda t: (order.get(providers.get(t, ""), len(order)), targets.index(t))
    )
    return PeerReview(reviews=reviews, ranking_rationale=script.RANKING_RATIONALE, ranking=ranking)


# --------------------------------------------------------------------------- chair


def _consensus(labels: Mapping[str, str]) -> list[ConsensusPoint]:
    holders: dict[str, list[str]] = {}
    for provider, blocks in script.ANSWERS.items():
        if provider not in labels:
            continue
        for block in blocks:
            if block.topic in script.CONSENSUS_TOPICS and block.stance == "support":
                names = holders.setdefault(block.topic, [])
                if labels[provider] not in names:
                    names.append(labels[provider])
    statements = {topic.key: topic.statement for topic in script.TOPICS}
    return [
        ConsensusPoint(statement=statements[topic], supporting=sorted(holders[topic]))
        for topic in script.CONSENSUS_TOPICS
        if len(holders.get(topic, [])) >= _MIN_SUPPORTERS
    ]


def _disputes(labels: Mapping[str, str]) -> list[DisputedPoint]:
    points: list[DisputedPoint] = []
    for dispute in script.DISPUTES:
        positions = [
            Position(label=labels[p.provider], stance=p.stance, argument=p.argument)
            for p in dispute.positions
            if p.provider in labels
        ]
        if len(positions) >= _MIN_SUPPORTERS:
            points.append(
                DisputedPoint(
                    id=f"d{len(points) + 1}",
                    question=dispute.question,
                    positions=positions,
                    chair_assessment=dispute.assessment,
                )
            )
    return points


def _apply_rulings(text: str, synthesis: Synthesis) -> Synthesis:
    """Drop decided disputes and included insights; add the decisions to the answer."""
    decided: set[str] = set()
    included: set[int] = set()
    notes: list[str] = []
    for line in text.splitlines():
        if (match := _DISPUTE_RULING.match(line)) is not None:
            decided.add(match.group(1))
            notes.append(f"- {match.group(3).strip()}")
        elif (match := _UNIQUE_RULING.match(line)) is not None and match.group(1) == "Include":
            included.add(int(match.group(2)) - 1)
            notes.append(f"- {match.group(4).strip()}")
    final = synthesis.final_text or ""
    if notes:
        final = final.rstrip() + "\n\n### Also consider\n\n" + "\n".join(notes) + "\n"
    return synthesis.model_copy(
        update={
            "disputed": [d for d in synthesis.disputed if d.id not in decided],
            "unique": [u for i, u in enumerate(synthesis.unique) if i not in included],
            "final_text": final,
        }
    )


def _previous(text: str) -> Synthesis | None:
    start = text.find("```json\n")
    if start == -1:
        return None
    end = text.find("\n```", start + 8)
    try:
        return Synthesis.model_validate(json.loads(text[start + 8 : end]))
    except ValueError:
        return None


def chair(text: str) -> Synthesis:
    """The canned synthesis for the answers found in a chair prompt."""
    labels = _labels_by_provider(text)
    if "## User's rulings" in text:
        previous = _previous(text)
        if previous is not None:
            return _apply_rulings(text, previous)
    synthesis = Synthesis(
        consensus=_consensus(labels),
        disputed=_disputes(labels),
        unique=[
            UniqueInsight(label=labels[provider], insight=insight)
            for provider, insight in script.UNIQUE_NOT_INCLUDED
            if provider in labels
        ],
        final_text=script.FINAL_TEXT,
    )
    return synthesis


# --------------------------------------------------------------------------- dispatch


def _schema_keys(schema: Mapping[str, object]) -> set[str]:
    properties = schema.get("properties")
    return set(properties) if isinstance(properties, Mapping) else set()


def respond(provider: str, request: CompletionRequest) -> str:
    """Reply to ``request`` as ``provider`` would in the demo script.

    Args:
        provider: Provider id of the demo model.
        request: The request; its ``response_schema`` selects the kind of reply.

    Returns:
        Free text for a step-1 answer, otherwise one JSON object matching the schema.
    """
    if request.response_schema is None:
        if request.tags.get("stage") == _CHAT_STAGE:
            return script.CHAT_REPLY
        return answer(provider)
    keys = _schema_keys(request.response_schema)
    text = _prompt_text(request)
    if "groups" in keys:
        return agreement(text).model_dump_json()
    if {"reviews", "ranking"} <= keys:
        return review(text).model_dump_json()
    if "final_text" in keys:
        return chair(text).model_dump_json()
    return "{}"


def known_segments() -> Sequence[str]:
    """Normalized texts of every canned segment (for tests)."""
    return tuple(_segment_topics())


__all__ = ["agreement", "answer", "chair", "known_segments", "respond", "review"]
