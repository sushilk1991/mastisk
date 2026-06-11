"""Routines API."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from mastisk.routines.sync import (
    archive_routine,
    completion_dates,
    create_routine_file,
    get_routine,
    list_routines,
    local_today,
    routine_streak_summary,
    toggle_routine_completion,
)

router = APIRouter(prefix="/api/routines", tags=["routines"])


class RoutineCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    domain: str | None = None
    time_of_day: Literal["morning", "afternoon", "evening", "anytime"] = "anytime"
    specific_time: str | None = None
    notify: bool = False
    streak_type: Literal["ongoing", "fixed"] = "ongoing"
    target_days: int | None = None
    start_date: str | None = None

    @field_validator("name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must be non-blank")
        return value.strip()


@router.get("")
async def list_routines_endpoint(archived: bool = False) -> dict[str, list[dict]]:
    today = local_today()
    grouped: dict[str, list[dict]] = {
        "morning": [],
        "afternoon": [],
        "evening": [],
        "anytime": [],
    }
    for routine in list_routines(include_archived=archived):
        dates = set(completion_dates(routine["slug"]))
        grouped[routine["time_of_day"]].append(
            {
                **routine,
                "completed_today": today in dates,
                "streak": routine_streak_summary(routine, today=today),
            }
        )
    return grouped


@router.post("", status_code=201)
async def create_routine_endpoint(req: RoutineCreate) -> dict:
    return create_routine_file(**req.model_dump())


@router.post("/{slug}/toggle")
async def toggle_routine_endpoint(slug: str, date: str | None = None) -> dict:
    try:
        updated = toggle_routine_completion(slug, date_value=date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="routine not found")
    return updated


@router.post("/{slug}/archive")
async def archive_routine_endpoint(slug: str) -> dict:
    updated = archive_routine(slug)
    if updated is None:
        raise HTTPException(status_code=404, detail="routine not found")
    return updated


@router.get("/{slug}/progress")
async def routine_progress_endpoint(slug: str, days: int | None = None) -> dict:
    routine = get_routine(slug, include_archived=True)
    if routine is None:
        raise HTTPException(status_code=404, detail="routine not found")
    if days is not None and days <= 0:
        raise HTTPException(status_code=422, detail="days must be positive")
    return {
        "slug": slug,
        "completion_dates": completion_dates(slug, days=days),
    }
