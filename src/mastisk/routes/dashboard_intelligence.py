"""Dashboard intelligence API."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from mastisk.dashboard.intelligence import (
    FocusFullError,
    dismiss_needs_review,
    list_focus,
    list_needs_review,
    list_slipping,
    mute_slipping,
    resurface_for_date,
    snooze_slipping,
    star_focus,
    unstar_focus,
)

router = APIRouter(tags=["dashboard-intelligence"])


class FocusStar(BaseModel):
    task_uid: str = Field(min_length=1)
    replace_uid: str | None = None


class SnoozeRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=365)


@router.get("/api/focus/{day}")
async def get_focus_endpoint(day: str) -> list[dict]:
    return list_focus(day)


@router.post("/api/focus/{day}", status_code=201)
async def star_focus_endpoint(day: str, req: FocusStar, response: Response) -> list[dict]:
    try:
        focus = star_focus(day, req.task_uid, replace_uid=req.replace_uid)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FocusFullError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "focus_full", "focus": exc.focus},
        ) from exc
    if req.replace_uid:
        response.status_code = 200
    return focus


@router.delete("/api/focus/{day}/{task_uid}")
async def unstar_focus_endpoint(day: str, task_uid: str) -> list[dict]:
    return unstar_focus(day, task_uid)


@router.get("/api/slipping")
async def list_slipping_endpoint() -> list[dict]:
    return list_slipping()


@router.post("/api/slipping/{entity_type}/{entity_id}/snooze")
async def snooze_slipping_endpoint(
    entity_type: str,
    entity_id: str,
    req: SnoozeRequest,
) -> dict:
    updated = snooze_slipping(entity_type, entity_id, days=req.days)
    if updated is None:
        raise HTTPException(status_code=404, detail="slipping entity not found")
    return updated


@router.post("/api/slipping/{entity_type}/{entity_id}/mute")
async def mute_slipping_endpoint(entity_type: str, entity_id: str) -> dict:
    updated = mute_slipping(entity_type, entity_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="slipping entity not found")
    return updated


@router.get("/api/resurface/{day}", response_model=None)
async def resurface_endpoint(day: str) -> dict | Response:
    item = resurface_for_date(day)
    if item is None:
        return Response(status_code=204)
    return item


@router.get("/api/needs-review")
async def list_needs_review_endpoint() -> list[dict]:
    return list_needs_review()


@router.post("/api/needs-review/{item_id}/dismiss")
async def dismiss_needs_review_endpoint(item_id: int) -> dict:
    dismissed = dismiss_needs_review(item_id)
    if dismissed is None:
        raise HTTPException(status_code=404, detail="needs-review item not found")
    return dismissed
