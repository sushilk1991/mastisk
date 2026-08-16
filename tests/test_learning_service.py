"""Unit tests for the pure learning-domain logic (DAG, pacing, FSRS wrapper).

Intent under test:
- The LLM's edge list is a *claim*; validate_dag must make it a real DAG.
- Timeline is the density dial: fewer remaining days per remaining concept
  must pack more concepts into each lesson, bounded by max_per_lesson.
- FSRS wrapping round-trips card state and moves due dates forward on
  success.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from mastisk.services.learning import (
    MASTERY_RECALLS,
    card_retrievability,
    concepts_per_lesson,
    new_card,
    review_card,
    target_concept_count,
    topological_order,
    validate_dag,
)


def _concepts(*slugs: str, tiers: dict[str, int] | None = None) -> list[dict]:
    tiers = tiers or {}
    return [{"slug": s, "tier": tiers.get(s, 1)} for s in slugs]


# ───── DAG hygiene ─────


def test_validate_dag_drops_self_loops_unknowns_and_duplicates() -> None:
    concepts = _concepts("a", "b")
    edges = [("a", "a"), ("ghost", "b"), ("a", "ghost"), ("a", "b"), ("a", "b")]
    assert validate_dag(concepts, edges) == [("a", "b")]


def test_validate_dag_drops_cycle_closing_edge_keeping_earlier_edges() -> None:
    concepts = _concepts("a", "b", "c")
    edges = [("a", "b"), ("b", "c"), ("c", "a")]  # last edge closes the cycle
    assert validate_dag(concepts, edges) == [("a", "b"), ("b", "c")]


def test_validate_dag_detects_transitive_cycles() -> None:
    concepts = _concepts("a", "b", "c", "d")
    edges = [("a", "b"), ("b", "c"), ("c", "d"), ("d", "a")]
    assert ("d", "a") not in validate_dag(concepts, edges)


def test_topological_order_respects_prerequisites() -> None:
    concepts = _concepts("advanced", "basics", "middle")
    edges = validate_dag(
        concepts, [("basics", "middle"), ("middle", "advanced")],
    )
    order = topological_order(concepts, edges)
    assert order.index("basics") < order.index("middle") < order.index("advanced")


def test_topological_order_tiebreaks_easier_tier_first() -> None:
    concepts = _concepts("hard", "easy", tiers={"hard": 3, "easy": 1})
    assert topological_order(concepts, []) == ["easy", "hard"]


def test_topological_order_covers_every_concept() -> None:
    concepts = _concepts("a", "b", "c")
    order = topological_order(concepts, [("a", "b")])
    assert sorted(order) == ["a", "b", "c"]


# ───── timeline pacing (the user's density requirement) ─────


def test_open_ended_goal_teaches_one_concept_per_lesson() -> None:
    assert concepts_per_lesson(
        remaining_concepts=30, target_date=None, today=date(2026, 8, 16),
    ) == 1


def test_short_timeline_packs_more_concepts_per_lesson() -> None:
    # 20 concepts, 7 days left → ceil(20/7) = 3 per lesson.
    assert concepts_per_lesson(
        remaining_concepts=20,
        target_date="2026-08-22",
        today=date(2026, 8, 16),
    ) == 3


def test_long_timeline_stays_at_one_concept_per_lesson() -> None:
    assert concepts_per_lesson(
        remaining_concepts=20,
        target_date="2026-12-31",
        today=date(2026, 8, 16),
    ) == 1


def test_density_clamps_at_max_per_lesson() -> None:
    # 40 concepts due tomorrow would need 20/day — clamp to the ceiling.
    assert concepts_per_lesson(
        remaining_concepts=40,
        target_date="2026-08-17",
        today=date(2026, 8, 16),
        max_per_lesson=5,
    ) == 5


def test_multiple_active_goals_share_the_daily_slot_and_raise_density() -> None:
    # Rotation halves this goal's lesson slots, so density doubles.
    one_goal = concepts_per_lesson(
        remaining_concepts=12, target_date="2026-08-27", today=date(2026, 8, 16),
    )
    two_goals = concepts_per_lesson(
        remaining_concepts=12, target_date="2026-08-27", today=date(2026, 8, 16),
        active_goals=2,
    )
    assert one_goal == 1
    assert two_goals == 2


def test_past_target_date_still_paces_sanely() -> None:
    got = concepts_per_lesson(
        remaining_concepts=9,
        target_date="2026-08-01",
        today=date(2026, 8, 16),
        max_per_lesson=5,
    )
    assert got == 5  # overdue: pack as much as allowed, never crash


def test_zero_remaining_concepts_means_no_new_teaching() -> None:
    assert concepts_per_lesson(
        remaining_concepts=0, target_date="2026-08-22", today=date(2026, 8, 16),
    ) == 0


def test_target_concept_count_scales_with_timeline() -> None:
    lo_short, hi_short = target_concept_count(5)
    lo_open, hi_open = target_concept_count(None)
    assert hi_short < hi_open
    assert lo_short <= hi_short
    assert (lo_open, hi_open) == (12, 40)


# ───── FSRS wrapper ─────


def test_review_card_good_schedules_future_due_date() -> None:
    card = new_card()
    now = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)
    updated, due = review_card(card, 3, now=now)
    assert updated != card
    assert datetime.fromisoformat(due) > now


def test_review_card_again_comes_back_sooner_than_easy() -> None:
    now = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)
    _, due_again = review_card(new_card(), 1, now=now)
    _, due_easy = review_card(new_card(), 4, now=now)
    assert datetime.fromisoformat(due_again) < datetime.fromisoformat(due_easy)


def test_review_card_rejects_out_of_range_rating() -> None:
    with pytest.raises(ValueError):
        review_card(new_card(), 5)
    with pytest.raises(ValueError):
        review_card(new_card(), 0)


def test_card_retrievability_is_a_probability() -> None:
    card, _ = review_card(new_card(), 3)
    assert 0.0 <= card_retrievability(card) <= 1.0
    assert card_retrievability("not json") == 0.0


def test_mastery_threshold_is_three_successive_recalls() -> None:
    # Guard: the successive-relearning contract the routes build on.
    assert MASTERY_RECALLS == 3
