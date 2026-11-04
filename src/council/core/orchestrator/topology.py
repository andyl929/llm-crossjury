"""Peer-review topologies (ADR 0003).

With ``n`` kept answers:

* ``FULL``: every member reviews every other answer. ``n * (n - 1)`` reviews.
* ``RING``: member ``i`` reviews answer ``i + 1 (mod n)``, optionally ``k`` steps.
  ``n * k`` reviews.
* ``SINGLE_JUDGE``: one designated judge reviews all answers in one call. ``1`` call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class ReviewTopology(StrEnum):
    FULL = "full"
    RING = "ring"
    SINGLE_JUDGE = "single_judge"


@dataclass(frozen=True)
class ReviewAssignment:
    """One review call: ``reviewer`` (member id) reviews ``targets`` (anonymous labels)."""

    reviewer: str
    targets: tuple[str, ...]


def assign_reviews(
    members: Sequence[str],
    labels: dict[str, str],
    topology: ReviewTopology,
    *,
    ring_k: int = 1,
    judge: str | None = None,
    seed: int | None = None,
) -> list[ReviewAssignment]:
    """Return review assignments.

    A member is never assigned its own answer. Targets are shuffled with ``seed``
    so that presentation order does not leak authorship.

    Args:
        members: Member ids whose answers survived checkpoint A.
        labels: Anonymous label -> member id.
        topology: Review topology.
        ring_k: Number of successors each member reviews in ``RING``.
        judge: Member id (or chair id) used by ``SINGLE_JUDGE``.
        seed: Shuffle seed, recorded for reproducibility.
    """
    raise NotImplementedError
