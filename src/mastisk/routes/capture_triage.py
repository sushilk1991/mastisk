"""Capture needs-triage queue API."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from mastisk.capture.triage import (
    TriageReclassifyError,
    list_triage_items,
    reclassify_triage_item,
)

# Localhost/tailnet trust model: triage can read vault contents and mutate
# files, so it must never live under /api/capture, the internet-tunneled ingress.
router = APIRouter(prefix="/api/triage", tags=["capture-triage"])

TriageTarget = Literal[
    "task",
    "note",
    "journal",
    "project_update",
    "routine_done",
    "person",
    "quote",
    "inventory",
    "content",
    "inbox",
    "dismiss",
]


class TriageReclassify(BaseModel):
    type: TriageTarget


@router.get("")
async def list_capture_triage_endpoint(
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    return list_triage_items(limit=limit)


@router.post("/{item_id}/reclassify")
async def reclassify_capture_triage_endpoint(
    item_id: str,
    req: TriageReclassify,
) -> dict:
    try:
        result = reclassify_triage_item(item_id, req.type)
    except TriageReclassifyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="triage item not found")
    return result
