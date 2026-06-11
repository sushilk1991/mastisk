"""Reminders API."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from mastisk.agents.reminder_engine import (
    cancel_reminder,
    create_custom_reminder,
    get_reminder,
    list_reminders,
    retry_reminder,
)

router = APIRouter(prefix="/api/reminders", tags=["reminders"])


class ReminderCreate(BaseModel):
    kind: Literal["custom"]
    fire_at: str
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    entity_type: str | None = None
    entity_id: str | None = None
    url: str | None = None

    @field_validator("fire_at")
    @classmethod
    def _valid_fire_at(cls, value: str) -> str:
        from datetime import datetime

        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("fire_at must be an ISO datetime") from exc
        return value


@router.get("")
async def list_reminders_endpoint(status: str | None = None) -> list[dict]:
    return list_reminders(status=status)


@router.post("", status_code=201)
async def create_reminder_endpoint(req: ReminderCreate) -> dict:
    return create_custom_reminder(
        fire_at=req.fire_at,
        title=req.title.strip(),
        body=req.body.strip(),
        entity_type=req.entity_type,
        entity_id=req.entity_id,
        url=req.url,
    )


@router.delete("/{reminder_id}")
async def cancel_reminder_endpoint(reminder_id: int) -> dict:
    existing = get_reminder(reminder_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    cancelled = cancel_reminder(reminder_id)
    if cancelled is None:
        raise HTTPException(status_code=409, detail="only pending reminders can be cancelled")
    return cancelled


@router.post("/{reminder_id}/retry")
async def retry_reminder_endpoint(reminder_id: int) -> dict:
    existing = get_reminder(reminder_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    retried = retry_reminder(reminder_id)
    if retried is None:
        raise HTTPException(status_code=409, detail="only notify_failed reminders can be retried")
    return retried
