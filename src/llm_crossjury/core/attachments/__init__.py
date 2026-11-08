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
from llm_crossjury.core.attachments.tokens import (
    NATIVE_TOKEN_RULES,
    NativeTokenRule,
    estimate_attachment_tokens,
)

__all__ = [
    "ATTACHMENT_NOTICE",
    "EXTRACTION_TIMEOUT_S",
    "MAX_FILE_BYTES",
    "MAX_PDF_PAGES",
    "MAX_TEXT_CHARS",
    "MAX_TOTAL_BYTES",
    "NATIVE_TOKEN_RULES",
    "AttachmentError",
    "AttachmentExtractionError",
    "AttachmentMeta",
    "AttachmentTimeoutError",
    "AttachmentTooLargeError",
    "BuiltAttachment",
    "FileType",
    "NativeTokenRule",
    "UnsupportedAttachmentError",
    "build_attachment",
    "detect_file_type",
    "estimate_attachment_tokens",
    "fallback_block",
    "sanitize_filename",
    "sort_attachments",
    "validate_total",
]
