"""Journal API."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from mastisk.journal import (
    JournalFrontmatterError,
    append_log,
    assemble_journal_day,
    list_journal_days,
    set_mood_energy,
    set_reflections,
)
from mastisk.settings import get_settings

router = APIRouter(prefix="/api/journal", tags=["journal"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class JournalLogCreate(BaseModel):
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must be non-blank")
        return value.strip()


class JournalPatch(BaseModel):
    mood: int | None = Field(default=None, ge=1, le=5)
    energy: int | None = Field(default=None, ge=1, le=5)
    reflections: str | None = None


@router.get("")
async def list_journal_endpoint(
    limit: int = Query(default=30, ge=1, le=365),
) -> list[dict]:
    return list_journal_days(limit=limit)


@router.get("/{day}")
async def get_journal_day_endpoint(day: str) -> dict:
    valid_day = _validate_day(day)
    try:
        assembled = assemble_journal_day(valid_day)
    except JournalFrontmatterError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if assembled is None:
        raise HTTPException(status_code=404, detail="journal day not found")
    return assembled


@router.post("/{day}/log", status_code=201)
async def append_journal_log_endpoint(day: str, req: JournalLogCreate) -> dict:
    valid_day = _validate_day(day)
    try:
        result = append_log(valid_day, req.text, _now_in_capture_timezone())
    except JournalFrontmatterError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        **result,
        "type": "journal",
        "destination": result["path"],
    }


@router.patch("/{day}")
async def patch_journal_day_endpoint(day: str, req: JournalPatch) -> dict:
    valid_day = _validate_day(day)
    try:
        scale_updates = {}
        if "mood" in req.model_fields_set:
            scale_updates["mood"] = req.mood
        if "energy" in req.model_fields_set:
            scale_updates["energy"] = req.energy
        if scale_updates:
            set_mood_energy(valid_day, **scale_updates)
        if "reflections" in req.model_fields_set:
            set_reflections(valid_day, req.reflections or "")
        assembled = assemble_journal_day(valid_day)
    except JournalFrontmatterError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if assembled is None:
        raise HTTPException(status_code=404, detail="journal day not found")
    return assembled


def _validate_day(value: str) -> str:
    if not _DATE_RE.match(value):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD") from exc
    if parsed > _tomorrow():
        raise HTTPException(status_code=422, detail="date cannot be later than tomorrow")
    return parsed.isoformat()


def _tomorrow() -> date:
    tz = ZoneInfo(get_settings().capture.default_timezone)
    return datetime.now(tz).date() + timedelta(days=1)


def _now_in_capture_timezone() -> datetime:
    tz = ZoneInfo(get_settings().capture.default_timezone)
    return datetime.now(tz)
