"""Turn an uploaded file into a contract :class:`Attachment` (ADR 0007)."""

from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version

from pydantic import BaseModel, ConfigDict

from llm_crossjury.core.attachments import isolate
from llm_crossjury.core.attachments.detect import (
    FileType,
    detect_file_type,
    image_size,
    sanitize_filename,
)
from llm_crossjury.core.attachments.extract import Extraction, extract_text
from llm_crossjury.core.attachments.limits import (
    EXTRACTION_TIMEOUT_S,
    MAX_FILE_BYTES,
    AttachmentError,
    AttachmentTooLargeError,
)
from llm_crossjury.core.attachments.render import fallback_block
from llm_crossjury.core.cost.estimate import estimate_tokens
from llm_crossjury.core.providers.base import Attachment, AttachmentKind

IMAGE_HEADER_EXTRACTOR = "image-header"
IMAGE_HEADER_VERSION = "1"
"""Version of the built-in image header parser; bump when its behavior changes."""


class AttachmentMeta(BaseModel):
    """What local processing learned about an attachment (not sent to vendors)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_count: int | None
    """PDF pages; ``None`` for other kinds."""
    image_size: tuple[int, int] | None
    """``(width, height)`` in pixels for images; ``None`` otherwise."""
    warnings: tuple[str, ...]
    """User-facing notes (scanned pages, truncation, dropped images). Never content."""
    extractor: str
    """Which local extractor produced ``fallback_text`` (or read the image header)."""
    extractor_version: str
    """Version(s) of that extractor, for run metadata."""
    text_tokens: int | None
    """Local estimate of the tokens of :func:`fallback_block`; ``None`` for images."""


class BuiltAttachment(BaseModel):
    """A contract attachment plus its local metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attachment: Attachment
    meta: AttachmentMeta


def build_attachment(filename: str, data: bytes) -> BuiltAttachment:
    """Validate an uploaded file and build its :class:`Attachment`.

    The type is detected by magic bytes; PDF and DOCX text is extracted in an
    isolated worker process with :data:`EXTRACTION_TIMEOUT_S`. This call blocks
    (it may take seconds); async callers should use ``asyncio.to_thread``.

    Args:
        filename: The user-supplied name; reduced to a safe base name.
        data: The raw file content.

    Returns:
        The attachment and its metadata.

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
    attachment = Attachment(
        kind=file_type.kind,
        media_type=file_type.value,
        filename=name,
        sha256=digest,
        data=data,
        fallback_text=extraction.text,
    )
    extractor, extractor_version = _extractor(file_type)
    meta = AttachmentMeta(
        page_count=extraction.page_count,
        image_size=None,
        warnings=extraction.warnings,
        extractor=extractor,
        extractor_version=extractor_version,
        text_tokens=estimate_tokens(fallback_block(attachment)),
    )
    return BuiltAttachment(attachment=attachment, meta=meta)


def _extractor(file_type: FileType) -> tuple[str, str]:
    if file_type is FileType.PDF:
        return "pypdf+pdfplumber", f"{_version('pypdf')}+{_version('pdfplumber')}"
    return "python-docx", _version("python-docx")


def _version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "unknown"
