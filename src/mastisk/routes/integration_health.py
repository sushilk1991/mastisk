"""Passive integration health for Settings/SystemRail."""
from __future__ import annotations

import importlib.util
import shutil

from fastapi import APIRouter

from mastisk.db.queries import connect
from mastisk.google_calendar import calendar_status
from mastisk.settings import get_settings

router = APIRouter(tags=["health"])


@router.get("/health/integrations")
def integration_health() -> dict:
    settings = get_settings()
    return {
        "calendar": calendar_status(),
        "push": {
            "backend": settings.notify.backend,
            "configured": _push_configured(settings.notify),
            "notify_failed_last_24h": _notify_failed_last_24h(),
        },
        "bridges": {
            "provider_order": settings.intelligence.provider_order,
            "claude": {
                "configured": bool(settings.claude_cmd),
                "cmd": settings.claude_cmd,
                "available": bool(shutil.which(settings.claude_cmd)),
            },
            "ollama_chat": {
                "local_url": settings.ollama_local_url,
                "local_only": settings.ollama_local_only,
                "cloud_configured": bool(settings.ollama_cloud_key),
                "model": settings.summarize_model_cheap,
            },
            "ollama_embed": {
                "local_url": settings.ollama_local_url,
                "model": settings.embed_model,
            },
        },
        "ingest": {
            "markitdown": _module_available("markitdown"),
            "docling": _module_available("docling.document_converter"),
            "mlx_whisper": _module_available("mlx_whisper"),
        },
    }


def _push_configured(notify_settings) -> bool:
    backend = notify_settings.backend.lower().strip()
    if backend == "none":
        return False
    if backend == "pushover":
        return bool(notify_settings.pushover_token and notify_settings.pushover_user)
    if backend == "ntfy":
        return bool(notify_settings.ntfy_topic)
    return False


def _notify_failed_last_24h() -> int:
    with connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n
               FROM feed
               WHERE agent = 'reminder_engine'
                 AND verb = 'notify_failed'
                 AND kind = 'reminder'
                 AND ts >= datetime('now', '-1 day')"""
        ).fetchone()
    return int(row["n"] if row else 0)


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False
