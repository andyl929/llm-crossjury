"""Peer-review topologies (ADR 0003).

With ``n`` kept answers:

* ``FULL``: every member reviews every other answer. ``n * (n - 1)`` reviews.
* ``RING``: member ``i`` reviews answer ``i + 1 (mod n)``, optionally ``k`` steps.
  ``n * k`` reviews.
* ``SINGLE_JUDGE``: one designated judge reviews all answers in one call. ``1`` call.

Default when not configured (``llm_crossjury.core.config.resolve_topology``):
``FULL`` for <= 3 reviewed answers, ``RING`` with ``k = 2`` for >= 4.
``RING`` with ``k = 1`` is the "issues only, no ranking" mode: each reviewer sees a
single target, so rankings carry no information and Borda aggregation is skipped.
"""

from __future__ import annotations

import random
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


def _ring_targets(member: str, ordered: list[str], labels: dict[str, str], k: int) -> list[str]:
    """The ``k`` successors (capped at ``n - 1``) of each answer ``member`` owns."""
    n = len(ordered)
    targets: list[str] = []
    for index, label in enumerate(ordered):
        if labels[label] != member:
            continue
        for step in range(1, min(k, n - 1) + 1):
            target = ordered[(index + step) % n]
            if labels[target] != member and target not in targets:
                targets.append(target)
    return targets


def assign_reviews(
    members: Sequence[str],
    labels: dict[str, str],
    topology: ReviewTopology,
    *,
    ring_k: int = 2,
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
        seed: Shuffle seed, recorded for reproducibility. The same inputs and seed
            always give the same assignments; ``None`` shuffles nondeterministically.

    Returns:
        One assignment per reviewer that has at least one target, in ``members``
        order (``SINGLE_JUDGE``: at most one assignment, for ``judge``). Answers are
        ordered by label; in ``RING`` the successors of an answer are the next labels
        in that order, wrapping around, and ``ring_k`` is capped at ``n - 1``.

    Raises:
        ValueError: on duplicate members, ``ring_k < 1`` in ``RING``, or a missing
            ``judge`` in ``SINGLE_JUDGE``.
    """
    if len(set(members)) != len(members):
        raise ValueError("duplicate member ids")
    ordered = sorted(labels)
    if topology is ReviewTopology.SINGLE_JUDGE:
        if judge is None:
            raise ValueError("single_judge topology needs a judge")
        plan = [(judge, [label for label in ordered if labels[label] != judge])]
    elif topology is ReviewTopology.FULL:
        plan = [
            (member, [label for label in ordered if labels[label] != member]) for member in members
        ]
    else:
        if ring_k < 1:
            raise ValueError("ring_k must be >= 1")
        plan = [(member, _ring_targets(member, ordered, labels, ring_k)) for member in members]
    # Not security-sensitive: the shuffle only hides authorship from position.
    rng = random.Random(seed)  # noqa: S311
    assignments: list[ReviewAssignment] = []
    for reviewer, targets in plan:
        if not targets:
            continue
        rng.shuffle(targets)
        assignments.append(ReviewAssignment(reviewer=reviewer, targets=tuple(targets)))
    return assignments
