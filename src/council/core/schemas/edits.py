"""Document Review mode: proposed edits instead of a rewritten document.

CONTRACT FILE. Do not modify; report problems in docs/contract-issues.md.
Rationale: docs/adr/0004-document-mode-edit-list.md.
"""

from __future__ import annotations

from enum import StrEnum

from council.core.schemas._base import Severity, StrictModel


class EditCategory(StrEnum):
    """Why an edit is proposed. Presets may restrict which categories are requested."""

    CORRECTNESS = "correctness"
    """Factual, logical, mathematical or technical error."""
    CLARITY = "clarity"
    """Hard to understand, ambiguous, or imprecise."""
    STRUCTURE = "structure"
    """Ordering, section organization, missing or redundant parts."""
    CONSISTENCY = "consistency"
    """Terminology, notation, numbering or claims that conflict across the document."""
    CITATION = "citation"
    """Missing, wrong, or unsupported reference."""
    STYLE = "style"
    """Tone, concision, register."""
    GRAMMAR = "grammar"
    """Spelling, grammar, punctuation."""
    FORMATTING = "formatting"
    """Markdown/LaTeX markup, layout, figures/tables markup."""


class EditLocation(StrictModel):
    """Where an edit applies.

    original in Edit is the authoritative anchor; these fields help a
    human find it and help the applier disambiguate repeated text.
    """

    section: str | None
    """Heading path, e.g. "3 Method > 3.2 Data"."""

    line_start: int | None
    """1-based line in the submitted source, if known."""

    line_end: int | None

    occurrence: int | None
    """1-based occurrence index of original if it appears more than once."""


class Edit(StrictModel):
    """A single proposed change."""

    id: str
    """Unique within one EditList, e.g. "e3"."""

    location: EditLocation

    original: str
    """Exact source text to be replaced (verbatim, including markup)."""

    replacement: str
    """New text. Empty string means deletion."""

    reason: str
    """Why the change improves the document."""

    category: EditCategory

    severity: Severity


class EditList(StrictModel):
    """All edits one member (or the chair) proposes for a document."""

    summary: str
    """Overall assessment of the document, a few sentences."""

    edits: list[Edit]
