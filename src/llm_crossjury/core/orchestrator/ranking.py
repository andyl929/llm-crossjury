"""Aggregate peer-review rankings with a normalized Borda count.

Each ``PeerReview.ranking`` covers ``m`` targets. Position ``p`` (0 = best) earns
``(m - 1 - p) / (m - 1)`` points, so every review contributes on a 0..1 scale
regardless of how many targets it ranked. Reviews with ``m == 1`` carry no ranking
information and contribute nothing (they still contribute issues). A label's score is
its mean points over the reviews that ranked it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from llm_crossjury.core.schemas.review import PeerReview


@dataclass(frozen=True)
class BordaResult:
    label: str
    score: float | None
    """Mean normalized Borda points; ``None`` if no informative review ranked it."""
    informative_reviews: int
    """Reviews with ``m >= 2`` that ranked this label."""


def borda_aggregate(reviews: Sequence[PeerReview]) -> list[BordaResult]:
    """Return one result per ranked label, best first (ties keep label order)."""
    raise NotImplementedError
