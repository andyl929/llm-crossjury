"""Local attachment processing: detection, extraction, prompt text, estimates (ADR 0007)."""

from llm_crossjury.core.attachments.build import (
    AttachmentMeta,
    BuiltAttachment,
    build_attachment,
)
from llm_crossjury.core.attachments.detect import FileType, detect_file_type, sanitize_filename
from llm_crossjury.core.attachments.limits import (
    EXTRACTION_TIMEOUT_S,
    MAX_FILE_BYTES,
    MAX_PDF_PAGES,
    MAX_TEXT_CHARS,
    MAX_TOTAL_BYTES,
    AttachmentError,
    AttachmentExtractionError,
    AttachmentTimeoutError,
    AttachmentTooLargeError,
    UnsupportedAttachmentError,
)
from llm_crossjury.core.attachments.render import (
    ATTACHMENT_NOTICE,
    fallback_block,
    sort_attachments,
    validate_total,
)

__all__ = [
    "ATTACHMENT_NOTICE",
    "EXTRACTION_TIMEOUT_S",
    "MAX_FILE_BYTES",
    "MAX_PDF_PAGES",
    "MAX_TEXT_CHARS",
    "MAX_TOTAL_BYTES",
    "AttachmentError",
    "AttachmentExtractionError",
    "AttachmentMeta",
    "AttachmentTimeoutError",
    "AttachmentTooLargeError",
    "BuiltAttachment",
    "FileType",
    "UnsupportedAttachmentError",
    "build_attachment",
    "detect_file_type",
    "fallback_block",
    "sanitize_filename",
    "sort_attachments",
    "validate_total",
]
