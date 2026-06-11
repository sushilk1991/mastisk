"""Tasks API."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from mastisk.paths import vault_dir
from mastisk.projects.sync import get_project
from mastisk.tasks.sync import (
    TaskLineMissingError,
    append_task_to_host,
    get_task,
    journal_host_for_today,
    list_tasks,
    rewrite_task,
)
from mastisk.tasks.parser import normalize_date_or_datetime

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskCreate(BaseModel):
    text: str = Field(min_length=1)
    host_path: str | None = None
    project: str | None = None
    due: str | None = None
    scheduled: str | None = None
    recurrence: str | None = None
    priority: Literal["high", "medium", "low"] | None = None
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must be non-blank")
        return value.strip()

    @field_validator("due", "scheduled")
    @classmethod
    def _valid_date_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_date_or_datetime(value)


class TaskPatch(BaseModel):
    due: str | None = None
    scheduled: str | None = None
    recurrence: str | None = None
    priority: Literal["high", "medium", "low"] | None = None

    @field_validator("due", "scheduled")
    @classmethod
    def _valid_date_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_date_or_datetime(value)


@router.get("")
async def list_tasks_endpoint(
    status: str | None = None,
    due_before: str | None = None,
    domain: str | None = None,
    project: str | None = None,
) -> list[dict]:
    return list_tasks(
        status=status,
        due_before=due_before,
        domain=domain,
        project=project,
    )


@router.post("", status_code=201)
async def create_task_endpoint(req: TaskCreate) -> dict:
    host = _host_for_task(req.project, req.host_path)
    return append_task_to_host(
        host,
        text=req.text,
        due=req.due,
        scheduled=req.scheduled,
        recurrence=req.recurrence,
        priority=req.priority,
        tags=req.tags,
        links=req.links,
    )


@router.patch("/{uid}/toggle")
async def toggle_task_endpoint(uid: str) -> dict:
    task = get_task(uid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        updated = rewrite_task(uid, checked=not task["checked"])
    except TaskLineMissingError as exc:
        raise HTTPException(status_code=404, detail="task line not found; mirror refreshed") from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="task not found")
    return updated


@router.patch("/{uid}")
async def patch_task_endpoint(uid: str, req: TaskPatch) -> dict:
    if get_task(uid) is None:
        raise HTTPException(status_code=404, detail="task not found")
    updates = {
        key: value
        for key, value in req.model_dump().items()
        if key in req.model_fields_set
    }
    try:
        updated = rewrite_task(uid, **updates)
    except TaskLineMissingError as exc:
        raise HTTPException(status_code=404, detail="task line not found; mirror refreshed") from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="task not found")
    return updated


def _host_for_task(project: str | None, host_path: str | None) -> Path:
    if project:
        row = get_project(project)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        return vault_dir() / row["path"]
    if host_path:
        rel = Path(host_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise HTTPException(status_code=422, detail="host_path must be vault-relative")
        return vault_dir() / rel
    return journal_host_for_today()
