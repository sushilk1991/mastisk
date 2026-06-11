"""Process-local locks for vault file read-modify-write sections."""
from __future__ import annotations

import threading
from pathlib import Path

_LOCKS_GUARD = threading.Lock()
_HOST_FILE_LOCKS: dict[Path, threading.Lock] = {}


def host_file_lock(path: Path) -> threading.Lock:
    key = path.resolve(strict=False)
    with _LOCKS_GUARD:
        lock = _HOST_FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _HOST_FILE_LOCKS[key] = lock
        return lock
