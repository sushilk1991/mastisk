"""Projects API."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from mastisk.projects.sync import (
    ChecklistTemplateValidationError,
    MilestoneConflictError,
    MilestoneMissingError,
    append_project_milestone,
    append_project_time,
    create_project_file,
    get_project,
    list_projects,
    patch_project_frontmatter,
    project_payload,
    set_project_milestone_done,
)
from mastisk.tasks.parser import normalize_date_or_datetime

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    type: Literal["project", "area", "retainer"] = "project"
    domain: str | None = None
    status: Literal["active", "someday", "paused", "done"] = "active"
    due: str | None = None
    template: str | None = None
    recurring_items: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must be non-blank")
        return value.strip()

    @field_validator("due")
    @classmethod
    def _valid_due(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_date_or_datetime(value)


class ProjectPatch(BaseModel):
    name: str | None = None
    type: Literal["project", "area", "retainer"] | None = None
    domain: str | None = None
    status: Literal["active", "someday", "paused", "done"] | None = None
    due: str | None = None
    recurring_items: list[str] | None = None

    @field_validator("due")
    @classmethod
    def _valid_due(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_date_or_datetime(value)


class MilestoneCreate(BaseModel):
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must be non-blank")
        return value.strip()


class MilestonePatch(BaseModel):
    done: bool
    expected_text: str = Field(min_length=1)

    @field_validator("expected_text")
    @classmethod
    def _non_blank_expected_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("expected_text must be non-blank")
        return value.strip()


class TimeCreate(BaseModel):
    date: str | None = None
    hours: float = Field(ge=0.01, le=10000)
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must be non-blank")
        return value.strip()


@router.get("")
async def list_projects_endpoint() -> list[dict]:
    return list_projects()


@router.post("", status_code=201)
async def create_project_endpoint(req: ProjectCreate) -> dict:
    try:
        return create_project_file(**req.model_dump())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ChecklistTemplateValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{slug}")
async def get_project_endpoint(slug: str) -> dict:
    payload = project_payload(slug)
    if payload is None:
        raise HTTPException(status_code=404, detail="project not found")
    return payload


@router.patch("/{slug}")
async def patch_project_endpoint(slug: str, req: ProjectPatch) -> dict:
    row = get_project(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="project not found")
    updates = {
        key: value
        for key, value in req.model_dump().items()
        if key in req.model_fields_set
    }
    updated = patch_project_frontmatter(slug, updates)
    if updated is None:
        raise HTTPException(status_code=404, detail="project not found")
    return updated


@router.post("/{slug}/milestones", status_code=201)
async def append_milestone_endpoint(slug: str, req: MilestoneCreate) -> dict:
    updated = append_project_milestone(slug, req.text)
    if updated is None:
        raise HTTPException(status_code=404, detail="project not found")
    return updated


@router.patch("/{slug}/milestones/{position}")
async def toggle_milestone_endpoint(
    slug: str,
    position: int,
    req: MilestonePatch,
) -> dict:
    try:
        updated = set_project_milestone_done(
            slug,
            position,
            done=req.done,
            expected_text=req.expected_text,
        )
    except MilestoneMissingError as exc:
        raise HTTPException(status_code=404, detail="milestone not found") from exc
    except MilestoneConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="project not found")
    return updated


@router.post("/{slug}/time", status_code=201)
async def append_time_endpoint(slug: str, req: TimeCreate) -> dict:
    try:
        updated = append_project_time(
            slug,
            entry_date=req.date,
            hours=req.hours,
            text=req.text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="project not found")
    return updated
