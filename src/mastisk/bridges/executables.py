"""Resolve installed CLIs identically for daemon calls and settings discovery."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def _find_cli(command: str) -> str | None:
    """Honor explicit paths; supplement a service PATH without trusting the cwd."""
    command = os.path.expanduser(command.strip())
    if not command:
        return None

    # An override is authoritative: a missing custom binary must not select another account.
    if os.sep in command:
        found = shutil.which(command)
        return str(Path(found).absolute()) if found else None

    # launchd omits user install directories; relative PATH entries could execute workspace code.
    extra = [Path.home() / ".local/bin", Path.home() / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    inherited = [entry for entry in os.get_exec_path() if os.path.isabs(entry)]
    path = os.pathsep.join([*inherited, *(str(entry) for entry in extra)])
    return shutil.which(command, path=path)
