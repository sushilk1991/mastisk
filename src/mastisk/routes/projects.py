"""Projects API."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from mastisk.paths import vault_dir
from mastisk.projects.sync import (
    create_project_file,
    get_project,
    list_projects,
    parse_project_file,
    patch_project_frontmatter,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    type: Literal["project", "area"] = "project"
    domain: str | None = None
    status: Literal["active", "someday", "paused", "done"] = "active"
    due: str | None = None

    @field_validator("name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must be non-blank")
        return value.strip()


class ProjectPatch(BaseModel):
    name: str | None = None
    type: Literal["project", "area"] | None = None
    domain: str | None = None
    status: Literal["active", "someday", "paused", "done"] | None = None
    due: str | None = None


@router.get("")
async def list_projects_endpoint() -> list[dict]:
    return list_projects()


@router.post("", status_code=201)
async def create_project_endpoint(req: ProjectCreate) -> dict:
    return create_project_file(**req.model_dump())


@router.get("/{slug}")
async def get_project_endpoint(slug: str) -> dict:
    row = get_project(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="project not found")
    parsed = parse_project_file(vault_dir() / row["path"])
    return {**row, "frontmatter": parsed["frontmatter"], "body": parsed["body"]}


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
