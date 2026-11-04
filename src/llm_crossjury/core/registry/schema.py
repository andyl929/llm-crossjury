"""Model registry schema (``config/models.yaml``).

CONTRACT FILE. Do not modify; report problems in docs/contract-issues.md.
Rationale: docs/adr/0006-model-registry.md.

The registry is the single source of truth for everything llm-crossjury knows about
a model: which backends can reach it, how to control its reasoning, how to pass it
to a CLI, its limits, and its prices. Every fact is copied from the vendor's official
documentation at ``source_url`` on ``last_verified``. Nothing is filled from memory.

An entry with ``source_url`` or ``last_verified`` unset is *unverified*: it can be
resolved, but its pricing is never used (a run that would need it is refused).

This is configuration, not structured output, so ADR 0001's strict-subset rules do
not apply; ``extra="forbid"`` does.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, HttpUrl

from llm_crossjury.core.providers.base import Backend, ReasoningEffort

NativeValue = str | int
"""A vendor-native reasoning value: a level name (``"high"``) or a budget (``8192``)."""


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReasoningControlType(StrEnum):
    """How a model's reasoning is controlled."""

    LEVELS = "levels"
    """A named effort parameter with a fixed set of values."""
    BUDGET = "budget"
    """A numeric thinking/reasoning token budget."""
    ALWAYS_ON = "always_on"
    """Reasoning always happens and cannot be tuned; only ``default`` is meaningful."""
    NONE = "none"
    """No reasoning control (and typically no separate reasoning tokens)."""


class EffortMapping(_Config):
    """Native value for each portable level. ``None`` = level not supported."""

    low: NativeValue | None
    medium: NativeValue | None
    high: NativeValue | None


class ReasoningControl(_Config):
    type: ReasoningControlType
    parameter: str | None
    """Native request parameter path, e.g. ``"<param.path>"``; ``None`` for
    ``always_on``/``none``."""
    allowed: tuple[NativeValue, ...] | None
    """``levels``: the accepted values. ``budget``: ``(min, max)``. Otherwise ``None``."""
    mapping: EffortMapping | None
    """low/medium/high to native values. Required for ``levels`` and ``budget``."""


class CliFlags(_Config):
    """Argument templates for the ``cli`` backend.

    Each template is a list of argv tokens. ``{model}`` and ``{value}`` are the only
    placeholders, and they are substituted into a single token each; values come from
    this registry, never from user or model text (ADR 0002).
    """

    model: tuple[str, ...] | None
    """e.g. ``["--model", "{model}"]``."""
    reasoning: tuple[str, ...] | None
    """e.g. ``["--effort", "{value}"]``; ``None`` = reasoning not settable via the CLI."""


class Pricing(_Config):
    """Prices in ``currency`` per 1M tokens (fields as in the former pricing.yaml)."""

    currency: str
    """ISO 4217 code, e.g. ``"USD"``."""
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cached_input_per_mtok: Decimal | None
    """``None``: cached tokens are billed at ``input_per_mtok``."""
    cache_write_per_mtok: Decimal | None
    """``None``: cache writes are billed at ``input_per_mtok``."""
    reasoning_per_mtok: Decimal | None
    """``None``: reasoning tokens are billed at ``output_per_mtok``."""
    batch_multiplier: Decimal | None
    """Multiplier applied to all rates for batch calls; ``None`` = no batch pricing."""


class ModelEntry(_Config):
    """Everything known about one vendor model."""

    provider: str
    """``anthropic`` | ``openai`` | ``gemini`` | ``xai``."""
    model_id: str
    """Exact vendor model id."""
    backends: tuple[Backend, ...]
    """Backends that can reach this model (``api`` and/or ``cli``)."""
    reasoning_control: ReasoningControl
    cli_flags: CliFlags | None
    """Required if ``cli`` is in ``backends``."""
    context_window: int | None
    max_output: int | None
    structured_output: bool | None
    """``None`` = not verified; treated as unsupported."""
    pricing: Pricing | None
    """``None`` = unpriced; ``api`` runs needing this model are refused."""
    reasoning_multipliers: dict[ReasoningEffort, float] | None
    """Per-model override of ``cost.reasoning_multipliers`` (reasoning tokens = visible
    output x multiplier). Levels not listed fall back to the global value; ``None`` =
    use the global values. Ledger calibration still takes precedence once enough
    observations exist."""
    source_url: HttpUrl | None
    last_verified: date | None
    notes: str | None


class ProviderAliases(_Config):
    """Portable aliases for one provider; each value is a ``model_id`` in the registry."""

    fast: str | None
    balanced: str | None
    best: str | None


class ModelRegistryFile(_Config):
    """Top-level shape of ``config/models.yaml``."""

    schema_version: int
    models: tuple[ModelEntry, ...]
    aliases: dict[str, ProviderAliases]
    """Provider -> aliases."""
