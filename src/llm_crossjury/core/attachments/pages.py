"""Render selected PDF pages as images (smart page images, ADR 0007).

Runs on untrusted input: :func:`build_attachment` calls :func:`render_pdf_pages` in
the isolated worker process (``isolate.py``) with the extraction time limit. Uses
pypdfium2 (PDFium, BSD/Apache) and Pillow for encoding. Never logs file content.
"""

from __future__ import annotations

import io
import warnings
from collections.abc import Sequence
from typing import TYPE_CHECKING

from llm_crossjury.core.attachments.extract import ExtractedImage, PageSelection
from llm_crossjury.core.attachments.limits import (
    MAX_DERIVED_BYTES_PER_FILE,
    MAX_DERIVED_IMAGE_BYTES,
    PAGE_IMAGE_JPEG_QUALITY,
    PAGE_IMAGE_LONG_EDGE_PX,
    AttachmentExtractionError,
)

if TYPE_CHECKING:
    import pypdfium2 as pdfium
    from PIL import Image


def render_pdf_pages(
    data: bytes, pages: Sequence[PageSelection]
) -> tuple[tuple[ExtractedImage, ...], int]:
    """Render ``pages`` of a PDF, long edge :data:`PAGE_IMAGE_LONG_EDGE_PX`.

    Each page is encoded as PNG and as JPEG; the smaller one is kept (PNG wins for
    text and line art, JPEG for photo-like scans). A blank page (one uniform color,
    e.g. an empty page flagged as scanned), a page image over
    :data:`MAX_DERIVED_IMAGE_BYTES`, or one past :data:`MAX_DERIVED_BYTES_PER_FILE`
    in total, is left out; only the size omissions are counted.

    Args:
        data: The raw PDF (already extracted successfully).
        pages: The pages to render (from :attr:`Extraction.render_pages`).

    Returns:
        The rendered pages in the given order, and how many were left out for size.

    Raises:
        AttachmentExtractionError: PDFium cannot open the document.
    """
    import pypdfium2 as pdfium  # noqa: PLC0415 - heavy import, worker only

    out: list[ExtractedImage] = []
    total = 0
    oversized = 0
    with warnings.catch_warnings():
        # pypdfium2 warns about objects it cleans up implicitly; inputs are untrusted.
        warnings.simplefilter("ignore")
        try:
            document = pdfium.PdfDocument(data)
        except Exception as exc:
            raise AttachmentExtractionError(
                f"could not render the PDF pages ({type(exc).__name__})"
            ) from None
        try:
            count = len(document)
            for selection in pages:
                if not 1 <= selection.page <= count:
                    continue
                image = _render_page(document, selection)
                if image is None:
                    continue
                if (
                    len(image.data) > MAX_DERIVED_IMAGE_BYTES
                    or total + len(image.data) > MAX_DERIVED_BYTES_PER_FILE
                ):
                    oversized += 1
                    continue
                total += len(image.data)
                out.append(image)
        finally:
            document.close()
    return tuple(out), oversized


def _render_page(document: pdfium.PdfDocument, selection: PageSelection) -> ExtractedImage | None:
    page = document[selection.page - 1]
    try:
        width, height = page.get_size()
        long_edge = max(float(width), float(height))
        if long_edge <= 0:
            return None
        bitmap = page.render(scale=PAGE_IMAGE_LONG_EDGE_PX / long_edge)
        try:
            picture = bitmap.to_pil().convert("RGB")
        finally:
            bitmap.close()
    finally:
        page.close()
    if all(low == high for low, high in picture.getextrema()):
        return None  # blank page: nothing to see
    media_type, encoded = _smallest_encoding(picture)
    return ExtractedImage(
        data=encoded,
        media_type=media_type,
        width=picture.width,
        height=picture.height,
        reason=selection.reason,
        page=selection.page,
        index=None,
    )


def _smallest_encoding(picture: Image.Image) -> tuple[str, bytes]:
    png = io.BytesIO()
    picture.save(png, format="PNG")
    jpeg = io.BytesIO()
    picture.save(jpeg, format="JPEG", quality=PAGE_IMAGE_JPEG_QUALITY)
    if len(jpeg.getvalue()) < len(png.getvalue()):
        return "image/jpeg", jpeg.getvalue()
    return "image/png", png.getvalue()
