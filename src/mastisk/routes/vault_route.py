"""Expose identity files + vault metadata so the UI can show/edit self.md etc."""
from __future__ import annotations

import hashlib
import logging
import re

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mastisk.file_locks import host_file_lock
from mastisk.paths import self_dir, vault_dir, vault_is_icloud
from mastisk.routes.notes import atomic_write
from mastisk.vault_paths import VaultPathError, vault_markdown_file
from mastisk.vault_rescan import rescan_vault_markdown_path

router = APIRouter(tags=["vault"])
log = logging.getLogger("mastisk.routes.vault_route")

_SELF_FILES = ("identity", "interests", "dislikes", "style", "learnings")


@router.get("/vault/info")
def info():
    return {
        "vault_path": str(vault_dir()),
        "icloud": vault_is_icloud(),
        "self_files": _SELF_FILES,
    }


@router.get("/vault/self/{name}")
def read_self(name: str):
    if name not in _SELF_FILES:
        raise HTTPException(404, "unknown self file")
    p = self_dir() / f"{name}.md"
    return {"name": name, "content": p.read_text() if p.exists() else ""}


class SelfIn(BaseModel):
    content: str


class VaultFileIn(BaseModel):
    path: str
    content: str
    base_sha256: str


@router.put("/vault/self/{name}")
def write_self(name: str, body: SelfIn):
    if name not in _SELF_FILES:
        raise HTTPException(404, "unknown self file")
    self_dir().mkdir(parents=True, exist_ok=True)
    (self_dir() / f"{name}.md").write_text(body.content)
    return {"ok": True}


@router.get("/vault/file")
def read_vault_file(path: str) -> dict[str, str]:
    try:
        rel_path, target = vault_markdown_file(path)
    except VaultPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="vault file not found")
    content = target.read_text(encoding="utf-8")
    return {
        "path": rel_path,
        "content": content,
        "content_sha256": _sha256_text(content),
    }


@router.put("/vault/file")
def write_vault_file(req: VaultFileIn) -> dict[str, str | bool]:
    try:
        rel_path, target = vault_markdown_file(req.path)
        _validate_frontmatter(req.content)
    except VaultPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with host_file_lock(target):
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if _sha256_text(current) != req.base_sha256:
            raise HTTPException(
                status_code=409,
                detail="vault file changed since editor opened",
            )
        try:
            if _frontmatter_block(req.content) != _frontmatter_block(current):
                raise ValueError("frontmatter edits are not supported")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        atomic_write(target, req.content)
    content_sha256 = _sha256_text(req.content)
    try:
        rescan_vault_markdown_path(rel_path, target)
    except Exception as exc:
        log.exception(
            "rescan failed after successful vault write: %s",
            rel_path,
        )
        return {
            "ok": True,
            "path": rel_path,
            "content_sha256": content_sha256,
            "rescan_failed": True,
            "rescan_error": str(exc),
        }
    return {"ok": True, "path": rel_path, "content_sha256": content_sha256}


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _validate_frontmatter(content: str) -> None:
    if not content.startswith("---\n"):
        return
    close = re.search(r"(?m)^---\s*$", content[4:])
    if close is None:
        raise ValueError("frontmatter is malformed")
    try:
        parsed = yaml.safe_load(content[4 : 4 + close.start()]) or {}
    except yaml.YAMLError as exc:
        raise ValueError("frontmatter is invalid YAML") from exc
    if not isinstance(parsed, dict):
        raise ValueError("frontmatter must be a mapping")


def _frontmatter_block(content: str) -> str | None:
    if not content.startswith("---\n"):
        return None
    close = re.search(r"(?m)^---[ \t]*$", content[4:])
    if close is None:
        raise ValueError("frontmatter is malformed")
    return content[: 4 + close.end()]
