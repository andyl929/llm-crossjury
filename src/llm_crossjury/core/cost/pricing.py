"""Price table loaded from ``config/pricing.yaml``.

Prices are NEVER hard-coded or guessed. Every entry carries the URL it was taken
from and the date it was last checked against that URL. An ``api`` model with no
entry cannot be estimated, and a run that needs it is always refused;
``allow_unknown_cost`` does not override this (it only admits ``cli`` members, whose
per-call cost is unknowable).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, HttpUrl

from llm_crossjury.core.providers.base import Backend


class PriceEntry(BaseModel):
    """Prices for one (provider, model, backend), in ``currency`` per 1M tokens.

    A ``None`` rate means the vendor does not bill that category separately or it was
    not verified; how ``None`` is treated is documented per field.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    backend: Backend
    currency: str
    """ISO 4217 code, e.g. ``"USD"``."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cached_input_per_mtok: Decimal | None
    """``None``: caching not priced; cached tokens are billed at ``input_per_mtok``."""
    cache_write_per_mtok: Decimal | None
    """``None``: cache writes billed at ``input_per_mtok``."""
    reasoning_per_mtok: Decimal | None
    """``None``: reasoning tokens billed at ``output_per_mtok``."""
    batch_multiplier: Decimal | None
    """Multiplier applied to all rates for batch calls; ``None`` means no batch pricing."""

    source_url: HttpUrl
    last_verified: date
    notes: str | None


class PricingFile(BaseModel):
    """Top-level shape of ``config/pricing.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    entries: tuple[PriceEntry, ...]


class PricingError(Exception):
    """The pricing file is missing, malformed, or lacks a needed entry."""


class PricingTable:
    """Read-only lookup over a validated :class:`PricingFile`."""

    def __init__(self, pricing: PricingFile) -> None:
        raise NotImplementedError

    @classmethod
    def load(cls, path: Path) -> PricingTable:
        """Load and validate a YAML pricing file.

        Raises:
            PricingError: on I/O or validation failure, or duplicate keys.
        """
        raise NotImplementedError

    def lookup(self, provider: str, model: str, backend: Backend) -> PriceEntry:
        """Return the entry for a model.

        Raises:
            PricingError: if no entry exists. There is no fallback price.
        """
        raise NotImplementedError

    def stale_entries(self, today: date, max_age_days: int) -> list[PriceEntry]:
        """Return entries whose ``last_verified`` is older than ``max_age_days``."""
        raise NotImplementedError
