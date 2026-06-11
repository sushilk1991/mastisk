"""Path resolution for Mastisk.

Vault:  iCloud Drive by default (if available), local fallback otherwise.
Data:   ~/Library/Application Support/Mastisk (DB, cache, raw artifacts).
Config: data_dir / config.toml.

Override via MASTISK_HOME or MASTISK_VAULT env vars.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

ICLOUD_ROOT = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Mastisk"


@lru_cache(maxsize=1)
def data_dir() -> Path:
    """Local-only data (DB, cache, raw). Never synced."""
    if override := os.environ.get("MASTISK_HOME"):
        return Path(override).expanduser()
    return APP_SUPPORT


@lru_cache(maxsize=1)
def vault_dir() -> Path:
    """Markdown vault. Prefers iCloud Drive; falls back to data_dir()/vault."""
    if override := os.environ.get("MASTISK_VAULT"):
        return Path(override).expanduser()
    if ICLOUD_ROOT.is_dir():
        return ICLOUD_ROOT / "Mastisk" / "vault"
    return data_dir() / "vault"


def vault_is_icloud() -> bool:
    return ICLOUD_ROOT.is_dir() and vault_dir().is_relative_to(ICLOUD_ROOT)


def db_path() -> Path:
    return data_dir() / "mastisk.db"


def config_path() -> Path:
    return data_dir() / "config.toml"


def raw_dir() -> Path:
    return data_dir() / "raw"


def tmp_dir() -> Path:
    return data_dir() / "tmp"


def log_dir() -> Path:
    return data_dir() / "logs"


def pwa_dir() -> Path:
    """Built frontend, shipped as package data."""
    return Path(__file__).parent / "pwa"


def self_dir() -> Path:
    return vault_dir() / "_self"


def notes_dir() -> Path:
    return vault_dir() / "_notes"


def notes_inbox_dir() -> Path:
    return notes_dir() / "inbox"


def notes_daily_dir() -> Path:
    return notes_dir() / "daily"


def blog_dir() -> Path:
    return vault_dir() / "blog"


def blog_drafts_dir() -> Path:
    return blog_dir() / "drafts"


def journal_dir() -> Path:
    return vault_dir() / "journal"


def projects_dir() -> Path:
    return vault_dir() / "projects"


def checklist_templates_dir() -> Path:
    return vault_dir() / "templates" / "checklists"


def routines_dir() -> Path:
    return vault_dir() / "routines"


def ensure_dirs() -> None:
    """Create all directories that should exist (idempotent)."""
    for d in [
        data_dir(),
        raw_dir(),
        tmp_dir(),
        log_dir(),
        vault_dir(),
        vault_dir() / "concepts",
        vault_dir() / "entities",
        vault_dir() / "sources",
        vault_dir() / "synthesis",
        self_dir(),
        notes_dir(),
        notes_inbox_dir(),
        notes_daily_dir(),
        blog_dir(),
        blog_drafts_dir(),
        journal_dir(),
        projects_dir(),
        checklist_templates_dir(),
        routines_dir(),
    ]:
        d.mkdir(parents=True, exist_ok=True)
