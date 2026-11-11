"""Smart page images: who receives derived images, prompts, receipts and estimates."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from llm_crossjury.core.attachments.prompt import (
    DERIVED_HEADER,
    add_derived_images,
    attachment_metas,
    attachment_tokens,
    derived_map,
    inline_attachments,
    prompt_attachment_tokens,
    receipts,
    select_derived_images,
    with_attachments,
)
from llm_crossjury.core.attachments.render import fallback_block
from llm_crossjury.core.attachments.tokens import estimate_attachment_tokens
from llm_crossjury.core.compare_service import (
    CompareDeps,
    Selection,
    estimate_compare_details,
    run_compare,
)
from llm_crossjury.core.providers.base import (
    AttachmentKind,
    Backend,
    CompletionRequest,
    Message,
    Prompt,
    Role,
)
from llm_crossjury.core.providers.mock import MockProvider
from llm_crossjury.core.synthesis import chair
from llm_crossjury.core.synthesis.types import ModelChoice
from llm_crossjury.core.synthesis_service import FinishRequest, StepOneAnswer, run_finish_step
from tests.unit._attachment_fakes import MemoryStore, built, caps, with_images
from tests.unit._compare_fakes import FakeCliProvider, make_deps
from tests.unit._synthesis_fakes import FakeStages, make_synthesis

TASK = "What does the chart on page 2 show?"
PDF = with_images(built(AttachmentKind.PDF, name="report.pdf"), 2)
DOCX = with_images(built(AttachmentKind.DOCUMENT, name="notes.docx"), 3)
PNG = built(AttachmentKind.IMAGE, name="photo.png")

REAL_RUN_CHAIR = chair.run_chair

PDF_NATIVE = frozenset({AttachmentKind.IMAGE, AttachmentKind.PDF})
IMAGES_ONLY = frozenset({AttachmentKind.IMAGE})
TEXT_ONLY: frozenset[AttachmentKind] = frozenset()

BASE = Prompt(
    stable_prefix=(Message(role=Role.SYSTEM, content="system"),),
    variable_suffix=(Message(role=Role.USER, content=TASK),),
)


def _files(prompt: Prompt) -> Message:
    return prompt.stable_prefix[1]


# --------------------------------------------------------------------------- the rule


@pytest.mark.parametrize("backend", [Backend.API, Backend.CLI])
def test_receipt_modes_per_capability(backend: Backend) -> None:
    items = [PDF, DOCX]
    native = {r.filename: (r.mode, r.images) for r in receipts(items, PDF_NATIVE, backend)}
    assert native == {"report.pdf": ("native", 0), "notes.docx": ("text+images", 3)}
    images = {r.filename: (r.mode, r.images) for r in receipts(items, IMAGES_ONLY, backend)}
    assert images == {"report.pdf": ("text+images", 2), "notes.docx": ("text+images", 3)}
    text = {r.filename: (r.mode, r.images) for r in receipts(items, TEXT_ONLY, backend)}
    assert text == {"report.pdf": ("text", 0), "notes.docx": ("text", 0)}
    refused = receipts([PNG], TEXT_ONLY, backend)
    assert refused[0].mode == "refused"


def test_pdf_native_member_gets_only_the_original() -> None:
    prompt = with_attachments(BASE, [PDF.attachment])
    out = add_derived_images(prompt, derived_map([PDF]), PDF_NATIVE)
    assert out == prompt


def test_text_only_member_gets_no_images() -> None:
    prompt = with_attachments(BASE, [PDF.attachment])
    assert add_derived_images(prompt, derived_map([PDF]), TEXT_ONLY) == prompt


def test_image_only_api_member_gets_text_and_page_images() -> None:
    prompt = with_attachments(BASE, [DOCX.attachment, PDF.attachment])
    out = _files(add_derived_images(prompt, derived_map([PDF, DOCX]), IMAGES_ONLY))
    expected = []
    for original in _files(prompt).attachments:
        source = PDF if original == PDF.attachment else DOCX
        expected += [original, *source.derived_images]
    assert out.attachments == tuple(expected)
    assert DERIVED_HEADER in out.content
    assert "- report.pdf#page-2.png (image/png)" in out.content


def test_image_only_cli_member_gets_text_inline_and_images_attached() -> None:
    prompt = with_attachments(BASE, [PDF.attachment])
    out = _files(
        inline_attachments(add_derived_images(prompt, derived_map([PDF]), IMAGES_ONLY), IMAGES_ONLY)
    )
    assert out.attachments == PDF.derived_images
    assert fallback_block(PDF.attachment) in out.content
    assert "report.pdf#page-1.png" in out.content


def test_member_byte_budget_drops_later_images() -> None:
    big = with_images(built(AttachmentKind.PDF, name="big.pdf"), 3, size=1000)
    selected = select_derived_images(
        [big.attachment], derived_map([big]), IMAGES_ONLY, max_bytes=2500
    )
    assert selected == {big.attachment.sha256: big.derived_images[:2]}
    # Native originals count first.
    photo = built(AttachmentKind.IMAGE, name="p.png", payload=b"x" * 2000)
    selected = select_derived_images(
        [photo.attachment, big.attachment], derived_map([big]), IMAGES_ONLY, max_bytes=2500
    )
    assert selected == {}


def test_receipt_notes_images_left_out_for_size(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_crossjury.core.attachments import prompt as prompt_module  # noqa: PLC0415

    monkeypatch.setattr(prompt_module, "MAX_TOTAL_BYTES", 150)
    (receipt,) = receipts([PDF], IMAGES_ONLY, Backend.API)
    assert (receipt.mode, receipt.images) == ("text+images", 1)
    assert any("1 derived image(s) were left out" in w for w in receipt.warnings)


# --------------------------------------------------------------------------- estimates


def test_estimate_adds_the_images() -> None:
    text = attachment_tokens([PDF], provider="xai", input_modalities=TEXT_ONLY, backend=Backend.API)
    images = attachment_tokens(
        [PDF], provider="xai", input_modalities=IMAGES_ONLY, backend=Backend.API
    )
    metas = attachment_metas([PDF])
    per_image = [
        estimate_attachment_tokens(img, metas[img.sha256], "xai", native=True)
        for img in PDF.derived_images
    ]
    pdf_id = PDF.attachment.sha256
    assert images[pdf_id] == (text[pdf_id] or 0) + sum(per_image)
    assert all(t > 0 for t in per_image)


def test_prompt_estimate_counts_derived_images() -> None:
    prompt = add_derived_images(
        with_attachments(BASE, [PDF.attachment]), derived_map([PDF]), IMAGES_ONLY
    )
    with_images_total = prompt_attachment_tokens(
        prompt,
        attachment_metas([PDF]),
        provider="xai",
        input_modalities=IMAGES_ONLY,
        backend=Backend.API,
    )
    expected = attachment_tokens(
        [PDF], provider="xai", input_modalities=IMAGES_ONLY, backend=Backend.API
    )
    assert with_images_total == expected[PDF.attachment.sha256]


# --------------------------------------------------------------------------- compare


def _deps(store: MemoryStore, **kwargs: Any) -> CompareDeps:
    return dataclasses.replace(make_deps(**kwargs), attachment_store=store)


def _provider(name: str, kinds: frozenset[AttachmentKind]) -> MockProvider:
    return MockProvider(name, lambda req: f"{name} answer", caps(*kinds))


def test_compare_delivers_per_member_capability() -> None:
    store = MemoryStore()
    ids = store.add(PDF)
    anthropic = _provider("anthropic", PDF_NATIVE)
    xai = _provider("xai", IMAGES_ONLY)
    openai = _provider("openai", TEXT_ONLY)
    codex = FakeCliProvider("openai", capabilities=caps(AttachmentKind.IMAGE))
    deps = _deps(
        store,
        providers={"anthropic": anthropic, "xai": xai, "gemini": openai},
        cli_providers={"openai": codex},
    )
    selections = [
        Selection(provider="anthropic", model="balanced"),
        Selection(provider="xai", model="balanced"),
        Selection(provider="gemini", model="balanced"),
        Selection(provider="openai", backend=Backend.CLI, model="openai-cli"),
    ]
    result = asyncio.run(
        run_compare(TASK, selections, deps, attachment_ids=ids, allow_unknown_cost=True)
    )
    modes = {m.provider: [(r.mode, r.images) for r in m.attachments] for m in result.members}
    assert modes == {
        "anthropic": [("native", 0)],
        "xai": [("text+images", 2)],
        "gemini": [("text", 0)],
        "openai": [("text+images", 2)],
    }
    assert _files(anthropic.requests[0].prompt).attachments == (PDF.attachment,)
    assert _files(xai.requests[0].prompt).attachments == (PDF.attachment, *PDF.derived_images)
    assert _files(openai.requests[0].prompt).attachments == (PDF.attachment,)
    cli_files = _files(codex.requests[0].prompt)
    assert cli_files.attachments == PDF.derived_images
    assert fallback_block(PDF.attachment) in cli_files.content


def test_compare_estimate_lines_include_images() -> None:
    store = MemoryStore()
    ids = store.add(PDF)
    deps = _deps(
        store,
        providers={"xai": _provider("xai", IMAGES_ONLY), "gemini": _provider("gemini", TEXT_ONLY)},
    )
    sel = [
        Selection(provider="xai", model="balanced"),
        Selection(provider="gemini", model="balanced"),
    ]
    details = asyncio.run(estimate_compare_details(TASK, sel, deps, attachment_ids=ids))
    lines = {line.provider: line for line in details.attachments}
    assert (lines["xai"].mode, lines["xai"].images, lines["xai"].kind) == (
        "text+images",
        2,
        AttachmentKind.PDF,
    )
    assert (lines["gemini"].mode, lines["gemini"].images) == ("text", 0)
    assert (lines["xai"].tokens or 0) > (lines["gemini"].tokens or 0)


# --------------------------------------------------------------------------- synthesis


def _responder(req: CompletionRequest) -> str:
    if req.response_schema is not None:
        return make_synthesis(["Response A", "Response B"]).model_dump_json()
    return "fine"


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch) -> FakeStages:
    fake = FakeStages().install(monkeypatch)
    monkeypatch.setattr(chair, "run_chair", REAL_RUN_CHAIR)
    return fake


def _answers(backend: Backend) -> tuple[StepOneAnswer, ...]:
    model = "openai-cli" if backend is Backend.CLI else "openai-model"
    return (
        StepOneAnswer(provider="openai", backend=backend, model=model, text="Answer one."),
        StepOneAnswer(provider="anthropic", model="anthropic-model", text="Answer two."),
    )


@pytest.mark.usefixtures("stages")
@pytest.mark.parametrize("backend", [Backend.API, Backend.CLI])
def test_chair_with_image_input_gets_page_images(backend: Backend) -> None:
    store = MemoryStore()
    ids = store.add(PDF)
    chair_provider = (
        FakeCliProvider("openai", responder=_responder, capabilities=caps(AttachmentKind.IMAGE))
        if backend is Backend.CLI
        else MockProvider("openai", _responder, caps(AttachmentKind.IMAGE))
    )
    others = {"anthropic": MockProvider("anthropic", _responder, caps())}
    deps = _deps(
        store,
        providers={**others, **({"openai": chair_provider} if backend is Backend.API else {})},
        cli_providers={"openai": chair_provider} if backend is Backend.CLI else None,
    )
    model = "openai-cli" if backend is Backend.CLI else "openai-model"
    req = FinishRequest(
        task=TASK,
        answers=_answers(backend),
        seed=1,
        chair=ModelChoice(provider="openai", model_id=model),
        attachment_ids=tuple(ids),
        allow_unknown_cost=True,
        skip_review=True,
    )
    asyncio.run(run_finish_step(req, deps))
    requests = [r for r in chair_provider.requests if r.tags.get("stage") == chair.STAGE]
    files = _files(requests[0].prompt)
    if backend is Backend.CLI:
        assert files.attachments == PDF.derived_images
        assert fallback_block(PDF.attachment) in files.content
    else:
        assert files.attachments == (PDF.attachment, *PDF.derived_images)
    assert DERIVED_HEADER in files.content
