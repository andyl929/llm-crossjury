"""Aggregate peer-review rankings with a normalized Borda count.

Each ``PeerReview.ranking`` covers ``m`` targets. Position ``p`` (0 = best) earns
``(m - 1 - p) / (m - 1)`` points, so every review contributes on a 0..1 scale
regardless of how many targets it ranked. Reviews with ``m == 1`` carry no ranking
information and contribute nothing (they still contribute issues). A label's score is
its mean points over the reviews that ranked it.

Not called at all in the "issues only" mode (``ring`` with ``k = 1``); the report then
shows issue counts by severity without a ranking.
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
    """Return one result per ranked label, best first (ties keep label order).

    Labels that appear only in rankings with ``m == 1`` get ``score=None`` and sort
    after every scored label. Callers are expected to pass only reviews whose ranking
    is a permutation of their targets; a repeated label in one ranking is counted at
    its first position only.
    """
    points: dict[str, list[float]] = {}
    for review in reviews:
        ranking = list(dict.fromkeys(review.ranking))
        m = len(ranking)
        for position, label in enumerate(ranking):
            earned = points.setdefault(label, [])
            if m >= 2:  # noqa: PLR2004
                earned.append((m - 1 - position) / (m - 1))
    results = [
        BordaResult(
            label=label,
            score=sum(earned) / len(earned) if earned else None,
            informative_reviews=len(earned),
        )
        for label, earned in points.items()
    ]
    results.sort(key=lambda r: (r.score is None, -(r.score or 0.0), r.label))
    return results
