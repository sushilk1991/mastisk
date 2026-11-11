"""Build a lightweight user interest profile from engagement signals.

The profile is a pair of term bags — positive (things you engaged with) and
negative (things you dismissed) — derived from the `signals` and `pinned`
tables. No embeddings: bag-of-words is deterministic, fast, and readable for
audit. When article_embeddings gets populated in the future, `interest_score`
in digest_ranker can transparently swap to vector cosine with the same profile
interface.

Positive-weighted signals, with per-kind horizon + weight:
  - liked      : any time, weight 3.0
  - pinned     : any time, weight 2.0
  - time_read  : last 30d,  weight 1.5  (read for any length)
  - opened     : last 30d,  weight 0.6  (clicked a title; weakest)
  - asked      : last 60d,  weight 1.0  (asked a question about it)

Negative-weighted signals:
  - disliked   : any time, weight 3.0
  - skipped    : last 30d, weight 0.8
  - deleted    : any time, weight 2.0

The profile is rebuilt fresh each digest request — 283 articles × a few
hundred signals is milliseconds.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

# Permissive tokenizer: word chars, len >= 3. Lowercased. Tuned for titles +
# summaries + tags, not dense prose, so we don't need stopword lists beyond
# a short one for common words that show up in every article.
_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9]{2,}")
_STOP = frozenset({
    "the", "and", "for", "that", "this", "with", "from", "what", "how",
    "its", "are", "was", "has", "have", "been", "not", "but", "you",
    "your", "all", "new", "when", "can", "may", "one", "two", "get",
    "out", "about", "into", "via", "more", "most", "some", "like",
    "over", "per", "day", "days", "week", "month", "year", "time",
    "just", "them", "their", "they", "there", "who", "which", "also",
    "now", "still", "than", "then", "too", "very", "way", "should",
    "could", "would", "will", "make", "made", "see", "post", "posts",
    "article", "articles", "page", "pages", "wiki", "note", "notes",
})


def _tokenize(text: str | None) -> list[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text) if t.lower() not in _STOP]


def _article_terms(row: sqlite3.Row | dict) -> list[str]:
    """Terms that characterize an article — cheap surface bag."""
    parts: list[str] = []
    parts.extend(_tokenize(row["title"] if row else None))
    parts.extend(_tokenize(row["summary"] if "summary" in row.keys() else None))
    # Body is too long; use the first ~600 chars, which covers the lead paragraph.
    body = row["body_md"] if "body_md" in row.keys() else ""
    if body:
        parts.extend(_tokenize(body[:600]))
    return parts


_SIGNAL_WEIGHT = {
    "liked":     (3.0, None),   # weight, horizon_days (None = unbounded)
    "disliked":  (3.0, None),
    "pinned":    (2.0, None),
    "deleted":   (2.0, None),
    "time_read": (1.5, 30),
    "asked":     (1.0, 60),
    "opened":    (0.6, 30),
    "skipped":   (0.8, 30),
}
_POSITIVE_KINDS = {"liked", "pinned", "time_read", "asked", "opened"}
_NEGATIVE_KINDS = {"disliked", "skipped", "deleted"}


@dataclass
class UserProfile:
    """Weighted term bags. `mass_*` is the sum of per-signal weights that went
    into the bag — used to normalize when comparing profiles of different
    sizes. Empty-profile is fine: interest_score returns a neutral 0.5."""
    positive: Counter[str] = field(default_factory=Counter)
    negative: Counter[str] = field(default_factory=Counter)
    mass_pos: float = 0.0
    mass_neg: float = 0.0
    article_count: int = 0  # number of distinct articles that fed either bag

    def is_empty(self) -> bool:
        return self.article_count == 0


def build_profile(conn: sqlite3.Connection) -> UserProfile:
    """Assemble the profile from all relevant signals + pins."""
    prof = UserProfile()
    seen: set[str] = set()

    # Gather per-article signal weights first so we only fetch each article once.
    per_article: dict[str, tuple[float, float]] = {}  # article_id -> (pos, neg)

    for kind, (weight, horizon) in _SIGNAL_WEIGHT.items():
        where = "kind = ? AND article_id IS NOT NULL"
        params: list = [kind]
        if horizon is not None:
            where += f" AND ts >= datetime('now', '-{horizon} day')"
        rows = conn.execute(
            f"SELECT article_id, COUNT(*) AS n FROM signals WHERE {where} GROUP BY article_id",
            params,
        ).fetchall()
        for r in rows:
            aid = r["article_id"]
            p, n = per_article.get(aid, (0.0, 0.0))
            # Diminishing returns: repeated signals of the same kind shouldn't
            # linearly pile up (sqrt keeps a long-time-read from drowning
            # everything else).
            contrib = weight * (r["n"] ** 0.5)
            if kind in _POSITIVE_KINDS:
                per_article[aid] = (p + contrib, n)
            elif kind in _NEGATIVE_KINDS:
                per_article[aid] = (p, n + contrib)

    # Explicit pins are a strong positive, even if no signals row exists yet.
    for r in conn.execute("SELECT article_id FROM pinned"):
        aid = r["article_id"]
        p, n = per_article.get(aid, (0.0, 0.0))
        per_article[aid] = (p + 2.0, n)

    if not per_article:
        return prof

    # One query for all articles in the set.
    ids = list(per_article.keys())
    placeholders = ",".join("?" * len(ids))
    art_rows = conn.execute(
        f"SELECT id, title, summary, body_md FROM articles WHERE id IN ({placeholders})",
        ids,
    ).fetchall()

    for row in art_rows:
        aid = row["id"]
        if aid in seen:
            continue
        seen.add(aid)
        pos_w, neg_w = per_article.get(aid, (0.0, 0.0))
        terms = _article_terms(row)
        if not terms:
            continue
        for t in terms:
            if pos_w:
                prof.positive[t] += pos_w
            if neg_w:
                prof.negative[t] += neg_w
        prof.mass_pos += pos_w
        prof.mass_neg += neg_w

    prof.article_count = len(seen)
    return prof


def article_bow(row: sqlite3.Row | dict) -> Counter[str]:
    """Bag-of-words for a candidate article (for interest scoring)."""
    return Counter(_article_terms(row))


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    """Cosine similarity between two term bags. Empty bags → 0."""
    if not a or not b:
        return 0.0
    # Pick the smaller for iteration.
    if len(a) > len(b):
        a, b = b, a
    dot = 0.0
    for t, ca in a.items():
        cb = b.get(t)
        if cb:
            dot += ca * cb
    if dot == 0:
        return 0.0
    na = sum(v * v for v in a.values()) ** 0.5
    nb = sum(v * v for v in b.values()) ** 0.5
    return dot / (na * nb)


def interest_score(bow: Counter[str], profile: UserProfile) -> float:
    """Score a candidate's relevance to the profile, in [0, 1].

    Neutral 0.5 when the profile is empty or the candidate has no overlap
    with either bag — so quality alone drives selection on day 1.
    """
    if profile.is_empty():
        return 0.5
    pos_sim = _cosine(bow, profile.positive)
    neg_sim = _cosine(bow, profile.negative)
    # Pull toward 0 for disliked terms, toward 1 for liked terms, with 0.5
    # as the "no information" fallback.
    raw = pos_sim - 0.6 * neg_sim
    # Sigmoid-ish map to [0, 1] with 0 → 0.5.
    return max(0.0, min(1.0, 0.5 + 0.5 * raw))
