"""Price lookup over the model registry (``config/models.yaml``, ADR 0006).

Prices are NEVER hard-coded or guessed. They live in ``ModelEntry.pricing`` and are
used only when the entry is verified (``source_url`` and ``last_verified`` set). An
``api`` model that is unregistered, unpriced or unverified cannot be estimated, and a
run that needs it is always refused; ``allow_unknown_cost`` does not override this
(it only admits ``cli`` members, whose per-call cost is unknowable). Prices apply to
the ``api`` backend only.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from llm_crossjury.core.providers.base import Backend
from llm_crossjury.core.registry.resolver import ModelRegistry
from llm_crossjury.core.registry.schema import ModelEntry, Pricing


class PricingError(Exception):
    """A needed price is missing, unverified, or malformed."""


class PricingTable:
    """Read-only price lookup backed by a :class:`ModelRegistry`."""

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> ModelRegistry:
        """The registry prices are read from."""
        return self._registry

    def lookup(self, provider: str, model_id: str) -> Pricing:
        """Return the verified price of an ``api`` model.

        Raises:
            PricingError: if the model is unregistered, unpriced or unverified. There is
                no fallback price.
        """
        name = f"{provider}/{model_id}"
        entry = self._registry.get(provider, model_id)
        if entry is None:
            raise PricingError(f"{name} is not in the model registry, so it has no price")
        if Backend.API not in entry.backends:
            raise PricingError(f"{name} is not reachable via the api backend")
        if entry.pricing is None:
            raise PricingError(f"{name} has no pricing in the model registry")
        if entry.source_url is None or entry.last_verified is None:
            raise PricingError(
                f"{name} is unverified (source_url/last_verified unset); its price is not used"
            )
        _check_pricing(name, entry.pricing)
        return entry.pricing

    def stale_entries(self, today: date, max_age_days: int) -> list[ModelEntry]:
        """Return priced entries whose ``last_verified`` is older than ``max_age_days``.

        Entries without ``last_verified`` are unverified, not stale; see
        :meth:`ModelRegistry.unverified`.
        """
        return [
            e
            for e in self._registry.entries()
            if e.pricing is not None
            and e.last_verified is not None
            and (today - e.last_verified).days > max_age_days
        ]


_CURRENCY = re.compile(r"^[A-Z]{3}$")


def _check_pricing(name: str, price: Pricing) -> None:
    if not _CURRENCY.match(price.currency):
        raise PricingError(f"{name}: currency {price.currency!r} is not an ISO 4217 code")
    rates: dict[str, Decimal | None] = {
        "input_per_mtok": price.input_per_mtok,
        "output_per_mtok": price.output_per_mtok,
        "cached_input_per_mtok": price.cached_input_per_mtok,
        "cache_write_per_mtok": price.cache_write_per_mtok,
        "reasoning_per_mtok": price.reasoning_per_mtok,
        "batch_multiplier": price.batch_multiplier,
    }
    for field, value in rates.items():
        if value is not None and (not value.is_finite() or value < 0):
            raise PricingError(f"{name}: {field} must be a finite, non-negative amount")
