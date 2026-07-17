"""Daily digest — ranked + filtered so the front page is signal, not raw feed.

The agents still ingest everything into the graph/wiki; this route picks the
top N articles to actually show for a given date, records every considered
candidate + its scores to `digest_candidates` (for audit), and stamps each
thread with the user's current feedback verdict if any.
"""
from __future__ import annotations

import calendar as _calendar
from datetime import date as _date
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.services import digest_ranker

router = APIRouter(tags=["digest"])


def _parse_date(s: str | None) -> _date:
    """Parse an ISO YYYY-MM-DD date; default to UTC today. 400 on bad input."""
    if s is None or s == "":
        return datetime.utcnow().date()
    try:
        return _date.fromisoformat(s)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid date: {s!r}") from e


def _latest_feedback(conn, article_id: str, digest_date: str) -> str | None:
    """'yes' | 'no' | None for the most recent vote this user cast on the article
    within the context of this digest. We scope to the digest_date so the audit
    view can attribute feedback to the day it was shown.

    The frontend lets users toggle a vote off (click the active thumb again),
    which inserts a `kind='edited'` clear marker. We consider *all three* signal
    kinds here and take the most recent — if it's the clear marker, feedback
    is None.
    """
    row = conn.execute(
        """SELECT kind FROM signals
           WHERE article_id = ?
             AND json_extract(value_json, '$.digest_date') = ?
             AND (kind IN ('liked', 'disliked')
                  OR (kind = 'edited' AND json_extract(value_json, '$.from') = 'feedback'))
           ORDER BY ts DESC, id DESC LIMIT 1""",
        (article_id, digest_date),
    ).fetchone()
    if not row:
        return None
    if row["kind"] == "liked":
        return "yes"
    if row["kind"] == "disliked":
        return "no"
    return None  # cleared


@router.get("/digest")
def digest(date: str | None = None, top_n: int = digest_ranker.DEFAULT_TOP_N):
    d = _parse_date(date)
    d_iso = d.isoformat()
    with connect() as conn:
        label = d.strftime("%a · %b %-d")

        sources_today = conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE DATE(fetched_at) = ?",
            (d_iso,),
        ).fetchone()["n"]
        pages_today = conn.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE DATE(updated_at) = ? AND body_md != ''",
            (d_iso,),
        ).fetchone()["n"]
        new_concepts = conn.execute(
            """SELECT COUNT(*) AS n FROM articles
               WHERE kind='Concept' AND body_md != ''
                 AND DATE(created_at) >= DATE(?, '-7 day')
                 AND DATE(created_at) <= DATE(?)""",
            (d_iso, d_iso),
        ).fetchone()["n"]
        open_qs = conn.execute(
            "SELECT COUNT(*) AS n FROM article_sections WHERE kind='open'"
        ).fetchone()["n"]

        # Score + persist candidates for this date, then fetch the selected ones.
        selected = digest_ranker.rank_for_digest(conn, d_iso, top_n=top_n)
        considered = conn.execute(
            "SELECT COUNT(*) AS n FROM digest_candidates WHERE digest_date = ?",
            (d_iso,),
        ).fetchone()["n"]
        unshown = max(0, considered - len(selected))

        threads = [
            {
                "title": c.title,
                "body": c.summary,
                "sources": c.sources_count,
                "links": [
                    rr["label"] for rr in conn.execute(
                        """SELECT articles.title AS label FROM links
                           JOIN articles ON articles.id = links.to_article
                           WHERE links.from_article = ? ORDER BY weight DESC LIMIT 4""",
                        (c.article_id,),
                    )
                ],
                "article_id": c.article_id,
                "scores": {
                    "quality": round(c.quality, 3),
                    "interest": round(c.interest, 3),
                    "final": round(c.final, 3),
                },
                "feedback": _latest_feedback(conn, c.article_id, d_iso),
            }
            for c in selected
        ]

        queue = [
            r["obj"]
            for r in conn.execute(
                "SELECT obj FROM feed WHERE verb IN ('queued','transcribing') ORDER BY ts DESC LIMIT 4"
            )
        ]

        if sources_today or pages_today or new_concepts:
            summary = (
                f"{sources_today} sources read, {pages_today} pages touched, "
                f"{new_concepts} new concept clusters this week. "
                f"Top {len(selected)} of {considered} surfaced below."
            )
        else:
            summary = f"No agent activity on {d_iso}."

        prev_row = conn.execute(
            """SELECT DATE(updated_at) AS d FROM articles
               WHERE DATE(updated_at) < ? AND body_md != ''
               ORDER BY updated_at DESC LIMIT 1""",
            (d_iso,),
        ).fetchone()
        next_row = conn.execute(
            """SELECT DATE(updated_at) AS d FROM articles
               WHERE DATE(updated_at) > ? AND body_md != ''
               ORDER BY updated_at ASC LIMIT 1""",
            (d_iso,),
        ).fetchone()
        prev_date = prev_row["d"] if prev_row else None
        next_date = next_row["d"] if next_row else None

        return {
            "date": label,
            "iso_date": d_iso,
            "prev_date": prev_date,
            "next_date": next_date,
            "summary": summary,
            "counters": [
                {"label": "Sources read",  "value": sources_today},
                {"label": "Pages touched", "value": pages_today},
                {"label": "New concepts",  "value": new_concepts},
                {"label": "Open questions","value": open_qs},
            ],
            "threads": threads,
            "queue": queue,
            "meta": {
                "considered": considered,
                "selected": len(selected),
                "unshown": unshown,
            },
        }


class FeedbackIn(BaseModel):
    article_id: str
    verdict: str        # 'yes' | 'no' | 'clear'
    digest_date: str    # ISO date the digest was shown for


@router.post("/digest/feedback")
def feedback(body: FeedbackIn):
    if body.verdict not in {"yes", "no", "clear"}:
        raise HTTPException(status_code=400, detail=f"bad verdict: {body.verdict!r}")
    try:
        _date.fromisoformat(body.digest_date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"bad digest_date: {body.digest_date!r}") from e

    with connect() as conn:
        # 'clear' = undo prior verdict for this (article, date). We can't mutate
        # history, so we just record a signal of kind='edited' with the prior
        # info for the audit trail.
        if body.verdict == "clear":
            conn.execute(
                """INSERT INTO signals (article_id, kind, value_json)
                   VALUES (?, 'edited', json_object('from', 'feedback', 'digest_date', ?, 'verdict', 'clear'))""",
                (body.article_id, body.digest_date),
            )
            return {"ok": True, "verdict": None}

        kind = "liked" if body.verdict == "yes" else "disliked"
        q.add_signal(
            conn,
            article_id=body.article_id,
            kind=kind,
            value={"digest_date": body.digest_date},
        )
    return {"ok": True, "verdict": body.verdict}


@router.get("/digest/audit")
def audit(date: str | None = None, window_days: int = 1):
    """Full candidate + feedback log for auditing the ranker.

    window_days=1 (default) → just the given date.
    window_days=7 → the given date and the six before it (weekly audit view).
    """
    d = _parse_date(date)
    if window_days < 1 or window_days > 60:
        raise HTTPException(status_code=400, detail="window_days must be 1..60")
    start = (d - timedelta(days=window_days - 1)).isoformat()
    end = d.isoformat()

    with connect() as conn:
        cand_rows = conn.execute(
            """SELECT dc.digest_date, dc.article_id, dc.quality_score, dc.interest_score,
                      dc.final_score, dc.selected, dc.rank, dc.computed_at,
                      a.title, a.kind, a.summary
               FROM digest_candidates dc
               JOIN articles a ON a.id = dc.article_id
               WHERE dc.digest_date BETWEEN ? AND ?
               ORDER BY dc.digest_date DESC, dc.final_score DESC""",
            (start, end),
        ).fetchall()

        # Batch-fetch latest feedback per (article, date). Include the 'edited'
        # clear marker so a toggled-off vote correctly reads as no feedback.
        # Use ROW_NUMBER with `ts DESC, id DESC` so ties on ts resolve to the
        # most recently-inserted row (id is AUTOINCREMENT), matching the
        # single-row query in _latest_feedback.
        fb_rows = conn.execute(
            """WITH ranked AS (
                   SELECT article_id, kind,
                          json_extract(value_json, '$.digest_date') AS digest_date,
                          ROW_NUMBER() OVER (
                              PARTITION BY article_id, json_extract(value_json, '$.digest_date')
                              ORDER BY ts DESC, id DESC
                          ) AS rn
                   FROM signals
                   WHERE json_extract(value_json, '$.digest_date') BETWEEN ? AND ?
                     AND (kind IN ('liked', 'disliked')
                          OR (kind = 'edited'
                              AND json_extract(value_json, '$.from') = 'feedback'))
               )
               SELECT article_id, kind, digest_date FROM ranked WHERE rn = 1""",
            (start, end),
        ).fetchall()
        fb_by_key: dict[tuple[str, str], str | None] = {}
        for r in fb_rows:
            if r["kind"] == "liked":
                v: str | None = "yes"
            elif r["kind"] == "disliked":
                v = "no"
            else:
                v = None  # cleared
            fb_by_key[(r["article_id"], r["digest_date"])] = v

        by_date: dict[str, list[dict]] = {}
        for r in cand_rows:
            d_iso = r["digest_date"]
            by_date.setdefault(d_iso, []).append({
                "article_id": r["article_id"],
                "title": r["title"],
                "kind": r["kind"],
                "summary": r["summary"] or "",
                "quality": round(r["quality_score"], 3),
                "interest": round(r["interest_score"], 3),
                "final": round(r["final_score"], 3),
                "selected": bool(r["selected"]),
                "rank": r["rank"],
                "feedback": fb_by_key.get((r["article_id"], d_iso)),
            })

        # Rollup
        total = len(cand_rows)
        shown_rows = [r for r in cand_rows if r["selected"]]
        shown = len(shown_rows)
        liked = sum(1 for r in shown_rows if fb_by_key.get((r["article_id"], r["digest_date"])) == "yes")
        disliked = sum(1 for r in shown_rows if fb_by_key.get((r["article_id"], r["digest_date"])) == "no")
        unrated = shown - liked - disliked

        days = sorted(by_date.keys(), reverse=True)

    return {
        "start": start,
        "end": end,
        "window_days": window_days,
        "days": [{"date": d, "candidates": by_date[d]} for d in days],
        "rollup": {
            "total_considered": total,
            "total_shown": shown,
            "liked": liked,
            "disliked": disliked,
            "unrated": unrated,
            "hit_rate": (liked / shown) if shown else 0.0,
        },
    }


@router.get("/digest/calendar")
def digest_calendar(year: int, month: int):
    """Dates within (year, month) where a ranked digest was actually saved."""
    if not 1 <= month <= 12:
        raise HTTPException(status_code=400, detail=f"invalid month: {month}")
    if not 1900 <= year <= 2999:
        raise HTTPException(status_code=400, detail=f"invalid year: {year}")
    first = _date(year, month, 1).isoformat()
    last = _date(year, month, _calendar.monthrange(year, month)[1]).isoformat()
    with connect() as conn:
        rows = conn.execute(
            """SELECT DISTINCT dc.digest_date AS d
               FROM digest_candidates dc
               JOIN articles a ON a.id = dc.article_id
               WHERE dc.selected = 1
                 AND dc.digest_date BETWEEN ? AND ?
                 AND DATE(a.updated_at) = dc.digest_date
                 AND LENGTH(a.body_md) >= ?
               ORDER BY d ASC""",
            (first, last, digest_ranker.MIN_BODY_CHARS),
        ).fetchall()
    return {"active_dates": [r["d"] for r in rows]}
