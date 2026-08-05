"""RSS feeds + source management."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from mastisk.db.queries import connect

router = APIRouter(tags=["sources"])


class FeedIn(BaseModel):
    url: str
    title: str | None = None


@router.get("/feeds")
def list_feeds():
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT url, title, last_fetched, last_etag, last_modified, enabled, added_at,
                      (SELECT COUNT(*) FROM sources
                         WHERE sources.url LIKE '%' || rss_feeds.url || '%' OR 0=1) AS items_ever
               FROM rss_feeds ORDER BY added_at DESC"""
        )]
    # items_ever is a rough count; useful enough for dashboard
    for r in rows:
        r["enabled"] = bool(r["enabled"])
    return {"feeds": rows}


@router.post("/feeds")
def add_feed(feed: FeedIn):
    url = feed.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must start with http:// or https://")
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO rss_feeds (url, title) VALUES (?, ?)",
            (url, feed.title or url),
        )
        row = conn.execute(
            "SELECT url, title, last_fetched, enabled, added_at FROM rss_feeds WHERE url = ?",
            (url,),
        ).fetchone()
    return {"ok": True, "feed": dict(row)}


@router.delete("/feeds")
def remove_feed(url: str):
    with connect() as conn:
        conn.execute("DELETE FROM rss_feeds WHERE url = ?", (url,))
    return {"ok": True}


@router.post("/feeds/toggle")
def toggle_feed(feed: FeedIn):
    with connect() as conn:
        conn.execute(
            "UPDATE rss_feeds SET enabled = 1 - enabled WHERE url = ?", (feed.url,)
        )
    return {"ok": True}


@router.post("/feeds/fetch")
async def fetch_now(feed: FeedIn, background: BackgroundTasks):
    """Run Scout against a specific feed right now (background task).

    The UI can call this when the user clicks "fetch now" on a feed row.
    """
    from mastisk.agents.scout import Scout

    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM rss_feeds WHERE url = ?", (feed.url,)).fetchone()
        if not exists:
            raise HTTPException(404, "feed not subscribed")

    async def run():
        await Scout()._fetch_feed(feed.url)

    background.add_task(run)
    return {"ok": True, "queued": feed.url}


@router.get("/sources")
def list_sources(limit: int = 50):
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT id, kind, url, title, published_at, fetched_at, author
               FROM sources ORDER BY fetched_at DESC LIMIT ?""",
            (limit,),
        )]
    return {"sources": rows}


@router.get("/jobs/{job_id}")
def get_job(job_id: int):
    """Single-job status lookup, used by the frontend to poll regenerate jobs
    without repeatedly pulling the full queue."""
    with connect() as conn:
        row = conn.execute(
            """SELECT id, agent, kind, status, attempts, error,
                      created_at, started_at, finished_at
               FROM jobs WHERE id = ?""",
            (job_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "job not found")
    return {"job": dict(row)}


@router.get("/jobs")
def list_jobs(limit: int = 50):
    """Return jobs with payload-derived detail so the queue view can show what's
    actually being compiled/scouted instead of opaque IDs."""
    import hashlib
    import json as _json

    def _src_id_for(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:16]

    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT id, agent, kind, status, attempts, created_at, started_at, finished_at, error, payload_json
               FROM jobs ORDER BY id DESC LIMIT ?""",
            (limit,),
        )]
        for r in rows:
            payload = {}
            try:
                payload = _json.loads(r.get("payload_json") or "{}")
            except Exception:
                payload = {}

            title = None
            subtitle = None
            source_kind = None
            url = None
            source_id: str | None = None

            if r["agent"] == "compiler" and payload.get("source_id"):
                source_id = payload["source_id"]
                src = conn.execute(
                    "SELECT title, url, kind FROM sources WHERE id=?",
                    (source_id,),
                ).fetchone()
                if src:
                    title = src["title"]
                    url = src["url"]
                    source_kind = src["kind"]
                    subtitle = _hostname(src["url"])
                else:
                    title = f"source {source_id}"
            elif r["agent"] == "scout" and payload.get("url"):
                url = payload["url"]
                feed = conn.execute(
                    "SELECT title FROM rss_feeds WHERE url=?", (url,),
                ).fetchone()
                title = (feed["title"] if feed else None) or _hostname(url) or url
                subtitle = _hostname(url)
                source_kind = "rss"
            elif r["agent"] == "listener":
                if r["kind"] == "transcribe" and payload.get("url"):
                    url = payload["url"]
                    source_id = _src_id_for(url)
                    src = conn.execute(
                        "SELECT title, kind FROM sources WHERE id=?", (source_id,),
                    ).fetchone()
                    title = (src["title"] if src and src["title"] else None) or _hostname(url) or url
                    source_kind = (
                        (src["kind"] if src else None)
                        or payload.get("media_type")
                        or _classify_url_cheap(url)
                    )
                    subtitle = _hostname(url)
                elif r["kind"] == "transcribe_audio":
                    audio_url = payload.get("audio_url") or ""
                    title = payload.get("episode_title") or payload.get("show_title") or audio_url
                    subtitle = payload.get("show_title") or _hostname(audio_url)
                    url = audio_url
                    source_kind = "podcast"
                    if audio_url:
                        source_id = _src_id_for(audio_url)
            elif r["agent"] == "ingest":
                title = payload.get("filename") or payload.get("source_rel_path") or r["kind"]
                subtitle = payload.get("source_rel_path") or payload.get("audio_path")
                source_kind = "document" if r["kind"] == "document" else "audio"
            elif r["agent"] == "artifact-agent" and payload.get("article_id"):
                art = conn.execute(
                    "SELECT title FROM articles WHERE id=?", (payload["article_id"],),
                ).fetchone()
                if art:
                    title = art["title"]
            elif r["agent"] in ("notetaker", "escalator") and payload.get("note_id"):
                note = conn.execute(
                    "SELECT slug, summary, classification, source FROM notes WHERE id=?",
                    (payload["note_id"],),
                ).fetchone()
                if note:
                    # Pre-classify notes have no summary yet — slug is the best
                    # human-readable fallback (matches the notes list UI).
                    title = note["summary"] or note["slug"]
                    subtitle = note["classification"] or note["source"]
            elif r["agent"] in ("github_poller", "github_ideator") and payload.get("repo_slug"):
                # Strip only the ``local:`` scheme prefix; github slugs are
                # already in owner/name form. The queue row's CSS truncates
                # long paths, so showing the full slug is fine.
                slug = payload["repo_slug"]
                title = slug[len("local:") :] if slug.startswith("local:") else slug

            # Resolve the downstream article id (if one exists) so the queue row
            # can become clickable. For compiler/listener sources, that's the
            # article whose row joins article_sources -> sources by source_id.
            # For artifact-agent, it's already the article_id in payload.
            article_id: str | None = None
            if r["agent"] == "artifact-agent":
                article_id = payload.get("article_id")
            elif source_id:
                art = conn.execute(
                    "SELECT article_id FROM article_sources WHERE source_id=? LIMIT 1",
                    (source_id,),
                ).fetchone()
                if art:
                    article_id = art["article_id"]

            r["detail"] = {
                "title": title,
                "subtitle": subtitle,
                "url": url,
                "source_kind": source_kind,
                "article_id": article_id,
            }
            r.pop("payload_json", None)
    return {"jobs": rows}


def _classify_url_cheap(url: str) -> str | None:
    """Cheap scheme/host-based URL type sniff for the queue display — avoids
    the async network-sniff in mastisk.integrations.podcasts.classify."""
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    lower = url.lower().split("?", 1)[0]
    if any(lower.endswith(ext) for ext in (".mp3", ".m4a", ".ogg", ".opus", ".wav")):
        return "podcast"
    if any(lower.endswith(ext) for ext in (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".m3u8")):
        return "video"
    return None


def _hostname(url: str | None) -> str | None:
    if not url:
        return None
    from urllib.parse import urlparse
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return None
    return host.removeprefix("www.") or None
