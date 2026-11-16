"""Offline tests for the M1.7 eval harness (scripts/eval/crossjury_eval).

No network: datasets are fake rows, providers are mocks, synthesis stages are fakes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from crossjury_eval.data import (
    LETTERS,
    EvalItem,
    gpqa_items,
    gpqa_password,
    sample_items,
    shuffle_options,
    simpleqa_items,
)
from crossjury_eval.grader import grader_prompt, parse_grade, template
from crossjury_eval.runner import (
    EvalConfig,
    EvalLedger,
    ModelSpec,
    ResultWriter,
    _current_question,
    finished_keys,
    pending_items,
    prior_spend,
    read_records,
    run_items,
    run_question,
    with_eval_ledger,
)
from crossjury_eval.scoring import (
    flag_stats,
    parse_letter,
    plurality_vote,
    strict_majority_correct,
)
from crossjury_eval.summary import HEADLINE_FLAG, latest_records, summarize, to_markdown
from llm_crossjury.core.compare_service import CompareDeps
from llm_crossjury.core.cost.ledger import Budget, BudgetExceededError, LedgerEntry
from llm_crossjury.core.providers.base import Backend, CompletionRequest, ReasoningEffort, Usage
from llm_crossjury.core.providers.mock import MockProvider
from tests.unit._compare_fakes import FakeLedger, make_deps
from tests.unit._synthesis_fakes import FakeStages

# --------------------------------------------------------------------------- fixtures


def _gpqa_row(i: int) -> dict[str, str]:
    return {
        "Record ID": f"rec{i:03d}",
        "Question": f"Fake question {i}?",
        "Correct Answer": f"right {i}",
        "Incorrect Answer 1": f"wrong a {i}",
        "Incorrect Answer 2": f"wrong b {i}",
        "Incorrect Answer 3": f"wrong c {i}",
    }


def _simpleqa_rows(n: int) -> list[dict[str, str]]:
    return [{"problem": f"Who made thing {i}?", "answer": f"Person {i}"} for i in range(n)]


def _item(dataset: str, item_id: str, gold: str = "B") -> EvalItem:
    return EvalItem(
        dataset=dataset,  # type: ignore[arg-type]
        item_id=item_id,
        qhash="0" * 16,
        question="Q?",
        task="Q?",
        gold=gold,
    )


# --------------------------------------------------------------------------- sampling


def test_sampling_is_deterministic_and_prefix_stable() -> None:
    items = simpleqa_items(_simpleqa_rows(40))
    first = sample_items(items, 10, seed=7)
    again = sample_items(list(reversed(items)), 10, seed=7)
    assert [i.item_id for i in first] == [i.item_id for i in again]
    small = sample_items(items, 3, seed=7)
    assert [i.item_id for i in small] == [i.item_id for i in first[:3]]
    other = sample_items(items, 10, seed=8)
    assert [i.item_id for i in other] != [i.item_id for i in first]
    assert len({i.item_id for i in first}) == 10


def test_sampling_caps_at_population() -> None:
    items = simpleqa_items(_simpleqa_rows(4))
    assert len(sample_items(items, 50, seed=7)) == 4


def test_simpleqa_ids_follow_csv_rows() -> None:
    items = simpleqa_items(_simpleqa_rows(3))
    assert [i.item_id for i in items] == ["simpleqa-0", "simpleqa-1", "simpleqa-2"]
    assert items[1].task == "Who made thing 1?"
    assert items[1].gold == "Person 1"


# --------------------------------------------------------------------------- gpqa


def test_shuffle_options_is_deterministic_and_tracks_gold() -> None:
    mapping, gold = shuffle_options("right", ["w1", "w2", "w3"], seed=7, item_id="rec1")
    again, gold_again = shuffle_options("right", ["w1", "w2", "w3"], seed=7, item_id="rec1")
    assert mapping == again
    assert gold == gold_again
    assert mapping[gold] == "right"
    assert sorted(mapping) == list(LETTERS)
    assert sorted(mapping.values()) == ["right", "w1", "w2", "w3"]


def test_shuffle_spreads_the_gold_letter() -> None:
    golds = {
        shuffle_options("right", ["w1", "w2", "w3"], seed=7, item_id=f"rec{i}")[1]
        for i in range(40)
    }
    assert golds == set(LETTERS)


def test_gpqa_items_render_the_multiple_choice_task() -> None:
    items = gpqa_items([_gpqa_row(1), _gpqa_row(2)], seed=7)
    item = items[0]
    assert item.item_id == "rec001"
    assert item.task.startswith("Answer the following multiple choice question.")
    assert f"{item.gold}) right 1" in item.task
    assert all(f"{letter}) " in item.task for letter in LETTERS)
    assert len(item.qhash) == 16
    assert "Fake question 1" not in item.qhash


def test_gpqa_password_is_read_from_the_readme() -> None:
    readme = "intro\ndataset.zip is password-protected with this password: `abc-def`.\n"
    assert gpqa_password(readme) == "abc-def"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Reasoning...\nAnswer: C", "C"),
        ("answer: b", "B"),
        ("**Answer: D**", "D"),
        ("**Answer:** (A)", "A"),
        ("Answer: $B$", "B"),
        ("Response A says Answer: A, but\n\nAnswer: C", "C"),
        ("C", "C"),
        ("**B**", "B"),
        ("Answer: Because it is", None),
        ("No letter here", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_letter(text: str | None, expected: str | None) -> None:
    assert parse_letter(text) == expected


# --------------------------------------------------------------------------- voting


def test_plurality_vote() -> None:
    assert plurality_vote(["A", "A", "B", None]) == "A"
    assert plurality_vote(["A", "A", "B", "B"]) is None
    assert plurality_vote(["A", "B", "C", "D"]) is None
    assert plurality_vote([None, None]) is None
    assert plurality_vote(["C", None, None, None]) == "C"


def test_strict_majority() -> None:
    assert strict_majority_correct([True, True, True, False])
    assert not strict_majority_correct([True, True, False, False])
    assert not strict_majority_correct([])
    assert strict_majority_correct([True, True, False])


# --------------------------------------------------------------------------- flags


def test_flag_stats_math() -> None:
    rows = [(True, False), (True, False), (True, True), (False, True), (False, False)]
    rows += [(False, True)] * 5
    stats = flag_stats(rows)
    assert stats.total == 10
    assert stats.flagged == 3
    assert stats.coverage == pytest.approx(0.3)
    assert stats.flagged_error_rate == pytest.approx(2 / 3)
    assert stats.unflagged_error_rate == pytest.approx(1 / 7)
    assert stats.error_recall == pytest.approx(2 / 3)
    assert stats.lift == pytest.approx((2 / 3) / (1 / 7))


def test_flag_stats_empty_groups() -> None:
    stats = flag_stats([(False, True), (False, True)])
    assert stats.flagged_error_rate is None
    assert stats.unflagged_error_rate == 0
    assert stats.error_recall is None
    assert stats.lift is None
    assert flag_stats([]).coverage is None


# --------------------------------------------------------------------------- grader


def test_grader_template_is_the_official_text() -> None:
    text = template()
    assert text.startswith("Your job is to look at a question, a gold target")
    filled = grader_prompt("Q?", "Gold", "Pred")
    assert "Question: Q?\nGold target: Gold\nPredicted answer: Pred" in filled


@pytest.mark.parametrize(
    ("reply", "grade"),
    [("A", "correct"), ("B", "incorrect"), ("C", "not_attempted"), ("x", "not_attempted")],
)
def test_parse_grade(reply: str, grade: str) -> None:
    assert parse_grade(reply) == grade


# --------------------------------------------------------------------------- ledger


def _entry(cost: Decimal | None, backend: Backend = Backend.API) -> LedgerEntry:
    from datetime import UTC, datetime  # noqa: PLC0415

    return LedgerEntry(
        run_id="r",
        stage="answer",
        provider="gemini",
        model="m",
        backend=backend,
        reasoning=ReasoningEffort.DEFAULT,
        usage=Usage(
            input_tokens=1,
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=1,
            reasoning_tokens=0,
        ),
        cost=cost,
        currency="USD",
        batched=False,
        duration_s=1.0,
        recorded_at=datetime.now(UTC),
    )


_BUDGET = Budget(
    run_limit=Decimal(100), daily_limit=Decimal(100), currency="USD", allow_unknown_cost=False
)


def test_eval_ledger_caps_total_spend_and_attributes_calls() -> None:
    ledger = EvalLedger(FakeLedger(), max_usd=Decimal("1.00"), prior=Decimal("0.50"))
    token = _current_question.set("gpqa:x")
    try:
        ledger.record(_entry(Decimal("0.30")))
        ledger.record(_entry(None, Backend.CLI))
    finally:
        _current_question.reset(token)
    assert ledger.total_spent == Decimal("0.80")
    spend = ledger.questions["gpqa:x"]
    assert (spend.cost, spend.api_calls, spend.cli_calls) == (Decimal("0.30"), 1, 1)
    ledger.check("r", Decimal("0.15"), _BUDGET)
    reservation = ledger.reserve("r", Decimal("0.15"))
    with pytest.raises(BudgetExceededError):
        ledger.check("r", Decimal("0.10"), _BUDGET)
    ledger.release(reservation)
    ledger.check("r", Decimal("0.10"), _BUDGET)


# --------------------------------------------------------------------------- resume


def test_read_records_skips_torn_lines(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    path.write_text('{"dataset": "gpqa", "item_id": "a", "status": "ok"}\n{"datas', "utf-8")
    assert read_records(path) == [{"dataset": "gpqa", "item_id": "a", "status": "ok"}]
    assert read_records(tmp_path / "missing.jsonl") == []


def test_finished_and_pending() -> None:
    records: list[dict[str, Any]] = [
        {"dataset": "gpqa", "item_id": "a", "status": "ok", "cost_usd": "0.1",
         "synthesis": {"ok": True}, "members": [{"ok": True}]},
        {"dataset": "gpqa", "item_id": "b", "status": "error", "cost_usd": "0.2"},
        {"dataset": "gpqa", "item_id": "c", "status": "ok", "cost_usd": "0",
         "synthesis": {"ok": False}, "members": [{"ok": True}]},
    ]  # fmt: skip
    assert finished_keys(records) == {"gpqa:a", "gpqa:c"}
    assert finished_keys(records, retry_failures=True) == {"gpqa:a"}
    items = [_item("gpqa", x) for x in ("a", "b", "c", "d")]
    pending = pending_items(items, records, retry_failures=False)
    assert [i.item_id for i in pending] == ["b", "d"]
    assert prior_spend(records) == Decimal("0.3")


def _fake_runner(statuses: dict[str, str]) -> Callable[..., Any]:
    async def runner(item: EvalItem, *args: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {
            "dataset": item.dataset,
            "item_id": item.item_id,
            "status": statuses.get(item.item_id, "ok"),
            "cost_usd": "0.01",
        }

    return runner


def test_run_items_resumes_and_skips_finished(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    deps = make_deps()
    ledger = EvalLedger(FakeLedger(), max_usd=Decimal(1))
    cfg = EvalConfig(concurrency=2)
    items = [_item("gpqa", x) for x in ("a", "b", "c")]

    first = asyncio.run(
        run_items(
            items[:2], cfg, deps, ledger, ResultWriter(path), grade=None,
            runner=_fake_runner({"b": "error"}),
        )
    )  # fmt: skip
    assert len(first) == 2
    pending = pending_items(items, read_records(path), retry_failures=False)
    assert [i.item_id for i in pending] == ["b", "c"]
    asyncio.run(
        run_items(pending, cfg, deps, ledger, ResultWriter(path), grade=None,
                  runner=_fake_runner({}))
    )  # fmt: skip
    records = read_records(path)
    assert len(records) == 4
    assert pending_items(items, records, retry_failures=False) == []
    assert latest_records(records)["gpqa:b"]["status"] == "ok"


def test_run_items_stops_after_a_budget_stop(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    items = [_item("gpqa", x) for x in ("a", "b", "c", "d")]
    results = asyncio.run(
        run_items(
            items, EvalConfig(concurrency=1), make_deps(),
            EvalLedger(FakeLedger(), max_usd=Decimal(1)), ResultWriter(path), grade=None,
            runner=_fake_runner({"b": "budget_stopped"}),
        )
    )  # fmt: skip
    assert [r["item_id"] for r in results] == ["a", "b"]
    assert len(read_records(path)) == 2


# --------------------------------------------------------------------------- end to end


def _member_provider(name: str, reply: str) -> MockProvider:
    return MockProvider(name, lambda req: reply)


def _grader_provider() -> MockProvider:
    def respond(req: CompletionRequest) -> str:
        content = req.prompt.variable_suffix[-1].content
        if "Gold target:" in content:
            return "A" if "Predicted answer: Person" in content else "B"
        return "Person 1"

    return MockProvider("anthropic", respond)


def _deps(replies: dict[str, str]) -> tuple[CompareDeps, EvalLedger]:
    providers = {name: _member_provider(name, reply) for name, reply in replies.items()}
    providers["anthropic"] = _grader_provider()
    ledger = EvalLedger(FakeLedger(), max_usd=Decimal(5))
    deps = with_eval_ledger(make_deps(providers=providers), ledger, Decimal(5))
    return deps, ledger


_MEMBERS = tuple(
    ModelSpec(p, Backend.API, f"{p}-model") for p in ("anthropic", "openai", "gemini", "xai")
)
_CFG = EvalConfig(
    members=_MEMBERS,
    chair=ModelSpec("anthropic", Backend.API, "anthropic-model"),
    grader=ModelSpec("anthropic", Backend.API, "anthropic-model"),
)


def test_run_question_gpqa_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeStages(final_text="Synthesis.\n\nAnswer: B").install(monkeypatch)
    replies = {"openai": "Answer: B", "gemini": "Answer: C", "xai": "Answer: B"}
    deps, ledger = _deps(replies)
    record = asyncio.run(run_question(_item("gpqa", "rec1", gold="B"), _CFG, deps, ledger, None))
    assert record["status"] == "ok", record["error"]
    answers = {m["provider"]: m["answer"] for m in record["members"]}
    assert answers == {"anthropic": None, "openai": "B", "gemini": "C", "xai": "B"}
    assert record["majority"] == {"answer": "B", "correct": True}
    synthesis = record["synthesis"]
    assert synthesis["ok"]
    assert synthesis["answer"] == "B"
    assert synthesis["correct"]
    assert synthesis["n_disputed"] == 1
    assert record["api_calls"] > 4
    assert Decimal(record["cost_usd"]) > 0
    json.dumps(record)


def test_run_question_simpleqa_grades_each_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeStages(final_text="Person 1").install(monkeypatch)
    replies = {"openai": "Person 1", "gemini": "Somebody else", "xai": "Person 1"}
    deps, ledger = _deps(replies)

    async def grade(question: str, target: str, predicted: str) -> Any:
        return "correct" if predicted.startswith(target) else "incorrect"

    item = _item("simpleqa", "simpleqa-1", gold="Person 1")
    record = asyncio.run(run_question(item, _CFG, deps, ledger, grade))
    assert record["status"] == "ok", record["error"]
    grades = {m["provider"]: m["answer"] for m in record["members"]}
    assert grades == {
        "anthropic": "correct",
        "openai": "correct",
        "gemini": "incorrect",
        "xai": "correct",
    }
    assert record["majority"]["correct"] is True
    assert record["synthesis"]["answer"] == "correct"


def test_run_question_reports_budget_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeStages().install(monkeypatch)
    deps, ledger = _deps({"openai": "Answer: B", "gemini": "Answer: B", "xai": "Answer: B"})
    ledger.max_usd = Decimal("0.0001")
    record = asyncio.run(run_question(_item("gpqa", "rec1"), _CFG, deps, ledger, None))
    assert record["status"] == "budget_stopped"


# --------------------------------------------------------------------------- report


def _record(item_id: str, flagged: bool, synth: bool, members: list[bool]) -> dict[str, Any]:
    return {
        "dataset": "gpqa",
        "item_id": item_id,
        "status": "ok",
        "cost_usd": "0.02",
        "cli_calls": 3,
        "api_calls": 1,
        "wall_s": 10.0,
        "members": [
            {"provider": f"p{i}", "model": "m", "backend": "cli", "ok": True, "correct": c}
            for i, c in enumerate(members)
        ],
        "majority": {"correct": sum(members) * 2 > len(members)},
        "synthesis": {"ok": True, "correct": synth, "n_disputed": int(flagged)},
    }


def test_summary_and_markdown_have_no_text_and_right_numbers() -> None:
    records = [
        _record("a", True, False, [True, False, False]),
        _record("b", False, True, [True, True, False]),
        _record("c", False, True, [True, True, True]),
        _record("d", True, True, [False, True, False]),
    ]
    summary = summarize(records, project_n=50, concurrency=2)
    gpqa = summary["datasets"]["gpqa"]
    assert gpqa["members"]["p0/m (cli)"]["accuracy"] == pytest.approx(0.75)
    assert gpqa["best_single"]["member"] in {"p0/m (cli)", "p1/m (cli)"}
    assert gpqa["majority_vote"]["accuracy"] == pytest.approx(0.5)
    assert gpqa["synthesis"]["accuracy"] == pytest.approx(0.75)
    flag = gpqa["flags"][HEADLINE_FLAG]
    assert flag["coverage"] == pytest.approx(0.5)
    assert flag["flagged_error_rate"] == pytest.approx(0.5)
    assert flag["unflagged_error_rate"] == 0
    assert summary["projection"]["by_dataset"]["gpqa"]["usd"] == "1.00"
    markdown = to_markdown(summary)
    assert "crossjury synthesis" in markdown
    assert "Fake question" not in markdown
