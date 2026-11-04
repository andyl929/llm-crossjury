"""Document Review mode: the Council flow over a Markdown/LaTeX file, producing edits.

v1 accepts ``.md`` and ``.tex`` only, up to ``DocumentConfig.max_document_tokens``
(estimated; refused before any call above that). The document is split into chunks
at Markdown headings (depth ``markdown_chunk_depth``) or LaTeX ``\\chapter`` /
``\\section``. The full document stays in the stable prompt prefix (cached), and
each call's variable suffix names the chunk to work on, so members keep
document-wide context for consistency edits while output stays bounded per call
(ADR 0003, ADR 0004).

Merging (deterministic, before the chair):

* identical edits from several members, and edits whose spans do not overlap, are
  merged automatically;
* overlapping spans with different replacements become an :class:`EditConflict`,
  surfaced as a disputed point at checkpoint B (under ``--auto`` the chair decides
  and every such decision is flagged).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from llm_crossjury.core.config import CrossjuryConfig, DocumentConfig
from llm_crossjury.core.orchestrator.pipeline import Pipeline
from llm_crossjury.core.schemas.edits import Edit, EditCategory, EditList


class DocumentFormat(StrEnum):
    MARKDOWN = "markdown"
    LATEX = "latex"


class SourceDocument(BaseModel):
    """A loaded input document. Line numbers refer to ``text`` exactly as read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    format: DocumentFormat
    text: str
    sha256: str
    estimated_tokens: int


class DocumentChunk(BaseModel):
    """A contiguous section of the source, used to focus one call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    """``"k1"``, ``"k2"``, ... in document order."""
    heading: str | None
    """Heading text that starts the chunk; ``None`` for a preamble chunk."""
    line_start: int
    line_end: int
    estimated_tokens: int


class Preset(BaseModel):
    """A named Document Review configuration, loaded from ``presets/<name>.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    instructions: str
    """English instructions; the output language is set separately (SPEC section 3.4)."""
    categories: tuple[EditCategory, ...]
    prompt_version: str


def load_document(path: Path, config: DocumentConfig) -> SourceDocument:
    """Read a ``.md`` or ``.tex`` file as UTF-8 and enforce the size limit.

    Raises:
        ValueError: unsupported extension, undecodable content, or too large.
    """
    raise NotImplementedError


def chunk_document(document: SourceDocument, config: DocumentConfig) -> list[DocumentChunk]:
    """Split at Markdown headings up to ``markdown_chunk_depth`` or at LaTeX
    ``\\chapter``/``\\section``. Headings inside code fences or comments are ignored."""
    raise NotImplementedError


def load_preset(name: str) -> Preset:
    """Load a built-in preset (``"paper"``) or a user preset by name."""
    raise NotImplementedError


def validate_edits(document: SourceDocument, edits: EditList) -> list[str]:
    """Return problems: ``original`` not found, ambiguous without ``occurrence``,
    overlapping edits within one list, duplicate ids."""
    raise NotImplementedError


@dataclass(frozen=True)
class SourcedEdit:
    """An edit together with the anonymous label(s) that proposed it."""

    edit: Edit
    labels: tuple[str, ...]


@dataclass(frozen=True)
class EditConflict:
    """Overlapping edits with different replacements (one dispute at checkpoint B)."""

    id: str
    span: tuple[int, int]
    """Character offsets in the source covered by the union of the edits."""
    candidates: tuple[SourcedEdit, ...]


@dataclass(frozen=True)
class MergeResult:
    merged: tuple[SourcedEdit, ...]
    """Auto-merged: identical edits (labels combined) and non-overlapping edits."""
    conflicts: tuple[EditConflict, ...]


def merge_edit_lists(document: SourceDocument, lists: dict[str, EditList]) -> MergeResult:
    """Merge per-label edit lists according to the rules in the module docstring."""
    raise NotImplementedError


def render_diff(document: SourceDocument, edits: Sequence[Edit]) -> str:
    """Render accepted edits as a unified diff for the user to apply themselves."""
    raise NotImplementedError


def build_document_review_pipeline(
    config: CrossjuryConfig, document: SourceDocument, preset: Preset
) -> Pipeline:
    raise NotImplementedError
