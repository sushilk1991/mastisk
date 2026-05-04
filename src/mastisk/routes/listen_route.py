"""POST /api/listen — enqueue a URL for the Listener agent."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mastisk.agents.base import enqueue
from mastisk.integrations import podcasts

log = logging.getLogger("mastisk.listen_route")

router = APIRouter(tags=["listener"])


class ListenIn(BaseModel):
    url: str


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
    # classify_and_resolve also auto-discovers RSS feeds inside HTML pages
    # (e.g. https://www.founderspodcast.com/episodes → its Megaphone feed),
    # so the user can paste a podcast show page and it Just Works. We pass
    # the *resolved* URL into the job so the Listener doesn't repeat discovery.
    try:
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

    job_id = enqueue("listener", "transcribe", {"url": resolved_url})
    kind_label = cls if cls != "unknown" else "source"
    discovered_note = (
        f" (auto-discovered feed: {resolved_url})"
        if resolved_url != url else ""
    )
    return {
        "job_id": job_id,
        "kind": "transcribe",
        "message": f"queued {kind_label} for transcription (job {job_id}){discovered_note}",
    }
