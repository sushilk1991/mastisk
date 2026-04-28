"""Escalator — researches high-value notes into wiki-article stubs.

Behavior per spec §§9.2, 9.3, 9.4, 10, 11:

Tick (every 60s):
  1. Retry-reactivation scan: for each note in state ``pending``/``retrying``
     whose ``escalation_next_attempt_at`` has arrived and has no queued
     ``evaluate`` job, enqueue one.
  2. Process ONE ``evaluate`` job (base ``Agent.run_once`` — serial).

Evaluate (under class-level ``asyncio.Lock``):
  - Rule check (auto trigger only — manual bypasses):
      classification ∈ auto_escalate_classifications
      confidence ≥ auto_escalate_min_confidence
      len(body) ≥ auto_escalate_min_length
      today's ``note_escalations.result='stub_created'`` count < cap
      no prior escalation within ``dedup_hours`` with ≥ 0.4 word-Jaccard
  - Rule fail → ``skipped_cap`` / ``skipped_dup`` / ``none`` + failed row.
  - Rule pass → ``pending`` → run Claude → build stub article.
  - On ClaudeError with retries remaining: ``retrying`` + backoff.
  - On exhausted retries: fall back to Ollama; if Ollama fails too → ``failed``.

See §9.4 for the full state machine.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, ClassVar

from slugify import slugify

from mastisk.agents.base import Agent, enqueue
from mastisk.bridges import claude_bridge, codex_bridge, ollama_bridge
from mastisk.bridges.claude_bridge import ClaudeError
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.paths import vault_dir
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.escalator")


TERMINAL_STATES = frozenset({"auto_done", "manual_done", "failed"})


ESCALATE_PROMPT = """You are researching a user's note and producing a wiki article stub.

## About the user
{identity}

## Known wiki article IDs (for linking)
{article_ids}

## The user's note
<<<
{note_body}
>>>

Reply with a single JSON block — no prose, no markdown fences:

{{
  "title": "Short human-readable title, max 80 chars",
  "kind": "Concept" | "Entity" | "Synthesis",
  "framing_paragraph": "1-2 paragraph framing of the topic, HTML-safe. Use <span class=\\"link\\" data-target=\\"slug\\"> for cross-references to the known article IDs.",
  "research_questions": ["question 1", "question 2", "..."]
}}

Rules:
- Pick "kind" based on the note: Concept for an abstract idea, Entity for a specific person/org/product, Synthesis when stitching multiple known articles.
- 3-5 research questions that an analyst would dig into next — NEVER metadata questions ("what's the date?"), always analytical threads.
- title is a slug-friendly phrase; avoid punctuation.
"""


class Escalator(Agent):
    """Escalator agent. See module docstring + spec §§9.2, 9.3, 9.4."""

    name: ClassVar[str] = "escalator"
    tick_seconds: ClassVar[int] = 60

    # Class-level — all rule checks + stub writes run under this lock so the
    # daily cap count + dedup stay race-free. Instantiated once at class load
    # (Python 3.10+ allows asyncio.Lock without a running loop).
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    # ───── scheduler entry point ─────

    async def run_once(self) -> None:
        """Reactivate due retries, then process one evaluate job."""
        try:
            self._reenqueue_due_retries()
        except Exception:
            log.exception("escalator: retry-reactivation scan failed")
        await super().run_once()

    def _reenqueue_due_retries(self) -> None:
        """For notes in pending/retrying whose next_attempt_at is due, enqueue
        one evaluate job per note (idempotent on already-queued jobs).

        Carries ``escalation_trigger`` from the note row into the job payload
        so the handler knows whether the original trigger was ``auto`` or
        ``manual`` without a second DB read.
        """
        now_iso = datetime.now().astimezone().isoformat()
        with connect() as conn:
            due = conn.execute(
                """SELECT id, escalation_trigger FROM notes
                   WHERE escalation_state IN ('pending', 'retrying')
                     AND escalation_next_attempt_at IS NOT NULL
                     AND escalation_next_attempt_at <= ?
                     AND deleted_at IS NULL""",
                (now_iso,),
            ).fetchall()
            for row in due:
                note_id = row["id"]
                trigger = row["escalation_trigger"] or "auto"
                existing = conn.execute(
                    """SELECT 1 FROM jobs
                       WHERE agent='escalator' AND kind='evaluate'
                         AND status IN ('queued', 'running')
                         AND payload_json LIKE ?""",
                    (f'%"note_id": {note_id}%',),
                ).fetchone()
                if existing:
                    continue
                # Safe to enqueue outside a nested tx — base.enqueue opens its own.
                enqueue(
                    "escalator", "evaluate",
                    {"note_id": note_id, "trigger": trigger},
                )

    # ───── evaluate handler ─────

    async def _handle(self, job: dict) -> None:
        payload = json.loads(job["payload_json"] or "{}")
        note_id = payload.get("note_id")
        if note_id is None:
            log.warning("escalator: no note_id in job %s", job["id"])
            return
        trigger = payload.get("trigger") or "auto"
        if trigger not in ("auto", "manual"):
            trigger = "auto"

        async with self._lock:
            await self._evaluate(note_id=note_id, trigger=trigger)

    async def _evaluate(self, *, note_id: int, trigger: str) -> None:
        """The full evaluate path — rule check, Claude call, retry/fallback.

        Runs inside ``self._lock`` so cap-check + stub-insert stay atomic.
        All state transitions happen inside the lock too.
        """
        with connect() as conn:
            note = q.get_note(conn, note_id)
        if note is None or note.get("deleted_at") is not None:
            log.info("escalator: note %s not found or deleted, skipping", note_id)
            return

        current_state = note["escalation_state"]
        if trigger == "auto" and current_state in TERMINAL_STATES:
            log.info(
                "escalator: note %s state=%s is terminal (auto trigger), skipping",
                note_id, current_state,
            )
            return

        settings = get_settings().notes

        # Rule check (auto only). Retries also re-check — the cap may have
        # filled between attempts. Manual trigger bypasses all five rules.
        if trigger == "auto":
            skip_state = self._check_auto_rule(note, settings)
            if skip_state is not None:
                self._record_skip(note_id, skip_state, reason=skip_state)
                return

        # Rule passes (or manual): transition to pending.
        self._transition(
            note_id,
            state="pending",
            trigger=trigger,
            note=note,
        )

        # Build prompt.
        with connect() as conn:
            article_rows = conn.execute(
                "SELECT id FROM articles ORDER BY updated_at DESC LIMIT 500"
            ).fetchall()
        article_ids = [r["id"] for r in article_rows]
        identity = self.load_identity()
        prompt = ESCALATE_PROMPT.format(
            identity=identity,
            article_ids="\n".join(f"- {aid}" for aid in article_ids) or "(none yet)",
            note_body=note["body"],
        )

        # Attempt Claude; on failure, roll through retry state machine.
        try:
            parsed = await self._call_claude(prompt)
        except ClaudeError as err:
            await self._handle_claude_error(
                note_id=note_id,
                trigger=trigger,
                prompt=prompt,
                error=err,
                settings=settings,
            )
            return

        # Claude succeeded.
        self._finish_success(
            note_id=note_id, note=note, trigger=trigger,
            parsed=parsed, model="claude",
        )

    # ───── rule evaluation ─────

    def _check_auto_rule(self, note: dict, settings) -> str | None:
        """Return None if the auto-rule passes, else the target state name
        (``skipped_cap`` / ``skipped_dup`` / ``none``).

        Order matters: classification/confidence/length fail back to ``none``
        (still eligible for a manual escalate), but cap/dedup fail to their
        own terminal-ish states per §9.4.
        """
        classification = note.get("classification") or ""
        if classification not in settings.auto_escalate_classifications:
            return "none"
        try:
            confidence = float(note.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < settings.auto_escalate_min_confidence:
            return "none"
        body = note.get("body") or ""
        if len(body) < settings.auto_escalate_min_length:
            return "none"

        # Daily cap — local midnight. datetime.now().astimezone() gives local tz.
        local_now = datetime.now().astimezone()
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        with connect() as conn:
            count_today = conn.execute(
                """SELECT COUNT(*) AS n FROM note_escalations
                   WHERE triggered_at >= ? AND result = 'stub_created'""",
                (local_midnight.isoformat(),),
            ).fetchone()["n"]
        if count_today >= settings.auto_escalate_cap:
            return "skipped_cap"

        # Dedup — word Jaccard on summary, window = dedup_hours.
        if _is_dedup_violation(note, settings):
            return "skipped_dup"
        return None

    # ───── state transitions / side effects ─────

    def _transition(
        self,
        note_id: int,
        *,
        state: str,
        trigger: str | None = None,
        note: dict | None = None,
        next_attempt_at: datetime | None = None,
        retry_count: int | None = None,
        article_id: str | None = None,
    ) -> None:
        """Update the note row's escalation fields. One SQL statement per call
        to keep the invariant "exactly one write per state transition".
        """
        sets: list[str] = ["escalation_state = ?"]
        params: list[Any] = [state]
        if trigger is not None:
            sets.append("escalation_trigger = ?")
            params.append(trigger)
        if next_attempt_at is not None:
            sets.append("escalation_next_attempt_at = ?")
            params.append(next_attempt_at.isoformat())
        else:
            # When moving out of retrying/pending for a success/skip, clear
            # the scheduled time so the reactivation scan doesn't repick.
            if state in TERMINAL_STATES or state.startswith("skipped") or state == "none":
                sets.append("escalation_next_attempt_at = NULL")
        if retry_count is not None:
            sets.append("escalation_retry_count = ?")
            params.append(retry_count)
        if article_id is not None:
            sets.append("escalation_article_id = ?")
            params.append(article_id)
        params.append(note_id)
        with connect() as conn:
            conn.execute(
                f"UPDATE notes SET {', '.join(sets)} WHERE id = ?",
                params,
            )

    def _record_skip(self, note_id: int, state: str, *, reason: str) -> None:
        """Transition to ``state``, insert a failed ``note_escalations`` row,
        emit a feed row. One-shot composite for skip outcomes.
        """
        self._transition(note_id, state=state)
        now_iso = datetime.now().astimezone().isoformat()
        with connect() as conn:
            conn.execute(
                """INSERT INTO note_escalations
                     (note_id, triggered_at, trigger, result, error, model)
                   VALUES (?, ?, 'auto', 'failed', ?, 'claude')""",
                (note_id, now_iso, reason),
            )
        self.emit_feed(
            verb="skipped",
            obj=str(note_id),
            kind="note",
            payload={"reason": reason, "state": state},
        )

    def _finish_success(
        self,
        *,
        note_id: int,
        note: dict,
        trigger: str,
        parsed: dict,
        model: str,
    ) -> None:
        """Build the stub article, update note state, record escalation row,
        rewrite the note's frontmatter, emit the feed row.

        Called from both the Claude-success path and the Ollama-fallback path.
        """
        title = str(parsed.get("title") or "Untitled note")[:80]
        kind = parsed.get("kind")
        if kind not in ("Concept", "Entity", "Synthesis"):
            kind = "Concept"
        framing = str(parsed.get("framing_paragraph") or "").strip()
        raw_questions = parsed.get("research_questions") or []
        questions = [str(q).strip() for q in raw_questions if str(q).strip()]

        summary = framing[:280] if framing else title
        body_parts: list[str] = []
        if framing:
            body_parts.append(framing)
        if questions:
            body_parts.append("## Research questions")
            body_parts.extend(f"- {qn}" for qn in questions)
        body_md = "\n\n".join(body_parts) or title

        # Derive stub id. Collisions on note_id are impossible, but rerunning
        # against a DB that was rebuilt could collide — handle suffix.
        base_id = f"note-{note_id:06d}-{slugify(title)[:40]}" or f"note-{note_id:06d}"
        stub_id = base_id
        with connect() as conn:
            inserted = q.ensure_note_stub_article(
                conn,
                id=stub_id,
                title=title,
                kind=kind,
                summary=summary,
                body_md=body_md,
                source_note_id=note_id,
            )
            if not inserted:
                alt_found = False
                for n in range(2, 100):
                    alt_id = f"{base_id}-{n}"
                    if q.ensure_note_stub_article(
                        conn,
                        id=alt_id,
                        title=title,
                        kind=kind,
                        summary=summary,
                        body_md=body_md,
                        source_note_id=note_id,
                    ):
                        stub_id = alt_id
                        alt_found = True
                        break
                if not alt_found:
                    raise RuntimeError(
                        f"escalator: exhausted stub id collisions for note {note_id}"
                    )
            # Fetch the stub's slug (ensure_note_stub_article computed it).
            stub_row = conn.execute(
                "SELECT slug FROM articles WHERE id = ?", (stub_id,)
            ).fetchone()
            stub_slug = stub_row["slug"] if stub_row else stub_id

        # Note state transition.
        final_state = "auto_done" if trigger == "auto" else "manual_done"
        self._transition(
            note_id,
            state=final_state,
            trigger=trigger,
            article_id=stub_id,
        )

        # Record the escalation attempt.
        now_iso = datetime.now().astimezone().isoformat()
        with connect() as conn:
            conn.execute(
                """INSERT INTO note_escalations
                     (note_id, triggered_at, trigger, result, stub_article_id, model)
                   VALUES (?, ?, ?, 'stub_created', ?, ?)""",
                (note_id, now_iso, trigger, stub_id, model),
            )

        # Rewrite the note's frontmatter with the new escalation metadata.
        # Best-effort: the DB update + feed row already landed; a frontmatter
        # write failure mustn't roll those back. The file-and-DB can also drift
        # briefly on iCloud anyway — the next scan reconciles.
        try:
            _update_note_frontmatter(
                note=note,
                escalation_state=final_state,
                escalation_article_slug=stub_slug,
            )
        except Exception:
            log.exception(
                "escalator: frontmatter update failed for note %s (non-fatal)",
                note_id,
            )

        verb = "auto-escalated" if trigger == "auto" else "escalated"
        self.emit_feed(
            verb=verb,
            obj=str(note_id),
            kind="note",
            payload={"stub_id": stub_id, "title": title},
        )

    # ───── Claude / Codex / Ollama plumbing ─────

    async def _call_claude(self, prompt: str) -> dict:
        """Run Claude and extract the JSON block. ParseFailure → ClaudeError."""
        result = await claude_bridge.run_claude(prompt)
        text = result.get("text", "") if isinstance(result, dict) else str(result)
        parsed = claude_bridge.extract_json_block(text)
        if parsed is None:
            raise ClaudeError(
                f"could not extract JSON block from claude response: {text[:200]!r}"
            )
        return parsed

    async def _call_codex(self, prompt: str) -> dict:
        """Run Codex (cloud-class fallback) and extract JSON.

        Codex sits between Claude and Ollama in the fallback chain. Same JSON
        extraction tolerance as ``_call_ollama`` — fenced block first, then
        naked-braces parse for models that skip the fence.
        """
        result = await codex_bridge.run_codex(prompt)
        text = result.get("text", "") if isinstance(result, dict) else str(result)
        parsed = claude_bridge.extract_json_block(text)
        if parsed is None:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    parsed = None
        if parsed is None:
            raise RuntimeError(
                f"escalator: could not extract JSON from codex response: {text[:200]!r}"
            )
        return parsed

    async def _call_ollama(self, prompt: str, *, model: str) -> dict:
        """Run Ollama (fallback engine) and extract JSON. Tolerant of models
        that emit the block without code fences.
        """
        result = await ollama_bridge.run_ollama(prompt, model)
        text = result.get("text", "") if isinstance(result, dict) else str(result)
        parsed = claude_bridge.extract_json_block(text)
        if parsed is None:
            # Fall back to naked-braces parse for models that skip the fence.
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    parsed = None
        if parsed is None:
            raise RuntimeError(
                f"escalator: could not extract JSON from ollama response: {text[:200]!r}"
            )
        return parsed

    async def _handle_claude_error(
        self,
        *,
        note_id: int,
        trigger: str,
        prompt: str,
        error: ClaudeError,
        settings,
    ) -> None:
        """Retry with backoff, or fall back to Codex → Ollama on exhaustion.

        State machine (§9.4):
            pending --ClaudeError, budget remains--> retrying
            pending --budget exhausted, Codex ok--> auto_done|manual_done
            pending --budget exhausted, Codex fail, Ollama ok--> auto_done|manual_done
            pending --budget exhausted, Codex+Ollama fail--> failed
        """
        with connect() as conn:
            note_row = q.get_note(conn, note_id)
        if note_row is None:
            return
        new_count = (note_row.get("escalation_retry_count") or 0) + 1
        backoff = settings.claude_retry_backoff_mins or []
        if new_count <= settings.claude_retry_count:
            # Retry later.
            idx = min(new_count - 1, len(backoff) - 1) if backoff else 0
            wait_mins = backoff[idx] if backoff else 30
            next_at = datetime.now().astimezone() + timedelta(minutes=wait_mins)
            self._transition(
                note_id,
                state="retrying",
                trigger=trigger,
                next_attempt_at=next_at,
                retry_count=new_count,
            )
            log.info(
                "escalator: note %s claude err → retrying in %sm (attempt %s/%s): %s",
                note_id, wait_mins, new_count, settings.claude_retry_count, error,
            )
            return

        # Exhausted. Bump retry_count (records that retries are used) and try
        # Codex (cloud-class) before falling all the way down to local Ollama.
        self._transition(
            note_id,
            state="pending",
            trigger=trigger,
            retry_count=new_count,
        )
        log.info(
            "escalator: note %s claude exhausted (count=%s), trying codex",
            note_id, new_count,
        )
        try:
            parsed = await self._call_codex(prompt)
        except Exception as codex_err:
            log.warning(
                "escalator: note %s codex fallback failed (%s); trying ollama",
                note_id, codex_err,
            )
        else:
            with connect() as conn:
                fresh = q.get_note(conn, note_id)
            if fresh is None:
                return
            self._finish_success(
                note_id=note_id, note=fresh, trigger=trigger,
                parsed=parsed, model="codex",
            )
            return

        try:
            parsed = await self._call_ollama(prompt, model=settings.escalator_model)
        except Exception as oll_err:
            log.warning(
                "escalator: note %s ollama fallback also failed: %s", note_id, oll_err
            )
            self._transition(note_id, state="failed", trigger=trigger)
            now_iso = datetime.now().astimezone().isoformat()
            with connect() as conn:
                conn.execute(
                    """INSERT INTO note_escalations
                         (note_id, triggered_at, trigger, result, error, model)
                       VALUES (?, ?, ?, 'failed', ?, 'ollama')""",
                    (note_id, now_iso, trigger, str(oll_err)[:500]),
                )
            self.emit_feed(
                verb="failed",
                obj=str(note_id),
                kind="note",
                payload={"error": str(oll_err)[:200]},
            )
            return

        # Ollama succeeded.
        with connect() as conn:
            fresh = q.get_note(conn, note_id)
        if fresh is None:
            return
        self._finish_success(
            note_id=note_id, note=fresh, trigger=trigger,
            parsed=parsed, model="ollama",
        )


# ─────────────────────────────── module helpers ───────────────────────────────


def _is_dedup_violation(note: dict, settings) -> bool:
    """Return True iff there's a prior successful escalation within
    ``dedup_hours`` whose stub summary word-Jaccard ≥ 0.4 with this note's
    summary.

    v1: substring / word-overlap only. Embedding-based dedup is noted in spec
    §15 as a later upgrade.
    """
    note_summary = (note.get("summary") or "").strip()
    if not note_summary:
        return False
    note_words = _word_set(note_summary)
    if not note_words:
        return False

    since = (
        datetime.now().astimezone()
        - timedelta(hours=int(settings.dedup_hours))
    ).isoformat()
    with connect() as conn:
        rows = conn.execute(
            """SELECT a.summary AS stub_summary, n.summary AS prior_note_summary
               FROM note_escalations ne
               LEFT JOIN articles a ON a.id = ne.stub_article_id
               LEFT JOIN notes n ON n.id = ne.note_id
               WHERE ne.triggered_at >= ?
                 AND ne.result = 'stub_created'
                 AND ne.note_id != ?""",
            (since, note.get("id")),
        ).fetchall()
    for r in rows:
        candidate = (r["stub_summary"] or r["prior_note_summary"] or "").strip()
        if not candidate:
            continue
        other_words = _word_set(candidate)
        if not other_words:
            continue
        inter = len(note_words & other_words)
        union = len(note_words | other_words)
        if union == 0:
            continue
        jaccard = inter / union
        if jaccard >= 0.4:
            return True
    return False


def _word_set(s: str) -> set[str]:
    """Lowercase, strip punctuation, drop short tokens, return as a set."""
    import re
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9\-]+", s.lower())
    return {t for t in tokens if len(t) > 2}


def _update_note_frontmatter(
    *,
    note: dict,
    escalation_state: str,
    escalation_article_slug: str,
) -> None:
    """Rewrite the on-disk note file's YAML frontmatter to reflect escalation.

    Imports ``write_frontmatter`` from notetaker to keep the one writer. Reads
    the current file, preserves body, merges the new escalation fields into
    the existing frontmatter block, writes atomically.
    """
    import yaml

    from mastisk.agents.notetaker import strip_frontmatter, write_frontmatter

    file_path = vault_dir() / note["path"]
    if not file_path.exists():
        return
    content = file_path.read_text(encoding="utf-8")

    existing_fm: dict = {}
    if content.startswith("---\n"):
        parts = content.split("---\n", 2)
        if len(parts) >= 3:
            try:
                loaded = yaml.safe_load(parts[1]) or {}
                if isinstance(loaded, dict):
                    existing_fm = loaded
            except yaml.YAMLError:
                existing_fm = {}
    body_core = strip_frontmatter(content)

    existing_fm["escalation_state"] = escalation_state
    existing_fm["escalation_article_slug"] = escalation_article_slug
    write_frontmatter(file_path, body_core, existing_fm)
