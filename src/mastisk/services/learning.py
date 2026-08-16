"""Pure learning-domain logic for Guru: syllabus DAG hygiene, timeline
pacing, and the FSRS scheduling wrapper.

Everything here is deterministic and DB-free so it can be unit-tested
without a daemon. The LLM proposes concepts and prerequisite edges;
this module is the code-side enforcement that turns that proposal into
a valid, ordered, pace-able syllabus:

- ``validate_dag`` drops self-loops, unknown refs, and cycle-closing
  edges (an LLM edge list is a claim, not a guarantee).
- ``topological_order`` linearizes the DAG with tier as tiebreak so
  easier concepts surface first among peers.
- ``concepts_per_lesson`` is the timeline→density rule: fewer remaining
  days per remaining concept ⇒ more concepts packed into each lesson.
- ``new_card`` / ``review_card`` wrap py-fsrs so callers only handle
  JSON blobs and ints.
"""
from __future__ import annotations

import math
from datetime import UTC, date, datetime

from fsrs import Card, Rating, Scheduler

# Successive relearning (Rawson & Dunlosky): a concept graduates to
# 'mastered' after this many successful recalls in separate sessions.
MASTERY_RECALLS = 3

# FSRS ratings — mirror fsrs.Rating values so the rest of the codebase
# can pass plain ints across the API boundary.
RATING_AGAIN = 1
RATING_HARD = 2
RATING_GOOD = 3
RATING_EASY = 4


# ───── syllabus DAG ─────


def validate_dag(concepts: list[dict], edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return the subset of ``edges`` that forms a valid DAG over ``concepts``.

    Each edge is ``(prereq_slug, dependent_slug)``. Drops, in order:
    self-loops, edges naming unknown slugs, duplicate edges, and any edge
    whose addition would close a cycle (checked incrementally, so earlier
    edges win — the LLM lists its most confident edges first).
    """
    known = {c["slug"] for c in concepts}
    adjacency: dict[str, set[str]] = {slug: set() for slug in known}
    accepted: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for prereq, dependent in edges:
        if prereq == dependent:
            continue
        if prereq not in known or dependent not in known:
            continue
        if (prereq, dependent) in seen:
            continue
        seen.add((prereq, dependent))
        if _reaches(adjacency, start=dependent, target=prereq):
            continue  # would close a cycle
        adjacency[prereq].add(dependent)
        accepted.append((prereq, dependent))
    return accepted


def _reaches(adjacency: dict[str, set[str]], *, start: str, target: str) -> bool:
    """DFS: is ``target`` reachable from ``start``?"""
    stack = [start]
    visited: set[str] = set()
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in visited:
            continue
        visited.add(node)
        stack.extend(adjacency.get(node, ()))
    return False


def topological_order(concepts: list[dict], edges: list[tuple[str, str]]) -> list[str]:
    """Kahn's algorithm over a validated edge set.

    Tiebreak among ready nodes: tier ASC (easier first), then original
    list position (the LLM's own narrative ordering) — this front-loads
    an early payoff without violating prerequisites.
    """
    order_index = {c["slug"]: i for i, c in enumerate(concepts)}
    tier = {c["slug"]: int(c.get("tier") or 1) for c in concepts}
    indegree = {slug: 0 for slug in order_index}
    dependents: dict[str, list[str]] = {slug: [] for slug in order_index}
    for prereq, dependent in edges:
        indegree[dependent] += 1
        dependents[prereq].append(dependent)

    ready = sorted(
        (slug for slug, deg in indegree.items() if deg == 0),
        key=lambda s: (tier[s], order_index[s]),
    )
    out: list[str] = []
    while ready:
        node = ready.pop(0)
        out.append(node)
        for dep in dependents[node]:
            indegree[dep] -= 1
            if indegree[dep] == 0:
                # Insert keeping (tier, original position) order.
                ready.append(dep)
                ready.sort(key=lambda s: (tier[s], order_index[s]))
    # Edges came from validate_dag, so a partial order means a bug — but
    # degrade gracefully by appending any stragglers in list order.
    if len(out) < len(order_index):
        out.extend(s for s in order_index if s not in set(out))
    return out


def target_concept_count(timeline_days: int | None) -> tuple[int, int]:
    """(min, max) concept-count guidance for the syllabus prompt.

    Open-ended goals get the full 12-40 range; a timeline caps the count
    so the syllabus is actually completable at a sane per-lesson density.
    """
    if timeline_days is None or timeline_days <= 0:
        return (12, 40)
    upper = max(6, min(40, timeline_days * 3))
    lower = max(4, min(12, timeline_days))
    return (min(lower, upper), upper)


def concepts_per_lesson(
    *,
    remaining_concepts: int,
    target_date: str | None,
    today: date,
    active_goals: int = 1,
    max_per_lesson: int = 5,
) -> int:
    """Timeline → lesson density.

    A goal shares the one-lesson-per-day budget with the other active
    goals (rotation), so its effective remaining lesson slots are
    ``remaining_days / active_goals``. Density is the ceiling needed to
    finish in those slots, clamped to [1, max_per_lesson]. No target
    date ⇒ the default single-concept Feynman lesson.
    """
    if remaining_concepts <= 0:
        return 0
    if not target_date:
        return 1
    try:
        target = date.fromisoformat(target_date[:10])
    except ValueError:
        return 1
    remaining_days = max(1, (target - today).days + 1)
    slots = max(1, remaining_days // max(1, active_goals))
    density = math.ceil(remaining_concepts / slots)
    return max(1, min(int(max_per_lesson), density))


# ───── FSRS wrapper ─────


def _scheduler(desired_retention: float = 0.9) -> Scheduler:
    return Scheduler(desired_retention=desired_retention)


def new_card() -> str:
    """A fresh FSRS card, serialized. Due immediately (first check
    question in the lesson is its first review)."""
    return Card().to_json()


def review_card(
    card_json: str,
    rating: int,
    *,
    desired_retention: float = 0.9,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Apply one review. Returns ``(new_card_json, due_at_utc_iso)``.

    ``rating`` is 1-4 (Again/Hard/Good/Easy). Invalid ratings raise
    ValueError — callers validate user input before reaching here.
    """
    if rating not in (1, 2, 3, 4):
        raise ValueError(f"rating must be 1-4, got {rating!r}")
    card = Card.from_json(card_json)
    scheduler = _scheduler(desired_retention)
    when = now.astimezone(UTC) if now else datetime.now(UTC)
    card, _log = scheduler.review_card(card, Rating(rating), review_datetime=when)
    return card.to_json(), card.due.astimezone(UTC).isoformat()


def card_retrievability(card_json: str, *, desired_retention: float = 0.9) -> float:
    """Current P(recall) for a serialized card — the interleaving
    selector uses this to pick the shakiest concepts for warmups."""
    try:
        card = Card.from_json(card_json)
    except Exception:
        return 0.0
    return float(_scheduler(desired_retention).get_card_retrievability(card))
