"""Shared validation for user-addressed vault paths."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from mastisk.paths import vault_dir

EDITABLE_MARKDOWN_PREFIXES = ("journal/", "_notes/", "projects/", "content/")


class VaultPathError(ValueError):
    pass


def normalize_vault_markdown_path(raw_path: str) -> str:
    """Return a safe vault-relative markdown path for editor reads/writes.

    The rich editor intentionally edits a narrow set of user-authored markdown
    files. Agent append paths still use their domain routes; this validator is
    for the full-file editor surface.
    """
    text = str(raw_path or "").strip()
    if not text:
        raise VaultPathError("path is required")
    if "\\" in text:
        raise VaultPathError("path must use forward slashes")
    posix = PurePosixPath(text)
    if posix.is_absolute():
        raise VaultPathError("path must be vault-relative")
    if any(part in {"", ".", ".."} for part in posix.parts):
        raise VaultPathError("path must not contain traversal")
    normalized = posix.as_posix()
    if not normalized.endswith(".md"):
        raise VaultPathError("path must be a markdown file")
    if not normalized.startswith(EDITABLE_MARKDOWN_PREFIXES):
        raise VaultPathError("path is not editable through this route")
    return normalized


def vault_markdown_file(raw_path: str) -> tuple[str, Path]:
    rel_path = normalize_vault_markdown_path(raw_path)
    root = vault_dir().resolve(strict=False)
    target = (root / rel_path).resolve(strict=False)
    if not target.is_relative_to(root):
        raise VaultPathError("path must stay inside the vault")
    return rel_path, target
