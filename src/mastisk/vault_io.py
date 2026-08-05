"""Resilient I/O for Mastisk's iCloud-backed identity files."""

from __future__ import annotations

import errno
import logging
import os
import tempfile
import threading
from contextlib import suppress
from pathlib import Path

from mastisk.paths import data_dir, vault_dir, vault_is_icloud

log = logging.getLogger("mastisk.vault_io")

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.Lock] = {}


def read_vault_text(
    path: Path,
    *,
    encoding: str = "utf-8",
    errors: str | None = None,
) -> str:
    """Read vault text, using a local identity cache after iCloud EDEADLK.

    LaunchAgents cannot reliably hydrate evicted iCloud placeholders. Mastisk's
    five ``_self`` files are runtime configuration for every generative agent,
    so successful reads are mirrored under local Application Support. If iCloud
    later evicts one, the daemon can continue from its last known local copy.
    Other vault paths and errors retain pathlib's normal behavior.
    """
    path = Path(path)
    kwargs = {"encoding": encoding}
    if errors is not None:
        kwargs["errors"] = errors

    try:
        content = path.read_text(**kwargs)
    except OSError as exc:
        if not _is_icloud_placeholder_error(path, exc):
            raise
    else:
        _update_self_cache(path, content, encoding=encoding, errors=errors)
        return content

    with _path_lock(path):
        # Another request may have hydrated or cached the file while this
        # caller waited.
        try:
            content = path.read_text(**kwargs)
        except OSError as exc:
            if not _is_icloud_placeholder_error(path, exc):
                raise
            cached = _read_self_cache(path, encoding=encoding, errors=errors)
            if cached is None:
                raise
            log.warning("using local identity cache after iCloud EDEADLK: %s", path)
            return cached
        else:
            _update_self_cache(path, content, encoding=encoding, errors=errors)
            return content


def write_vault_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    errors: str | None = None,
) -> int:
    """Write text, atomically replacing an evicted placeholder after EDEADLK."""
    path = Path(path)
    kwargs = {"encoding": encoding}
    if errors is not None:
        kwargs["errors"] = errors

    try:
        written = path.write_text(content, **kwargs)
    except OSError as exc:
        if not _is_icloud_placeholder_error(path, exc):
            raise
    else:
        _update_self_cache(path, content, encoding=encoding, errors=errors)
        return written

    with _path_lock(path):
        try:
            written = path.write_text(content, **kwargs)
        except OSError as exc:
            if not _is_icloud_placeholder_error(path, exc):
                raise
            log.info("replacing iCloud vault file atomically after EDEADLK: %s", path)
            written = _atomic_replace_text(path, content, **kwargs)
        _update_self_cache(path, content, encoding=encoding, errors=errors)
        return written


def _is_icloud_placeholder_error(path: Path, exc: OSError) -> bool:
    if exc.errno != errno.EDEADLK or not vault_is_icloud():
        return False
    try:
        return path.absolute().is_relative_to(vault_dir().absolute())
    except (OSError, ValueError):
        return False


def _path_lock(path: Path) -> threading.Lock:
    key = path.absolute()
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


def _self_cache_path(path: Path) -> Path | None:
    try:
        relative = path.absolute().relative_to((vault_dir() / "_self").absolute())
    except (OSError, ValueError):
        return None
    if len(relative.parts) != 1 or relative.suffix != ".md":
        return None
    return data_dir() / "cache" / "vault-self" / relative.name


def _read_self_cache(
    path: Path,
    *,
    encoding: str,
    errors: str | None,
) -> str | None:
    cache_path = _self_cache_path(path)
    if cache_path is None or not cache_path.exists():
        return None
    kwargs = {"encoding": encoding}
    if errors is not None:
        kwargs["errors"] = errors
    try:
        return cache_path.read_text(**kwargs)
    except OSError as exc:
        log.warning("could not read local identity cache %s: %s", cache_path, exc)
        return None


def _update_self_cache(
    path: Path,
    content: str,
    *,
    encoding: str,
    errors: str | None,
) -> None:
    cache_path = _self_cache_path(path)
    if cache_path is None:
        return
    kwargs = {"encoding": encoding}
    if errors is not None:
        kwargs["errors"] = errors
    try:
        if cache_path.exists() and cache_path.read_text(**kwargs) == content:
            return
        _atomic_replace_text(cache_path, content, **kwargs)
    except OSError as exc:
        # The successful vault read remains authoritative. Cache maintenance
        # must not turn a healthy identity read into an agent failure.
        log.warning("could not update local identity cache %s: %s", cache_path, exc)


def _atomic_replace_text(
    path: Path,
    content: str,
    *,
    encoding: str,
    errors: str | None = None,
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        handle = os.fdopen(fd, "w", encoding=encoding, errors=errors)
    except Exception:
        with suppress(OSError):
            os.close(fd)
        with suppress(FileNotFoundError):
            tmp_path.unlink()
        raise
    try:
        with handle:
            written = handle.write(content)
        os.replace(tmp_path, path)
        return written
    except Exception:
        with suppress(FileNotFoundError):
            tmp_path.unlink()
        raise
