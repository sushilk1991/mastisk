"""Agent Studio API."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from mastisk.agents.registry import agent_definition
from mastisk.agents.studio import (
    PlaceholderValidationError,
    delete_agent_skill,
    list_skills,
    profile_payload,
    skill_payload,
    write_agent_profile,
    write_agent_skill,
)
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.routes.feed_route import _agents_snapshot

router = APIRouter(tags=["agents"])
# /api/agents* and /api/agent-skills* are prompt-control surfaces under the
# local trust model. They must NEVER be tunnel-exposed; the PWA carries no auth
# token that could make these safe on the public internet.


class AgentProfileUpdate(BaseModel):
    enabled: bool | None = None
    model: str | None = None
    skills: list[str] | None = None
    prompt_override: str | None = None
    slot_overrides: dict[str, str | None] | None = None


class AgentSkillCreate(BaseModel):
    slug: str | None = None
    name: str = Field(min_length=1)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    body: str = ""

    @field_validator("name")
    @classmethod
    def _name_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must be non-blank")
        return value.strip()


class AgentSkillUpdate(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    body: str = ""

    @field_validator("name")
    @classmethod
    def _name_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must be non-blank")
        return value.strip()


@router.get("/agents")
async def list_agents_endpoint() -> dict[str, Any]:
    with connect() as conn:
        agents = _agents_snapshot(conn)
    return {"agents": agents}


@router.get("/agents/{agent_id}")
async def agent_detail_endpoint(agent_id: str) -> dict[str, Any]:
    try:
        definition = agent_definition(agent_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="agent not found") from exc
    with connect() as conn:
        snapshots = _agents_snapshot(conn)
        recent_rows = [
            q._feed_row_for_ui(dict(row))
            for row in conn.execute(
                """SELECT *
                   FROM feed
                   WHERE agent = ?
                   ORDER BY ts DESC
                   LIMIT 20""",
                (agent_id,),
            ).fetchall()
        ]
        queue_stats = _queue_stats(conn, agent_id)
    agent = next((row for row in snapshots if row["id"] == agent_id), None)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    profile = profile_payload(agent_id)
    invalid_slots = profile.get("invalid_slots") or {}
    slot_overrides = profile.get("slot_overrides") or {}
    slots = []
    for slot in definition.slots:
        override = profile.get("prompt_override") if slot.primary else slot_overrides.get(slot.slot_id, "")
        slots.append(
            {
                "slot_id": slot.slot_id,
                "label": slot.label,
                "module": slot.module,
                "module_attr": slot.module_attr,
                "primary": slot.primary,
                "editable": slot.editable,
                "default": slot.default_prompt,
                "override": override or "",
                "required_placeholders": list(slot.required_placeholders),
                "invalid": slot.slot_id in invalid_slots,
                "invalid_reason": _slot_invalid_reason(slot.slot_id, invalid_slots),
            }
        )
    return {
        "agent": agent,
        "definition": {
            "id": definition.id,
            "name": definition.name,
            "role": definition.role,
            "module": definition.module,
            "model_supported": definition.model_supported,
            "model_note": definition.model_note,
        },
        "profile": profile,
        "slots": slots,
        "skills": list_skills(),
        "recent_feed": recent_rows,
        "queue": queue_stats,
    }


@router.put("/agents/{agent_id}/profile")
async def update_agent_profile_endpoint(agent_id: str, req: AgentProfileUpdate) -> dict[str, Any]:
    try:
        agent_definition(agent_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="agent not found") from exc
    updates = {
        key: value
        for key, value in req.model_dump().items()
        if key in req.model_fields_set
    }
    try:
        profile = write_agent_profile(agent_id, updates)
    except PlaceholderValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_placeholders",
                "message": str(exc),
                "missing": exc.missing,
            },
        ) from exc
    return {"profile": profile}


@router.get("/agent-skills")
async def list_agent_skills_endpoint() -> dict[str, Any]:
    return {"skills": list_skills()}


@router.post("/agent-skills", status_code=201)
async def create_agent_skill_endpoint(req: AgentSkillCreate) -> dict[str, Any]:
    try:
        return write_agent_skill(**req.model_dump(), overwrite=False)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="skill already exists") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/agent-skills/{slug}")
async def update_agent_skill_endpoint(slug: str, req: AgentSkillUpdate) -> dict[str, Any]:
    try:
        return write_agent_skill(slug=slug, **req.model_dump(), overwrite=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/agent-skills/{slug}")
async def get_agent_skill_endpoint(slug: str) -> dict[str, Any]:
    skill = skill_payload(slug)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return skill


@router.delete("/agent-skills/{slug}")
async def delete_agent_skill_endpoint(slug: str) -> dict[str, Any]:
    if not delete_agent_skill(slug):
        raise HTTPException(status_code=404, detail="skill not found")
    return {"ok": True}


def _queue_stats(conn, agent_id: str) -> dict[str, int]:
    row = conn.execute(
        """SELECT
             SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued,
             SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,
             SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
           FROM jobs
           WHERE agent = ?""",
        (agent_id,),
    ).fetchone()
    return {
        "queued": int(row["queued"] or 0),
        "running": int(row["running"] or 0),
        "failed": int(row["failed"] or 0),
    }


def _slot_invalid_reason(slot_id: str, invalid_slots: dict[str, Any]) -> str | None:
    missing = invalid_slots.get(slot_id)
    if not missing:
        return None
    if not isinstance(missing, list):
        return "override ignored"
    return "override ignored: " + ", ".join(str(item) for item in missing)
