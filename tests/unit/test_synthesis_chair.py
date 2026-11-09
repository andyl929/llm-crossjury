"""Chair stage of the synthesis upgrade: prompt, validation, rulings, additional list."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from llm_crossjury.core.providers.base import Backend, CompletionResult, Usage
from llm_crossjury.core.schemas._base import Severity
from llm_crossjury.core.schemas.agreement import AgreementJudgment, ClaimGroup, ClaimRef
from llm_crossjury.core.schemas.export import strict_json_schema
from llm_crossjury.core.schemas.review import PeerReview, ReviewIssue, TargetReview
from llm_crossjury.core.schemas.synthesis import (
    ConsensusPoint,
    DisputedPoint,
    Position,
    Synthesis,
    UniqueInsight,
)
from llm_crossjury.core.synthesis.agreement import AgreementOutcome
from llm_crossjury.core.synthesis.chair import (
    MAX_OUTPUT_TOKENS,
    STAGE,
    Ruling,
    build_additional_list,
    estimate_chair,
    run_chair,
)
from llm_crossjury.core.synthesis.llm import StructuredOutputError
from llm_crossjury.core.synthesis.review import RankedLabel, ReviewOutcome
from llm_crossjury.core.synthesis.types import AnonAnswer, CallSpec, ModelChoice

CHAIR = ModelChoice(provider="fake", model_id="fake-chair")
TASK = "How should I store passwords?"
ANSWERS = (
    AnonAnswer(label="Response A", text="Use bcrypt with a per-user salt."),
    AnonAnswer(label="Response B", text="Use Argon2id. MD5 is fine for legacy systems."),
    AnonAnswer(label="Response C", text="Use a slow hash such as scrypt or Argon2id."),
)


def _result(text: str) -> CompletionResult:
    return CompletionResult(
        text=text,
        usage=Usage(
            input_tokens=1,
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=1,
            reasoning_tokens=0,
        ),
        provider="fake",
        model="fake-chair",
        backend=Backend.MOCK,
        finish_reason="stop",
        started_at=datetime.now(UTC),
        latency_s=0.0,
        native_params_used={},
    )


def _ref(label: str, claim_id: str = "s1") -> ClaimRef:
    return ClaimRef(label=label, claim_id=claim_id)


def _agreement() -> AgreementOutcome:
    consensus = ClaimGroup(
        statement="Use a slow, salted password hash.",
        supporting=[_ref("Response A"), _ref("Response C"), _ref("Response C", "s2")],
        contradicting=[],
    )
    disputed = ClaimGroup(
        statement="MD5 is acceptable for legacy systems.",
        supporting=[_ref("Response B", "s2")],
        contradicting=[_ref("Response C")],
    )
    unique = ClaimGroup(
        statement="Use a per-user salt.",
        supporting=[_ref("Response A")],
        contradicting=[],
    )
    return AgreementOutcome(
        segments=(),
        judgment=AgreementJudgment(groups=[consensus, disputed, unique]),
        consensus=(consensus,),
        disputed=(disputed,),
        unique=(unique,),
        ratio=0.5,
    )


def _issue(quote: str, problem: str, severity: Severity) -> ReviewIssue:
    return ReviewIssue(
        quote=quote, claim_id=None, problem=problem, severity=severity, suggested_fix=None
    )


def _review(target: str, *issues: ReviewIssue) -> PeerReview:
    return PeerReview(
        reviews=[TargetReview(target=target, issues=list(issues), strengths=[])],
        ranking_rationale="",
        ranking=[target],
    )


def _reviews() -> ReviewOutcome:
    return ReviewOutcome(
        reviews={
            "Response C": _review(
                "Response B",
                _issue("MD5 is fine for legacy systems.", "MD5 is broken.", Severity.MAJOR),
                _issue("Use Argon2id.", "Name the parameters.", Severity.NIT),
            ),
            "Response A": _review(
                "Response B",
                _issue("  md5 is fine for   legacy systems ", "Insecure.", Severity.CRITICAL),
            ),
            "Response B": _review(
                "Response A",
                _issue("Use bcrypt", "bcrypt truncates long inputs.", Severity.MAJOR),
            ),
        },
        ranking=(
            RankedLabel(label="Response C", score=1.0, informative_reviews=2),
            RankedLabel(label="Response B", score=None, informative_reviews=0),
        ),
        failed_reviewers=(),
    )


def _synthesis(**overrides: Any) -> Synthesis:
    data: dict[str, Any] = {
        "consensus": [
            ConsensusPoint(
                statement="Use a slow hash.", supporting=["Response A", "Response C"]
            ).model_dump()
        ],
        "disputed": [
            DisputedPoint(
                id="d1",
                question="Is MD5 acceptable for legacy systems?",
                positions=[
                    Position(label="Response B", stance="MD5 is fine.", argument="Legacy."),
                    Position(label="Response C", stance="Never MD5.", argument="Broken."),
                ],
                chair_assessment="Follows Response C: MD5 is broken.",
            ).model_dump()
        ],
        "unique": [UniqueInsight(label="Response A", insight="Pepper the hash.").model_dump()],
        "final_text": "Use Argon2id with a per-user salt.",
    }
    data.update(overrides)
    return Synthesis.model_validate(data)


class FakeCall:
    """Records every spec and replies with queued texts."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.specs: list[CallSpec] = []

    async def __call__(self, spec: CallSpec) -> CompletionResult:
        self.specs.append(spec)
        return _result(self.replies.pop(0))


def _run(call: FakeCall, **kwargs: Any) -> Any:
    params: dict[str, Any] = {
        "task": TASK,
        "agreement": _agreement(),
        "reviews": _reviews(),
        "extra_instructions": "Keep it short.",
        "call": call,
    }
    params.update(kwargs)
    return asyncio.run(run_chair(ANSWERS, CHAIR, **params))


def test_run_chair_builds_spec_and_outcome() -> None:
    call = FakeCall(_synthesis().model_dump_json())
    outcome = _run(call)

    spec = call.specs[0]
    assert spec.stage == STAGE
    assert (spec.provider, spec.model_id) == ("fake", "fake-chair")
    assert spec.response_schema == strict_json_schema(Synthesis)
    assert spec.max_output_tokens == MAX_OUTPUT_TOKENS == 32_000

    prefix = spec.prompt.stable_prefix[0].content
    for answer in ANSWERS:
        assert f"### {answer.label}\n\n{answer.text}" in prefix
    assert TASK in prefix
    assert "- Use a slow, salted password hash. (supported by Response A, Response C)" in prefix
    assert "(supported by Response B; contradicted by Response C)" in prefix
    assert '[major] "MD5 is fine for legacy systems.": MD5 is broken.' in prefix
    assert "#### Issues in Response A" in prefix
    assert "1. Response C (score 1.00, 2 informative review(s))" in prefix
    assert "2. Response B (score n/a" in prefix
    assert "{material}" not in prefix
    assert "Keep it short." not in prefix

    suffix = spec.prompt.variable_suffix[0].content
    assert "Keep it short." in suffix
    assert "Previous synthesis" not in suffix

    assert outcome.synthesis.final_text == "Use Argon2id with a per-user salt."
    assert outcome.additional.disputed == tuple(outcome.synthesis.disputed)
    assert outcome.additional.unique == tuple(outcome.synthesis.unique)
    assert outcome.additional.possibly_wrong[0].flagged_by == 2
    assert json.loads(outcome.model_dump_json())["synthesis"]["final_text"]


def test_prompt_without_agreement_or_reviews() -> None:
    call = FakeCall(_synthesis().model_dump_json())
    outcome = _run(call, agreement=None, reviews=None, extra_instructions=None)
    prefix = call.specs[0].prompt.stable_prefix[0].content
    assert "No agreement analysis is available." in prefix
    assert "No peer review was run." in prefix
    assert "(none)" in call.specs[0].prompt.variable_suffix[0].content
    assert outcome.additional.possibly_wrong == ()


def test_resynthesis_keeps_prefix_and_renders_rulings() -> None:
    first_call = FakeCall(_synthesis().model_dump_json())
    first = _run(first_call)
    previous = first.synthesis

    second_call = FakeCall(_synthesis(disputed=[], unique=[]).model_dump_json())
    rulings = [
        Ruling(kind="dispute", target="d1", decision="Response B"),
        Ruling(kind="unique", target="0", decision="include"),
    ]
    second = _run(second_call, previous=previous, rulings=rulings)

    one, two = first_call.specs[0], second_call.specs[0]
    assert one.prompt.stable_prefix == two.prompt.stable_prefix
    suffix = two.prompt.variable_suffix[0].content
    assert previous.model_dump_json(indent=2) in suffix
    assert (
        "For dispute d1 (Is MD5 acceptable for legacy systems?), adopt Response B's "
        "position: MD5 is fine." in suffix
    )
    assert "Include unique insight 1 (from Response A): Pepper the hash." in suffix
    assert two.tag == "resynthesis"
    assert second.synthesis.disputed == []


@pytest.mark.parametrize("final_text", [None, "", "   \n"])
def test_missing_final_text_raises(final_text: str | None) -> None:
    call = FakeCall(_synthesis(final_text=final_text).model_dump_json())
    with pytest.raises(StructuredOutputError, match="final_text"):
        _run(call)


def test_invalid_json_twice_raises() -> None:
    call = FakeCall("not json", "still not json")
    with pytest.raises(StructuredOutputError):
        _run(call)
    assert len(call.specs) == 2


def test_unknown_position_labels_are_dropped() -> None:
    disputed = [
        {
            "id": "d1",
            "question": "Q1",
            "positions": [
                {"label": "Response A", "stance": "x", "argument": "a"},
                {"label": "Response Z", "stance": "y", "argument": "b"},
                {"label": "Response C", "stance": "z", "argument": "c"},
            ],
            "chair_assessment": "A.",
        },
        {
            "id": "d2",
            "question": "Q2",
            "positions": [
                {"label": "Response A", "stance": "x", "argument": "a"},
                {"label": "Model GPT", "stance": "y", "argument": "b"},
            ],
            "chair_assessment": None,
        },
        {
            "id": "d1",
            "question": "Q3",
            "positions": [
                {"label": "Response A", "stance": "x", "argument": "a"},
                {"label": "Response B", "stance": "y", "argument": "b"},
            ],
            "chair_assessment": None,
        },
    ]
    call = FakeCall(json.dumps({**_synthesis().model_dump(), "disputed": disputed}))
    outcome = _run(call)
    points = outcome.synthesis.disputed
    assert [p.id for p in points] == ["d1", "d1-2"]
    assert [pos.label for pos in points[0].positions] == ["Response A", "Response C"]
    assert outcome.additional.disputed == tuple(points)


@pytest.mark.parametrize(
    ("rulings", "with_previous"),
    [
        ([Ruling(kind="dispute", target="d9", decision="Response B")], True),
        ([Ruling(kind="dispute", target="d1", decision="Response A")], True),
        ([Ruling(kind="unique", target="1", decision="include")], True),
        ([Ruling(kind="unique", target="-1", decision="include")], True),
        ([Ruling(kind="unique", target="x", decision="include")], True),
        ([Ruling(kind="unique", target="0", decision="maybe")], True),
        (
            [
                Ruling(kind="dispute", target="d1", decision="Response B"),
                Ruling(kind="dispute", target="d1", decision="Response C"),
            ],
            True,
        ),
        ([Ruling(kind="dispute", target="d1", decision="Response B")], False),
    ],
)
def test_bad_rulings_raise_before_calling(rulings: list[Ruling], with_previous: bool) -> None:
    call = FakeCall()
    previous = _synthesis() if with_previous else None
    with pytest.raises(ValueError, match=r"."):
        _run(call, previous=previous, rulings=rulings)
    assert call.specs == []


def test_exclude_ruling_is_rendered() -> None:
    call = FakeCall(_synthesis().model_dump_json())
    _run(
        call,
        previous=_synthesis(),
        rulings=[Ruling(kind="unique", target="0", decision="exclude")],
    )
    suffix = call.specs[0].prompt.variable_suffix[0].content
    assert "Do not include unique insight 1 (from Response A)" in suffix


def test_no_answers_raises() -> None:
    with pytest.raises(ValueError, match="answer"):
        asyncio.run(
            run_chair(
                [],
                CHAIR,
                task=TASK,
                agreement=None,
                reviews=None,
                extra_instructions=None,
                call=FakeCall(),
            )
        )


def test_additional_list_merges_and_sorts_serious_issues() -> None:
    additional = build_additional_list(_synthesis(), _reviews())
    wrong = additional.possibly_wrong
    assert len(wrong) == 2  # the nit is ignored
    merged, single = wrong
    assert merged.target == "Response B"
    assert merged.flagged_by == 2
    assert merged.severity is Severity.CRITICAL
    # Reviewers are visited in sorted order: "Response A" reported first.
    assert merged.problem == "Insecure."
    assert single.target == "Response A"
    assert single.severity is Severity.MAJOR
    assert single.flagged_by == 1


def test_additional_list_counts_distinct_reviewers_and_sorts_by_count() -> None:
    twice = _review(
        "Response A",
        _issue("Q1", "first", Severity.MAJOR),
        _issue("q1", "again", Severity.MAJOR),
    )
    reviews = ReviewOutcome(
        reviews={
            "Response B": twice,
            "Response C": _review(
                "Response A",
                _issue("Other", "lone", Severity.MAJOR),
                _issue("Q1", "third", Severity.MAJOR),
            ),
            "Response D": _review("Response A", _issue("other", "x", Severity.MAJOR)),
            "Response E": _review("Response A", _issue("other", "y", Severity.MAJOR)),
        },
        ranking=(),
        failed_reviewers=(),
    )
    wrong = build_additional_list(_synthesis(), reviews).possibly_wrong
    assert [(w.quote, w.flagged_by, w.problem) for w in wrong] == [
        ("Other", 3, "lone"),
        ("Q1", 2, "first"),
    ]


def test_additional_list_without_reviews() -> None:
    additional = build_additional_list(_synthesis(), None)
    assert additional.possibly_wrong == ()
    assert len(additional.disputed) == 1
    assert len(additional.unique) == 1


def test_estimate_is_one_call_with_heuristics() -> None:
    base = estimate_chair(ANSWERS, CHAIR, task=TASK, with_reviews=False, extra_instructions=None)
    assert len(base) == 1
    plan = base[0]
    assert plan.stage == STAGE
    assert (plan.provider, plan.model_id) == ("fake", "fake-chair")
    assert 0 < plan.output_tokens_low <= plan.output_tokens_high <= MAX_OUTPUT_TOKENS
    assert plan.input_tokens > sum(len(a.text) for a in ANSWERS) // 3

    reviewed = estimate_chair(
        ANSWERS, CHAIR, task=TASK, with_reviews=True, extra_instructions=None
    )[0]
    assert reviewed.input_tokens > plan.input_tokens
    resynth = estimate_chair(
        ANSWERS, CHAIR, task=TASK, with_reviews=True, extra_instructions=None, resynthesis=True
    )[0]
    assert resynth.input_tokens > reviewed.input_tokens
    extra = estimate_chair(
        ANSWERS, CHAIR, task=TASK, with_reviews=False, extra_instructions="More detail " * 50
    )[0]
    assert extra.input_tokens > plan.input_tokens


def test_estimate_output_is_capped() -> None:
    long_answers = [AnonAnswer(label="Response A", text="word " * 60000)]
    plan = estimate_chair(
        long_answers, CHAIR, task=TASK, with_reviews=True, extra_instructions=None
    )[0]
    assert plan.output_tokens_low == plan.output_tokens_high == MAX_OUTPUT_TOKENS
