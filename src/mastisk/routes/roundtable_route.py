"""Roundtable API — fan-out sessions across Claude/Codex/Gemini/Ollama."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from mastisk.agents.base import enqueue
from mastisk.db import queries as q
from mastisk.db.queries import connect

router = APIRouter(prefix="/api/roundtables", tags=["roundtable"])


class CreateRoundtableRequest(BaseModel):
    input_type: Literal["note", "article", "prompt"]
    input_ref: str = ""
    prompt: str = Field(min_length=1)

    @field_validator("prompt")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("prompt must be non-blank")
        return v


@router.post("", status_code=202)
async def create_roundtable_endpoint(req: CreateRoundtableRequest) -> dict:
    # Validate the input_ref points at something real (or is empty for 'prompt').
    if req.input_type == "note":
        try:
            nid = int(req.input_ref)
        except ValueError:
            raise HTTPException(status_code=422, detail="note input_ref must be an integer note_id")
        with connect() as conn:
            n = q.get_note(conn, nid)
        if n is None or n.get("deleted_at") is not None:
            raise HTTPException(status_code=404, detail="note not found")
    elif req.input_type == "article":
        if not req.input_ref:
            raise HTTPException(status_code=422, detail="article input_ref must be a non-empty article id")
        with connect() as conn:
            row = conn.execute("SELECT 1 FROM articles WHERE id = ?", (req.input_ref,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="article not found")
    # 'prompt' needs no input_ref validation — can be empty.

    with connect() as conn:
        rt_id = q.create_roundtable(
            conn,
            input_type=req.input_type,
            input_ref=req.input_ref,
            prompt=req.prompt,
        )
    enqueue("roundtable", "fan_out", {"roundtable_id": rt_id})
    return {"id": rt_id, "status": "pending"}


@router.get("")
async def list_roundtables_endpoint(limit: int = 50, before: int | None = None) -> list[dict]:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be 1..500")
    with connect() as conn:
        rows = q.list_roundtables(conn, limit=limit, before_id=before)
    return [_summary(r) for r in rows]


@router.get("/{rt_id}")
async def get_roundtable_endpoint(rt_id: int) -> dict:
    with connect() as conn:
        rt = q.get_roundtable(conn, rt_id)
        if rt is None:
            raise HTTPException(status_code=404, detail="roundtable not found")
        perspectives = q.list_roundtable_perspectives(conn, rt_id)
    return {**_detail(rt), "perspectives": perspectives}


@router.post("/{rt_id}/save-as-note", status_code=201)
async def save_as_note_endpoint(rt_id: int) -> dict:
    with connect() as conn:
        rt = q.get_roundtable(conn, rt_id)
    if rt is None:
        raise HTTPException(status_code=404, detail="roundtable not found")
    if rt.get("saved_as_note_id"):
        # Idempotent — return existing note
        with connect() as conn:
            existing = q.get_note(conn, rt["saved_as_note_id"])
        if existing and existing.get("deleted_at") is None:
            return {"note_id": existing["id"], "slug": existing["slug"], "reused": True}
    if not rt.get("synthesis"):
        raise HTTPException(status_code=409, detail="roundtable has no synthesis yet")

    # Write synthesis as a note via the Phase 1 capture pipeline
    from mastisk.routes.notes import persist_note_capture

    # Derive slug from the prompt (or synthesis if no prompt) so each saved
    # roundtable note has a distinguishable filename instead of sharing the
    # 'from-roundtable-N' preamble prefix. persist_note_capture resolves slug
    # collisions before touching disk, so a same-second, same-prompt save
    # can't overwrite another note's file.
    row = persist_note_capture(
        body=_body_from_roundtable(rt),
        source="pwa",
        slug_text=rt["prompt"] or rt.get("synthesis") or f"roundtable-{rt['id']}",
    )
    note_id = row["id"]
    with connect() as conn:
        # Link back to the roundtable
        conn.execute(
            "UPDATE roundtables SET saved_as_note_id = ? WHERE id = ?",
            (note_id, rt_id),
        )
    return {"note_id": note_id, "slug": row["slug"], "reused": False}


def _summary(row: dict) -> dict:
    return {
        "id": row["id"],
        "input_type": row["input_type"],
        "input_ref": row["input_ref"],
        "status": row["status"],
        "synthesis_preview": (row["synthesis"] or "")[:240] if row["synthesis"] else None,
        "synthesis_model": row.get("synthesis_model"),
        "created_at": row["created_at"],
        "finished_at": row.get("finished_at"),
        "saved_as_note_id": row.get("saved_as_note_id"),
    }


def _detail(row: dict) -> dict:
    return {
        **_summary(row),
        "prompt": row["prompt"],
        "synthesis": row["synthesis"],
        "error": row.get("error"),
    }


def _body_from_roundtable(rt: dict) -> str:
    """Format the roundtable as a note body. Context preamble matches Phase 1.5 style."""
    lines = []
    lines.append(f"> From roundtable #{rt['id']}")
    lines.append(f"> Prompt: {rt['prompt'][:200]}")
    lines.append("")
    lines.append(rt["synthesis"])
    return "\n".join(lines)
