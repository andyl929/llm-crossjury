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
