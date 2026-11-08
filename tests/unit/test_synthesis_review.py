"""Stage 2 of the synthesis upgrade: anonymous peer review with a fake CallFn."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from llm_crossjury.core.cost.estimate import estimate_tokens
from llm_crossjury.core.providers.base import Backend, CompletionResult, Usage
from llm_crossjury.core.schemas.agreement import AgreementJudgment, ClaimGroup, ClaimRef
from llm_crossjury.core.schemas.export import strict_json_schema
from llm_crossjury.core.schemas.review import PeerReview
from llm_crossjury.core.synthesis.agreement import AgreementOutcome
from llm_crossjury.core.synthesis.review import (
    MAX_OUTPUT_TOKENS,
    STAGE,
    ReviewOutcome,
    estimate_reviews,
    run_reviews,
)
from llm_crossjury.core.synthesis.types import AnonAnswer, CallSpec, ModelChoice

LABELS = ("Response A", "Response B", "Response C", "Response D")
TASK = "Explain why the sky is blue. Keep {responses} and {focus} literal."
_TARGETS_RE = re.compile(r"Review these responses, and only these: (.+)")

Reply = Callable[[CallSpec, list[str]], str]


def _answers(n: int) -> list[AnonAnswer]:
    # Deliberately out of label order: the prompt must sort them.
    return [AnonAnswer(label=label, text=f"Answer text of {label}.") for label in LABELS[:n]][::-1]


def _reviewers(n: int) -> dict[str, ModelChoice]:
    return {
        label: ModelChoice(provider=f"p{i}", model_id=f"m{i}") for i, label in enumerate(LABELS[:n])
    }


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
        model="fake-model",
        backend=Backend.MOCK,
        finish_reason="stop",
        started_at=datetime.now(UTC),
        latency_s=0.0,
        native_params_used={},
    )


def targets_of(spec: CallSpec) -> list[str]:
    match = _TARGETS_RE.search(spec.prompt.variable_suffix[0].content)
    assert match is not None
    return match.group(1).split(", ")


def good_reply(spec: CallSpec, targets: list[str]) -> str:
    """A valid review that ranks targets alphabetically."""
    return json.dumps(
        {
            "reviews": [
                {
                    "target": t,
                    "issues": [
                        {
                            "quote": f"Answer text of {t}.",
                            "claim_id": None,
                            "problem": "Too short.",
                            "severity": "minor",
                            "suggested_fix": None,
                        }
                    ],
                    "strengths": ["Clear."],
                }
                for t in targets
            ],
            "ranking_rationale": "Alphabetical.",
            "ranking": sorted(targets),
        }
    )


class FakeCall:
    """Records every spec; replies per reviewer tag (retries reuse the base tag)."""

    def __init__(self, replies: dict[str, Reply | Exception] | None = None) -> None:
        self.replies = replies or {}
        self.specs: list[CallSpec] = []

    async def __call__(self, spec: CallSpec) -> CompletionResult:
        self.specs.append(spec)
        await asyncio.sleep(0)
        reply = self.replies.get(spec.tag.removesuffix(":retry"), good_reply)
        if isinstance(reply, Exception):
            raise reply
        return _result(reply(spec, targets_of(spec)))


def _run(
    n: int,
    call: FakeCall,
    *,
    focus: AgreementOutcome | None = None,
    extra: str | None = None,
    seed: int = 7,
) -> ReviewOutcome:
    return asyncio.run(
        run_reviews(
            _answers(n),
            _reviewers(n),
            task=TASK,
            focus=focus,
            extra_instructions=extra,
            call=call,
            seed=seed,
        )
    )


def _focus() -> AgreementOutcome:
    def group(statement: str, sup: list[str], con: list[str]) -> ClaimGroup:
        return ClaimGroup(
            statement=statement,
            supporting=[ClaimRef(label=label, claim_id="s1") for label in sup],
            contradicting=[ClaimRef(label=label, claim_id="s2") for label in con],
        )

    consensus = group("Rayleigh scattering is involved.", ["Response A", "Response B"], [])
    disputed = group("Violet light dominates.", ["Response A"], ["Response B", "Response C"])
    unique_a = group("Ozone matters.", ["Response A"], [])
    unique_c = group("Dust matters.", ["Response C"], [])
    return AgreementOutcome(
        segments=(),
        judgment=AgreementJudgment(groups=[consensus, disputed, unique_a, unique_c]),
        consensus=(consensus,),
        disputed=(disputed,),
        unique=(unique_a, unique_c),
        ratio=0.5,
    )


def test_three_answers_full_topology_and_shared_prefix() -> None:
    call = FakeCall()
    outcome = _run(3, call)
    assert len(call.specs) == 3
    prefixes = {spec.prompt.stable_prefix for spec in call.specs}
    assert len(prefixes) == 1  # byte-identical across reviewers
    prefix = call.specs[0].prompt.stable_prefix[0].content
    positions = [prefix.index(f"### {label}\n\nAnswer text of {label}.") for label in LABELS[:3]]
    assert positions == sorted(positions)
    assert TASK in prefix  # braces in the task are not substituted
    for spec in call.specs:
        assert spec.stage == STAGE
        assert spec.response_schema == strict_json_schema(PeerReview)
        assert spec.max_output_tokens == MAX_OUTPUT_TOKENS
        targets = targets_of(spec)
        assert spec.tag not in targets
        assert len(targets) == 2
    assert outcome.failed_reviewers == ()
    assert set(outcome.reviews) == set(LABELS[:3])
    ranking = outcome.ranking
    # Alphabetical ranking by every reviewer: A wins (1, 1), C loses (0, 0).
    assert [r.label for r in ranking] == ["Response A", "Response B", "Response C"]
    assert [r.score for r in ranking] == [1.0, 0.5, 0.0]
    assert all(r.informative_reviews == 2 for r in ranking)


def test_two_answers_is_issues_only() -> None:
    call = FakeCall()
    outcome = _run(2, call)
    assert {spec.tag: targets_of(spec) for spec in call.specs} == {
        "Response A": ["Response B"],
        "Response B": ["Response A"],
    }
    assert outcome.ranking == ()
    assert len(outcome.reviews) == 2
    assert all(r.reviews[0].issues for r in outcome.reviews.values())


def test_four_answers_ring_k2() -> None:
    call = FakeCall()
    outcome = _run(4, call)
    assigned = {spec.tag: set(targets_of(spec)) for spec in call.specs}
    assert assigned == {
        "Response A": {"Response B", "Response C"},
        "Response B": {"Response C", "Response D"},
        "Response C": {"Response D", "Response A"},
        "Response D": {"Response A", "Response B"},
    }
    assert {r.label for r in outcome.ranking} == set(LABELS)


def test_failed_reviewers_do_not_fail_the_stage() -> None:
    call = FakeCall(
        {
            "Response A": RuntimeError("provider down"),
            "Response B": lambda spec, targets: "not json at all",
        }
    )
    outcome = _run(3, call)
    assert outcome.failed_reviewers == ("Response A", "Response B")
    assert set(outcome.reviews) == {"Response C"}
    # B was retried once by structured_call, then given up.
    assert [s.tag for s in call.specs].count("Response B:retry") == 1
    ranking = outcome.ranking
    assert [(r.label, r.informative_reviews) for r in ranking] == [
        ("Response A", 1),
        ("Response B", 1),
        ("Response C", 0),
    ]
    assert ranking[-1].score is None


def test_unassigned_targets_dropped_and_bad_ranking_excluded() -> None:
    def bad(spec: CallSpec, targets: list[str]) -> str:
        data = json.loads(good_reply(spec, [*targets, spec.tag]))  # reviews itself too
        data["ranking"] = [targets[0]]  # not a permutation
        return json.dumps(data)

    call = FakeCall({"Response A": bad})
    outcome = _run(3, call)
    review_a = outcome.reviews["Response A"]
    assert {r.target for r in review_a.reviews} == {"Response B", "Response C"}
    assert all(r.issues for r in review_a.reviews)  # issues kept
    assert review_a.ranking == []
    by_label = {r.label: r for r in outcome.ranking}
    assert by_label["Response B"].informative_reviews == 1  # only C's review counts
    assert by_label["Response C"].informative_reviews == 1


def test_focus_and_extra_instructions_in_suffix() -> None:
    call = FakeCall()
    _run(3, call, focus=_focus(), extra="Prefer peer-reviewed sources.")
    suffixes = {spec.tag: spec.prompt.variable_suffix[0].content for spec in call.specs}
    for suffix in suffixes.values():
        assert (
            "- [disputed] Violet light dominates. (supported by Response A; "
            "contradicted by Response B, Response C)"
        ) in suffix
        assert "Rayleigh" not in suffix  # consensus is not a focus point
        assert "Prefer peer-reviewed sources." in suffix
    # Unique points only for reviewers that review the response holding them.
    assert "Ozone matters." not in suffixes["Response A"]
    assert "- [unique] Ozone matters. (supported by Response A)" in suffixes["Response B"]
    assert "Dust matters." not in suffixes["Response C"]
    assert "Violet" not in call.specs[0].prompt.stable_prefix[0].content


def test_no_focus_and_no_extra_instructions() -> None:
    call = FakeCall()
    _run(3, call)
    suffix = call.specs[0].prompt.variable_suffix[0].content
    assert "No focus points were identified" in suffix
    assert suffix.rstrip().endswith("None.")


def test_target_order_is_deterministic_by_seed() -> None:
    def orders(seed: int) -> dict[str, list[str]]:
        call = FakeCall()
        _run(3, call, seed=seed)
        return {spec.tag: targets_of(spec) for spec in call.specs}

    assert orders(1) == orders(1)
    assert len({tuple(orders(s)["Response A"]) for s in range(20)}) == 2


def test_reviewers_run_concurrently() -> None:
    started = 0
    all_started = asyncio.Event()

    async def call(spec: CallSpec) -> CompletionResult:
        nonlocal started
        started += 1
        if started == 3:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)
        return _result(good_reply(spec, targets_of(spec)))

    outcome = asyncio.run(
        run_reviews(
            _answers(3),
            _reviewers(3),
            task=TASK,
            focus=None,
            extra_instructions=None,
            call=call,
            seed=0,
        )
    )
    assert outcome.failed_reviewers == ()


@pytest.mark.parametrize("n", [2, 3, 4])
def test_estimate_one_call_per_reviewer(n: int) -> None:
    planned = estimate_reviews(
        _answers(n), _reviewers(n), task=TASK, focus=_focus(), extra_instructions="x"
    )
    assert len(planned) == n
    call = FakeCall()
    _run(n, call, focus=_focus(), extra="x", seed=0)
    for plan in planned:
        assert plan.stage == STAGE
        assert 0 < plan.output_tokens_low <= plan.output_tokens_high <= MAX_OUTPUT_TOKENS
    by_model = {p.model_id: p for p in planned}
    for spec in call.specs:
        text = "\n\n".join(
            m.content for m in (*spec.prompt.stable_prefix, *spec.prompt.variable_suffix)
        )
        assert by_model[spec.model_id].input_tokens == estimate_tokens(text)


def test_estimate_skips_reviewers_without_targets_or_model() -> None:
    answers = _answers(2)
    only_a = {"Response A": ModelChoice(provider="p", model_id="m")}
    planned = estimate_reviews(answers, only_a, task=TASK, focus=None, extra_instructions=None)
    assert [p.model_id for p in planned] == ["m"]
    single = estimate_reviews(
        _answers(1), _reviewers(1), task=TASK, focus=None, extra_instructions=None
    )
    assert single == []


def test_estimate_output_scales_with_targets_and_is_capped() -> None:
    two = estimate_reviews(
        _answers(2), _reviewers(2), task=TASK, focus=None, extra_instructions=None
    )
    three = estimate_reviews(
        _answers(3), _reviewers(3), task=TASK, focus=None, extra_instructions=None
    )
    assert three[0].output_tokens_low > two[0].output_tokens_low
    assert three[0].output_tokens_high >= two[0].output_tokens_high
    assert all(p.output_tokens_high <= MAX_OUTPUT_TOKENS for p in three)
