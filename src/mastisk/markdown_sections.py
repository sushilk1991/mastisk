"""Small markdown section helpers for file-first writes."""
from __future__ import annotations

import re


def append_to_section(markdown: str, heading: str, line: str) -> str:
    lines = markdown.splitlines()
    section_re = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.I)
    next_section_re = re.compile(r"^##\s+")
    insert_at: int | None = None
    for idx, existing in enumerate(lines):
        if not section_re.match(existing):
            continue
        insert_at = len(lines)
        for j in range(idx + 1, len(lines)):
            if next_section_re.match(lines[j]):
                insert_at = j
                break
        break

    if insert_at is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"## {heading}", line])
    else:
        while insert_at > 0 and lines[insert_at - 1] == "":
            insert_at -= 1
        lines.insert(insert_at, line)
    return "\n".join(lines) + "\n"
