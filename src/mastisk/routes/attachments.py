"""Vault attachment upload and read-only serving."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from slugify import slugify

from mastisk.paths import attachments_dir, vault_dir
from mastisk.settings import get_settings

router = APIRouter(prefix="/api/attachments", tags=["attachments"])

_TYPE_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/heic": "heic",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "application/pdf": "pdf",
}
_EXT_TO_TYPE = {ext: ctype for ctype, ext in _TYPE_TO_EXT.items()}
_EXT_TO_TYPE["jpg"] = "image/jpeg"
_ATTACHMENT_NAME_RE = re.compile(
    r"^(?P<hash>[a-f0-9]{32}|[a-f0-9]{64})\.(?P<ext>png|jpg|gif|webp|heic|mp4|mov|pdf)$"
)


@router.post("", status_code=201)
async def upload_attachment(file: Annotated[UploadFile, File(...)]) -> dict[str, str]:
    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    ext = _TYPE_TO_EXT.get(content_type)
    if ext is None:
        raise HTTPException(status_code=415, detail="unsupported attachment type")

    max_bytes = max(0, int(get_settings().attachments.max_mb)) * 1024 * 1024
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail="attachment exceeds configured size cap")

    digest = hashlib.sha256(raw).hexdigest()
    target = attachments_dir() / f"{digest[:32]}.{ext}"
    if target.exists() and target.read_bytes() != raw:
        target = attachments_dir() / f"{digest}.{ext}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        _atomic_write_bytes(target, raw)

    rel_path = str(target.relative_to(vault_dir()))
    label = _markdown_label(file.filename)
    return {
        "path": rel_path,
        "markdown": _markdown_for_attachment(rel_path, label, content_type),
    }


@router.get("/{name}")
async def get_attachment(name: str) -> FileResponse:
    match = _ATTACHMENT_NAME_RE.match(name)
    if not match:
        raise HTTPException(status_code=404, detail="attachment not found")
    path = attachments_dir() / name
    root = attachments_dir().resolve(strict=False)
    target = path.resolve(strict=False)
    if not target.is_relative_to(root) or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="attachment not found")
    media_type = _EXT_TO_TYPE.get(match.group("ext"), "application/octet-stream")
    return FileResponse(target, media_type=media_type)


def _markdown_label(filename: str | None) -> str:
    stem = (filename or "attachment").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = stem.rsplit(".", 1)[0]
    return slugify(stem, separator=" ") or "attachment"


def _markdown_for_attachment(path: str, label: str, content_type: str) -> str:
    if content_type.startswith("image/"):
        return f"![{label}]({path})"
    return f"[{label}]({path})"


def _atomic_write_bytes(target, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(content)
        fd = -1
        os.replace(tmp_path, target)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
