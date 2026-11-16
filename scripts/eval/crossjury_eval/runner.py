"""Run questions through crossjury (step 1 members, step 2 synthesis) and score them.

Every model call goes through the project's own services and adapters:
:func:`~llm_crossjury.core.compare_service.run_compare` for the members,
:func:`~llm_crossjury.core.synthesis_service.run_agreement_step` and
:func:`~llm_crossjury.core.synthesis_service.run_finish_step` for the synthesis, and
:func:`~llm_crossjury.core.compare_service.execute_budgeted_call` for the SimpleQA
grader. Spend is checked against a hard cap across the whole eval (:class:`EvalLedger`).
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from crossjury_eval.data import EvalItem
from crossjury_eval.grader import Grade, grader_prompt, parse_grade
from crossjury_eval.scoring import parse_letter, plurality_vote, strict_majority_correct
from llm_crossjury.core.compare_service import (
    BudgetRefusedError,
    CompareDeps,
    MemberResult,
    Selection,
    checked_price,
    estimate_compare,
    execute_budgeted_call,
    run_compare,
)
from llm_crossjury.core.config import BudgetConfig
from llm_crossjury.core.cost.ledger import Budget, BudgetExceededError, LedgerEntry
from llm_crossjury.core.providers.base import (
    Backend,
    CompletionRequest,
    Message,
    Prompt,
    ProviderError,
    ReasoningEffort,
    Role,
)
from llm_crossjury.core.registry.resolver import Role as ModelRole
from llm_crossjury.core.synthesis.types import ModelChoice, PlannedCall
from llm_crossjury.core.synthesis_service import (
    MAX_SEED,
    FinishRequest,
    FinishStepResult,
    StepOneAnswer,
    SynthesisRequest,
    SynthesisStageError,
    estimate_planned,
    estimate_synthesis,
    run_agreement_step,
    run_finish_step,
)

RECORD_VERSION = 1
ReviewPolicy = Literal["always", "auto", "never"]

GRADER_TIMEOUT_S = 300.0
GRADER_MAX_OUTPUT_TOKENS = 64
GRADER_ATTEMPTS = 2
ASSUMED_ANSWER_WORDS = 1200
"""Estimate only: assumed length (words) of each member answer when estimating the
synthesis before the answers exist."""

_current_question: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_question", default=None
)


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class ModelSpec:
    """A provider, backend and model (``provider:backend:model``)."""

    provider: str
    backend: Backend
    model: str

    @classmethod
    def parse(cls, text: str) -> ModelSpec:
        """Parse ``provider:backend:model`` (the model may itself contain ``:``).

        Raises:
            ValueError: malformed text or unknown backend.
        """
        parts = text.split(":", 2)
        if len(parts) != 3 or not all(p.strip() for p in parts):  # noqa: PLR2004
            raise ValueError(f"expected provider:backend:model, got {text!r}")
        provider, backend, model = (p.strip() for p in parts)
        return cls(provider=provider, backend=Backend(backend), model=model)

    def __str__(self) -> str:
        return f"{self.provider}:{self.backend.value}:{self.model}"


DEFAULT_MEMBERS: tuple[ModelSpec, ...] = (
    ModelSpec("anthropic", Backend.CLI, "claude-sonnet-5-5"),
    ModelSpec("openai", Backend.CLI, "gpt-6.1-sol"),
    ModelSpec("xai", Backend.CLI, "grok-4.7"),
    ModelSpec("gemini", Backend.API, "gemini-3.8-flash"),
)
DEFAULT_CHAIR = ModelSpec("anthropic", Backend.CLI, "claude-sonnet-5-5")
DEFAULT_GRADER = ModelSpec("anthropic", Backend.CLI, "claude-sonnet-5-5")


@dataclass(frozen=True)
class EvalConfig:
    """Settings of one eval run."""

    members: tuple[ModelSpec, ...] = DEFAULT_MEMBERS
    chair: ModelSpec = DEFAULT_CHAIR
    grader: ModelSpec = DEFAULT_GRADER
    seed: int = 7
    review: ReviewPolicy = "always"
    member_max_output_tokens: int = 16_000
    max_usd: Decimal = Decimal(8)
    concurrency: int = 2

    def manifest(self) -> dict[str, Any]:
        """JSON-serializable description (for the run manifest)."""
        return {
            "members": [str(m) for m in self.members],
            "chair": str(self.chair),
            "grader": str(self.grader),
            "seed": self.seed,
            "review": self.review,
            "member_max_output_tokens": self.member_max_output_tokens,
            "max_usd": str(self.max_usd),
            "concurrency": self.concurrency,
        }


# --------------------------------------------------------------------------- ledger


@dataclass
class QuestionSpend:
    """Calls and known cost attributed to one question."""

    cost: Decimal = Decimal(0)
    api_calls: int = 0
    cli_calls: int = 0
    unknown_cost_calls: int = 0
    by_stage: dict[str, int] = field(default_factory=dict)


class EvalLedger:
    """Wraps a ledger: attributes recorded calls to the current question and enforces a
    hard cap on the eval's total priced spend (``prior`` + this session + reserved).

    Implements the ``LedgerLike`` protocol of the compare service.
    """

    def __init__(self, inner: Any, *, max_usd: Decimal, prior: Decimal = Decimal(0)) -> None:
        self._inner = inner
        self.max_usd = max_usd
        self.prior = prior
        self.session_spent = Decimal(0)
        self._reserved: dict[str, Decimal] = {}
        self.questions: dict[str, QuestionSpend] = {}

    @property
    def total_spent(self) -> Decimal:
        """Priced spend of earlier sessions plus this one."""
        return self.prior + self.session_spent

    def record(self, entry: LedgerEntry) -> None:
        """Record in the inner ledger and attribute to the current question."""
        self._inner.record(entry)
        if entry.cost is not None:
            self.session_spent += entry.cost
        key = _current_question.get()
        if key is None:
            return
        spend = self.questions.setdefault(key, QuestionSpend())
        if entry.backend is Backend.CLI:
            spend.cli_calls += 1
        else:
            spend.api_calls += 1
        if entry.cost is None:
            spend.unknown_cost_calls += 1
        else:
            spend.cost += entry.cost
        spend.by_stage[entry.stage] = spend.by_stage.get(entry.stage, 0) + 1

    def spent_on(self, day: date) -> Decimal:
        """Delegated."""
        result: Decimal = self._inner.spent_on(day)
        return result

    def reserve(self, run_id: str, amount: Decimal) -> str:
        """Delegated; the amount also counts against the eval cap until released."""
        reservation: str = self._inner.reserve(run_id, amount)
        self._reserved[reservation] = amount
        return reservation

    def release(self, reservation_id: str) -> None:
        """Delegated."""
        self._inner.release(reservation_id)
        self._reserved.pop(reservation_id, None)

    def check(self, run_id: str, next_call_high: Decimal, budget: Budget) -> None:
        """The inner checks, then the eval cap.

        Raises:
            BudgetExceededError: scope ``eval`` when the call could pass ``max_usd``.
        """
        self._inner.check(run_id, next_call_high, budget)
        reserved = sum(self._reserved.values(), Decimal(0))
        if self.total_spent + reserved + next_call_high > self.max_usd:
            raise BudgetExceededError("eval", self.total_spent, reserved, self.max_usd)


def eval_budget(max_usd: Decimal) -> BudgetConfig:
    """Service budget for the eval: every limit equals the eval cap (the eval ledger
    is separate from the user's, so its daily total is the eval's own spend)."""
    return BudgetConfig(
        run_limit=max_usd, confirm_above=max_usd, daily_limit=max_usd, currency="USD"
    )


# --------------------------------------------------------------------------- helpers


def question_key(item: EvalItem) -> str:
    """Unique key of a question across datasets."""
    return f"{item.dataset}:{item.item_id}"


def synthesis_seed(seed: int, item_id: str) -> int:
    """Deterministic label-shuffle seed for one question."""
    digest = hashlib.sha256(f"{seed}:{item_id}".encode()).hexdigest()
    return int(digest[:8], 16) % (MAX_SEED + 1)


def selections(cfg: EvalConfig) -> list[Selection]:
    """Compare selections of the configured members."""
    return [
        Selection(
            provider=m.provider,
            backend=m.backend,
            model=m.model,
            max_output_tokens=cfg.member_max_output_tokens,
        )
        for m in cfg.members
    ]


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


GradeFn = Callable[[str, str, str], Awaitable[Grade]]
"""``(question, gold target, predicted answer) -> grade``."""


def make_grader(deps: CompareDeps, spec: ModelSpec) -> GradeFn:
    """Build the SimpleQA grader: one call per answer through the project's adapters,
    the official template as the only (user) message, budget-checked and recorded."""
    resolved = deps.resolver.resolve(
        member_id="grader",
        provider=spec.provider,
        model=spec.model,
        backend=spec.backend,
        role=ModelRole.ANSWERER,
        reasoning=ReasoningEffort.DEFAULT,
        native=None,
    )
    price = checked_price(spec.provider, resolved.model_id, spec.backend, deps)

    async def grade(question: str, target: str, predicted: str) -> Grade:
        content = grader_prompt(question, target, predicted)
        prompt = Prompt(
            stable_prefix=(),
            variable_suffix=(Message(role=Role.USER, content=content),),
        )
        call_high = Decimal(0)
        if price is not None:
            planned = PlannedCall(
                stage="grader",
                provider=spec.provider,
                model_id=resolved.model_id,
                input_tokens=deps.count_tokens(content),
                output_tokens_low=GRADER_MAX_OUTPUT_TOKENS,
                output_tokens_high=GRADER_MAX_OUTPUT_TOKENS,
            )
            call_high = estimate_planned([planned], deps, {spec.provider: spec.backend}).total_high
        request = CompletionRequest(
            model=resolved.model_id,
            prompt=prompt,
            response_schema=None,
            temperature=None,
            max_output_tokens=GRADER_MAX_OUTPUT_TOKENS,
            reasoning=resolved.effective_reasoning,
            native_params=dict(resolved.native_params),
            cache_prefix=False,
            timeout_s=GRADER_TIMEOUT_S,
            tags={"stage": "grader"},
        )
        last: Exception | None = None
        for _ in range(GRADER_ATTEMPTS):
            try:
                done = await execute_budgeted_call(
                    run_id=uuid.uuid4().hex,
                    stage="grader",
                    provider=spec.provider,
                    resolved=resolved,
                    pricing=price,
                    request=request,
                    call_high=call_high,
                    deps=deps,
                )
            except ProviderError as exc:
                last = exc
                continue
            return parse_grade(done.result.text)
        raise GraderError(f"grader failed: {last}")

    return grade


class GraderError(Exception):
    """The grader call failed on every attempt."""


class BudgetStopError(Exception):
    """The eval cap (or a service budget) stopped a question."""


# --------------------------------------------------------------------------- one question


def _member_entry(member: MemberResult) -> dict[str, Any]:
    return {
        "provider": member.provider,
        "backend": member.backend.value,
        "model": member.model,
        "ok": member.error is None,
        "error": member.error,
        "latency_s": member.latency_s,
        "cost_usd": _money(member.cost),
        "output_tokens": None if member.usage is None else member.usage.output_tokens,
        "text": member.text,
    }


def _flags(
    finish: FinishStepResult, agreement_ratio: float | None, disputed_groups: int
) -> dict[str, Any]:
    additional = finish.chair.additional
    return {
        "n_disputed": len(additional.disputed),
        "n_possibly_wrong": len(additional.possibly_wrong),
        "n_unique": len(additional.unique),
        "n_consensus": len(finish.chair.synthesis.consensus),
        "agreement_ratio": agreement_ratio,
        "agreement_disputed_groups": disputed_groups,
        "review_skipped": finish.review_skipped,
    }


async def _synthesize(
    item: EvalItem, members: Sequence[MemberResult], cfg: EvalConfig, deps: CompareDeps
) -> dict[str, Any]:
    answers = tuple(
        StepOneAnswer(provider=m.provider, backend=m.backend, model=m.model, text=m.text)
        for m in members
        if m.error is None and m.text and m.text.strip()
    )
    entry: dict[str, Any] = {"ok": False, "error": None, "stage": None, "final_text": None}
    if len(answers) < 2:  # noqa: PLR2004
        entry.update(error="fewer than 2 successful member answers", stage="input")
        return entry
    req = SynthesisRequest(
        task=item.task,
        answers=answers,
        seed=synthesis_seed(cfg.seed, item.item_id),
        chair=ModelChoice(provider=cfg.chair.provider, model_id=cfg.chair.model),
        chair_backend=cfg.chair.backend,
        review_tier="economy",
        allow_unknown_cost=True,
    )
    try:
        started = time.perf_counter()
        step1 = await run_agreement_step(req, deps)
        agreement_s = time.perf_counter() - started
        skip = cfg.review == "never" or (cfg.review == "auto" and step1.suggest_skip)
        finish = await run_finish_step(
            FinishRequest(
                **req.model_dump(),
                run_id=step1.run_id,
                agreement=step1.agreement,
                skip_review=skip,
            ),
            deps,
        )
    except SynthesisStageError as exc:
        if exc.code == "budget_stopped":
            raise BudgetStopError(str(exc)) from None
        entry.update(error=f"{exc.code}: {exc}", stage=exc.stage)
        return entry
    except BudgetRefusedError as exc:
        raise BudgetStopError(str(exc)) from None
    entry.update(
        ok=True,
        final_text=finish.chair.synthesis.final_text,
        agreement_s=round(agreement_s, 2),
        finish_s=round(time.perf_counter() - started - agreement_s, 2),
        **_flags(finish, step1.agreement.ratio, len(step1.agreement.disputed)),
    )
    return entry


async def _score_gpqa(record: dict[str, Any], item: EvalItem) -> None:
    letters: list[str | None] = []
    for member in record["members"]:
        letter = parse_letter(member["text"]) if member["ok"] else None
        member["answer"] = letter
        member["correct"] = letter == item.gold
        letters.append(letter)
    vote = plurality_vote(letters)
    record["majority"] = {"answer": vote, "correct": vote == item.gold}
    synthesis = record["synthesis"]
    final = parse_letter(synthesis["final_text"]) if synthesis["ok"] else None
    synthesis["answer"] = final
    synthesis["correct"] = synthesis["ok"] and final == item.gold


async def _score_simpleqa(record: dict[str, Any], item: EvalItem, grade: GradeFn) -> None:
    texts: list[str | None] = [m["text"] if m["ok"] else None for m in record["members"]]
    synthesis = record["synthesis"]
    texts.append(synthesis["final_text"] if synthesis["ok"] else None)

    async def one(text: str | None) -> Grade | None:
        if text is None or not text.strip():
            return None
        return await grade(item.question, item.gold, text)

    grades = await asyncio.gather(*(one(t) for t in texts))
    for member, member_grade in zip(record["members"], grades[:-1], strict=True):
        member["answer"] = member_grade
        member["correct"] = member_grade == "correct"
    record["majority"] = {
        "answer": None,
        "correct": strict_majority_correct([m["correct"] for m in record["members"]]),
    }
    synthesis["answer"] = grades[-1]
    synthesis["correct"] = grades[-1] == "correct"


async def run_question(
    item: EvalItem,
    cfg: EvalConfig,
    deps: CompareDeps,
    ledger: EvalLedger,
    grade: GradeFn | None,
) -> dict[str, Any]:
    """Run, synthesize and score one question; return its JSONL record.

    The record's ``status`` is ``ok`` (scored; member or synthesis failures are part of
    the result), ``error`` (harness failure: retried on the next run) or
    ``budget_stopped``.
    """
    key = question_key(item)
    token = _current_question.set(key)
    started = time.perf_counter()
    record: dict[str, Any] = {
        "version": RECORD_VERSION,
        "dataset": item.dataset,
        "item_id": item.item_id,
        "qhash": item.qhash,
        "gold": item.gold,
        "status": "ok",
        "error": None,
        "started_at": _now(),
    }
    try:
        compare = await run_compare(
            item.task, selections(cfg), deps, run_id=uuid.uuid4().hex, allow_unknown_cost=True
        )
        record["members"] = [_member_entry(m) for m in compare.members]
        if any(m.error and "budget would be exceeded" in m.error for m in compare.members):
            raise BudgetStopError("a member was not started: budget")
        record["synthesis"] = await _synthesize(item, compare.members, cfg, deps)
        if item.dataset == "gpqa":
            await _score_gpqa(record, item)
        else:
            if grade is None:
                raise GraderError("no grader configured")
            await _score_simpleqa(record, item, grade)
    except (BudgetStopError, BudgetRefusedError, BudgetExceededError) as exc:
        record.update(status="budget_stopped", error=str(exc))
    except GraderError as exc:
        record.update(status="error", error=str(exc))
    except Exception as exc:  # a harness bug must not lose the other questions
        record.update(status="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        _current_question.reset(token)
    spend = ledger.questions.get(key, QuestionSpend())
    record.update(
        finished_at=_now(),
        wall_s=round(time.perf_counter() - started, 2),
        cost_usd=str(spend.cost),
        api_calls=spend.api_calls,
        cli_calls=spend.cli_calls,
        calls_by_stage=dict(spend.by_stage),
    )
    return record


# --------------------------------------------------------------------------- estimate


async def estimate_question(
    item: EvalItem, cfg: EvalConfig, deps: CompareDeps
) -> tuple[Decimal, Decimal]:
    """Low and high estimate of the priced (``api``) cost of one question.

    Members are estimated exactly as the compare service does. The synthesis is
    estimated with synthetic answers of :data:`ASSUMED_ANSWER_WORDS` words (its real
    input does not exist yet); the SimpleQA grader is included when it is priced.
    """
    members = await estimate_compare(item.task, selections(cfg), deps)
    filler = " ".join(["word"] * ASSUMED_ANSWER_WORDS)
    answers = tuple(
        StepOneAnswer(
            provider=m.provider,
            backend=m.backend,
            model=deps.resolver.resolve_alias(m.provider, m.model),
            text=filler,
        )
        for m in cfg.members
    )
    synthesis = estimate_synthesis(
        SynthesisRequest(
            task=item.task,
            answers=answers,
            seed=synthesis_seed(cfg.seed, item.item_id),
            chair=ModelChoice(provider=cfg.chair.provider, model_id=cfg.chair.model),
            chair_backend=cfg.chair.backend,
            review_tier="economy",
            allow_unknown_cost=True,
        ),
        deps,
    )
    review_scale = 0 if cfg.review == "never" else 1
    low = members.total_low + synthesis.total_low
    high = members.total_high + synthesis.total_high
    if review_scale == 0:
        review = next(s.estimate for s in synthesis.stages if s.stage == "review")
        low -= review.total_low
        high -= review.total_high
    if item.dataset == "simpleqa" and cfg.grader.backend is not Backend.CLI:
        planned = PlannedCall(
            stage="grader",
            provider=cfg.grader.provider,
            model_id=cfg.grader.model,
            input_tokens=deps.count_tokens(filler) + 1500,
            output_tokens_low=GRADER_MAX_OUTPUT_TOKENS,
            output_tokens_high=GRADER_MAX_OUTPUT_TOKENS,
        )
        grader = estimate_planned(
            [planned] * (len(cfg.members) + 1), deps, {cfg.grader.provider: cfg.grader.backend}
        )
        low += grader.total_low
        high += grader.total_high
    return low, high


# --------------------------------------------------------------------------- results io


def read_records(path: Path) -> list[dict[str, Any]]:
    """All JSONL records in ``path`` (missing file = none; a torn last line is skipped)."""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def finished_keys(
    records: Iterable[Mapping[str, Any]], *, retry_failures: bool = False
) -> set[str]:
    """Keys (``dataset:item_id``) of questions that need no rerun.

    A question is finished when a record with ``status == "ok"`` exists. With
    ``retry_failures``, an ok record whose synthesis or any member failed does not count.
    """
    done: set[str] = set()
    for record in records:
        if record.get("status") != "ok":
            continue
        if retry_failures:
            synthesis_ok = bool(record.get("synthesis", {}).get("ok"))
            members_ok = all(m.get("ok") for m in record.get("members", []))
            if not (synthesis_ok and members_ok):
                continue
        done.add(f"{record.get('dataset')}:{record.get('item_id')}")
    return done


def prior_spend(records: Iterable[Mapping[str, Any]]) -> Decimal:
    """Priced spend of every earlier attempt (all statuses)."""
    return sum((Decimal(str(r.get("cost_usd") or 0)) for r in records), Decimal(0))


def pending_items(
    items: Sequence[EvalItem], records: Iterable[Mapping[str, Any]], *, retry_failures: bool
) -> list[EvalItem]:
    """Items without a finished record, in input order."""
    done = finished_keys(records, retry_failures=retry_failures)
    return [item for item in items if question_key(item) not in done]


class ResultWriter:
    """Appends one JSON line per question and flushes it at once (resumable)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def write(self, record: Mapping[str, Any]) -> None:
        """Append ``record`` as one line."""
        line = json.dumps(record, ensure_ascii=False, default=str)
        async with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()


# --------------------------------------------------------------------------- driver


Progress = Callable[[dict[str, Any]], None]


async def run_items(
    items: Sequence[EvalItem],
    cfg: EvalConfig,
    deps: CompareDeps,
    ledger: EvalLedger,
    writer: ResultWriter,
    *,
    grade: GradeFn | None,
    progress: Progress | None = None,
    runner: Callable[..., Awaitable[dict[str, Any]]] = run_question,
) -> list[dict[str, Any]]:
    """Run ``items`` with at most ``cfg.concurrency`` questions at once.

    After a budget stop no new question is started. Every record is written as soon as
    its question finishes.
    """
    semaphore = asyncio.Semaphore(max(1, cfg.concurrency))
    stopped = asyncio.Event()
    results: list[dict[str, Any]] = []

    async def one(item: EvalItem) -> None:
        async with semaphore:
            if stopped.is_set():
                return
            record = await runner(item, cfg, deps, ledger, grade)
            if record.get("status") == "budget_stopped":
                stopped.set()
            await writer.write(record)
            results.append(record)
            if progress is not None:
                progress(record)

    await asyncio.gather(*(one(item) for item in items))
    return results


def with_eval_ledger(deps: CompareDeps, ledger: EvalLedger, max_usd: Decimal) -> CompareDeps:
    """``deps`` with the eval ledger and the eval budget."""
    return replace(deps, ledger=ledger, budget=eval_budget(max_usd))
