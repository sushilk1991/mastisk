"""Content pipeline API."""
from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from mastisk.agents.base import enqueue
from mastisk.content.sync import (
    CONTENT_STATUSES,
    content_payload,
    create_content_file,
    list_content,
    patch_content,
)
from mastisk.db import queries as q
from mastisk.db.queries import connect

router = APIRouter(prefix="/api/content", tags=["content"])

ContentKind = Literal["video", "article", "podcast", "newsletter"]
ContentStatus = Literal["idea", "outline", "editing", "waiting", "published", "done"]


class ContentCreate(BaseModel):
    title: str = Field(min_length=1)
    kind: ContentKind
    status: ContentStatus = "idea"
    domain: str | None = None
    channel: str | None = None
    url: str | None = None
    publish_date: str | None = None
    outline: str | None = None
    checklist_template: str | None = None

    @field_validator("title")
    @classmethod
    def _title_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must be non-blank")
        return value.strip()


class ContentPatch(BaseModel):
    status: ContentStatus | None = None
    domain: str | None = None
    channel: str | None = None
    url: str | None = None
    publish_date: str | None = None


@router.get("")
async def list_content_endpoint(
    kind: ContentKind | None = None,
    status: ContentStatus | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    items = list_content(kind=kind, status=status, domain=domain)
    return {"items": items, "kanban": _kanban(items)}


@router.post("", status_code=201)
async def create_content_endpoint(req: ContentCreate) -> dict[str, Any]:
    try:
        return create_content_file(**req.model_dump())
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{slug}")
async def get_content_endpoint(slug: str) -> dict[str, Any]:
    item = content_payload(slug)
    if item is None:
        raise HTTPException(status_code=404, detail="content item not found")
    return item


@router.patch("/{slug}")
async def patch_content_endpoint(slug: str, req: ContentPatch) -> dict[str, Any]:
    updates = {
        key: value
        for key, value in req.model_dump().items()
        if key in req.model_fields_set
    }
    try:
        item = patch_content(slug, updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="content item not found")
    return item


@router.post("/{slug}/draft", status_code=202)
async def spawn_content_draft_endpoint(slug: str) -> dict[str, Any]:
    item = content_payload(slug)
    if item is None:
        raise HTTPException(status_code=404, detail="content item not found")
    if item["kind"] not in {"article", "newsletter"}:
        raise HTTPException(
            status_code=422,
            detail="content draft spawning currently supports article or newsletter items via blog_writer",
        )
    existing = _active_content_draft(item["slug"])
    if existing is not None:
        return {**existing, "reused": True}
    theme = _blog_theme(item)
    with connect() as conn:
        blog_post_id = q.create_blog_post(conn, theme=theme, window_days=14)
    enqueue(
        "blog_writer",
        "draft",
        {
            "blog_post_id": blog_post_id,
            "content_slug": item["slug"],
            "content_source": _blog_content_source(item),
        },
    )
    return {"blog_post_id": blog_post_id, "status": "pending", "reused": False}


def _active_content_draft(slug: str) -> dict[str, Any] | None:
    with connect() as conn:
        rows = conn.execute(
            """SELECT payload_json FROM jobs
               WHERE agent = 'blog_writer'
                 AND kind = 'draft'
                 AND status IN ('queued', 'running')
                 AND json_valid(payload_json)
                 AND json_extract(payload_json, '$.content_slug') = ?
               ORDER BY id DESC
               LIMIT 5""",
            (slug,),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
                blog_post_id = int(payload["blog_post_id"])
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
            blog = q.get_blog_post(conn, blog_post_id)
            if blog and blog.get("deleted_at") is None and blog.get("status") in {"pending", "running"}:
                return {"blog_post_id": blog_post_id, "status": blog["status"]}
    return None


def _kanban(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    board = {status: [] for status in CONTENT_STATUSES}
    for item in items:
        board.setdefault(item["status"], []).append(item)
    return board


def _blog_theme(item: dict[str, Any]) -> str:
    outline = " ".join((item.get("body") or "").split())
    if outline:
        return f"{item['title']} — {outline}"[:500]
    return str(item["title"])[:500]


def _blog_content_source(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": item["slug"],
        "title": item["title"],
        "kind": item["kind"],
        "domain": item.get("domain"),
        "channel": item.get("channel"),
        "body": item.get("body") or item["title"],
        "updated_at": item.get("updated_at"),
        "path": item.get("path"),
    }
