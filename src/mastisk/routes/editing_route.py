"""Rich-editor advisory lock routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from mastisk.editing import heartbeat_path, lock_path, unlock_path
from mastisk.vault_paths import VaultPathError

router = APIRouter(prefix="/api/editing", tags=["editing"])


class EditingPath(BaseModel):
    path: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must be non-blank")
        return value.strip()


@router.post("/lock")
async def lock_editing_endpoint(req: EditingPath) -> dict[str, str]:
    try:
        return lock_path(req.path)
    except VaultPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/unlock")
async def unlock_editing_endpoint(req: EditingPath) -> dict[str, str]:
    try:
        return unlock_path(req.path)
    except VaultPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/heartbeat")
async def heartbeat_editing_endpoint(req: EditingPath) -> dict[str, str]:
    try:
        return heartbeat_path(req.path)
    except VaultPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
