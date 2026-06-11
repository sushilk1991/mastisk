"""Kindle `My Clippings.txt` parser and importer helpers."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from mastisk.db.queries import connect
from mastisk.library.sync import add_book_highlight, create_book_file, create_quote_file, find_book


@dataclass(frozen=True)
class ParsedClipping:
    title: str
    author: str | None
    metadata: str
    content: str
    raw_block: str


@dataclass(frozen=True)
class ReviewBlock:
    raw_block: str
    reason: str
    parsed_title: str | None = None
    parsed_author: str | None = None
    parsed_content: str | None = None


@dataclass(frozen=True)
class ParseResult:
    highlights: list[ParsedClipping]
    review_blocks: list[ReviewBlock]


def parse_clippings(text: str) -> ParseResult:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks = [
        block.strip("\n")
        for block in re.split(r"^=+\s*$", normalized, flags=re.MULTILINE)
        if block.strip()
    ]
    highlights: list[ParsedClipping] = []
    review: list[ReviewBlock] = []
    for block in blocks:
        parsed = _parse_block(block)
        if isinstance(parsed, ParsedClipping):
            highlights.append(parsed)
        else:
            review.append(parsed)
    return ParseResult(highlights=highlights, review_blocks=review)


def import_clippings_text(text: str) -> dict[str, int]:
    parsed = parse_clippings(text)
    imported = 0
    skipped = 0
    review_count = 0
    for item in parsed.highlights:
        book = find_book(item.title, author=item.author)
        if book is None:
            book = create_book_file(title=item.title, author=item.author)
        highlight = add_book_highlight(book["slug"], item.content)
        if highlight is None:
            continue
        if highlight.get("created"):
            imported += 1
        else:
            skipped += 1
    with connect() as conn:
        for item in parsed.review_blocks:
            raw_hash = _raw_hash(item.raw_block)
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO kindle_import_review
                   (raw_hash, raw_block, reason, parsed_title, parsed_author, parsed_content)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    raw_hash,
                    item.raw_block,
                    item.reason,
                    item.parsed_title,
                    item.parsed_author,
                    item.parsed_content,
                ),
            )
            if conn.total_changes > before:
                review_count += 1
    return {
        "imported": imported,
        "skipped_duplicates": skipped,
        "review_count": review_count,
    }


def list_review_blocks() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT *
               FROM kindle_import_review
               WHERE status = 'open'
               ORDER BY created_at ASC, id ASC"""
        ).fetchall()
    return [dict(row) for row in rows]


def dismiss_review_block(item_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM kindle_import_review WHERE id = ? AND status = 'open'",
            (item_id,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            """UPDATE kindle_import_review
                  SET status = 'dismissed',
                      resolved_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (item_id,),
        )
        updated = conn.execute("SELECT * FROM kindle_import_review WHERE id = ?", (item_id,)).fetchone()
    return dict(updated)


def retry_review_as_quote(
    item_id: int,
    *,
    text: str | None = None,
    source_type: str = "conversation",
    source_ref: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM kindle_import_review WHERE id = ? AND status = 'open'",
            (item_id,),
        ).fetchone()
    if row is None:
        return None
    quote_text = (text or row["parsed_content"] or row["raw_block"]).strip()
    quote = create_quote_file(
        text=quote_text,
        source_type=source_type,
        source_ref=source_ref,
        tags=tags or [],
    )
    with connect() as conn:
        conn.execute(
            """UPDATE kindle_import_review
                  SET status = 'resolved',
                      quote_id = ?,
                      resolved_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (quote["id"], item_id),
        )
        updated = conn.execute("SELECT * FROM kindle_import_review WHERE id = ?", (item_id,)).fetchone()
    return dict(updated)


def _parse_block(block: str) -> ParsedClipping | ReviewBlock:
    lines = [line.rstrip() for line in block.split("\n")]
    lines = _trim_blank_edges(lines)
    if len(lines) < 3:
        return ReviewBlock(raw_block=block, reason="too_few_lines")
    title_line = lines[0].strip().lstrip("\ufeff")
    metadata = lines[1].strip()
    title, author = _parse_title_author(title_line)
    if not title:
        return ReviewBlock(raw_block=block, reason="missing_title")
    if not metadata.startswith("-"):
        return ReviewBlock(
            raw_block=block,
            reason="missing_metadata",
            parsed_title=title,
            parsed_author=author,
        )
    content_lines = _content_lines(lines[2:])
    content = "\n".join(content_lines).strip()
    if not content:
        return ReviewBlock(
            raw_block=block,
            reason="missing_content",
            parsed_title=title,
            parsed_author=author,
        )
    return ParsedClipping(
        title=title,
        author=author,
        metadata=metadata,
        content=content,
        raw_block=block,
    )


def _parse_title_author(line: str) -> tuple[str, str | None]:
    match = re.match(r"^(?P<title>.+?)\s+\((?P<author>[^()]*)\)\s*$", line)
    if not match:
        return re.sub(r"\s+", " ", line).strip(), None
    return (
        re.sub(r"\s+", " ", match.group("title")).strip(),
        re.sub(r"\s+", " ", match.group("author")).strip() or None,
    )


def _content_lines(lines: list[str]) -> list[str]:
    remaining = list(lines)
    while remaining and not remaining[0].strip():
        remaining.pop(0)
    while remaining and not remaining[-1].strip():
        remaining.pop()
    return remaining


def _trim_blank_edges(lines: list[str]) -> list[str]:
    trimmed = list(lines)
    while trimmed and not trimmed[0].strip():
        trimmed.pop(0)
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    return trimmed


def _raw_hash(raw: str) -> str:
    normalized = re.sub(r"\s+", " ", raw).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
