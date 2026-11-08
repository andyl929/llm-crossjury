"""Attachment token estimates for the cost preview."""

from __future__ import annotations

import pytest

from llm_crossjury.core.attachments import (
    NATIVE_TOKEN_RULES,
    AttachmentMeta,
    estimate_attachment_tokens,
    fallback_block,
)
from llm_crossjury.core.cost.estimate import estimate_tokens
from llm_crossjury.core.providers.base import Attachment, AttachmentKind

TEXT = "Extracted page text. " * 50


def _meta(*, pages: int | None = None, size: tuple[int, int] | None = None) -> AttachmentMeta:
    return AttachmentMeta(
        page_count=pages,
        image_size=size,
        warnings=(),
        extractor="test",
        extractor_version="0",
        text_tokens=None,
    )


def _att(kind: AttachmentKind, text: str | None) -> Attachment:
    return Attachment(
        kind=kind,
        media_type="application/pdf" if kind is AttachmentKind.PDF else "image/png",
        filename="f",
        sha256="0" * 64,
        data=b"x",
        fallback_text=text,
    )


PDF = _att(AttachmentKind.PDF, TEXT)
IMAGE = _att(AttachmentKind.IMAGE, None)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini", "xai", "other"])
def test_text_fallback_uses_block(provider: str) -> None:
    expected = estimate_tokens(fallback_block(PDF))
    assert estimate_attachment_tokens(PDF, _meta(pages=3), provider, native=False) == expected


def test_document_is_always_text() -> None:
    doc = _att(AttachmentKind.DOCUMENT, TEXT)
    expected = estimate_tokens(fallback_block(doc))
    assert estimate_attachment_tokens(doc, _meta(), "anthropic", native=True) == expected


@pytest.mark.parametrize(
    ("provider", "size", "expected"),
    [
        ("anthropic", (280, 56), 10 * 2),
        ("anthropic", (281, 57), 11 * 3),
        ("anthropic", (8000, 8000), 4784),  # capped
        ("openai", (320, 64), 24),  # 10 * 2 patches x 1.2
        ("openai", (321, 64), 27),  # 11 * 2 patches x 1.2 = 26.4, rounded up
        ("gemini", (8000, 8000), 1120),
        ("gemini", (1, 1), 1120),
    ],
)
def test_native_image(provider: str, size: tuple[int, int], expected: int) -> None:
    assert estimate_attachment_tokens(IMAGE, _meta(size=size), provider, native=True) == expected


def test_xai_image_uses_openai_stand_in_flagged_unverified() -> None:
    meta = _meta(size=(1000, 700))
    assert estimate_attachment_tokens(IMAGE, meta, "xai", native=True) == (
        estimate_attachment_tokens(IMAGE, meta, "openai", native=True)
    )
    assert NATIVE_TOKEN_RULES["xai"].verified is False
    assert all(r.verified for p, r in NATIVE_TOKEN_RULES.items() if p != "xai")


def test_native_pdf() -> None:
    meta = _meta(pages=3)
    text_tokens = estimate_tokens(TEXT)
    assert estimate_attachment_tokens(PDF, meta, "anthropic", native=True) == 3 * (3000 + 4784)
    assert estimate_attachment_tokens(PDF, meta, "gemini", native=True) == 3 * 1120
    assert estimate_attachment_tokens(PDF, meta, "openai", native=True) == (text_tokens + 3 * 1844)


def test_xai_pdf_falls_back_to_text() -> None:
    expected = estimate_tokens(fallback_block(PDF))
    assert estimate_attachment_tokens(PDF, _meta(pages=3), "xai", native=True) == expected


def test_unknown_provider_takes_maximum() -> None:
    meta = _meta(pages=2, size=(2000, 2000))
    known = ["anthropic", "openai", "gemini", "xai"]
    for att in (PDF, IMAGE):
        worst = max(estimate_attachment_tokens(att, meta, p, native=True) for p in known)
        assert estimate_attachment_tokens(att, meta, "mystery", native=True) == worst


def test_missing_metadata_or_text_raises() -> None:
    with pytest.raises(ValueError, match="image size"):
        estimate_attachment_tokens(IMAGE, _meta(), "anthropic", native=True)
    with pytest.raises(ValueError, match="page count"):
        estimate_attachment_tokens(PDF, _meta(), "anthropic", native=True)
    with pytest.raises(ValueError, match="no text form"):
        estimate_attachment_tokens(IMAGE, _meta(size=(1, 1)), "anthropic", native=False)


def test_rules_cite_sources() -> None:
    for rule in NATIVE_TOKEN_RULES.values():
        assert rule.sources
        assert all(url.startswith("https://") for url in rule.sources)
