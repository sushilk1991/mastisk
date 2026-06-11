"""Small markdown section helpers for file-first writes."""
from __future__ import annotations

import re


def normalize_section_heading(line: str) -> str | None:
    match = re.match(r"^##\s+(?P<heading>.+?)\s*$", line)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group("heading").strip()).lower()


def section_line_numbers(markdown: str, headings: set[str]) -> set[int]:
    normalized = {
        re.sub(r"\s+", " ", heading.strip()).lower()
        for heading in headings
        if heading.strip()
    }
    if not normalized:
        return set()
    active = False
    matches: set[int] = set()
    for line_number, line in enumerate(markdown.splitlines(), start=1):
        heading = normalize_section_heading(line)
        if heading is not None:
            active = heading in normalized
            continue
        if active:
            matches.add(line_number)
    return matches


def section_lines(markdown: str, heading: str) -> list[str]:
    line_numbers = section_line_numbers(markdown, {heading})
    return [
        line
        for line_number, line in enumerate(markdown.splitlines(), start=1)
        if line_number in line_numbers
    ]


def append_to_section(markdown: str, heading: str, line: str) -> str:
    lines = markdown.splitlines()
    normalized_heading = normalize_section_heading(f"## {heading}")
    next_section_re = re.compile(r"^##\s+")
    insert_at: int | None = None
    for idx, existing in enumerate(lines):
        if normalize_section_heading(existing) != normalized_heading:
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
