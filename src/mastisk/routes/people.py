"""People / personal CRM API."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from mastisk.people.sync import (
    append_interaction,
    archive_person,
    create_person_file,
    list_people,
    patch_person,
    person_payload,
)
from mastisk.settings import get_settings

router = APIRouter(prefix="/api/people", tags=["people"])


class PersonCreate(BaseModel):
    name: str = Field(min_length=1)
    birthday: str | None = None
    anniversary: str | None = None
    facts: dict[str, Any] = Field(default_factory=dict)
    body: str | None = None
    follow_up_at: str | None = None

    @field_validator("name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must be non-blank")
        return value.strip()


class PersonPatch(BaseModel):
    name: str | None = None
    birthday: str | None = None
    anniversary: str | None = None
    facts: dict[str, Any] | None = None
    body: str | None = None
    follow_up_at: str | None = None


class InteractionCreate(BaseModel):
    text: str = Field(min_length=1)
    ts: str | None = None

    @field_validator("text")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must be non-blank")
        return value.strip()


@router.get("")
async def list_people_endpoint() -> list[dict]:
    today = _local_today()
    return [
        {
            **person,
            "birthday_soon": _date_within_days(person.get("birthday"), today, days=30),
        }
        for person in list_people()
    ]


@router.post("", status_code=201)
async def create_person_endpoint(req: PersonCreate) -> dict:
    try:
        return create_person_file(**req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{slug}")
async def get_person_endpoint(slug: str) -> dict:
    person = person_payload(slug)
    if person is None:
        raise HTTPException(status_code=404, detail="person not found")
    return {
        **person,
        "birthday_soon": _date_within_days(person.get("birthday"), _local_today(), days=30),
    }


@router.patch("/{slug}")
async def patch_person_endpoint(slug: str, req: PersonPatch) -> dict:
    updates = {
        key: value
        for key, value in req.model_dump().items()
        if key in req.model_fields_set
    }
    try:
        updated = patch_person(slug, updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="person not found")
    return updated


@router.post("/{slug}/interactions", status_code=201)
async def append_interaction_endpoint(slug: str, req: InteractionCreate) -> dict:
    try:
        updated = append_interaction(slug, req.text, ts=req.ts)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="person not found")
    return updated


@router.delete("/{slug}")
async def delete_person_endpoint(slug: str) -> dict:
    archived = archive_person(slug)
    if archived is None:
        raise HTTPException(status_code=404, detail="person not found")
    return archived


def _local_today() -> date:
    tz = ZoneInfo(get_settings().capture.default_timezone)
    return datetime.now(tz).date()


def _date_within_days(value: str | None, today: date, *, days: int) -> bool:
    if not value:
        return False
    month_day = value[5:] if len(value) == 10 else value
    try:
        month, day = [int(part) for part in month_day.split("-", 1)]
        candidate = date(today.year, month, day)
    except (ValueError, TypeError):
        return False
    if candidate < today:
        candidate = date(today.year + 1, month, day)
    return today <= candidate <= today + timedelta(days=days)
