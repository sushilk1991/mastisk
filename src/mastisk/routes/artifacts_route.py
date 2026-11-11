"""Per-article artifacts — charts, comparison cards, timelines, stat panels.

Artifacts are declarative specs (Chart.js config for charts; structured JSON
for the rest) that the right-rail component renders next to an article body.
The route layer handles the dict<->JSON string conversion; the DB column is
TEXT and the API surface is parsed ``spec``.
"""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mastisk.agents.base import enqueue
from mastisk.db import queries as q
from mastisk.db.queries import connect

router = APIRouter(tags=["artifacts"])


# ─────────────────────────────── Pydantic models ───────────────────────────────

class ArtifactIn(BaseModel):
    kind: Literal["chart", "comparison", "timeline", "stat"]
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    spec_json: dict  # the raw spec; server stores as JSON text
    created_by: str | None = None


class ArtifactPatch(BaseModel):
    """PATCH body — every field optional; only provided fields are updated."""
    kind: Literal["chart", "comparison", "timeline", "stat"] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    spec_json: dict | None = None


class ArtifactOut(BaseModel):
    id: int
    article_id: str
    kind: str
    title: str
    description: str | None
    spec: dict   # deserialized; always returned as parsed JSON
    created_by: str | None
    created_at: str
    updated_at: str


# ─────────────────────────────── Helpers ───────────────────────────────

def _row_to_out(row: dict) -> ArtifactOut:
    """Convert a DB row (with ``spec_json`` as TEXT) to an ArtifactOut (parsed ``spec``).

    Corrupt JSON in the DB is a 500 — writes go through this module and always
    ``json.dumps`` a dict, so seeing invalid JSON on read means something has
    gone very wrong and silently falling back would hide the bug.
    """
    raw = row.get("spec_json") or "null"
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"corrupt artifact spec for id={row.get('id')}: {e}")
    if not isinstance(spec, dict):
        raise HTTPException(500, f"artifact spec for id={row.get('id')} is not an object")
    return ArtifactOut(
        id=row["id"],
        article_id=row["article_id"],
        kind=row["kind"],
        title=row["title"],
        description=row.get("description"),
        spec=spec,
        created_by=row.get("created_by"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _ensure_article(conn, article_id: str) -> None:
    row = conn.execute("SELECT 1 FROM articles WHERE id = ?", (article_id,)).fetchone()
    if not row:
        raise HTTPException(404, "article not found")


# ─────────────────────────────── Routes ───────────────────────────────

@router.get("/articles/{article_id}/artifacts")
def list_article_artifacts(article_id: str):
    with connect() as conn:
        rows = q.list_artifacts(conn, article_id)
    return {"artifacts": [_row_to_out(r) for r in rows]}


@router.post("/articles/{article_id}/artifacts")
def create_article_artifact(article_id: str, body: ArtifactIn):
    with connect() as conn:
        _ensure_article(conn, article_id)
        row = q.create_artifact(
            conn,
            article_id=article_id,
            kind=body.kind,
            title=body.title,
            description=body.description,
            spec_json=json.dumps(body.spec_json),
            created_by=body.created_by or "user",
        )
    return {"artifact": _row_to_out(row)}


@router.patch("/artifacts/{artifact_id}")
def update_article_artifact(artifact_id: int, body: ArtifactPatch):
    with connect() as conn:
        # 404 early so we don't do a no-op update and then re-check
        if q.get_artifact(conn, artifact_id) is None:
            raise HTTPException(404, "artifact not found")
        updated = q.update_artifact(
            conn,
            artifact_id,
            kind=body.kind,
            title=body.title,
            description=body.description,
            spec_json=json.dumps(body.spec_json) if body.spec_json is not None else None,
        )
        if updated is None:
            # Race: row vanished between the SELECT and the UPDATE. Unlikely
            # given single-writer SQLite, but return 404 rather than 500.
            raise HTTPException(404, "artifact not found")
    return {"artifact": _row_to_out(updated)}


@router.delete("/artifacts/{artifact_id}")
def delete_article_artifact(artifact_id: int):
    with connect() as conn:
        ok = q.delete_artifact(conn, artifact_id)
    if not ok:
        raise HTTPException(404, "artifact not found")
    return {"ok": True}


@router.post("/articles/{article_id}/artifacts/regenerate")
def regenerate_article_artifacts(article_id: str):
    """Enqueue an artifact-agent job (``agents/artifact_agent.py``)."""
    with connect() as conn:
        _ensure_article(conn, article_id)
    job_id = enqueue("artifact-agent", "generate", {"article_id": article_id})
    return {"queued": True, "job_id": job_id}
