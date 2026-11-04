"""Anonymity protection for answers before they reach reviewers and the chair.

Two layers (SPEC section 6):

1. Every member prompt asks the model not to reveal its identity, vendor, or model.
2. :func:`scrub` removes self-identifying phrases with regular expressions,
   replacing each match with :data:`REDACTION`. Scrubbing is applied to stage-1
   output (and Document Review edit reasons) before anyone else sees it, and the
   number of replacements is recorded in the run record.

Patterns are deliberately conservative: false negatives are acceptable, false
positives that change meaning are not. Users may add patterns in configuration.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

REDACTION = "[identity removed]"

_NAMES = r"(?:Claude|ChatGPT|GPT(?:-\w+)?|Codex|Gemini|Grok|Anthropic|OpenAI|Google|xAI)"

DEFAULT_PATTERNS: tuple[str, ...] = (
    rf"\b(?:as|I am|I'm)\s+(?:an?\s+)?(?:AI\s+)?(?:model\s+|assistant\s+)?{_NAMES}\b",
    rf"\b(?:made|built|trained|developed|created)\s+by\s+{_NAMES}\b",
    rf"\bmy name is\s+{_NAMES}\b",
)
"""Initial English patterns, compiled case-insensitively. To be extended with tests."""


@dataclass(frozen=True)
class ScrubResult:
    text: str
    replacements: int


def compile_patterns(extra: Sequence[str] = ()) -> tuple[re.Pattern[str], ...]:
    """Compile :data:`DEFAULT_PATTERNS` plus user-supplied ``extra`` patterns."""
    raise NotImplementedError


def scrub(text: str, patterns: Sequence[re.Pattern[str]]) -> ScrubResult:
    """Replace every match of ``patterns`` in ``text`` with :data:`REDACTION`."""
    raise NotImplementedError
