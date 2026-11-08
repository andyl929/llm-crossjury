"""Peer-review topologies and normalized Borda aggregation."""

from __future__ import annotations

import pytest

from llm_crossjury.core.orchestrator.ranking import BordaResult, borda_aggregate
from llm_crossjury.core.orchestrator.topology import (
    ReviewAssignment,
    ReviewTopology,
    assign_reviews,
)
from llm_crossjury.core.schemas.review import PeerReview

A, B, C, D = "Response A", "Response B", "Response C", "Response D"


def _self(*labels: str) -> dict[str, str]:
    return {label: label for label in labels}


def _targets(assignments: list[ReviewAssignment]) -> dict[str, set[str]]:
    return {a.reviewer: set(a.targets) for a in assignments}


def test_full_reviews_every_other_answer() -> None:
    result = assign_reviews([A, B, C], _self(A, B, C), ReviewTopology.FULL, seed=1)
    assert _targets(result) == {A: {B, C}, B: {A, C}, C: {A, B}}
    assert [a.reviewer for a in result] == [A, B, C]


def test_full_with_two_answers_gives_one_target_each() -> None:
    result = assign_reviews([A, B], _self(A, B), ReviewTopology.FULL, seed=0)
    assert [(a.reviewer, a.targets) for a in result] == [(A, (B,)), (B, (A,))]


def test_ring_k2_with_four_answers() -> None:
    result = assign_reviews([A, B, C, D], _self(A, B, C, D), ReviewTopology.RING, seed=3)
    assert _targets(result) == {A: {B, C}, B: {C, D}, C: {D, A}, D: {A, B}}
    # Every answer is reviewed exactly k times.
    counts = {label: 0 for label in (A, B, C, D)}
    for assignment in result:
        for target in assignment.targets:
            counts[target] += 1
    assert set(counts.values()) == {2}


def test_ring_k_is_capped_at_n_minus_one() -> None:
    result = assign_reviews([A, B, C], _self(A, B, C), ReviewTopology.RING, ring_k=5, seed=0)
    assert _targets(result) == {A: {B, C}, B: {A, C}, C: {A, B}}


def test_ring_k1_is_issues_only() -> None:
    result = assign_reviews([A, B, C], _self(A, B, C), ReviewTopology.RING, ring_k=1, seed=0)
    assert [(a.reviewer, a.targets) for a in result] == [(A, (B,)), (B, (C,)), (C, (A,))]


def test_ring_rejects_k_below_one() -> None:
    with pytest.raises(ValueError, match="ring_k"):
        assign_reviews([A, B], _self(A, B), ReviewTopology.RING, ring_k=0)


def test_single_judge_reviews_all_but_its_own() -> None:
    labels = _self(A, B, C)
    result = assign_reviews([A, B, C], labels, ReviewTopology.SINGLE_JUDGE, judge=B, seed=0)
    assert _targets(result) == {B: {A, C}}
    outsider = assign_reviews([A, B], _self(A, B), ReviewTopology.SINGLE_JUDGE, judge="chair")
    assert _targets(outsider) == {"chair": {A, B}}


def test_single_judge_requires_judge() -> None:
    with pytest.raises(ValueError, match="judge"):
        assign_reviews([A, B], _self(A, B), ReviewTopology.SINGLE_JUDGE)


def test_duplicate_members_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        assign_reviews([A, A], _self(A, B), ReviewTopology.FULL)


def test_never_self_with_distinct_member_ids() -> None:
    labels = {A: "gpt", B: "claude", C: "gemini", D: "grok"}
    for topology in (ReviewTopology.FULL, ReviewTopology.RING):
        for assignment in assign_reviews(list(labels.values()), labels, topology, seed=9):
            assert all(labels[t] != assignment.reviewer for t in assignment.targets)


def test_single_answer_yields_no_assignments() -> None:
    assert assign_reviews([A], _self(A), ReviewTopology.FULL, seed=0) == []


def test_shuffle_is_deterministic_by_seed() -> None:
    members = [A, B, C, D]
    labels = _self(*members)
    first = assign_reviews(members, labels, ReviewTopology.FULL, seed=42)
    assert first == assign_reviews(members, labels, ReviewTopology.FULL, seed=42)
    orders = {
        a.targets
        for s in range(20)
        for a in assign_reviews(members, labels, ReviewTopology.FULL, seed=s)
        if a.reviewer == A
    }
    assert len(orders) > 1  # the seed actually changes the presentation order


def _review(*ranking: str) -> PeerReview:
    return PeerReview(reviews=[], ranking_rationale="", ranking=list(ranking))


def test_borda_normalized_points_and_mean() -> None:
    results = borda_aggregate([_review(A, B, C), _review(B, A), _review(A, C)])
    # A: 1, 0, 1 -> 2/3; B: 0.5, 1 -> 3/4; C: 0, 0 -> 0.
    assert [(r.label, r.informative_reviews) for r in results] == [(B, 2), (A, 3), (C, 2)]
    assert [r.score for r in results] == pytest.approx([0.75, 2 / 3, 0.0])


def test_borda_ignores_single_target_rankings() -> None:
    results = borda_aggregate([_review(B), _review(A, C)])
    assert results == [
        BordaResult(label=A, score=1.0, informative_reviews=1),
        BordaResult(label=C, score=0.0, informative_reviews=1),
        BordaResult(label=B, score=None, informative_reviews=0),
    ]


def test_borda_ties_keep_label_order() -> None:
    results = borda_aggregate([_review(C, B), _review(B, C)])
    assert [r.label for r in results] == [B, C]
    assert all(r.score == 0.5 for r in results)


def test_borda_empty() -> None:
    assert borda_aggregate([]) == []
