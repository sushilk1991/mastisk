"""Where the Coreader library is, and whether Mastisk should read it.

The ``[coreader]`` table of config.toml is read from disk on every call
instead of through the cached ``get_settings()``. ``mastisk coreader connect``
runs in a separate process from the daemon, and a restart interrupts running
agents, so the daemon has to notice the change by itself on its next scan.
"""
from __future__ import annotations

import json
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from mastisk.paths import ICLOUD_ROOT, config_path
from mastisk.settings import CoreaderSettings

log = logging.getLogger("mastisk.coreader.config")


@dataclass(frozen=True)
class CoreaderConfig:
    enabled: bool
    library_root: Path
    # Which rule chose library_root: "config.toml", "coreader-config" or "default".
    root_source: str

    @property
    def books_dir(self) -> Path:
        return self.library_root / "Books"


def coreader_app_config_path() -> Path:
    """The file where the Coreader app records the library folder it uses."""
    return Path.home() / "Library" / "Application Support" / "Coreader" / "config.json"


def default_library_root() -> Path:
    """Coreader's own default: iCloud Drive when it is on, Documents otherwise."""
    if ICLOUD_ROOT.is_dir():
        return ICLOUD_ROOT / "Coreader"
    return Path.home() / "Documents" / "Coreader"


def load_config() -> CoreaderConfig:
    section = _read_section()
    if section.library_root.strip():
        root, source = Path(section.library_root.strip()).expanduser(), "config.toml"
    elif (app_root := _root_from_coreader_app()) is not None:
        root, source = app_root, "coreader-config"
    else:
        root, source = default_library_root(), "default"
    return CoreaderConfig(enabled=section.enabled, library_root=root, root_source=source)


def _read_section() -> CoreaderSettings:
    path = config_path()
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle).get("coreader")
    except FileNotFoundError:
        return CoreaderSettings()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("coreader: cannot read %s, treating the connector as off: %s", path, exc)
        return CoreaderSettings()
    if not isinstance(raw, dict):
        return CoreaderSettings()
    try:
        return CoreaderSettings(**raw)
    except ValidationError as exc:
        log.warning("coreader: invalid [coreader] table, treating the connector as off: %s", exc)
        return CoreaderSettings()


def _root_from_coreader_app() -> Path | None:
    try:
        data = json.loads(coreader_app_config_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    root = data.get("libraryRoot") if isinstance(data, dict) else None
    if not isinstance(root, str) or not root.strip():
        return None
    return Path(root.strip()).expanduser()
