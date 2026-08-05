"""POST /api/listen — enqueue a URL for the Listener agent."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mastisk.agents.base import enqueue
from mastisk.integrations import podcasts

log = logging.getLogger("mastisk.listen_route")

router = APIRouter(tags=["listener"])


class ListenIn(BaseModel):
    url: str
    media_type: Literal["video", "podcast"] | None = None
    media_scope: Literal["episode", "show"] | None = None


@router.post("/listen")
async def listen(body: ListenIn) -> dict:
    url = (body.url or "").strip()
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

    payload = {"url": resolved_url}
    if body.media_type:
        payload["media_type"] = body.media_type
    if body.media_type == "podcast" and body.media_scope:
        payload["media_scope"] = body.media_scope
    job_id = enqueue("listener", "transcribe", payload)
    kind_label = body.media_type or (cls if cls != "unknown" else "source")
    discovered_note = (
        f" (auto-discovered feed: {resolved_url})"
        if resolved_url != url else ""
    )
    return {
        "job_id": job_id,
        "kind": "transcribe",
        "message": f"queued {kind_label} for transcription (job {job_id}){discovered_note}",
    }
