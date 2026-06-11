"""Token-authenticated capture ingress.

Phase 1: a capture becomes a note in the existing _notes/inbox pipeline.
Intent routing arrives in Phase 2.
"""
from __future__ import annotations

import hmac
import logging
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
import yaml

from mastisk.capture.router import Capture, command_detected, route_capture
from mastisk.routes.notes import persist_note_capture
from mastisk.settings import read_capture_bearer_token

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
    # Reserved for Phase 2 relative-date resolution; accepted and ignored now.
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
        row = persist_note_capture(body=req.text, source=req.source)
        return {
            "id": row["id"],
            "type": "inbox",
            "destination": row["path"],
            "needs_triage": True,
        }

    if not command_detected(routed) and routed.confidence < 0.5:
        row = persist_note_capture(body=req.text, source=req.source)
        return {
            "id": row["id"],
            "type": "inbox",
            "destination": row["path"],
            "needs_triage": True,
        }

    needs_triage = False if command_detected(routed) else routed.confidence < 0.85
    body_for_file = _body_with_capture_frontmatter(routed, needs_triage=needs_triage)
    row = persist_note_capture(
        body=body_for_file,
        source=req.source,
        slug_text=req.text,
    )
    return {
        "id": row["id"],
        "type": routed.type,
        "destination": row["path"],
        "needs_triage": needs_triage,
    }


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
