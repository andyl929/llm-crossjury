"""The three modes. Each module exposes a pipeline builder."""

from enum import StrEnum


class Mode(StrEnum):
    COMPARE = "compare"
    COUNCIL = "council"
    DOCUMENT_REVIEW = "document-review"


__all__ = ["Mode"]
