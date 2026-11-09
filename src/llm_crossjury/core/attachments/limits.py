"""Attachment limits and errors (ADR 0007).

The limits are product settings, not vendor facts: they keep every request below the
smallest vendor inline limit and bound the work done on untrusted files.
"""

from __future__ import annotations

MAX_FILE_BYTES = 20 * 1024 * 1024
"""Maximum size of one attached file (20 MB)."""

MAX_TOTAL_BYTES = 20 * 1024 * 1024
"""Maximum total size of all attachments of one run (20 MB, ADR 0007 §4)."""

MAX_PDF_PAGES = 300
"""Maximum number of pages of an attached PDF."""

MAX_TEXT_CHARS = 400_000
"""Extracted text longer than this is truncated (with a warning)."""

EXTRACTION_TIMEOUT_S = 30.0
"""Wall-clock limit for extracting one file in the isolated worker process."""

MAX_FILENAME_CHARS = 128
"""Sanitized file names are cut to this many characters (keeping the extension)."""

MAX_ZIP_MEMBERS = 2_000
"""DOCX: maximum number of zip members (zip-bomb guard)."""

MAX_ZIP_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
"""DOCX: maximum declared total uncompressed size of all zip members (zip-bomb guard)."""

SCANNED_PAGE_MIN_CHARS = 20
"""A PDF page with fewer non-whitespace characters than this is flagged as possibly
scanned (no text layer)."""


# --------------------------------------------------------------- derived images

MAX_PAGE_IMAGES = 8
"""PDF: at most this many pages are rendered as images (smart page images)."""

MAX_DOCX_IMAGES = 8
"""DOCX: at most this many embedded images are kept (in document order)."""

PAGE_IMAGE_LONG_EDGE_PX = 1280
"""Rendered PDF pages are scaled so that their long edge has this many pixels."""

PAGE_IMAGE_JPEG_QUALITY = 85
"""JPEG quality of a rendered page; PNG is used instead when it is smaller."""

FIGURE_MIN_IMAGE_PX = 64
"""A PDF image smaller than this (width or height, in source pixels) is ignored when
looking for figure pages (icons, bullets, rules)."""

FIGURE_FULL_COVERAGE = 0.25
"""A PDF page whose images cover at least this share of the page area is a figure
page, however much text it has."""

FIGURE_MIN_COVERAGE = 0.03
"""A PDF page whose images cover at least this share of the page area is a figure
page when it also has little text (see :data:`FIGURE_PAGE_MAX_CHARS`). Smaller
images (logos) are ignored."""

FIGURE_PAGE_MAX_CHARS = 1500
"""Low text density: fewer non-whitespace characters than this on a page."""

MIN_EMBEDDED_IMAGE_BYTES = 2048
"""DOCX: embedded images smaller than this are skipped (icons, bullets)."""

MIN_EMBEDDED_IMAGE_PX = 32
"""DOCX: embedded images narrower or lower than this are skipped."""

MAX_DERIVED_IMAGE_BYTES = 5 * 1024 * 1024
"""One derived image (page image or embedded image) may have at most 5 MB."""

MAX_DERIVED_IMAGE_EDGE_PX = 8000
"""Embedded images with an edge longer than this are skipped (vendor limit)."""

MAX_DERIVED_BYTES_PER_FILE = 8 * 1024 * 1024
"""All derived images of one file together may have at most 8 MB; later images are
dropped with a warning. Derived images do not count toward :data:`MAX_TOTAL_BYTES`
(the user's upload limit), but a member never receives more than
:data:`MAX_TOTAL_BYTES` of binary attachment data in total (see ``prompt.py``)."""


class AttachmentError(ValueError):
    """An attachment was rejected or could not be processed.

    Messages are safe to show to the user: they never contain file content.
    """


class UnsupportedAttachmentError(AttachmentError):
    """The file type is not supported (detected by magic bytes)."""


class AttachmentTooLargeError(AttachmentError):
    """A size, page or member-count limit was exceeded."""


class AttachmentExtractionError(AttachmentError):
    """The file looked supported but could not be read (corrupt, encrypted, ...)."""


class AttachmentTimeoutError(AttachmentExtractionError):
    """Extraction exceeded :data:`EXTRACTION_TIMEOUT_S`."""
