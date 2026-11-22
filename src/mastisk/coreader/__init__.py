"""Coreader connector: reading notes indexed in place, never copied."""

from mastisk.coreader.config import CoreaderConfig, load_config
from mastisk.coreader.sync import deep_link, list_books, read_notes, scan_coreader, status

__all__ = [
    "CoreaderConfig",
    "deep_link",
    "list_books",
    "load_config",
    "read_notes",
    "scan_coreader",
    "status",
]
