"""Price lookup over the model registry (``config/models.yaml``, ADR 0006).

Prices are NEVER hard-coded or guessed. They live in ``ModelEntry.pricing`` and are
used only when the entry is verified (``source_url`` and ``last_verified`` set). An
``api`` model that is unregistered, unpriced or unverified cannot be estimated, and a
run that needs it is always refused; ``allow_unknown_cost`` does not override this
(it only admits ``cli`` members, whose per-call cost is unknowable). Prices apply to
the ``api`` backend only.
"""

from __future__ import annotations

from datetime import date

from llm_crossjury.core.registry.resolver import ModelRegistry
from llm_crossjury.core.registry.schema import ModelEntry, Pricing


class PricingError(Exception):
    """A needed price is missing, unverified, or malformed."""


class PricingTable:
    """Read-only price lookup backed by a :class:`ModelRegistry`."""

    def __init__(self, registry: ModelRegistry) -> None:
        raise NotImplementedError

    def lookup(self, provider: str, model_id: str) -> Pricing:
        """Return the verified price of an ``api`` model.

        Raises:
            PricingError: if the model is unregistered, unpriced or unverified. There is
                no fallback price.
        """
        raise NotImplementedError

    def stale_entries(self, today: date, max_age_days: int) -> list[ModelEntry]:
        """Return priced entries whose ``last_verified`` is older than ``max_age_days``."""
        raise NotImplementedError
