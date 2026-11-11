"""Local token estimates for attachments, for the pre-run cost preview (ADR 0007 §10).

ESTIMATE ASSUMPTIONS, NOT BILLING FACTS. Vendor token-count endpoints take precedence
where they exist; these heuristics fill the gap and are deliberately conservative
(they over-count). The per-vendor numbers below were read from the vendors' public
docs on 2026-11-08 and live in :data:`NATIVE_TOKEN_RULES` only, so they are easy to
update:

* Anthropic image: ceil(w/28) * ceil(h/28) tokens, capped at 4784.
  https://platform.claude.com/docs/en/build-with-claude/vision
* Anthropic PDF: each page costs its text (assumed <= 3000 tokens) plus a page image
  (assumed at the 4784 image cap).
  https://platform.claude.com/docs/en/build-with-claude/pdf-support
* OpenAI image: ceil(w/32) * ceil(h/32) patches x 1.2 (no cap assumed: conservative).
  https://developers.openai.com/api/docs/guides/images-vision
* OpenAI PDF: the extracted text plus one page image per page; the page image is
  assumed at a 1536-patch cap x 1.2 = 1844 tokens.
  https://developers.openai.com/api/docs/guides/pdf-files
* Gemini image: 1120 tokens; Gemini PDF: 1120 tokens per page.
  https://aistudio.google.com/docs/media-resolution?generate-content=true
* xAI image: UNVERIFIED, the OpenAI rule is used as a stand-in. xAI PDFs are always
  sent as extracted text (ADR 0007 §3), so no native PDF rule exists.

A provider missing from the table gets the maximum over all rules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

from llm_crossjury.core.attachments.build import AttachmentMeta
from llm_crossjury.core.attachments.render import fallback_block
from llm_crossjury.core.cost.estimate import estimate_tokens
from llm_crossjury.core.providers.base import Attachment, AttachmentKind


@dataclass(frozen=True)
class NativeTokenRule:
    """Estimate assumptions for one vendor's native image and PDF input."""

    image_patch_px: int | None
    """Patch edge in pixels for ``ceil(w/p) * ceil(h/p)``; ``None`` = flat cost."""
    image_patch_factor: Fraction
    """Tokens per patch."""
    image_max_tokens: int | None
    """Cap on the patch-based image cost; ``None`` = no cap."""
    image_flat_tokens: int | None
    """Flat image cost when ``image_patch_px`` is ``None``."""
    pdf_page_tokens: int | None
    """Tokens per PDF page; ``None`` = PDFs are never native (text estimate)."""
    pdf_adds_text: bool
    """Whether the extracted text is billed on top of ``pdf_page_tokens``."""
    verified: bool
    """``False`` when the rule is a stand-in, not read from the vendor's docs."""
    sources: tuple[str, ...]


_ANTHROPIC_IMAGE_CAP = 4784
_ANTHROPIC_PDF_PAGE_TEXT = 3000
_OPENAI_FACTOR = Fraction(6, 5)
_OPENAI_PDF_PAGE_IMAGE = math.ceil(1536 * _OPENAI_FACTOR)
_GEMINI_MEDIA = 1120

NATIVE_TOKEN_RULES: dict[str, NativeTokenRule] = {
    "anthropic": NativeTokenRule(
        image_patch_px=28,
        image_patch_factor=Fraction(1),
        image_max_tokens=_ANTHROPIC_IMAGE_CAP,
        image_flat_tokens=None,
        pdf_page_tokens=_ANTHROPIC_PDF_PAGE_TEXT + _ANTHROPIC_IMAGE_CAP,
        pdf_adds_text=False,
        verified=True,
        sources=(
            "https://platform.claude.com/docs/en/build-with-claude/vision",
            "https://platform.claude.com/docs/en/build-with-claude/pdf-support",
        ),
    ),
    "openai": NativeTokenRule(
        image_patch_px=32,
        image_patch_factor=_OPENAI_FACTOR,
        image_max_tokens=None,
        image_flat_tokens=None,
        pdf_page_tokens=_OPENAI_PDF_PAGE_IMAGE,
        pdf_adds_text=True,
        verified=True,
        sources=(
            "https://developers.openai.com/api/docs/guides/images-vision",
            "https://developers.openai.com/api/docs/guides/pdf-files",
        ),
    ),
    "gemini": NativeTokenRule(
        image_patch_px=None,
        image_patch_factor=Fraction(1),
        image_max_tokens=None,
        image_flat_tokens=_GEMINI_MEDIA,
        pdf_page_tokens=_GEMINI_MEDIA,
        pdf_adds_text=False,
        verified=True,
        sources=("https://aistudio.google.com/docs/media-resolution?generate-content=true",),
    ),
    "xai": NativeTokenRule(
        image_patch_px=32,
        image_patch_factor=_OPENAI_FACTOR,
        image_max_tokens=None,
        image_flat_tokens=None,
        pdf_page_tokens=None,
        pdf_adds_text=False,
        verified=False,
        sources=("https://developers.openai.com/api/docs/guides/images-vision",),
    ),
}
"""Native-input estimate assumptions per provider id (researched 2026-11-08)."""


def estimate_attachment_tokens(
    att: Attachment, meta: AttachmentMeta, provider: str, native: bool
) -> int:
    """Estimate the input tokens one attachment adds to one call.

    Args:
        att: The attachment.
        meta: Its metadata from :func:`build_attachment`.
        provider: Provider id (``"anthropic"``, ``"openai"``, ``"gemini"``, ``"xai"``);
            unknown ids get the most conservative rule.
        native: Whether the adapter sends the file natively (kind in the model's
            ``input_modalities``) rather than as :func:`fallback_block` text.

    Returns:
        A conservative token estimate (never zero for a non-empty attachment).

    Raises:
        ValueError: Text was requested for an image (no text form), or the metadata
            lacks the page count / image size a native estimate needs.
    """
    if not native or att.kind is AttachmentKind.DOCUMENT:
        return estimate_tokens(fallback_block(att))
    rule = NATIVE_TOKEN_RULES.get(provider)
    rules = [rule] if rule is not None else list(NATIVE_TOKEN_RULES.values())
    if att.kind is AttachmentKind.IMAGE:
        if meta.image_size is None:
            raise ValueError("a native image estimate needs the image size")
        return max(_image_tokens(r, *meta.image_size) for r in rules)
    if meta.page_count is None:
        raise ValueError("a native PDF estimate needs the page count")
    text_tokens = estimate_tokens(att.fallback_text or "")
    return max(_pdf_tokens(r, meta.page_count, att, text_tokens) for r in rules)


def _image_tokens(rule: NativeTokenRule, width: int, height: int) -> int:
    if rule.image_patch_px is None:
        return rule.image_flat_tokens or 0
    patches = math.ceil(width / rule.image_patch_px) * math.ceil(height / rule.image_patch_px)
    tokens = math.ceil(patches * rule.image_patch_factor)
    if rule.image_max_tokens is not None:
        tokens = min(tokens, rule.image_max_tokens)
    return tokens


def _pdf_tokens(rule: NativeTokenRule, pages: int, att: Attachment, text_tokens: int) -> int:
    if rule.pdf_page_tokens is None:
        return estimate_tokens(fallback_block(att))
    return pages * rule.pdf_page_tokens + (text_tokens if rule.pdf_adds_text else 0)
