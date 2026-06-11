"""Notetaker — scans vault/_notes/inbox/ every 30s, classifies stable files via Claude Code.

Behavior per spec §9.1, §10, §11, §12:
- Scan inbox for *.md; skip .icloud, dotfiles, conflict copies.
- A file is considered stable when (mtime, size) is unchanged between two ticks
  ≥ ``classify_stable_mtime_seconds`` apart. In-memory cache; resets on restart.
- External-editor drops get a fresh ``notes`` row (source='file'); the route-
  captured ones already have a row.
- Overrides ``run_once`` to batch up to ``notetaker_concurrency`` queued jobs
  concurrently under an ``asyncio.Semaphore``. Enqueue → classify → write YAML
  frontmatter (preserving prose) → move inbox→YYYY-MM-DD/ → update DB → insert
  ``note_links`` → emit feed row.

Phase 4-5 hand-off to the Escalator is marked with a TODO and left commented
out until that agent exists.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import yaml

from mastisk.agents.base import Agent
from mastisk.bridges import intelligence
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.paths import notes_daily_dir, notes_dir, notes_inbox_dir, vault_dir
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.notetaker")

VALID_CLASSIFICATIONS = {
    "insight", "question", "idea", "todo", "observation", "rant",
}

CLASSIFY_PROMPT = """You are classifying a short personal note into a structured record.

## About the user
{identity}

## Known wiki articles (ids)
{article_ids}

## The note to classify
<<<
{body}
>>>

Respond with a single JSON block — no prose, no markdown, no preamble — matching this schema EXACTLY:

{{
  "classification": "insight|question|idea|todo|observation|rant",
  "summary": "one-sentence capture of what this note is about, max 140 chars",
  "confidence": 0.0,
  "tags": ["tag-1", "tag-2"],
  "related_articles": ["article-id-1", "article-id-2"]
}}

Rules:
- "classification" must be one of the six listed values, exact spelling.
- "summary" is under 140 chars; a terse paraphrase, not a quote.
- "confidence" is your self-calibration 0..1 — how confident you are that the classification and summary are accurate.
- "tags" are 1-5 lowercase kebab-case tags that capture the note's topics.
- "related_articles" ARE IDS from the known wiki articles list above. Only include articles that genuinely overlap with the note's subject. Max 5. If nothing overlaps, return [].
- If the note body contains a structured preamble starting with "> From article:", treat the referenced article as highly relevant — include its id in related_articles if not already there.
"""


class Notetaker(Agent):
    """Notetaker agent. See module docstring + spec §§9.1, 10, 11, 12."""

    name: ClassVar[str] = "notetaker"
    tick_seconds: ClassVar[int] = 30

    def __init__(self) -> None:
        # path -> (mtime, size, first_seen_at). Used for the two-tick stability
        # check: only classify when mtime+size stay identical across at least
        # ``classify_stable_mtime_seconds``. Process-lifetime only.
        self._stability_cache: dict[str, tuple[float, int, float]] = {}

    # ───── scheduler entry point ─────

    async def run_once(self) -> None:
        """Scan inbox + enqueue, then batch-process up to ``notetaker_concurrency`` jobs."""
        settings = get_settings().notes
        inbox = notes_inbox_dir()
        if inbox.exists():
            try:
                await self._scan_inbox(
                    inbox, stable_threshold=settings.classify_stable_mtime_seconds
                )
            except Exception:
                # A scan blow-up mustn't block job draining on this tick.
                log.exception("notetaker: inbox scan failed")

        jobs = self._pick_jobs(limit=settings.notetaker_concurrency)
        if not jobs:
            return

        sem = asyncio.Semaphore(settings.notetaker_concurrency)

        async def run_one(job: dict) -> None:
            async with sem:
                log.info("notetaker: picking job %s (%s)", job["id"], job["kind"])
                self._mark_running(job["id"])
                try:
                    await self._handle(job)
                    self._mark_done(job["id"])
                except Exception as e:
                    log.exception("notetaker: job %s failed", job["id"])
                    self._mark_failed(job["id"], str(e))

        await asyncio.gather(*(run_one(j) for j in jobs))

    def _pick_jobs(self, limit: int) -> list[dict]:
        """Notetaker-private batched variant of ``Agent._pick_job``.

        NOT on the base class — per spec §10, only Notetaker batches; other
        agents keep the one-job-per-tick contract.
        """
        with connect() as conn:
            rows = conn.execute(
                """SELECT * FROM jobs
                   WHERE agent = ? AND status = 'queued'
                   ORDER BY id ASC LIMIT ?""",
                (self.name, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ───── scan + enqueue ─────

    async def _scan_inbox(self, inbox: Path, stable_threshold: float) -> None:
        """Walk inbox/*.md, skip junk, enforce two-tick stability, enqueue classify jobs."""
        now = time.time()
        for p in inbox.iterdir():
            if not p.is_file():
                continue
            name = p.name
            # iCloud placeholders: `.foo.md.icloud`. Scan once it's downloaded.
            if name.endswith(".icloud"):
                continue
            # Dotfiles (our own atomic-write temps + hidden junk).
            if name.startswith("."):
                continue
            if not name.endswith(".md"):
                continue
            if "conflicted" in name.lower():
                continue
            try:
                stat = p.stat()
            except FileNotFoundError:
                # Race: file vanished mid-scan (e.g. user moved it in Obsidian).
                continue

            key = str(p.resolve())
            cached = self._stability_cache.get(key)
            if cached is None or cached[0] != stat.st_mtime or cached[1] != stat.st_size:
                # First sight, or changed since last tick: reset the stability clock.
                self._stability_cache[key] = (stat.st_mtime, stat.st_size, now)
                continue
            first_seen = cached[2]
            if now - first_seen < stable_threshold:
                # Same mtime+size, but we haven't waited long enough yet.
                continue

            # File is stable. Hand off to the per-file enqueue path.
            try:
                await self._enqueue_if_needed(p, stat)
            except Exception:
                log.exception("notetaker: enqueue failed for %s", p)

    async def _enqueue_if_needed(self, path: Path, stat: os.stat_result) -> None:
        """If we don't already have a classified-or-pending row for this file, enqueue classification.

        Handles three cases:
          1. No DB row (external editor dropped a file): insert one (source='file').
             If the file already carries frontmatter it's been classified on a
             previous run — do nothing.
          2. DB row exists, still unclassified: enqueue.
          3. DB row exists, already classified: no-op.
        """
        rel_path = str(path.relative_to(vault_dir()))
        with connect() as conn:
            row = conn.execute(
                "SELECT id, classification FROM notes WHERE path = ? AND deleted_at IS NULL",
                (rel_path,),
            ).fetchone()
            if row is None:
                # External-editor drop. Read the file, insert a notes row.
                try:
                    body = path.read_text(encoding="utf-8")
                except Exception as e:
                    log.warning("notetaker: can't read %s: %s", path, e)
                    return
                if body.startswith("---\n"):
                    # Already classified on disk (frontmatter present). Either
                    # this is a re-scan of a dated file sitting in inbox, or
                    # a user manually added frontmatter. Don't touch.
                    return
                body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
                slug = path.stem
                created_at = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()
                try:
                    cur = conn.execute(
                        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at)
                           VALUES (?, ?, ?, ?, 'file', ?)""",
                        (slug, rel_path, body, body_sha, created_at),
                    )
                    note_id = cur.lastrowid or 0
                except sqlite3.IntegrityError:
                    # Race: API captured a file with the same path/slug between
                    # our SELECT and our INSERT. Re-read the row and proceed.
                    existing = conn.execute(
                        "SELECT id, classification FROM notes WHERE path = ?",
                        (rel_path,),
                    ).fetchone()
                    if existing is None:
                        raise
                    if existing["classification"] is not None:
                        return
                    note_id = existing["id"]
            else:
                if row["classification"] is not None:
                    return
                try:
                    body = path.read_text(encoding="utf-8")
                except Exception as e:
                    log.warning("notetaker: can't read %s: %s", path, e)
                    return
                if has_typed_capture_frontmatter(body):
                    log.info("notetaker: routed capture %s already typed, skipping", path)
                    return
                note_id = row["id"]

        # Enqueue outside any transaction. `enqueue` opens its own connection.
        from mastisk.agents.base import enqueue
        enqueue("notetaker", "classify", {"note_id": note_id})

    # ───── classify handler ─────

    async def _handle(self, job: dict) -> None:
        payload = json.loads(job["payload_json"] or "{}")
        note_id = payload.get("note_id")
        if note_id is None:
            log.warning("notetaker: no note_id in job %s", job["id"])
            return

        with connect() as conn:
            note = q.get_note(conn, note_id)
        if note is None or note.get("deleted_at") is not None:
            log.info("notetaker: note %s not found or deleted, skipping", note_id)
            return
        if note.get("classification") is not None:
            log.info("notetaker: note %s already classified, skipping", note_id)
            return

        file_path = vault_dir() / note["path"]
        if not file_path.exists():
            log.warning("notetaker: file missing for note %s: %s", note_id, file_path)
            return

        body_on_disk = file_path.read_text(encoding="utf-8")
        if has_typed_capture_frontmatter(body_on_disk):
            log.info("notetaker: note %s is already routed, skipping classifier", note_id)
            return
        body_core = strip_frontmatter(body_on_disk)
        sha_after = hashlib.sha256(body_core.encode("utf-8")).hexdigest()

        # Build the prompt. `article_ids` is capped so the prompt stays cheap;
        # the 500 most recent articles is plenty for a classifier that only
        # needs to spot topical overlap.
        with connect() as conn:
            article_rows = conn.execute(
                "SELECT id FROM articles ORDER BY updated_at DESC LIMIT 500"
            ).fetchall()
        article_ids = [r["id"] for r in article_rows]
        identity = self.load_identity()
        prompt = CLASSIFY_PROMPT.format(
            identity=identity,
            article_ids="\n".join(f"- {aid}" for aid in article_ids) or "(none yet)",
            body=body_core,
        )

        result, _ = await intelligence.run_intelligence(prompt, timeout_s=180)
        raw_text = result.get("text", "") if isinstance(result, dict) else str(result)
        parsed = _extract_json(raw_text)

        classification = parsed.get("classification")
        if classification not in VALID_CLASSIFICATIONS:
            raise ValueError(
                f"notetaker: invalid classification {classification!r} for note {note_id}"
            )

        summary = (parsed.get("summary") or "")[:140]
        try:
            confidence = float(parsed.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        tags = [str(t) for t in (parsed.get("tags") or [])][:10]
        related_articles_raw = [str(a) for a in (parsed.get("related_articles") or [])]

        # Validate related_articles actually exist — hallucinated ids happen.
        related_articles: list[str] = []
        if related_articles_raw:
            with connect() as conn:
                placeholders = ",".join("?" * len(related_articles_raw))
                existing = {
                    r["id"]
                    for r in conn.execute(
                        f"SELECT id FROM articles WHERE id IN ({placeholders})",
                        related_articles_raw,
                    ).fetchall()
                }
            # Preserve Ollama's ordering; dedup while we're at it.
            seen: set[str] = set()
            for a in related_articles_raw:
                if a in existing and a not in seen:
                    related_articles.append(a)
                    seen.add(a)
                if len(related_articles) >= 5:
                    break

        classified_at = datetime.now().astimezone().isoformat()
        frontmatter = {
            "created_at": note["created_at"],
            "classified_at": classified_at,
            "classification": classification,
            "summary": summary,
            "confidence": round(confidence, 2),
            "tags": tags,
            "related_articles": related_articles,
            "escalation_state": "none",
        }

        # Target path: `_notes/YYYY-MM-DD/<filename>.md`, bucket by created_at.
        created_dt = _parse_iso(note["created_at"])
        date_dir = notes_dir() / created_dt.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        new_path = date_dir / file_path.name
        new_rel_path = str(new_path.relative_to(vault_dir()))

        # Write frontmatter + preserved body atomically to the new location.
        write_frontmatter(new_path, body_on_disk, frontmatter)

        # Remove the old inbox file and drop the stability-cache entry.
        if file_path.resolve() != new_path.resolve():
            with contextlib.suppress(FileNotFoundError):
                file_path.unlink()
        self._stability_cache.pop(str(file_path.resolve()), None)

        # Persist classification. Path changes too, so update it in the same
        # UPDATE rather than a second statement.
        with connect() as conn:
            conn.execute(
                """UPDATE notes
                   SET classified_at = ?, classification = ?, summary = ?, confidence = ?,
                       tags_json = ?, path = ?, body_sha256 = ?
                   WHERE id = ?""",
                (
                    classified_at, classification, summary, confidence,
                    json.dumps(tags), new_rel_path, sha_after, note_id,
                ),
            )
            # rank >= 1 for classifier-created links (rank 0 is reserved for
            # direct user-action links — see spec §5 + queries.insert_note_link).
            for i, aid in enumerate(related_articles):
                q.insert_note_link(conn, note_id=note_id, article_id=aid, rank=i + 1)

        self.emit_feed(
            verb="classified",
            obj=note["slug"],
            kind="note",
            payload={
                "classification": classification,
                "note_id": note_id,
                "related_articles": related_articles,
            },
        )

        # Phase 3: regenerate the daily digest for this note's date. Best-effort —
        # classification already succeeded, so a digest failure mustn't fail the
        # job. The next classification on the same date will re-attempt.
        try:
            with connect() as conn:
                write_daily_digest(conn, created_dt.strftime("%Y-%m-%d"))
        except Exception:
            log.exception(
                "notetaker: daily digest regen failed for note %s (non-fatal)", note_id
            )

        # Hand off to the Escalator. It decides whether the auto-rule fires.
        # Every hand-off is a `jobs` row so crashes don't lose work. `trigger`
        # in the payload defaults to 'auto' in the evaluate handler, but being
        # explicit here makes debugging easier.
        from mastisk.agents.base import enqueue
        enqueue("escalator", "evaluate", {"note_id": note_id, "trigger": "auto"})


# ─────────────────────────────── module helpers ───────────────────────────────

def strip_frontmatter(text: str) -> str:
    """If ``text`` starts with ``---\\n``, strip through the next ``---\\n``.

    Idempotent if no frontmatter is present. Returns the body with any leading
    blank line after the closing fence removed so the caller can re-embed.
    """
    _frontmatter, body = split_frontmatter(text)
    return body


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Return ``(frontmatter, body)`` for a markdown file."""
    if not text.startswith("---\n"):
        return {}, text
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return {}, text
    raw_frontmatter = parts[1]
    body = parts[2].lstrip("\n")
    try:
        parsed = yaml.safe_load(raw_frontmatter) or {}
    except yaml.YAMLError:
        return {}, body
    if not isinstance(parsed, dict):
        return {}, body
    return parsed, body


def has_typed_capture_frontmatter(text: str) -> bool:
    """True when frontmatter marks a routed non-note capture."""
    frontmatter, _body = split_frontmatter(text)
    capture = frontmatter.get("capture")
    if not isinstance(capture, dict):
        return False
    capture_type = capture.get("type")
    return isinstance(capture_type, str) and capture_type not in {"note", "inbox"}


def write_frontmatter(path: Path, body_text: str, frontmatter: dict) -> None:
    """Write ``---\\n<yaml>\\n---\\n\\n<body>`` atomically (tempfile + rename).

    ``body_text`` is treated as the core body; any pre-existing frontmatter in
    it is stripped before re-embedding so the operation is idempotent.
    """
    existing_frontmatter, body_core = split_frontmatter(body_text)
    merged_frontmatter = {**existing_frontmatter, **frontmatter}
    fm_yaml = yaml.safe_dump(
        merged_frontmatter, sort_keys=False, default_flow_style=False, allow_unicode=True
    ).strip()
    content = f"---\n{fm_yaml}\n---\n\n{body_core}"

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.rename(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_daily_digest(conn: sqlite3.Connection, date_str: str) -> Path:
    """(Re)generate the daily digest for a given date.

    Format: a markdown page with YAML frontmatter (tiny) + one section per note,
    chronological. Meant to be read in Obsidian/Files. Regenerable from the DB
    at any time — delete it and the next Notetaker tick will rebuild it.

    Returns the absolute path written.
    """
    rows = conn.execute(
        """SELECT id, slug, path, created_at, classification, summary, tags_json, body
           FROM notes
           WHERE substr(created_at, 1, 10) = ?
             AND classified_at IS NOT NULL
             AND deleted_at IS NULL
           ORDER BY created_at ASC, id ASC""",
        (date_str,),
    ).fetchall()

    digest_dir = notes_daily_dir()
    digest_dir.mkdir(parents=True, exist_ok=True)
    out = digest_dir / f"{date_str}.md"

    fm = {
        "date": date_str,
        "note_count": len(rows),
        "generated_at": datetime.now().astimezone().isoformat(),
    }
    fm_yaml = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False).strip()

    lines: list[str] = [f"---\n{fm_yaml}\n---\n", f"# Notes · {date_str}\n"]
    if not rows:
        lines.append("*(no classified notes yet for this date)*\n")
    for r in rows:
        # Time-of-day anchor: pull HH:MM from the ISO timestamp.
        ts = r["created_at"]
        try:
            hhmm = datetime.fromisoformat(ts).strftime("%H:%M")
        except Exception:
            hhmm = ts[11:16] if len(ts) >= 16 else "??:??"
        classification = r["classification"] or "?"
        summary = (r["summary"] or "").strip() or r["slug"]
        try:
            tags = json.loads(r["tags_json"] or "[]") or []
        except Exception:
            tags = []
        tag_str = " ".join(f"#{t}" for t in tags)
        # Relative link from daily/ to the note's file (always resolves via iCloud).
        relative_to_daily = Path("..") / Path(r["path"]).relative_to(Path("_notes"))
        lines.append(f"## {hhmm} · {classification} — {summary}\n")
        if tag_str:
            lines.append(f"*{tag_str}*\n")
        lines.append(f"[Open note]({relative_to_daily.as_posix()})\n")
        # Small excerpt: first 200 chars after stripping frontmatter + "> From"
        # context preamble lines.
        body = r["body"] or ""
        body_core = strip_frontmatter(body) if body.startswith("---\n") else body
        core_lines = [ln for ln in body_core.splitlines() if not ln.startswith("> ")]
        core_text = "\n".join(core_lines).strip()
        if core_text:
            excerpt = core_text[:200] + ("…" if len(core_text) > 200 else "")
            lines.append(f"> {excerpt}\n")
        lines.append("")  # blank between entries

    content = "\n".join(lines).rstrip() + "\n"
    # Atomic write (match write_frontmatter's pattern).
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.rename(tmp, out)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return out


def _extract_json(text: str) -> dict:
    """Lenient JSON extractor — handles fenced blocks + naked braces.

    Strategy: raw json.loads, then ```json fenced``` blocks, then first-{-to-
    last-} substring. A parse failure here raises ``ValueError`` so the Agent
    base marks the job failed and it can be retried.
    """
    # 1) Clean raw parse.
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2) ```json ... ``` or ``` ... ``` block.
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 3) First `{` through last `}` — covers the case where the model prepends
    # chatter before the JSON.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"no parseable JSON in classifier response: {text[:200]!r}")


def _parse_iso(value: str) -> datetime:
    """Parse a DB-stored datetime string.

    SQLite stores our ``created_at`` as either a Python ``datetime.isoformat()``
    string (from the capture route) or CURRENT_TIMESTAMP (``'YYYY-MM-DD HH:MM:SS'``).
    Handle both.
    """
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        # CURRENT_TIMESTAMP format — no 'T' separator.
        return datetime.fromisoformat(value.replace(" ", "T"))
