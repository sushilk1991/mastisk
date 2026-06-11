"""Library mirror scanner and file-first mutations.

Books and quotes are separate canonical markdown files:

- `library/books/<slug>.md` owns book metadata and the `## Highlights` list.
- `library/quotes/<id>.md` owns quote text and append-only `## Thoughts`.

Highlight dual-write order is intentionally book-first:

1. Append the highlight to the book file under its file lock.
2. Scan the book file back into `book_highlights`.
3. Create or reuse the linked quote file.
4. Store the quote id on the highlight row.

If the process crashes after step 1 or 2, the next `scan_library()` can recover
the missing quote because the book file remains canonical and each highlight has
a deterministic content hash.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from slugify import slugify

from mastisk.db.queries import connect
from mastisk.file_locks import host_file_lock
from mastisk.integrations import openlibrary
from mastisk.markdown_sections import append_to_section, section_lines
from mastisk.paths import books_dir, quotes_dir, vault_dir
from mastisk.projects.sync import split_frontmatter
from mastisk.routes.notes import atomic_write
from mastisk.settings import get_settings

_CREATE_BOOK_LOCK = threading.Lock()
_CREATE_QUOTE_LOCK = threading.Lock()
_VALID_BOOK_STATUSES = {"want", "reading", "finished", "abandoned"}
_VALID_SOURCE_TYPES = {"book", "article", "podcast", "conversation"}
_HIGHLIGHT_RE = re.compile(r"^\s*-\s+(?P<text>.+?)\s*$")
_THOUGHT_RE = re.compile(
    r"^\s*-\s+(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s+(?P<text>.+?)\s*$"
)


def scan_library() -> dict[str, int]:
    book_result = scan_books(recover=False)
    quote_result = scan_quotes()
    recovered = recover_highlight_quotes()
    return {
        "books": book_result["upserted"],
        "quotes": quote_result["upserted"],
        "recovered_quotes": recovered,
    }


def scan_books(paths: list[Path] | None = None, *, recover: bool = False) -> dict[str, int]:
    book_paths = paths if paths is not None else _book_paths()
    seen: set[str] = set()
    upserted = 0
    with connect() as conn:
        for raw_path in book_paths:
            path = _absolute_path(raw_path)
            if not path.exists() or path.name.startswith("."):
                if paths is not None and path.suffix == ".md":
                    _soft_delete_missing_path(conn, path, table="books")
                continue
            with host_file_lock(path):
                book = parse_book_file(path)
            slug = book["slug"]
            seen.add(slug)
            existing_quote_ids = {
                row["content_hash"]: row["quote_id"]
                for row in conn.execute(
                    "SELECT content_hash, quote_id FROM book_highlights WHERE book_slug = ?",
                    (slug,),
                ).fetchall()
                if row["quote_id"]
            }
            conn.execute(
                """INSERT INTO books
                   (slug, path, title, author, cover_url, status, format, started,
                    finished, rating, isbn, summary, deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(slug) DO UPDATE SET
                     path=excluded.path,
                     title=excluded.title,
                     author=excluded.author,
                     cover_url=excluded.cover_url,
                     status=excluded.status,
                     format=excluded.format,
                     started=excluded.started,
                     finished=excluded.finished,
                     rating=excluded.rating,
                     isbn=excluded.isbn,
                     summary=excluded.summary,
                     deleted_at=NULL,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    slug,
                    book["path"],
                    book["title"],
                    book.get("author"),
                    book.get("cover_url"),
                    book["status"],
                    book.get("format"),
                    book.get("started"),
                    book.get("finished"),
                    book.get("rating"),
                    book.get("isbn"),
                    book.get("summary"),
                ),
            )
            conn.execute("DELETE FROM book_highlights WHERE book_slug = ?", (slug,))
            for highlight in book["highlights"]:
                conn.execute(
                    """INSERT OR IGNORE INTO book_highlights
                       (book_slug, position, text, content_hash, quote_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        slug,
                        highlight["position"],
                        highlight["text"],
                        highlight["content_hash"],
                        existing_quote_ids.get(highlight["content_hash"]),
                    ),
                )
            upserted += 1
        if paths is None:
            _soft_delete_disappeared(conn, "books", seen)
    if recover:
        recover_highlight_quotes(book_slugs=seen if paths is not None else None)
    return {"upserted": upserted}


def scan_quotes(paths: list[Path] | None = None) -> dict[str, int]:
    quote_paths = paths if paths is not None else _quote_paths()
    seen: set[str] = set()
    upserted = 0
    with connect() as conn:
        for raw_path in quote_paths:
            path = _absolute_path(raw_path)
            if not path.exists() or path.name.startswith("."):
                if paths is not None and path.suffix == ".md":
                    _soft_delete_missing_path(conn, path, table="quotes")
                continue
            with host_file_lock(path):
                quote = parse_quote_file(path)
            quote_id = quote["id"]
            seen.add(quote_id)
            conn.execute(
                """INSERT INTO quotes
                   (id, path, text, content_hash, source_type, source_ref, tags_json, deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(id) DO UPDATE SET
                     path=excluded.path,
                     text=excluded.text,
                     content_hash=excluded.content_hash,
                     source_type=excluded.source_type,
                     source_ref=excluded.source_ref,
                     tags_json=excluded.tags_json,
                     deleted_at=NULL,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    quote_id,
                    quote["path"],
                    quote["text"],
                    quote["content_hash"],
                    quote["source_type"],
                    quote.get("source_ref"),
                    json.dumps(quote["tags"], ensure_ascii=False),
                ),
            )
            conn.execute("DELETE FROM quote_thoughts WHERE quote_id = ?", (quote_id,))
            for thought in quote["thoughts"]:
                conn.execute(
                    """INSERT OR IGNORE INTO quote_thoughts
                       (quote_id, ts, text)
                       VALUES (?, ?, ?)""",
                    (quote_id, thought["ts"], thought["text"]),
                )
            upserted += 1
        if paths is None:
            _soft_delete_disappeared(conn, "quotes", seen)
    return {"upserted": upserted}


def parse_book_file(path: Path) -> dict[str, Any]:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    title = _clean_text(meta.get("title")) or path.stem.replace("-", " ").title()
    highlights = []
    seen_hashes: set[str] = set()
    for position, line in enumerate(section_lines(body, "Highlights"), start=1):
        match = _HIGHLIGHT_RE.match(line)
        if not match:
            continue
        text = _clean_text(match.group("text"))
        if not text:
            continue
        digest = content_hash(text)
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        highlights.append({"position": position, "text": text, "content_hash": digest})
    return {
        "slug": path.stem,
        "path": str(path.relative_to(vault_dir())),
        "title": title,
        "author": _clean_text(meta.get("author")),
        "cover_url": _clean_text(meta.get("cover_url")),
        "status": _clean_book_status(meta.get("status")),
        "format": _clean_text(meta.get("format")),
        "started": _clean_date(meta.get("started")),
        "finished": _clean_date(meta.get("finished")),
        "rating": _clean_rating(meta.get("rating")),
        "isbn": _clean_text(meta.get("isbn")),
        "summary": _clean_text(meta.get("summary")),
        "highlights": highlights,
        "body": body,
        "frontmatter": meta,
    }


def parse_quote_file(path: Path) -> dict[str, Any]:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    quote_text, thoughts, thought_lines = _split_quote_thoughts(body)
    source_type = _clean_source_type(meta.get("source_type"))
    tags = _string_list(meta.get("tags"))
    return {
        "id": path.stem,
        "path": str(path.relative_to(vault_dir())),
        "text": quote_text,
        "content_hash": content_hash(quote_text),
        "source_type": source_type,
        "source_ref": _clean_text(meta.get("source_ref")),
        "tags": tags,
        "thoughts": thoughts,
        "thought_lines": thought_lines,
        "frontmatter": meta,
    }


def create_book_file(
    *,
    title: str,
    author: str | None = None,
    cover_url: str | None = None,
    status: str = "want",
    format: str | None = None,
    started: str | None = None,
    finished: str | None = None,
    rating: int | None = None,
    isbn: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    clean_title = _clean_text(title)
    if not clean_title:
        raise ValueError("title must be non-blank")
    existing = find_book(clean_title, author=author)
    if existing is not None:
        return book_payload(existing["slug"]) or existing
    meta = {
        "title": clean_title,
        "author": _clean_text(author),
        "cover_url": _clean_text(cover_url),
        "status": _clean_book_status(status),
        "format": _clean_text(format),
        "started": _clean_date(started),
        "finished": _clean_date(finished),
        "rating": _clean_rating(rating),
        "isbn": _clean_text(isbn),
        "summary": _clean_text(summary),
    }
    content = dump_book_file(meta, "## Highlights\n")
    with _CREATE_BOOK_LOCK:
        path = _create_book_file_exclusive(clean_title, content)
    scan_books([path], recover=False)
    book = book_payload(path.stem)
    if book is None:
        raise RuntimeError(f"book mirror missing after write: {path.stem}")
    return book


async def create_book_file_with_lookup(
    *,
    title: str,
    author: str | None = None,
    isbn: str | None = None,
    lookup: bool = False,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if lookup:
        try:
            found = await openlibrary.search_book(title, author=author)
        except Exception:
            found = None
        if found:
            metadata["title"] = found.get("title") or title
            authors = found.get("authors") or []
            metadata["author"] = author or (authors[0] if authors else None)
            metadata["cover_url"] = found.get("cover_url")
            subjects = found.get("subjects") or []
            if subjects:
                metadata["summary"] = ", ".join(str(s) for s in subjects[:5])
        else:
            metadata["title"] = title
            metadata["author"] = author
    else:
        metadata["title"] = title
        metadata["author"] = author
    metadata["isbn"] = isbn
    return create_book_file(**metadata)


async def refresh_book_metadata(slug: str) -> dict[str, Any] | None:
    book = book_payload(slug)
    if book is None:
        return None
    try:
        found = await openlibrary.search_book(book["title"], author=book.get("author"))
    except Exception:
        found = None
    if not found:
        return book
    updates: dict[str, Any] = {
        "cover_url": found.get("cover_url"),
    }
    authors = found.get("authors") or []
    if authors and not book.get("author"):
        updates["author"] = authors[0]
    subjects = found.get("subjects") or []
    if subjects and not book.get("summary"):
        updates["summary"] = ", ".join(str(s) for s in subjects[:5])
    return patch_book(slug, updates)


def patch_book(slug: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    book = book_payload(slug)
    if book is None:
        return None
    path = vault_dir() / book["path"]
    with host_file_lock(path):
        parsed = parse_book_file(path)
        meta = dict(parsed["frontmatter"])
        for key in (
            "title",
            "author",
            "cover_url",
            "format",
            "started",
            "finished",
            "isbn",
            "summary",
        ):
            if key in updates:
                meta[key] = _clean_text(updates[key])
        if "status" in updates:
            meta["status"] = _clean_book_status(updates["status"])
        if "rating" in updates:
            meta["rating"] = _clean_rating(updates["rating"])
        atomic_write(path, dump_book_file(meta, parsed["body"]))
    scan_books([path], recover=False)
    return book_payload(slug)


def add_book_highlight(book_slug: str, text: str) -> dict[str, Any] | None:
    """Append a book highlight and create the linked quote.

    Recovery contract: if quote creation fails after the book file is written,
    a later `scan_library()` will parse the highlight and call
    `recover_highlight_quotes()` to create the missing quote.
    """
    book = book_payload(book_slug)
    if book is None:
        return None
    clean = _clean_text(text)
    if not clean:
        raise ValueError("highlight text must be non-blank")
    digest = content_hash(clean)
    path = vault_dir() / book["path"]
    created = False
    with host_file_lock(path):
        parsed = parse_book_file(path)
        existing_hashes = {item["content_hash"] for item in parsed["highlights"]}
        if digest not in existing_hashes:
            markdown = path.read_text(encoding="utf-8")
            atomic_write(path, append_to_section(markdown, "Highlights", f"- {clean}"))
            created = True
    scan_books([path], recover=False)
    quote = create_quote_file(text=clean, source_type="book", source_ref=book_slug)
    with connect() as conn:
        row = conn.execute(
            """SELECT * FROM book_highlights
               WHERE book_slug = ? AND content_hash = ?""",
            (book_slug, digest),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            """UPDATE book_highlights
                  SET quote_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (quote["id"], row["id"]),
        )
        refreshed = conn.execute("SELECT * FROM book_highlights WHERE id = ?", (row["id"],)).fetchone()
    payload = _highlight_row(dict(refreshed))
    payload["created"] = created
    return payload


def create_quote_file(
    *,
    text: str,
    source_type: str = "conversation",
    source_ref: str | None = None,
    tags: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    clean = _clean_text(text)
    if not clean:
        raise ValueError("quote text must be non-blank")
    source = _clean_source_type(source_type)
    ref = _clean_text(source_ref)
    digest = content_hash(clean)
    existing = _find_quote_by_source_hash(source, ref, digest)
    if existing is not None:
        if not (vault_dir() / existing["path"]).exists():
            _write_quote_file(existing["id"], clean, source, ref, _clean_tags(tags or existing["tags"]))
            scan_quotes([vault_dir() / existing["path"]])
        return existing
    meta = {
        "source_type": source,
        "source_ref": ref,
        "tags": _clean_tags(tags or []),
    }
    content = dump_quote_file(meta, clean, [])
    with _CREATE_QUOTE_LOCK:
        path = _create_quote_file_exclusive(clean, content, now=now)
    scan_quotes([path])
    quote = quote_payload(path.stem)
    if quote is None:
        raise RuntimeError(f"quote mirror missing after write: {path.stem}")
    return quote


def append_quote_thought(
    quote_id: str,
    text: str,
    *,
    ts: str | datetime | None = None,
) -> dict[str, Any] | None:
    quote = quote_payload(quote_id)
    if quote is None:
        return None
    clean = _clean_text(text)
    if not clean:
        raise ValueError("thought text must be non-blank")
    path = vault_dir() / quote["path"]
    timestamp = _clean_thought_ts(ts)
    with host_file_lock(path):
        markdown = path.read_text(encoding="utf-8")
        atomic_write(path, append_to_section(markdown, "Thoughts", f"- {timestamp} {clean}"))
    scan_quotes([path])
    return quote_payload(quote_id)


def recover_highlight_quotes(*, book_slugs: set[str] | None = None) -> int:
    clauses = ["(bh.quote_id IS NULL OR q.id IS NULL OR q.deleted_at IS NOT NULL)"]
    params: list[Any] = []
    if book_slugs:
        placeholders = ",".join("?" for _ in book_slugs)
        clauses.append(f"bh.book_slug IN ({placeholders})")
        params.extend(sorted(book_slugs))
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT bh.id, bh.book_slug, bh.text
                FROM book_highlights bh
                LEFT JOIN quotes q ON q.id = bh.quote_id
                WHERE {' AND '.join(clauses)}
                ORDER BY bh.book_slug, bh.position""",
            tuple(params),
        ).fetchall()
    recovered = 0
    for row in rows:
        quote = create_quote_file(
            text=row["text"],
            source_type="book",
            source_ref=row["book_slug"],
        )
        with connect() as conn:
            conn.execute(
                """UPDATE book_highlights
                      SET quote_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?""",
                (quote["id"], row["id"]),
            )
        recovered += 1
    return recovered


def list_books(*, status: str | None = None) -> list[dict[str, Any]]:
    clauses = ["b.deleted_at IS NULL"]
    params: list[Any] = []
    if status:
        clauses.append("b.status = ?")
        params.append(status)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT b.*,
                       COUNT(bh.id) AS highlight_count
                FROM books b
                LEFT JOIN book_highlights bh ON bh.book_slug = b.slug
                WHERE {' AND '.join(clauses)}
                GROUP BY b.slug
                ORDER BY lower(b.title), b.title""",
            tuple(params),
        ).fetchall()
    return [_book_row(dict(row)) for row in rows]


def book_payload(slug: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT b.*,
                      COUNT(bh.id) AS highlight_count
               FROM books b
               LEFT JOIN book_highlights bh ON bh.book_slug = b.slug
               WHERE b.slug = ? AND b.deleted_at IS NULL
               GROUP BY b.slug""",
            (slug,),
        ).fetchone()
    if row is None:
        return None
    payload = _book_row(dict(row))
    path = vault_dir() / payload["path"]
    if not path.exists():
        _soft_delete_slug("books", slug)
        return None
    parsed = parse_book_file(path)
    with connect() as conn:
        highlights = [
            _highlight_row(dict(item))
            for item in conn.execute(
                """SELECT * FROM book_highlights
                   WHERE book_slug = ?
                   ORDER BY position, id""",
                (slug,),
            ).fetchall()
        ]
        linked_quotes = [
            _quote_row(dict(item))
            for item in conn.execute(
                """SELECT q.*
                   FROM quotes q
                   JOIN book_highlights bh ON bh.quote_id = q.id
                   WHERE bh.book_slug = ? AND q.deleted_at IS NULL
                   ORDER BY bh.position, bh.id""",
                (slug,),
            ).fetchall()
        ]
    return {
        **payload,
        "frontmatter": parsed["frontmatter"],
        "body": parsed["body"],
        "highlights": highlights,
        "linked_quotes": linked_quotes,
    }


def list_quotes(
    *,
    source_type: str | None = None,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["deleted_at IS NULL"]
    params: list[Any] = []
    if source_type:
        clauses.append("source_type = ?")
        params.append(source_type)
    with connect() as conn:
        rows = [_quote_row(dict(row)) for row in conn.execute(
            f"SELECT * FROM quotes WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id DESC",
            tuple(params),
        ).fetchall()]
    if tag:
        rows = [row for row in rows if tag in row["tags"]]
    return rows


def quote_payload(quote_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM quotes WHERE id = ? AND deleted_at IS NULL",
            (quote_id,),
        ).fetchone()
    if row is None:
        return None
    payload = _quote_row(dict(row))
    path = vault_dir() / payload["path"]
    if not path.exists():
        _soft_delete_slug("quotes", quote_id)
        return None
    parsed = parse_quote_file(path)
    with connect() as conn:
        thoughts = [
            dict(row)
            for row in conn.execute(
                """SELECT ts, text FROM quote_thoughts
                   WHERE quote_id = ?
                   ORDER BY ts, rowid""",
                (quote_id,),
            ).fetchall()
        ]
    return {
        **payload,
        "text": parsed["text"],
        "source_type": parsed["source_type"],
        "source_ref": parsed.get("source_ref"),
        "tags": parsed["tags"],
        "frontmatter": parsed["frontmatter"],
        "thoughts": thoughts,
    }


def find_book(ref: str | None, *, author: str | None = None) -> dict[str, Any] | None:
    clean = _clean_text(ref)
    if not clean:
        return None
    slug_ref = slugify(clean)
    with connect() as conn:
        if slug_ref:
            row = conn.execute(
                "SELECT * FROM books WHERE slug = ? AND deleted_at IS NULL",
                (slug_ref,),
            ).fetchone()
            if row:
                return _book_row(dict(row))
        rows = conn.execute(
            "SELECT * FROM books WHERE deleted_at IS NULL ORDER BY updated_at DESC"
        ).fetchall()
    target_title = _normalize_title(clean)
    target_author = _normalize_title(author)
    for row in rows:
        book = _book_row(dict(row))
        if _normalize_title(book["title"]) != target_title:
            continue
        if target_author and _normalize_title(book.get("author")) != target_author:
            continue
        return book
    return None


def match_book_in_text(text: str) -> dict[str, Any] | None:
    normalized = _normalize_title(text)
    if not normalized:
        return None
    candidates = sorted(list_books(), key=lambda book: len(book["title"]), reverse=True)
    for book in candidates:
        title = _normalize_title(book["title"])
        if title and title in normalized:
            return book
    return None


def content_hash(text: str) -> str:
    return hashlib.sha256(_normalize_content(text).encode("utf-8")).hexdigest()


def dump_book_file(meta: dict[str, Any], body: str) -> str:
    frontmatter = yaml.safe_dump(
        _clean_frontmatter(meta),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    clean_body = body.strip() or "## Highlights"
    return f"---\n{frontmatter}\n---\n\n{clean_body}\n"


def dump_quote_file(
    meta: dict[str, Any],
    text: str,
    thoughts: list[dict[str, str]],
    *,
    thought_lines: list[str] | None = None,
) -> str:
    frontmatter = yaml.safe_dump(
        _clean_frontmatter(meta),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    lines = [f"---\n{frontmatter}\n---", "", text.strip(), "", "## Thoughts"]
    if thought_lines is None:
        thought_lines = [f"- {thought['ts']} {thought['text']}" for thought in thoughts]
    lines.extend(thought_lines)
    return "\n".join(lines).rstrip() + "\n"


def _write_quote_file(
    quote_id: str,
    text: str,
    source_type: str,
    source_ref: str | None,
    tags: list[str],
) -> Path:
    path = quotes_dir() / f"{quote_id}.md"
    atomic_write(
        path,
        dump_quote_file(
            {"source_type": source_type, "source_ref": source_ref, "tags": tags},
            text,
            [],
        ),
    )
    return path


def _book_paths() -> list[Path]:
    directory = books_dir()
    if not directory.exists():
        return []
    return sorted(path for path in directory.glob("*.md") if path.is_file())


def _quote_paths() -> list[Path]:
    directory = quotes_dir()
    if not directory.exists():
        return []
    return sorted(path for path in directory.glob("*.md") if path.is_file())


def _absolute_path(path: Path) -> Path:
    return path if path.is_absolute() else vault_dir() / path


def _create_book_file_exclusive(title: str, content: str) -> Path:
    base = slugify(title)[:80] or "book"
    books_dir().mkdir(parents=True, exist_ok=True)
    for attempt in range(1, 100):
        slug = base if attempt == 1 else f"{base}-{attempt}"
        path = books_dir() / f"{slug}.md"
        if path.exists():
            continue
        try:
            with host_file_lock(path), path.open("x", encoding="utf-8") as handle:
                handle.write(content)
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"unable to allocate book slug for {title!r}")


def _create_quote_file_exclusive(text: str, content: str, *, now: datetime | None) -> Path:
    current = now or _now_in_capture_timezone()
    digest = content_hash(text)[:10]
    slug_part = slugify(text[:48])[:32] or "quote"
    base = f"{current.strftime('%Y%m%d')}-{digest}-{slug_part}"[:80]
    quotes_dir().mkdir(parents=True, exist_ok=True)
    for attempt in range(1, 100):
        quote_id = base if attempt == 1 else f"{base}-{attempt}"
        path = quotes_dir() / f"{quote_id}.md"
        if path.exists():
            continue
        try:
            with host_file_lock(path), path.open("x", encoding="utf-8") as handle:
                handle.write(content)
            return path
        except FileExistsError:
            continue
    raise RuntimeError("unable to allocate quote id")


def _split_quote_thoughts(body: str) -> tuple[str, list[dict[str, str]], list[str]]:
    lines = body.splitlines()
    quote_lines: list[str] = []
    thoughts: list[dict[str, str]] = []
    thought_lines: list[str] = []
    in_thoughts = False
    for line in lines:
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            in_thoughts = heading.group(1).strip().lower() == "thoughts"
            if in_thoughts:
                continue
        if in_thoughts:
            thought_lines.append(line)
            match = _THOUGHT_RE.match(line)
            if match:
                thoughts.append({"ts": match.group("ts"), "text": match.group("text").strip()})
        else:
            quote_lines.append(line)
    return "\n".join(quote_lines).strip(), thoughts, thought_lines


def _find_quote_by_source_hash(
    source_type: str,
    source_ref: str | None,
    digest: str,
) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT * FROM quotes
               WHERE source_type = ?
                 AND COALESCE(source_ref, '') = ?
                 AND content_hash = ?
                 AND deleted_at IS NULL
               ORDER BY created_at ASC
               LIMIT 1""",
            (source_type, source_ref or "", digest),
        ).fetchone()
    return _quote_row(dict(row)) if row else None


def _clean_frontmatter(meta: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in meta.items():
        if value is None or value == "":
            continue
        if key == "tags" and value == []:
            continue
        clean[key] = value
    return clean


def _highlight_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "book_slug": row["book_slug"],
        "position": row["position"],
        "text": row["text"],
        "content_hash": row["content_hash"],
        "quote_id": row["quote_id"],
    }


def _book_row(row: dict[str, Any]) -> dict[str, Any]:
    row["highlight_count"] = int(row.get("highlight_count") or 0)
    return row


def _quote_row(row: dict[str, Any]) -> dict[str, Any]:
    row["tags"] = json.loads(row.pop("tags_json") or "[]")
    return row


def _soft_delete_missing_path(conn, path: Path, *, table: str) -> None:
    try:
        rel = str(path.relative_to(vault_dir()))
    except ValueError:
        return
    conn.execute(
        f"UPDATE {table} SET deleted_at = CURRENT_TIMESTAMP WHERE path = ? AND deleted_at IS NULL",
        (rel,),
    )


def _soft_delete_disappeared(conn, table: str, seen: set[str]) -> None:
    id_column = "slug" if table == "books" else "id"
    if seen:
        placeholders = ",".join("?" for _ in seen)
        conn.execute(
            f"""UPDATE {table} SET deleted_at = CURRENT_TIMESTAMP
                WHERE deleted_at IS NULL AND {id_column} NOT IN ({placeholders})""",
            tuple(seen),
        )
    else:
        conn.execute(f"UPDATE {table} SET deleted_at = CURRENT_TIMESTAMP WHERE deleted_at IS NULL")


def _soft_delete_slug(table: str, entity_id: str) -> None:
    id_column = "slug" if table == "books" else "id"
    with connect() as conn:
        conn.execute(
            f"""UPDATE {table}
                   SET deleted_at = CURRENT_TIMESTAMP
                 WHERE {id_column} = ? AND deleted_at IS NULL""",
            (entity_id,),
        )


def _clean_book_status(value: object) -> str:
    status = str(value or "want").strip().lower()
    return status if status in _VALID_BOOK_STATUSES else "want"


def _clean_source_type(value: object) -> str:
    source = str(value or "conversation").strip().lower()
    return source if source in _VALID_SOURCE_TYPES else "conversation"


def _clean_text(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _clean_date(value: object) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    return text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else None


def _clean_rating(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        rating = int(value)
    except (TypeError, ValueError):
        return None
    return rating if 1 <= rating <= 5 else None


def _string_list(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return _clean_tags(value)
    if isinstance(value, str):
        return _clean_tags([part.strip() for part in value.split(",")])
    return []


def _clean_tags(values: list[object]) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for value in values:
        tag = slugify(str(value).strip())
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def _normalize_content(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _normalize_title(value: object) -> str:
    return _normalize_content(str(value or ""))


def _clean_thought_ts(value: str | datetime | None) -> str:
    if isinstance(value, datetime):
        current = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if _THOUGHT_RE.match(f"- {text} x"):
            return text[:16]
        try:
            current = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            current = _now_in_capture_timezone()
    else:
        current = _now_in_capture_timezone()
    if current.tzinfo is not None:
        current = current.astimezone(ZoneInfo(get_settings().capture.default_timezone))
    return current.strftime("%Y-%m-%d %H:%M")


def _now_in_capture_timezone() -> datetime:
    tz = ZoneInfo(get_settings().capture.default_timezone)
    return datetime.now(tz)
