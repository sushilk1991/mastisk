"""Reusable vault templates API."""
from __future__ import annotations

from fastapi import APIRouter

from mastisk.projects.sync import list_checklist_templates

router = APIRouter(prefix="/api/templates", tags=["templates"])


@router.get("/checklists")
async def list_checklist_templates_endpoint() -> list[dict]:
    return list_checklist_templates()
