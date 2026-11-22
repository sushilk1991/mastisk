"""Coreader connector API: state, mirrored books, and an on-demand rescan."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from mastisk.coreader.sync import list_books, scan_coreader, status

router = APIRouter(tags=["coreader"])


@router.get("/api/coreader")
def coreader_status() -> dict[str, Any]:
    return status()


@router.get("/api/coreader/books")
def coreader_books() -> list[dict[str, Any]]:
    return list_books()


@router.post("/api/coreader/scan")
def coreader_scan() -> dict[str, Any]:
    """Rescan now instead of waiting for the five-minute tick."""
    return scan_coreader()
