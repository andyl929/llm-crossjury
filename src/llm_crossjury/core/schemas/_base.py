"""Base model and shared enums for structured-output schemas.

CONTRACT FILE. Do not modify; report problems in ``docs/contract-issues.md``.

Strict-subset rules (ADR 0001, "Structured output schema convention"):

1. Every field is required. "Optional" means ``T | None`` with **no default**.
2. Nullable is allowed only on scalars (str, int, float, bool), never on objects,
   arrays or enums.
3. Every object has ``additionalProperties: false`` (``extra="forbid"``).
4. No unions other than scalar-or-null; no ``oneOf``/``anyOf``/``allOf``; no
   recursion; no ``dict`` fields (open maps); no tuples.
5. No value constraints in the schema (min/max, patterns, lengths). Range checks
   are done by validators in Python after parsing.
6. Field order is generation order: evidence fields come before judgments.

These rules are enforced by ``tests/unit/test_schema_conventions.py``.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Base for every schema that may be sent to a model as structured output."""

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)


class Severity(StrEnum):
    """How much an issue matters, from most to least severe."""

    CRITICAL = "critical"
    """Wrong or harmful; invalidates the answer if not fixed."""
    MAJOR = "major"
    """Significant gap or error; the answer is usable only after fixing it."""
    MINOR = "minor"
    """Real but limited problem."""
    NIT = "nit"
    """Style or polish."""


class Confidence(StrEnum):
    """Self-reported confidence of a claim."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
