"""Document Review mode: the Council flow over a Markdown/LaTeX file, producing edits.

v1 accepts ``.md`` and ``.tex`` only. The document goes into the stable prompt
prefix so every member and reviewer call can reuse the cache (ADR 0003).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from council.core.config import CouncilConfig
from council.core.orchestrator.pipeline import Pipeline
from council.core.schemas.edits import EditCategory, EditList


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


class Preset(BaseModel):
    """A named Document Review configuration, loaded from ``presets/<name>.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    instructions: str
    categories: tuple[EditCategory, ...]
    prompt_version: str


def load_document(path: Path) -> SourceDocument:
    """Read a ``.md`` or ``.tex`` file as UTF-8.

    Raises:
        ValueError: unsupported extension or undecodable content.
    """
    raise NotImplementedError


def load_preset(name: str) -> Preset:
    """Load a built-in preset (``"paper"``) or a user preset by name."""
    raise NotImplementedError


def validate_edits(document: SourceDocument, edits: EditList) -> list[str]:
    """Return problems: ``original`` not found, ambiguous without ``occurrence``,
    overlapping edits, duplicate ids."""
    raise NotImplementedError


def render_diff(document: SourceDocument, edits: EditList) -> str:
    """Render accepted edits as a unified diff for the user to apply themselves."""
    raise NotImplementedError


def build_document_review_pipeline(
    config: CouncilConfig, document: SourceDocument, preset: Preset
) -> Pipeline:
    raise NotImplementedError
