"""Rank articles for the daily digest and persist the candidate log.

Scoring is two layers:
  - quality_score : intrinsic — kind, sources, link degree, body depth, freshness.
  - interest_score: personal — bag-of-words cosine against the user profile.

final_score = quality * (0.3 + 0.7 * interest)

We anchor at 0.3 on the interest axis so a very high-quality item can still
surface even if the interest signal is lukewarm — that's deliberate; a brand
new strong synthesis shouldn't be invisible just because its terms don't
overlap with prior reads yet.

Candidate selection:
  1. Take all articles updated in the window (default: the digest's date)
     with non-empty body_md (exclude stubs).
  2. Score each.
  3. Persist every eligible candidate to `digest_candidates` with scores
     + selected flag + rank. This is the audit log the UI reads.
  4. Return the top N.
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mastisk.services import user_profile as up

DEFAULT_TOP_N = 5

_KIND_WEIGHT = {
    "Synthesis": 2.0,
    "Concept":   1.3,
    "Entity":    0.8,
    "Source":    0.4,
}

# Body length (in chars) below which we treat the article as too thin to
# include. Stubs (body_md == '') are already filtered by SQL; this catches
# lightly-compiled clips that nobody should read in depth.
MIN_BODY_CHARS = 1500


@dataclass
class ScoredCandidate:
    article_id: str
    title: str
    summary: str
    kind: str
    body_md: str
    sources_count: int
    backlinks_count: int
    forwardlinks_count: int
    updated_at: str
    quality: float
    interest: float
    final: float
    selected: bool = False
    rank: int | None = None


def _log1p_normalized(n: int | None, saturates_at: int) -> float:
    """log(n+1) mapped to [0, 1], saturating around `saturates_at`."""
    if not n or n <= 0:
        return 0.0
    return min(1.0, math.log1p(n) / math.log1p(saturates_at))


def _freshness(updated_at: str, reference_iso: str) -> float:
    """Exponential decay with ~2-day half-life, relative to the digest date."""
    try:
        u = datetime.fromisoformat(updated_at.replace(" ", "T"))
        r = datetime.fromisoformat(reference_iso)
        delta_h = max(0.0, (r - u.date() if hasattr(u, "date") else r - u).total_seconds() / 3600)
    except Exception:
        # Same-day if parsing fails — don't penalize.
        return 1.0
    return math.exp(-delta_h / 48.0)


def quality_score(article: dict[str, Any], reference_iso: str) -> float:
    """Intrinsic quality score. Returns 0 for disqualified (below MIN_BODY_CHARS)."""
    body = article.get("body_md") or ""
    if len(body) < MIN_BODY_CHARS:
        # Still return a tiny score > 0 so the audit log sees these too, but
        # well below any well-formed article.
        return 0.05

    kind_w = _KIND_WEIGHT.get(article.get("kind") or "", 0.5)
    src = _log1p_normalized(article.get("sources_count"), saturates_at=5)
    deg = _log1p_normalized(
        (article.get("backlinks_count") or 0) + (article.get("forwardlinks_count") or 0),
        saturates_at=8,
    )
    # Freshness is mild — a killer article from yesterday shouldn't lose to
    # today's mediocre one.
    fresh = _freshness(article.get("updated_at") or reference_iso, reference_iso)

    shape = 0.4 + 0.3 * src + 0.3 * deg  # 0.4..1.0
    return kind_w * shape * (0.5 + 0.5 * fresh)


def score_candidates(
    conn: sqlite3.Connection,
    digest_date: str,
    profile: up.UserProfile | None = None,
) -> list[ScoredCandidate]:
    """Score every article updated on `digest_date` that has a body.

    Pure — does not write to the DB. Caller decides what to persist.
    """
    if profile is None:
        profile = up.build_profile(conn)

    rows = conn.execute(
        """SELECT id, title, summary, kind, body_md, updated_at,
                  sources_count, backlinks_count, forwardlinks_count
           FROM articles
           WHERE DATE(updated_at) = ? AND body_md != ''""",
        (digest_date,),
    ).fetchall()

    out: list[ScoredCandidate] = []
    for r in rows:
        d = dict(r)
        bow = up.article_bow(r)
        q = quality_score(d, digest_date)
        i = up.interest_score(bow, profile)
        final = q * (0.3 + 0.7 * i)
        out.append(
            ScoredCandidate(
                article_id=d["id"],
                title=d["title"],
                summary=d["summary"] or "",
                kind=d["kind"],
                body_md=d["body_md"],
                sources_count=d["sources_count"] or 0,
                backlinks_count=d["backlinks_count"] or 0,
                forwardlinks_count=d["forwardlinks_count"] or 0,
                updated_at=d["updated_at"],
                quality=q,
                interest=i,
                final=final,
            )
        )

    out.sort(key=lambda c: c.final, reverse=True)
    return out


_DIVERSITY_THRESHOLD = 0.4


def _word_bag(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z]{3,}", text)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _diversified_top_n(
    candidates: list[ScoredCandidate],
    top_n: int,
    threshold: float = _DIVERSITY_THRESHOLD,
) -> list[int]:
    """Greedy MMR-style selection: walk the score-sorted list and skip any
    candidate whose title+summary+body-prefix has Jaccard overlap >= threshold
    with an already-selected candidate."""
    selected_idx: list[int] = []
    selected_bags: list[set[str]] = []
    for idx, c in enumerate(candidates):
        if len(selected_idx) >= top_n:
            break
        if c.quality < 0.1:
            continue
        bag = _word_bag(f"{c.title} {c.summary} {c.body_md[:500]}")
        if any(_jaccard(bag, sb) >= threshold for sb in selected_bags):
            continue
        selected_idx.append(idx)
        selected_bags.append(bag)
    return selected_idx


def persist_candidates(
    conn: sqlite3.Connection,
    digest_date: str,
    candidates: list[ScoredCandidate],
    top_n: int = DEFAULT_TOP_N,
) -> list[ScoredCandidate]:
    """Mark the top N as selected, upsert all candidates into digest_candidates.

    Returns the same list with `selected` and `rank` populated, top first.
    Re-running for the same date overwrites prior scores (the ranker may have
    changed, or new signals may shift the interest axis).
    """
    picks = set(_diversified_top_n(candidates, top_n))
    rank = 1
    for idx in sorted(picks):
        candidates[idx].selected = True
        candidates[idx].rank = rank
        rank += 1

    conn.executemany(
        """INSERT INTO digest_candidates
               (digest_date, article_id, quality_score, interest_score, final_score, selected, rank)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(digest_date, article_id) DO UPDATE SET
               quality_score  = excluded.quality_score,
               interest_score = excluded.interest_score,
               final_score    = excluded.final_score,
               selected       = excluded.selected,
               rank           = excluded.rank,
               computed_at    = CURRENT_TIMESTAMP""",
        [
            (digest_date, c.article_id, c.quality, c.interest, c.final,
             1 if c.selected else 0, c.rank)
            for c in candidates
        ],
    )
    return candidates


def rank_for_digest(
    conn: sqlite3.Connection,
    digest_date: str,
    top_n: int = DEFAULT_TOP_N,
) -> list[ScoredCandidate]:
    """Public entry: score, persist, return top-N selected."""
    cands = score_candidates(conn, digest_date)
    persist_candidates(conn, digest_date, cands, top_n=top_n)
    return [c for c in cands if c.selected]
