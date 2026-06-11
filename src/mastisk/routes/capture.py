"""Token-authenticated capture ingress with Phase-2 intent routing."""
from __future__ import annotations

import hmac
import logging
import re
from typing import Literal

import yaml
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from mastisk.capture.router import Capture, route_capture
from mastisk.paths import vault_dir
from mastisk.projects.sync import append_project_log, get_project
from mastisk.routes.notes import persist_note_capture
from mastisk.settings import read_capture_bearer_token
from mastisk.tasks.sync import append_task_to_host, journal_host_for_today

router = APIRouter(prefix="/api/capture", tags=["capture"])
log = logging.getLogger("mastisk.capture")


def require_capture_token(authorization: str | None) -> None:
    """Bearer-token gate for the ingress."""
    token = read_capture_bearer_token()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="capture ingress not configured - run `mastisk capture-token`",
        )
    expected = f"Bearer {token}".encode("utf-8", "surrogateescape")
    actual = authorization.encode("utf-8", "surrogateescape") if authorization else b""
    if not hmac.compare_digest(actual, expected):
        raise HTTPException(status_code=401, detail="invalid or missing token")


class CaptureRequest(BaseModel):
    text: str = Field(min_length=1)
    source: Literal["watch", "phone", "pwa", "cli"] = "watch"
    # Request timestamp used by the router for relative-date resolution.
    ts: str | None = None

    @field_validator("text")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must be non-blank")
        return v


@router.post("", status_code=201)
async def capture(
    req: CaptureRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    require_capture_token(authorization)

    try:
        routed = await route_capture(req.text, source=req.source, ts=req.ts)
    except Exception:
        log.exception("capture router failed; falling back to raw inbox note")
        return _persist_inbox_fallback(req.text, req.source)

    if routed.type == "inbox" or (
        not routed.command_detected and routed.confidence < 0.5
    ):
        return _persist_inbox_fallback(req.text, req.source)

    needs_triage = False if routed.command_detected else routed.confidence < 0.85

    if routed.type == "task":
        try:
            return _persist_task_capture(routed, ts=req.ts, needs_triage=needs_triage)
        except Exception:
            log.exception("capture task write failed; falling back to raw inbox note")
            return _persist_inbox_fallback(req.text, req.source)

    if routed.type == "project_update" and routed.project:
        try:
            filed = _persist_project_update_capture(routed)
            if filed is not None:
                return {**filed, "needs_triage": needs_triage}
        except Exception:
            log.exception("capture project update write failed; falling back to raw inbox note")
            return _persist_inbox_fallback(req.text, req.source)

    try:
        row = persist_note_capture(
            body=routed.body,
            source=req.source,
            slug_text=req.text,
            file_content=(
                _body_with_capture_frontmatter(routed, needs_triage=needs_triage)
                if _has_typed_capture_metadata(routed)
                else routed.body
            ),
        )
    except Exception:
        log.exception("capture typed note write failed; falling back to raw inbox note")
        return _persist_inbox_fallback(req.text, req.source)
    return {
        "id": row["id"],
        "type": routed.type,
        "destination": row["path"],
        "needs_triage": needs_triage,
    }


def _persist_task_capture(capture: Capture, *, ts: str | None, needs_triage: bool) -> dict:
    project = get_project(capture.project) if capture.project else None
    host = (vault_dir() / project["path"]) if project is not None else journal_host_for_today(ts)
    row = append_task_to_host(
        host,
        text=capture.body,
        due=_date_marker(capture.due),
        scheduled=_date_marker(capture.scheduled),
        recurrence=capture.recurrence,
        priority=capture.priority,
        tags=capture.tags,
        links=capture.related,
    )
    return {
        "id": row["uid"],
        "type": "task",
        "destination": row["host_path"],
        "needs_triage": needs_triage,
    }


def _persist_project_update_capture(capture: Capture) -> dict | None:
    if not capture.project:
        return None
    project = append_project_log(capture.project, capture.body)
    if project is None:
        return None
    return {
        "id": project["slug"],
        "type": "project_update",
        "destination": project["path"],
    }


def _date_marker(value: str | None) -> str | None:
    if not value:
        return None
    return value[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", value) else value


def _persist_inbox_fallback(text: str, source: str) -> dict:
    row = persist_note_capture(body=text, source=source)
    return {
        "id": row["id"],
        "type": "inbox",
        "destination": row["path"],
        "needs_triage": True,
    }


def _has_typed_capture_metadata(capture: Capture) -> bool:
    return capture.type not in {"note", "inbox"}


def _body_with_capture_frontmatter(capture: Capture, *, needs_triage: bool) -> str:
    frontmatter = {
        "capture": capture.model_dump(),
        "needs_triage": needs_triage,
    }
    fm_yaml = yaml.safe_dump(
        frontmatter,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    return f"---\n{fm_yaml}\n---\n\n{capture.body}"
