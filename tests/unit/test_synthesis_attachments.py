"""Synthesis with attachments: only the chair receives them (ADR 0007, item 8)."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from llm_crossjury.core.attachments.prompt import ATTACHMENTS_HEADER
from llm_crossjury.core.attachments.render import (
    ATTACHMENT_NOTICE,
    fallback_block,
    sort_attachments,
)
from llm_crossjury.core.compare_service import CompareDeps, SelectionError
from llm_crossjury.core.providers.base import (
    AttachmentKind,
    Backend,
    CompletionRequest,
    CompletionResult,
    ReasoningEffort,
    Role,
)
from llm_crossjury.core.providers.mock import MockProvider
from llm_crossjury.core.synthesis import chair
from llm_crossjury.core.synthesis.types import AnonAnswer, CallSpec, ModelChoice
from llm_crossjury.core.synthesis_service import (
    FinishRequest,
    ResynthesisRequest,
    StepOneAnswer,
    SynthesisRequest,
    estimate_synthesis,
    run_finish_step,
    run_resynthesis,
)
from tests.unit._attachment_fakes import MemoryStore, built, caps
from tests.unit._compare_fakes import FakeCliProvider, make_deps
from tests.unit._synthesis_fakes import FakeStages, make_synthesis

REAL_RUN_CHAIR = chair.run_chair
REAL_ESTIMATE_CHAIR = chair.estimate_chair

TASK = "Compare the attached report with the notes."
PDF = built(AttachmentKind.PDF, name="report.pdf")
DOCX = built(AttachmentKind.DOCUMENT, name="notes.docx")
PNG = built(AttachmentKind.IMAGE, name="chart.png")
LABELS = ["Response A", "Response B"]


def _responder(req: CompletionRequest) -> str:
    if req.response_schema is not None:
        return make_synthesis(LABELS).model_dump_json()
    return "fine"


def _provider(name: str, *kinds: AttachmentKind) -> MockProvider:
    return MockProvider(name, _responder, caps(*kinds))


def _deps(store: MemoryStore, **kwargs: Any) -> CompareDeps:
    return dataclasses.replace(make_deps(**kwargs), attachment_store=store)


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch) -> FakeStages:
    """Fake agreement and review stages; the real chair stage."""
    fake = FakeStages().install(monkeypatch)
    monkeypatch.setattr(chair, "run_chair", REAL_RUN_CHAIR)
    monkeypatch.setattr(chair, "estimate_chair", REAL_ESTIMATE_CHAIR)
    return fake


def _answers(backend: Backend = Backend.API) -> tuple[StepOneAnswer, ...]:
    model = "anthropic-cli" if backend is Backend.CLI else "anthropic-model"
    return (
        StepOneAnswer(provider="anthropic", backend=backend, model=model, text="Answer one."),
        StepOneAnswer(provider="openai", model="openai-model", text="Answer two."),
    )


# --------------------------------------------------------------------------- chair stage


class FakeCall:
    def __init__(self) -> None:
        self.specs: list[CallSpec] = []

    async def __call__(self, spec: CallSpec) -> CompletionResult:
        self.specs.append(spec)
        return await MockProvider("x", _responder).complete(
            CompletionRequest(
                model=spec.model_id,
                prompt=spec.prompt,
                response_schema=spec.response_schema,
                temperature=None,
                max_output_tokens=None,
                reasoning=ReasoningEffort.DEFAULT,
                native_params={},
                cache_prefix=True,
                timeout_s=None,
                tags={},
            )
        )


def test_run_chair_puts_sorted_attachments_after_the_system_message() -> None:
    call = FakeCall()
    answers = [AnonAnswer(label=label, text="text") for label in LABELS]
    asyncio.run(
        REAL_RUN_CHAIR(
            answers,
            ModelChoice(provider="anthropic", model_id="m"),
            task=TASK,
            agreement=None,
            reviews=None,
            extra_instructions=None,
            call=call,
            attachments=[PNG.attachment, DOCX.attachment, PDF.attachment],
        )
    )
    system, files = call.specs[0].prompt.stable_prefix
    assert system.role is Role.SYSTEM
    assert system.content.endswith(ATTACHMENT_NOTICE)
    assert files.role is Role.USER
    assert files.content.startswith(ATTACHMENTS_HEADER)
    assert files.attachments == sort_attachments([PNG.attachment, DOCX.attachment, PDF.attachment])
    assert all(not m.attachments for m in call.specs[0].prompt.variable_suffix)


def test_estimate_chair_adds_attachment_tokens() -> None:
    answers = [AnonAnswer(label=label, text="text") for label in LABELS]
    model = ModelChoice(provider="anthropic", model_id="m")
    args: dict[str, Any] = {"task": TASK, "with_reviews": False, "extra_instructions": None}
    plain = REAL_ESTIMATE_CHAIR(answers, model, **args)[0].input_tokens
    text = REAL_ESTIMATE_CHAIR(answers, model, attachments=[DOCX.attachment], **args)[0]
    given = REAL_ESTIMATE_CHAIR(
        answers, model, attachments=[DOCX.attachment], attachment_tokens=5000, **args
    )[0]
    assert text.input_tokens > plain
    assert given.input_tokens > plain + 5000 - 1


# --------------------------------------------------------------------------- service


def _chair_requests(provider: MockProvider) -> list[CompletionRequest]:
    return [r for r in provider.requests if r.tags.get("stage") == chair.STAGE]


def test_only_the_chair_receives_the_attachments(stages: FakeStages) -> None:
    store = MemoryStore()
    ids = store.add(PDF, PNG)
    anthropic = _provider("anthropic", AttachmentKind.IMAGE, AttachmentKind.PDF)
    openai = _provider("openai")
    deps = _deps(store, providers={"anthropic": anthropic, "openai": openai})
    req = FinishRequest(
        task=TASK,
        answers=_answers(),
        seed=1,
        chair=ModelChoice(provider="anthropic", model_id="anthropic-model"),
        attachment_ids=tuple(ids),
    )
    result = asyncio.run(run_finish_step(req, deps))
    assert result.chair.synthesis.final_text
    chair_calls = _chair_requests(anthropic)
    assert len(chair_calls) == 1
    files = chair_calls[0].prompt.stable_prefix[1]
    assert files.attachments == sort_attachments([PDF.attachment, PNG.attachment])
    assert chair_calls[0].prompt.stable_prefix[0].content.endswith(ATTACHMENT_NOTICE)
    # Reviewers (fake review stage) got no attachments.
    others = [r for p in (anthropic, openai) for r in p.requests if r not in chair_calls]
    assert others
    for request in others:
        for message in (*request.prompt.stable_prefix, *request.prompt.variable_suffix):
            assert not message.attachments
            assert ATTACHMENT_NOTICE not in message.content


def test_resynthesis_chair_receives_the_attachments(stages: FakeStages) -> None:
    store = MemoryStore()
    ids = store.add(DOCX)
    anthropic = _provider("anthropic")
    deps = _deps(store, providers={"anthropic": anthropic, "openai": _provider("openai")})
    req = ResynthesisRequest(
        task=TASK,
        answers=_answers(),
        seed=1,
        chair=ModelChoice(provider="anthropic", model_id="anthropic-model"),
        attachment_ids=tuple(ids),
        previous=make_synthesis(LABELS),
    )
    asyncio.run(run_resynthesis(req, deps))
    (request,) = _chair_requests(anthropic)
    assert request.prompt.stable_prefix[1].attachments == (DOCX.attachment,)


def test_cli_chair_gets_inlined_text(stages: FakeStages) -> None:
    store = MemoryStore()
    ids = store.add(PDF, DOCX)
    cli = FakeCliProvider("anthropic", responder=_responder)
    deps = _deps(store, providers={"openai": _provider("openai")}, cli_providers={"anthropic": cli})
    req = FinishRequest(
        task=TASK,
        answers=_answers(Backend.CLI),
        seed=1,
        chair=ModelChoice(provider="anthropic", model_id="anthropic-cli"),
        attachment_ids=tuple(ids),
        allow_unknown_cost=True,
        skip_review=True,
    )
    asyncio.run(run_finish_step(req, deps))
    (request,) = _chair_requests(cli)
    files = request.prompt.stable_prefix[1]
    assert not files.attachments
    for item in (PDF, DOCX):
        assert fallback_block(item.attachment) in files.content


@pytest.mark.parametrize("backend", [Backend.CLI, Backend.API])
def test_chair_that_cannot_read_an_image_is_refused(stages: FakeStages, backend: Backend) -> None:
    store = MemoryStore()
    ids = store.add(PNG)
    deps = _deps(
        store,
        providers={"anthropic": _provider("anthropic"), "openai": _provider("openai")},
        cli_providers={"anthropic": FakeCliProvider("anthropic")},
    )
    model = "anthropic-cli" if backend is Backend.CLI else "anthropic-model"
    req = SynthesisRequest(
        task=TASK,
        answers=_answers(backend),
        seed=1,
        chair=ModelChoice(provider="anthropic", model_id=model),
        attachment_ids=tuple(ids),
    )
    with pytest.raises(SelectionError, match=r"chair cannot receive.*chart\.png") as info:
        estimate_synthesis(req, deps)
    assert "TOP-SECRET" not in str(info.value)


def test_synthesis_estimate_counts_attachments_for_the_chair_only(stages: FakeStages) -> None:
    store = MemoryStore()
    ids = store.add(PDF)
    deps = _deps(
        store,
        providers={
            "anthropic": _provider("anthropic", AttachmentKind.PDF),
            "openai": _provider("openai"),
        },
    )
    base = SynthesisRequest(
        task=TASK,
        answers=_answers(),
        seed=1,
        chair=ModelChoice(provider="anthropic", model_id="anthropic-model"),
    )
    plain = estimate_synthesis(base, deps)
    with_files = estimate_synthesis(base.model_copy(update={"attachment_ids": tuple(ids)}), deps)
    tokens = {s.stage: s.estimate.lines[0].input_tokens for s in plain.stages}
    tokens_files = {s.stage: s.estimate.lines[0].input_tokens for s in with_files.stages}
    assert tokens_files["chair"] > tokens["chair"]
    assert tokens_files["agreement"] == tokens["agreement"]
    assert tokens_files["review"] == tokens["review"]
