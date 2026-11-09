"""Smart page images: PDF page selection, rendering, DOCX images, build and store."""

from __future__ import annotations

import io
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image

from llm_crossjury.core.attachments import build_attachment, isolate
from llm_crossjury.core.attachments.build import BuiltAttachment, derived_filename
from llm_crossjury.core.attachments.extract import (
    ExtractedImage,
    PageSelection,
    _usable_image,
    extract_docx,
    extract_pdf,
    is_figure_page,
)
from llm_crossjury.core.attachments.limits import (
    MAX_DOCX_IMAGES,
    MAX_PAGE_IMAGES,
    PAGE_IMAGE_LONG_EDGE_PX,
    AttachmentExtractionError,
    AttachmentTimeoutError,
)
from llm_crossjury.core.attachments.pages import render_pdf_pages
from llm_crossjury.core.attachments.store import AttachmentNotFoundError, AttachmentStore
from llm_crossjury.core.providers.base import AttachmentKind
from tests.unit._attachment_files import (
    FULL_FIGURE,
    LOGO,
    SCAN,
    SMALL_FIGURE,
    make_bmp,
    make_docx_with_images,
    make_image_pdf,
    make_noise_png,
    make_png,
)

BODY = [f"This is a full line of ordinary body text, line number {i}." for i in range(40)]
SHORT = BODY[:3]


def _direct(func: Callable[..., object], args: tuple[object, ...], timeout_s: float) -> object:
    del timeout_s
    return func(*args)


@pytest.fixture
def in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run extraction and rendering in-process (fast); isolation is tested below."""
    monkeypatch.setattr(isolate, "run_isolated", _direct)


def _sample_pdf() -> bytes:
    return make_image_pdf(
        [
            (BODY, None),  # 1: text only -> skipped
            (SHORT, FULL_FIGURE),  # 2: large figure -> figure
            ([], SCAN),  # 3: no text layer -> scanned
            (BODY, LOGO),  # 4: text with a small logo -> skipped
            (SHORT, SMALL_FIGURE),  # 5: small figure, little text -> figure
            (BODY, SMALL_FIGURE),  # 6: small figure in dense text -> skipped
        ]
    )


# --------------------------------------------------------------------------- PDF selection


def test_page_selection_scanned_figure_and_text_pages() -> None:
    result = extract_pdf(_sample_pdf())
    assert result.render_pages == (
        PageSelection(2, "figure"),
        PageSelection(3, "scanned"),
        PageSelection(5, "figure"),
    )


def test_page_selection_respects_the_cap() -> None:
    pdf = make_image_pdf([([], SCAN)] * (MAX_PAGE_IMAGES + 2))
    result = extract_pdf(pdf)
    assert [s.page for s in result.render_pages] == list(range(1, MAX_PAGE_IMAGES + 1))
    assert any("only the first 8 are rendered" in w for w in result.warnings)


def test_figure_heuristic_thresholds() -> None:
    assert is_figure_page(5000, 0.3)  # large image, dense text
    assert is_figure_page(100, 0.05)  # medium image, little text
    assert not is_figure_page(5000, 0.05)  # medium image, dense text
    assert not is_figure_page(0, 0.01)  # a logo


def test_text_only_pdf_selects_nothing() -> None:
    assert extract_pdf(make_image_pdf([(BODY, None), (BODY, LOGO)])).render_pages == ()


# --------------------------------------------------------------------------- rendering


def test_render_selected_pages() -> None:
    pdf = _sample_pdf()
    images, oversized = render_pdf_pages(pdf, extract_pdf(pdf).render_pages)
    assert oversized == 0
    assert [(i.page, i.reason) for i in images] == [(2, "figure"), (3, "scanned"), (5, "figure")]
    for image in images:
        assert image.media_type in {"image/png", "image/jpeg"}
        assert max(image.width, image.height) == PAGE_IMAGE_LONG_EDGE_PX
        decoded = Image.open(io.BytesIO(image.data))
        assert decoded.size == (image.width, image.height)
        assert decoded.format == ("PNG" if image.media_type == "image/png" else "JPEG")


def test_render_skips_blank_pages() -> None:
    pdf = make_image_pdf([([], None), (SHORT, FULL_FIGURE)])
    selection = extract_pdf(pdf).render_pages
    assert [s.page for s in selection] == [1, 2]
    images, oversized = render_pdf_pages(pdf, selection)
    assert [i.page for i in images] == [2]
    assert oversized == 0


def test_render_rejects_garbage_without_content_in_the_message() -> None:
    with pytest.raises(AttachmentExtractionError, match="could not render") as info:
        render_pdf_pages(b"%PDF-1.4 SECRET-GARBAGE", (PageSelection(1, "scanned"),))
    assert "SECRET" not in str(info.value)


# --------------------------------------------------------------------------- DOCX


def test_docx_images_markers_and_skips() -> None:
    first, second = make_noise_png(64, 64, seed=1), make_noise_png(80, 60, seed=2)
    data = make_docx_with_images([first, make_png(4, 4), make_bmp(), second])
    result = extract_docx(data)
    assert [(i.index, i.media_type, i.data) for i in result.images] == [
        (1, "image/png", first),
        (2, "image/png", second),
    ]
    assert all(i.reason == "embedded" and i.page is None for i in result.images)
    lines = result.text.split("\n\n")
    assert lines[lines.index("Paragraph 1.") + 1] == "[Image 1 of 2]"
    assert lines[lines.index("Paragraph 4.") + 1] == "[Image 2 of 2]"
    assert result.text.count("[Image") == 2  # tiny icon and BMP leave no marker
    assert any("2 embedded image(s) were skipped" in w for w in result.warnings)
    assert any("2 embedded image(s) are sent as images" in w for w in result.warnings)


def test_docx_image_cap_keeps_document_order() -> None:
    pictures = [make_noise_png(64, 64, seed=n) for n in range(MAX_DOCX_IMAGES + 2)]
    result = extract_docx(make_docx_with_images(pictures))
    assert [i.data for i in result.images] == pictures[:MAX_DOCX_IMAGES]
    assert f"[Image {MAX_DOCX_IMAGES} of {MAX_DOCX_IMAGES}]" in result.text
    assert f"[Image {MAX_DOCX_IMAGES + 1}" not in result.text
    assert any("2 embedded image(s) were skipped" in w for w in result.warnings)


def test_usable_image_rules() -> None:
    assert _usable_image(make_noise_png()) is not None
    assert _usable_image(make_png(4, 4)) is None  # tiny
    assert _usable_image(make_bmp()) is None  # unsupported format
    emf = b"\x01\x00\x00\x00" + b"\x00" * 36 + b" EMF" + b"\x00" * 4000
    assert _usable_image(emf) is None
    assert _usable_image(make_noise_png(2000, 20)) is None  # 20 px high: a rule


def test_derived_filename_is_generated_and_safe() -> None:
    page = ExtractedImage(b"x", "image/png", 1, 1, "figure", 3, None)
    embedded = ExtractedImage(b"x", "image/jpeg", 1, 1, "embedded", None, 2)
    assert derived_filename("report.pdf", page) == "report.pdf#page-3.png"
    assert derived_filename("notes.docx", embedded) == "notes.docx#image-2.jpg"
    long_name = derived_filename("a" * 200 + ".pdf", page)
    assert long_name.endswith("#page-3.png")
    assert len(long_name) <= 128


# --------------------------------------------------------------------------- build


@pytest.mark.usefixtures("in_process")
def test_build_pdf_with_page_images() -> None:
    built = build_attachment("report.pdf", _sample_pdf())
    assert [(m.page, m.reason) for m in built.meta.derived] == [
        (2, "figure"),
        (3, "scanned"),
        (5, "figure"),
    ]
    assert [a.filename.split("#")[1].split(".")[0] for a in built.derived_images] == [
        "page-2",
        "page-3",
        "page-5",
    ]
    for image, meta in zip(built.derived_images, built.meta.derived, strict=True):
        assert image.kind is AttachmentKind.IMAGE
        assert image.fallback_text is None
        assert meta.sha256 == image.sha256
        assert meta.size == len(image.data)
        assert meta.filename == image.filename


@pytest.mark.usefixtures("in_process")
def test_build_docx_with_images() -> None:
    built = build_attachment("notes.docx", make_docx_with_images([make_noise_png()]))
    assert [a.filename for a in built.derived_images] == ["notes.docx#image-1.png"]
    assert built.meta.derived[0].reason == "embedded"
    assert built.attachment.fallback_text is not None
    assert "[Image 1 of 1]" in built.attachment.fallback_text


@pytest.mark.usefixtures("in_process")
def test_build_text_only_pdf_has_no_page_images(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def spy(func: Callable[..., object], args: tuple[object, ...], timeout_s: float) -> object:
        calls.append(func)
        return _direct(func, args, timeout_s)

    monkeypatch.setattr(isolate, "run_isolated", spy)
    built = build_attachment("plain.pdf", make_image_pdf([(BODY, None)]))
    assert built.derived_images == ()
    assert calls == [calls[0]]  # extraction only: no render worker is started


@pytest.mark.parametrize(
    "error",
    [
        AttachmentTimeoutError("extraction took longer than 30 s and was stopped"),
        AttachmentExtractionError("the extraction worker crashed"),
    ],
)
def test_render_failure_keeps_the_text(
    monkeypatch: pytest.MonkeyPatch, error: AttachmentExtractionError
) -> None:
    def fake(func: Callable[..., object], args: tuple[object, ...], timeout_s: float) -> object:
        if func is render_pdf_pages:
            raise error
        return _direct(func, args, timeout_s)

    monkeypatch.setattr(isolate, "run_isolated", fake)
    built = build_attachment("report.pdf", _sample_pdf())
    assert built.attachment.fallback_text
    assert built.derived_images == ()
    assert any("could not be rendered as images" in w for w in built.meta.warnings)


def test_isolated_render_worker_end_to_end() -> None:
    pdf = make_image_pdf([(SHORT, FULL_FIGURE)])
    images, oversized = isolate.run_isolated(
        render_pdf_pages, (pdf, (PageSelection(1, "figure"),)), 30
    )
    assert len(images) == 1
    assert oversized == 0


def test_isolated_render_crash_is_reported() -> None:
    with pytest.raises(AttachmentExtractionError, match="crashed"):
        isolate.run_isolated(os._exit, (3,), 30)


def test_derived_images_must_match_their_metadata() -> None:
    built = _built_docx()
    with pytest.raises(ValueError, match="do not match"):
        BuiltAttachment(attachment=built.attachment, meta=built.meta, derived_images=())


# --------------------------------------------------------------------------- store


def _built_docx() -> BuiltAttachment:
    data = make_docx_with_images([make_noise_png(seed=1), make_noise_png(seed=2)])
    return build_attachment_in_process("notes.docx", data)


def build_attachment_in_process(name: str, data: bytes) -> BuiltAttachment:
    original = isolate.run_isolated
    isolate.run_isolated = _direct  # type: ignore[assignment]
    try:
        return build_attachment(name, data)
    finally:
        isolate.run_isolated = original


def test_store_round_trip_with_derived_images(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "att")
    item = _built_docx()
    assert len(item.derived_images) == 2
    loaded = store.get(store.put(item))
    assert loaded == item
    assert loaded.derived_images[1].data == item.derived_images[1].data


def test_store_rejects_a_tampered_derived_image(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "att")
    attachment_id = store.put(_built_docx())
    (tmp_path / "att" / attachment_id / "image-1.bin").write_bytes(b"tampered")
    with pytest.raises(AttachmentNotFoundError):
        store.get(attachment_id)
    assert not (tmp_path / "att" / attachment_id).exists()


def test_store_rejects_a_missing_derived_image(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "att")
    attachment_id = store.put(_built_docx())
    (tmp_path / "att" / attachment_id / "image-0.bin").unlink()
    with pytest.raises(AttachmentNotFoundError):
        store.get(attachment_id)
