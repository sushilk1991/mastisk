"""Token-authenticated capture ingress with Phase-2 intent routing."""
from __future__ import annotations

import hmac
import logging
from typing import Literal

import yaml
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

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

    if not command_detected(routed) and routed.confidence < 0.5:
        return _persist_inbox_fallback(req.text, req.source)

    try:
        needs_triage = False if command_detected(routed) else routed.confidence < 0.85
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
