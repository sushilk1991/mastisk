"""Agent ticker — latest N entries + an SSE live stream."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from mastisk.agents.registry import agent_catalog
from mastisk.agents.studio import profile_payload
from mastisk.db import queries as q
from mastisk.db.queries import connect

router = APIRouter(tags=["feed"])


@router.get("/feed")
def feed(limit: int = 50):
    with connect() as conn:
        return {"feed": q.recent_feed(conn, limit=limit), "agents": _agents_snapshot(conn)}


# ``load_cap`` is the denominator in the "how busy is this lane" calculation.
# For daily-budgeted agents (scout/listener/compiler/linter/synthesizer) we
# override it at runtime with the user's configured budget. For agents that
# aren't daily-budgeted (notetaker, github_*, blog_writer, roundtable,
# escalator, artifact-agent), the fallback here is used directly so the bar
# shows a sensible fill instead of spiking to 100% on a single job.
_AGENT_CATALOG: list[dict] = agent_catalog()


def _agents_snapshot(conn) -> list[dict]:
    """Build a real snapshot from the jobs table + recent feed activity.

    - status: 'active' if any job is running OR a feed row was emitted in the
      last 2 minutes; 'idle' otherwise; 'disabled' for agents without code.
    - load: queued-job depth / load_cap, clamped to [0, 1]. For daily-budgeted
      agents the cap is the user's configured daily budget; for the rest it's
      the catalog's load_cap fallback.
    """
    from mastisk.settings import get_settings

    s = get_settings()
    # Daily-budget overrides for the five core agents. Other agents keep the
    # static load_cap from the catalog.
    budget_overrides = {
        "scout":       s.budget.scout,
        "listener":    s.budget.listener,
        "compiler":    s.budget.compiler,
        "linter":      s.budget.linter,
        "synthesizer": s.budget.synthesizer,
    }

    job_counts = {
        r["agent"]: {"queued": r["queued"], "running": r["running"]}
        for r in conn.execute(
            """SELECT agent,
                      SUM(CASE WHEN status='queued'  THEN 1 ELSE 0 END) AS queued,
                      SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running
                 FROM jobs GROUP BY agent"""
        )
    }
    recent_agents = {
        r["agent"] for r in conn.execute(
            "SELECT DISTINCT agent FROM feed WHERE ts >= datetime('now', '-2 minutes')"
        )
    }

    out: list[dict] = []
    for a in _AGENT_CATALOG:
        profile = profile_payload(a["id"])
        counts = job_counts.get(a["id"], {"queued": 0, "running": 0})
        queued = counts["queued"] or 0
        running = counts["running"] or 0
        cap = max(1, budget_overrides.get(a["id"], a.get("load_cap", 10)))
        load = min(1.0, (queued + running) / cap)

        if not profile.get("enabled", True) or not a["implemented"]:
            status = "disabled"
        elif running > 0 or a["id"] in recent_agents:
            status = "active"
        else:
            status = "idle"

        out.append({
            "id": a["id"], "name": a["name"], "role": a["role"], "color": a["color"],
            "status": status, "load": round(load, 3),
            "implemented": a["implemented"],
            "queued": queued, "running": running,
            "profile": {
                "enabled": profile.get("enabled", True),
                "skills": profile.get("skills", []),
                "invalid": profile.get("invalid", False),
                "invalid_reason": profile.get("invalid_reason"),
            },
        })
    return out


@router.get("/feed/stream")
async def feed_stream(request: Request):
    """SSE stream — pushes new feed rows as they appear."""
    async def event_gen():
        last_id = _peek_last_feed_id()
        while True:
            if await request.is_disconnected():
                break
            rows = _new_feed_rows_since(last_id)
            for row in rows:
                last_id = max(last_id, row["id"])
                yield {"event": "tick", "data": json.dumps(row)}
            await asyncio.sleep(2)

    return EventSourceResponse(event_gen())


def _peek_last_feed_id() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM feed").fetchone()
        return int(row["id"]) if row else 0


def _new_feed_rows_since(last_id: int) -> list[dict]:
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM feed WHERE id > ? ORDER BY id ASC LIMIT 50", (last_id,)
        )]
    return [{**r, **q._feed_row_for_ui(r)} for r in rows]
