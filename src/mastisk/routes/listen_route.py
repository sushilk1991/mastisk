"""POST /api/listen — enqueue a URL for the Listener agent."""
from __future__ import annotations

import json
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mastisk.db.queries import connect
from mastisk.integrations import podcasts, youtube

log = logging.getLogger("mastisk.listen_route")

router = APIRouter(tags=["listener"])


class ListenIn(BaseModel):
    url: str
    title: str | None = Field(default=None, max_length=500)
    media_type: Literal["video", "podcast"] | None = None
    media_scope: Literal["episode", "show"] | None = None


@router.post("/listen")
async def listen(body: ListenIn) -> dict:
    url = youtube.canonicalize_url(body.url)
    if not url:
        raise HTTPException(400, "url required")
    # Only reject Spotify here — it's definitively unsupported (DRM). For
    # anything else, queue the job and let the agent do a more definitive
    # classification. Rejecting on "unknown" from the route would wrongly 400
    # on transient network failures during classify.
    #
    # Podcast show hints use classify_and_resolve so an advertised RSS feed is
    # preferred over scraping/transcribing the show page itself. Episode hints
    # keep the exact page: replacing one with a show feed would transcribe the
    # newest episode instead. Video hints likewise keep the original page rather
    # than following an unrelated site-wide RSS link.
    try:
        if body.media_type == "video" or (
            body.media_type == "podcast" and body.media_scope == "episode"
        ):
            cls = await podcasts.classify(url)
            resolved_url = url
        else:
            cls, resolved_url = await podcasts.classify_and_resolve(url)
    except Exception as e:
        log.info("classify failed for %s: %s", url, e)
        cls, resolved_url = "unknown", url

    if cls == "spotify":
        raise HTTPException(
            400,
            "Spotify podcasts are DRM-protected and can't be ingested. "
            "Try the podcast's RSS feed URL or Apple Podcasts link.",
        )

    resolved_url = youtube.canonicalize_url(resolved_url)
    payload = {"url": resolved_url}
    if body.media_type:
        payload["media_type"] = body.media_type
    if body.media_type == "podcast" and body.media_scope:
        payload["media_scope"] = body.media_scope
    title = (body.title or "").strip()
    if title:
        payload["title"] = title

    # A completed podcast-show job may legitimately be submitted again later to
    # discover a newer episode. A video URL, by contrast, names one immutable
    # media item, so reuse its completed job/source as well as active work.
    reuse_done = (
        body.media_type == "video"
        or body.media_scope == "episode"
        or cls in {"youtube", "direct_audio"}
    )
    queued = enqueue_listener_once(payload, reuse_done=reuse_done)
    job_id = queued["job_id"]
    kind_label = body.media_type or (cls if cls != "unknown" else "source")
    discovered_note = (
        f" (auto-discovered feed: {resolved_url})"
        if resolved_url != url else ""
    )
    if queued["duplicate"]:
        state = queued["status"]
        if state == "done":
            message = f"{kind_label} already ingested"
        else:
            job_note = f" (job {job_id})" if job_id else ""
            message = f"{kind_label} already {state}{job_note}"
        return {
            **queued,
            "kind": "transcribe",
            "message": f"{message}{discovered_note}",
        }
    return {
        "job_id": job_id,
        "kind": "transcribe",
        "status": "queued",
        "duplicate": False,
        "message": f"queued {kind_label} for transcription (job {job_id}){discovered_note}",
    }


def enqueue_listener_once(payload: dict, *, reuse_done: bool) -> dict:
    """Atomically reuse matching Listener work or insert exactly one job.

    ``BEGIN IMMEDIATE`` makes the read-then-insert decision safe across
    concurrent requests and multiple server workers without adding a global
    uniqueness rule that would prevent failed-job retries or periodic podcast
    refreshes.
    """
    url = str(payload["url"])
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if reuse_done:
            source = conn.execute(
                "SELECT id FROM sources WHERE url=? LIMIT 1",
                (url,),
            ).fetchone()
            if source is not None:
                conn.execute("COMMIT")
                return {
                    "job_id": None,
                    "status": "done",
                    "duplicate": True,
                    "source_id": source["id"],
                }

        statuses = ("queued", "running", "done") if reuse_done else ("queued", "running")
        placeholders = ", ".join("?" for _ in statuses)
        existing = conn.execute(
            f"""SELECT id, status, payload_json FROM jobs
                WHERE agent='listener' AND kind='transcribe'
                  AND status IN ({placeholders})
                  AND json_valid(payload_json)
                  AND json_extract(payload_json, '$.url') = ?
                ORDER BY CASE status
                           WHEN 'running' THEN 0
                           WHEN 'queued' THEN 1
                           ELSE 2
                         END,
                         id ASC
                LIMIT 1""",
            (*statuses, url),
        ).fetchone()
        if existing is None and reuse_done:
            # Upgrade safety: jobs queued by older extension/CLI versions may
            # still carry an embed, Shorts, mobile, or share URL. Compare the
            # small active set after canonicalization so rollout itself cannot
            # create one last duplicate before those legacy jobs finish.
            candidates = conn.execute(
                """SELECT id, status, payload_json FROM jobs
                   WHERE agent='listener' AND kind='transcribe'
                     AND status IN ('queued', 'running')
                     AND json_valid(payload_json)
                   ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, id ASC"""
            ).fetchall()
            for candidate in candidates:
                try:
                    candidate_payload = json.loads(candidate["payload_json"] or "{}")
                except json.JSONDecodeError:
                    continue
                if not isinstance(candidate_payload, dict):
                    continue
                candidate_url = youtube.canonicalize_url(candidate_payload.get("url") or "")
                if candidate_url == url:
                    existing = candidate
                    break
        if existing is not None:
            # A later duplicate may carry the browser's useful page title while
            # an older caller sent only the URL. Fill that display metadata in
            # without creating another unit of work.
            try:
                original_payload = json.loads(existing["payload_json"] or "{}")
            except json.JSONDecodeError:
                original_payload = {}
            existing_payload = dict(original_payload)
            if payload.get("title") and not existing_payload.get("title"):
                existing_payload["title"] = payload["title"]
            if existing_payload.get("url") != url:
                existing_payload["url"] = url
            if existing_payload != original_payload:
                conn.execute(
                    "UPDATE jobs SET payload_json=? WHERE id=?",
                    (json.dumps(existing_payload), existing["id"]),
                )
            conn.execute("COMMIT")
            return {
                "job_id": existing["id"],
                "status": existing["status"],
                "duplicate": True,
            }

        cur = conn.execute(
            "INSERT INTO jobs (agent, kind, payload_json) VALUES ('listener', 'transcribe', ?)",
            (json.dumps(payload),),
        )
        conn.execute("COMMIT")
        return {
            "job_id": cur.lastrowid or 0,
            "status": "queued",
            "duplicate": False,
        }
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
