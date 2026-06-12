"""Handwritten journal photo OCR seam."""
from __future__ import annotations

from pathlib import Path


class VisionUnavailable(RuntimeError):
    """Raised when no verified non-interactive vision path is available."""


async def extract_journal_photo_text(_image_path: Path) -> str:
    raise VisionUnavailable(
        "OCR pending: vision path unavailable for installed Claude Code -p"
    )
