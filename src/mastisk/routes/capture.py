"""Token-authenticated capture ingress.

Phase 1: a capture becomes a note in the existing _notes/inbox pipeline.
Intent routing arrives in Phase 2.
"""
from __future__ import annotations

import hmac
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from mastisk.routes.notes import persist_note_capture
from mastisk.settings import read_capture_bearer_token

router = APIRouter(prefix="/api/capture", tags=["capture"])


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
    row = persist_note_capture(body=req.text, source=req.source)
    return {
        "id": row["id"],
        "type": "note",
        "destination": row["path"],
        "needs_triage": False,
    }
