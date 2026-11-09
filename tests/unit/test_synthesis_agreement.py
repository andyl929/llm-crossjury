"""Agreement stage of the synthesis upgrade: segmentation, judge call, classification."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from llm_crossjury.core.cost.estimate import estimate_tokens
from llm_crossjury.core.orchestrator.agreement import agreement_ratio
from llm_crossjury.core.providers.base import Backend, CompletionResult, Role, Usage
from llm_crossjury.core.schemas.agreement import AgreementJudgment, ClaimGroup, ClaimRef
from llm_crossjury.core.schemas.export import strict_json_schema
from llm_crossjury.core.synthesis.agreement import (
    MAX_OUTPUT_TOKENS,
    PROMPT_ID,
    STAGE,
    build_agreement_spec,
    classify,
    estimate_agreement,
    run_agreement,
    segment_answer,
)
from llm_crossjury.core.synthesis.llm import StructuredOutputError
from llm_crossjury.core.synthesis.types import AnonAnswer, CallSpec, ModelChoice

JUDGE = ModelChoice(provider="fake", model_id="fake-judge")
A, B, C = "Response A", "Response B", "Response C"


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
        model="fake-judge",
        backend=Backend.MOCK,
        finish_reason="stop",
        started_at=datetime.now(UTC),
        latency_s=0.0,
        native_params_used={},
    )


class FakeCall:
    """A ``CallFn`` that replays canned replies and records the specs."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.specs: list[CallSpec] = []

    async def __call__(self, spec: CallSpec) -> CompletionResult:
        self.specs.append(spec)
        return _result(self.replies.pop(0))


def _ref(label: str, claim_id: str) -> ClaimRef:
    return ClaimRef(label=label, claim_id=claim_id)


def _group(
    statement: str,
    supporting: list[ClaimRef],
    contradicting: list[ClaimRef] | None = None,
) -> ClaimGroup:
    return ClaimGroup(statement=statement, supporting=supporting, contradicting=contradicting or [])


def _texts(text: str, label: str = A) -> list[str]:
    return [s.text for s in segment_answer(AnonAnswer(label=label, text=text))]


# --- agreement_ratio -------------------------------------------------------------


def test_ratio_none_for_no_groups() -> None:
    assert agreement_ratio(AgreementJudgment(groups=[]), [A, B]) is None


def test_ratio_counts_groups_backed_by_every_label_and_uncontradicted() -> None:
    judgment = AgreementJudgment(
        groups=[
            _group("all agree", [_ref(A, "s1"), _ref(B, "s1")]),
            _group("only A", [_ref(A, "s2")]),
            _group("all but contradicted", [_ref(A, "s3"), _ref(B, "s2")], [_ref(B, "s3")]),
            _group("all again", [_ref(B, "s4"), _ref(A, "s4"), _ref(A, "s5")]),
        ]
    )
    assert agreement_ratio(judgment, [A, B]) == pytest.approx(0.5)
    assert agreement_ratio(judgment, [A, B, C]) == pytest.approx(0.0)


# --- segment_answer --------------------------------------------------------------


def test_segments_split_paragraphs_and_list_items_with_sequential_ids() -> None:
    text = (
        "# Overview\n\n"
        "Python is a good first language for most people.\n"
        "It reads almost like plain English.\n\n"
        "Reasons:\n"
        "- The syntax is small and very consistent.\n"
        "* There is a huge standard library available.\n"
        "  It covers files, networking and more.\n"
        "1. Tooling such as uv makes setup easy.\n"
        "2) ok\n"
        "\n---\n\n"
        "In short, start with Python and expand later."
    )
    segments = segment_answer(AnonAnswer(label=B, text=text))
    assert [s.segment_id for s in segments] == ["s1", "s2", "s3", "s4", "s5"]
    assert all(s.label == B for s in segments)
    assert [s.text for s in segments] == [
        "Python is a good first language for most people. It reads almost like plain English.",
        "The syntax is small and very consistent.",
        "There is a huge standard library available. It covers files, networking and more.",
        "Tooling such as uv makes setup easy.",
        "In short, start with Python and expand later.",
    ]


def test_fenced_code_block_stays_one_segment() -> None:
    text = (
        "Use a list comprehension for this kind of task.\n"
        "```python\n"
        "x = 1\n"
        "\n"
        "- not a list item\n"
        "```\n"
        "That is all you really need here."
    )
    assert _texts(text) == [
        "Use a list comprehension for this kind of task.",
        "```python\nx = 1\n\n- not a list item\n```",
        "That is all you really need here.",
    ]


def test_unterminated_fence_keeps_rest_as_code() -> None:
    assert _texts("~~~\nprint(1)\n\nprint(2)") == ["~~~\nprint(1)\n\nprint(2)"]


def test_short_fragments_headings_and_empty_text_are_dropped() -> None:
    assert _texts("## Big heading with many words here\n\nYes.\n\nThree words only") == []
    assert _texts("") == []


def test_cjk_characters_count_as_words() -> None:
    assert _texts("Python是很好的入门语言。") == ["Python是很好的入门语言。"]
    assert _texts("好的") == []


# --- build / estimate ------------------------------------------------------------

ANSWERS = [
    AnonAnswer(label=A, text="Use PostgreSQL for this workload.\n\n- Add an index on user_id."),
    AnonAnswer(label=B, text="MongoDB fits this workload better.\n\nTiny."),
]


def test_spec_settings_and_prompt_layout() -> None:
    spec = build_agreement_spec(ANSWERS, JUDGE, task="Which database should I use?")
    assert spec.stage == STAGE
    assert (spec.provider, spec.model_id) == ("fake", "fake-judge")
    assert spec.max_output_tokens == MAX_OUTPUT_TOKENS == 16_000
    assert spec.response_schema == strict_json_schema(AgreementJudgment)
    assert PROMPT_ID == "synthesis/agreement.v1"

    (system,) = spec.prompt.stable_prefix
    assert system.role is Role.SYSTEM
    assert "PostgreSQL" not in system.content
    assert "Which database" not in system.content
    assert "same language as the task" in system.content

    (user,) = spec.prompt.variable_suffix
    assert user.role is Role.USER
    assert "Which database should I use?" in user.content
    assert "Response A / s1: Use PostgreSQL for this workload." in user.content
    assert "Response A / s2: Add an index on user_id." in user.content
    assert "Response B / s1: MongoDB fits this workload better." in user.content
    assert "Tiny." not in user.content
    assert "{segments}" not in user.content


def test_stable_prefix_is_independent_of_input() -> None:
    one = build_agreement_spec(ANSWERS, JUDGE, task="t1")
    two = build_agreement_spec(ANSWERS[:1], JUDGE, task="something else")
    assert one.prompt.stable_prefix == two.prompt.stable_prefix


def test_estimate_is_one_call_scaled_by_segments_and_capped() -> None:
    task = "Which database should I use?"
    (plan,) = estimate_agreement(ANSWERS, JUDGE, task=task)
    spec = build_agreement_spec(ANSWERS, JUDGE, task=task)
    text = "\n\n".join(
        m.content for m in (*spec.prompt.stable_prefix, *spec.prompt.variable_suffix)
    )
    assert plan.stage == STAGE
    assert (plan.provider, plan.model_id) == ("fake", "fake-judge")
    assert plan.input_tokens == estimate_tokens(text)
    assert 0 < plan.output_tokens_low <= plan.output_tokens_high <= MAX_OUTPUT_TOKENS

    many = [
        AnonAnswer(
            label=label,
            text="\n".join(f"- point number {i} is worth a mention" for i in range(200)),
        )
        for label in (A, B)
    ]
    (big,) = estimate_agreement(many, JUDGE, task=task)
    assert big.output_tokens_high == MAX_OUTPUT_TOKENS
    assert big.output_tokens_low > plan.output_tokens_low


# --- classify --------------------------------------------------------------------


def test_classify_categories_and_ratio() -> None:
    consensus = _group("c", [_ref(A, "s1"), _ref(B, "s1")])
    same_label_twice = _group("u2", [_ref(A, "s3"), _ref(A, "s4")])
    unique = _group("u", [_ref(B, "s2")])
    disputed = _group("d", [_ref(A, "s2")], [_ref(B, "s3")])
    only_contra = _group("d2", [], [_ref(B, "s4")])
    judgment = AgreementJudgment(
        groups=[consensus, same_label_twice, unique, disputed, only_contra]
    )

    outcome = classify(judgment, [A, B])
    assert outcome.segments == ()
    assert outcome.judgment == judgment
    assert outcome.consensus == (consensus,)
    assert outcome.unique == (same_label_twice, unique)
    assert outcome.disputed == (disputed, only_contra)
    assert outcome.ratio == pytest.approx(1 / 5)


# --- run_agreement ---------------------------------------------------------------


def _reply(groups: list[dict[str, object]]) -> str:
    return json.dumps({"groups": groups})


def _r(label: str, claim_id: str) -> dict[str, str]:
    return {"label": label, "claim_id": claim_id}


def test_run_agreement_validates_refs_and_classifies() -> None:
    reply = _reply(
        [
            {
                "statement": "A relational database is the right choice.",
                "supporting": [_r(A, "s1"), _r("Response Z", "s1"), _r(A, "s9")],
                "contradicting": [_r(B, "s1")],
            },
            {
                "statement": "Index the user column.",
                "supporting": [_r(A, "s2"), _r(A, "s2"), _r(A, "s1")],
                "contradicting": [],
            },
            {"statement": "Ghost.", "supporting": [_r(C, "s1")], "contradicting": []},
            {"statement": "Dup only.", "supporting": [_r(B, "s1")], "contradicting": []},
        ]
    )
    fake = FakeCall("Sure! " + reply)
    outcome = asyncio.run(run_agreement(ANSWERS, JUDGE, task="db?", call=fake))

    assert len(fake.specs) == 1
    assert [(s.label, s.segment_id) for s in outcome.segments] == [(A, "s1"), (A, "s2"), (B, "s1")]
    assert outcome.judgment.groups == [
        _group("A relational database is the right choice.", [_ref(A, "s1")], [_ref(B, "s1")]),
        _group("Index the user column.", [_ref(A, "s2")]),
    ]
    assert [g.statement for g in outcome.disputed] == ["A relational database is the right choice."]
    assert [g.statement for g in outcome.unique] == ["Index the user column."]
    assert outcome.consensus == ()
    assert outcome.ratio == pytest.approx(0.0)
    json.loads(outcome.model_dump_json())  # serializable for the UI


def test_run_agreement_retries_once_on_invalid_reply() -> None:
    good = _reply(
        [
            {
                "statement": "Both pick a database.",
                "supporting": [_r(A, "s1"), _r(B, "s1")],
                "contradicting": [],
            }
        ]
    )
    fake = FakeCall("not json at all", good)
    outcome = asyncio.run(run_agreement(ANSWERS, JUDGE, task="db?", call=fake))
    assert len(fake.specs) == 2
    assert fake.specs[1].tag.endswith(":retry")
    assert outcome.ratio == pytest.approx(1.0)
    assert len(outcome.consensus) == 1


def test_run_agreement_raises_after_second_invalid_reply() -> None:
    fake = FakeCall("nope", "still nope")
    with pytest.raises(StructuredOutputError):
        asyncio.run(run_agreement(ANSWERS, JUDGE, task="db?", call=fake))


def test_run_agreement_skips_judge_without_segments() -> None:
    fake = FakeCall()
    answers = [AnonAnswer(label=A, text="Ok."), AnonAnswer(label=B, text="# Title")]
    outcome = asyncio.run(run_agreement(answers, JUDGE, task="?", call=fake))
    assert fake.specs == []
    assert outcome.segments == ()
    assert outcome.judgment.groups == []
    assert outcome.ratio is None
