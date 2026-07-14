"""Automations — CRUD + run-now for prose-defined background tasks."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mastisk.bgtasks import runner, sync
from mastisk.db.queries import connect

router = APIRouter(tags=["automations"])


class AutomationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    instructions: str = Field(min_length=1, max_length=8000)
    triggers: dict[str, Any] | None = None
    model: str | None = Field(default=None, max_length=80)
    active: bool = True


class AutomationPatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    instructions: str | None = Field(default=None, max_length=8000)
    triggers: dict[str, Any] | None = None
    model: str | None = Field(default=None, max_length=80)
    active: bool | None = None


@router.get("/automations")
def list_automations():
    return {"automations": sync.list_bg_tasks()}


@router.post("/automations", status_code=201)
def create_automation(body: AutomationCreate):
    try:
        return sync.create_bg_task(
            name=body.name, instructions=body.instructions,
            triggers=body.triggers, model=body.model, active=body.active,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/automations/{slug}")
def get_automation(slug: str):
    task = sync.bg_task_payload(slug)
    if task is None:
        raise HTTPException(status_code=404, detail="automation not found")
    task["index_md"] = sync.read_index(slug)
    with connect() as conn:
        runs = conn.execute(
            "SELECT * FROM bg_task_runs WHERE slug = ? ORDER BY id DESC LIMIT 20",
            (slug,),
        ).fetchall()
    task["runs"] = [dict(r) for r in runs]
    return task


@router.patch("/automations/{slug}")
def patch_automation(slug: str, body: AutomationPatch):
    updates = {k: v for k, v in body.model_dump().items() if k in body.model_fields_set}
    try:
        task = sync.patch_bg_task(slug, updates)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if task is None:
        raise HTTPException(status_code=404, detail="automation not found")
    return task


@router.post("/automations/{slug}/run", status_code=202)
async def run_now(slug: str):
    try:
        return await runner.run_task(slug, trigger="manual")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
