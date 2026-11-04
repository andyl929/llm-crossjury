"""Anonymity protection for answers before they reach reviewers and the chair.

Two layers (SPEC §6):

1. Every member prompt asks the model not to reveal its identity, vendor, or model.
2. :func:`scrub` replaces model and vendor names in member output with
   :data:`REDACTION`. This is language-independent: no sentence patterns, just
   names (and hyphenated version suffixes such as ``GPT-x``). The number of
   replacements is recorded in the run record.

The scrub is **disabled for the whole run** when the original task input (task text,
or the document in Document Review) mentions any of the names, because the task is
then legitimately about them. :func:`scrub_enabled_for` makes that decision once per
run. Matching is case-sensitive so that ordinary words (e.g. "grok") are untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REDACTION = "[model]"

MODEL_AND_VENDOR_NAMES: tuple[str, ...] = (
    "Claude",
    "ChatGPT",
    "GPT",
    "Gemini",
    "Grok",
    "Anthropic",
    "OpenAI",
    "Google",
    "xAI",
)

NAME_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?:" + "|".join(MODEL_AND_VENDOR_NAMES) + r")(?:-[\w.]+)*\b"
)
"""Whole names plus optional hyphenated suffixes; case-sensitive."""


@dataclass(frozen=True)
class ScrubResult:
    text: str
    replacements: int


def scrub_enabled_for(task_inputs: list[str], configured: bool) -> bool:
    """Return ``False`` if scrubbing is off in config or any task input mentions a name."""
    raise NotImplementedError


def scrub(text: str) -> ScrubResult:
    """Replace every :data:`NAME_PATTERN` match in ``text`` with :data:`REDACTION`."""
    raise NotImplementedError
