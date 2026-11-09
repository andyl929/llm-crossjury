"""Turn an uploaded file into a contract :class:`Attachment` (ADR 0007).

Besides the attachment itself, a PDF or DOCX may get *derived images* (smart page
images): rendered PDF pages that hold scans or figures, and images embedded in a
DOCX. They are ordinary ``IMAGE`` attachments, made at upload time, and sent only to
members that read images but not the file's own kind (see ``prompt.py``).
"""

from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from llm_crossjury.core.attachments import isolate
from llm_crossjury.core.attachments.detect import (
    FileType,
    detect_file_type,
    image_size,
    sanitize_filename,
)
from llm_crossjury.core.attachments.extract import ExtractedImage, Extraction, extract_text
from llm_crossjury.core.attachments.limits import (
    EXTRACTION_TIMEOUT_S,
    MAX_FILE_BYTES,
    MAX_FILENAME_CHARS,
    AttachmentError,
    AttachmentTooLargeError,
)
from llm_crossjury.core.attachments.pages import render_pdf_pages
from llm_crossjury.core.attachments.render import fallback_block
from llm_crossjury.core.cost.estimate import estimate_tokens
from llm_crossjury.core.providers.base import Attachment, AttachmentKind

IMAGE_HEADER_EXTRACTOR = "image-header"
IMAGE_HEADER_VERSION = "1"
"""Version of the built-in image header parser; bump when its behavior changes."""

_IMAGE_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}

DerivedReason = Literal["scanned", "figure", "embedded"]
"""Why a derived image exists (see :class:`DerivedImageMeta`)."""


class DerivedImageMeta(BaseModel):
    """Metadata of one image derived from a document (smart page images, ADR 0007)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filename: str
    """Generated, sanitized name, e.g. ``report.pdf#page-3.png``."""
    media_type: str
    sha256: str
    size: int
    """Bytes of the encoded image."""
    width: int
    height: int
    reason: DerivedReason
    """``scanned`` (a PDF page without text), ``figure`` (a PDF page with images) or
    ``embedded`` (an image inside a DOCX)."""
    page: int | None
    """1-based PDF page; ``None`` for DOCX images."""
    index: int | None
    """1-based number of a DOCX image (its ``[Image i of n]`` marker); ``None`` for
    PDF pages."""


class AttachmentMeta(BaseModel):
    """What local processing learned about an attachment (not sent to vendors)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_count: int | None
    """PDF pages; ``None`` for other kinds."""
    image_size: tuple[int, int] | None
    """``(width, height)`` in pixels for images; ``None`` otherwise."""
    warnings: tuple[str, ...]
    """User-facing notes (scanned pages, truncation, skipped images). Never content."""
    extractor: str
    """Which local extractor produced ``fallback_text`` (or read the image header)."""
    extractor_version: str
    """Version(s) of that extractor, for run metadata."""
    text_tokens: int | None
    """Local estimate of the tokens of :func:`fallback_block`; ``None`` for images."""
    derived: tuple[DerivedImageMeta, ...] = ()
    """Images derived from the document (rendered PDF pages, DOCX images), in the
    order they are sent; see :attr:`BuiltAttachment.derived_images`."""


class BuiltAttachment(BaseModel):
    """A contract attachment plus its local metadata and derived images."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attachment: Attachment
    meta: AttachmentMeta
    derived_images: tuple[Attachment, ...] = ()
    """Images derived from the document, as ordinary ``IMAGE`` attachments, one per
    entry of ``meta.derived`` (same order). Members that read images but not this
    file's kind receive them with the text (ADR 0007, smart page images)."""

    @model_validator(mode="after")
    def _derived_match(self) -> BuiltAttachment:
        metas = self.meta.derived
        if len(metas) != len(self.derived_images) or any(
            m.sha256 != a.sha256 or a.kind is not AttachmentKind.IMAGE
            for m, a in zip(metas, self.derived_images, strict=True)
        ):
            raise ValueError("derived images do not match their metadata")
        return self


def build_attachment(filename: str, data: bytes) -> BuiltAttachment:
    """Validate an uploaded file and build its :class:`Attachment`.

    The type is detected by magic bytes; PDF and DOCX text is extracted in an
    isolated worker process with :data:`EXTRACTION_TIMEOUT_S`. PDF pages selected
    for page images are then rendered in a second isolated worker with the same
    limit; if rendering fails or times out, the file is kept without page images
    (with a warning). This call blocks (it may take seconds); async callers should
    use ``asyncio.to_thread``.

    Args:
        filename: The user-supplied name; reduced to a safe base name.
        data: The raw file content.

    Returns:
        The attachment, its metadata and its derived images.

    Raises:
        AttachmentError: The file is empty, too large, unsupported, malformed or
            could not be extracted in time. The message is safe to show.
    """
    if not data:
        raise AttachmentError("the file is empty")
    if len(data) > MAX_FILE_BYTES:
        raise AttachmentTooLargeError(
            f"the file has {len(data)} bytes, over the {MAX_FILE_BYTES} byte limit "
            f"({MAX_FILE_BYTES // (1024 * 1024)} MB)"
        )
    file_type = detect_file_type(data)
    name = sanitize_filename(filename)
    digest = hashlib.sha256(data).hexdigest()

    if file_type.kind is AttachmentKind.IMAGE:
        attachment = Attachment(
            kind=AttachmentKind.IMAGE,
            media_type=file_type.value,
            filename=name,
            sha256=digest,
            data=data,
            fallback_text=None,
        )
        meta = AttachmentMeta(
            page_count=None,
            image_size=image_size(file_type, data),
            warnings=(),
            extractor=IMAGE_HEADER_EXTRACTOR,
            extractor_version=IMAGE_HEADER_VERSION,
            text_tokens=None,
        )
        return BuiltAttachment(attachment=attachment, meta=meta)

    extraction: Extraction = isolate.run_isolated(
        extract_text, (file_type.value, data), EXTRACTION_TIMEOUT_S
    )
    warnings = list(extraction.warnings)
    images = extraction.images
    if extraction.render_pages:
        images, note = _render_pages(data, extraction)
        if note:
            warnings.append(note)
    attachment = Attachment(
        kind=file_type.kind,
        media_type=file_type.value,
        filename=name,
        sha256=digest,
        data=data,
        fallback_text=extraction.text,
    )
    extractor, extractor_version = _extractor(file_type)
    derived_images, derived_meta = _derived(name, images)
    meta = AttachmentMeta(
        page_count=extraction.page_count,
        image_size=None,
        warnings=tuple(warnings),
        extractor=extractor,
        extractor_version=extractor_version,
        text_tokens=estimate_tokens(fallback_block(attachment)),
        derived=derived_meta,
    )
    return BuiltAttachment(attachment=attachment, meta=meta, derived_images=derived_images)


def _render_pages(
    data: bytes, extraction: Extraction
) -> tuple[tuple[ExtractedImage, ...], str | None]:
    """Render the selected pages in the isolated worker; never fail the upload."""
    try:
        images, oversized = isolate.run_isolated(
            render_pdf_pages, (data, extraction.render_pages), EXTRACTION_TIMEOUT_S
        )
    except AttachmentError as exc:
        # The text is usable on its own: keep the file, without page images.
        return (), (
            f"pages could not be rendered as images ({exc}); members without PDF input "
            "receive the extracted text only"
        )
    if oversized:
        return images, f"{oversized} page image(s) were left out (over the size limit)"
    return images, None


def derived_filename(source: str, image: ExtractedImage) -> str:
    """Generated name of a derived image, e.g. ``report.pdf#page-3.png``.

    Args:
        source: The (sanitized) name of the source file.
        image: The derived image.

    Returns:
        A sanitized base name that always keeps the page or image number and the
        extension (a long source name is shortened instead).
    """
    ext = _IMAGE_EXTENSIONS.get(image.media_type, "img")
    part = f"page-{image.page}" if image.page is not None else f"image-{image.index}"
    suffix = f"#{part}.{ext}"
    return sanitize_filename(source[: max(1, MAX_FILENAME_CHARS - len(suffix))] + suffix)


def _derived(
    source: str, images: tuple[ExtractedImage, ...]
) -> tuple[tuple[Attachment, ...], tuple[DerivedImageMeta, ...]]:
    attachments: list[Attachment] = []
    metas: list[DerivedImageMeta] = []
    for image in images:
        digest = hashlib.sha256(image.data).hexdigest()
        filename = derived_filename(source, image)
        attachments.append(
            Attachment(
                kind=AttachmentKind.IMAGE,
                media_type=image.media_type,
                filename=filename,
                sha256=digest,
                data=image.data,
                fallback_text=None,
            )
        )
        metas.append(
            DerivedImageMeta(
                filename=filename,
                media_type=image.media_type,
                sha256=digest,
                size=len(image.data),
                width=image.width,
                height=image.height,
                reason=image.reason,
                page=image.page,
                index=image.index,
            )
        )
    return tuple(attachments), tuple(metas)


def _extractor(file_type: FileType) -> tuple[str, str]:
    if file_type is FileType.PDF:
        return "pypdf+pdfplumber", f"{_version('pypdf')}+{_version('pdfplumber')}"
    return "python-docx", _version("python-docx")


def _version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "unknown"
