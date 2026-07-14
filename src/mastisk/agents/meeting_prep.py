"""MeetingPrep — a "what matters" brief before each meeting, from your own CRM.

Rowboat-style: retrieval is deterministic and entity-keyed — attendees are
resolved against People by ``facts.email`` (exact, casefolded) then by name;
their facts, recent interactions, and open follow-ups become the context. The
LLM only writes the 3-5 bullet brief, and only from that context ("Use ONLY
the context provided"). The prep note lands in ``vault/meetings/prep/`` and
the Today view shows the brief on the event card.

Timer-driven: 15-min tick, prepping events that start within
``calendar.prep_lead_hours`` (default 6h). Dedup per (event, start) in the
``meeting_preps`` table, so a rescheduled meeting gets fresh prep. The brief
is best-effort — if no LLM tier is reachable, the deterministic note is still
written.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from slugify import slugify

from mastisk.agents.base import Agent
from mastisk.agents.registry import resolve_prompt
from mastisk.bridges import intelligence
from mastisk.db.queries import connect
from mastisk.paths import vault_dir
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.meeting_prep")

BRIEF_PROMPT = """You write a short, concrete "what matters for this meeting" brief.

Rules:
- Use ONLY the context provided below. Never invent facts, names, or commitments.
- 3-5 bullet points, one line each. No preamble, no headings, no sign-off.
- Lead with what the user should focus on or decide. Reference open follow-ups
  and known facts by name where the context supplies them.
- If the context is thin, say so in one line rather than padding.

{context}
"""


class MeetingPrep(Agent):
    """Pre-meeting brief generator. See module docstring."""

    name: ClassVar[str] = "meeting_prep"
    tick_seconds: ClassVar[int] = 900  # 15 min

    # Timer-driven, not job-driven (same pattern as TopicSuggester/Gardener).
    async def _handle(self, job: dict) -> None:  # pragma: no cover - never invoked
        raise NotImplementedError("meeting_prep is timer-driven, not job-driven")

    async def run_once(self) -> None:
        if self.disabled_tick():
            return
        cfg = get_settings().calendar
        if not cfg.prep_enabled:
            return
        for event in self._due_events(lead_hours=cfg.prep_lead_hours):
            try:
                await self._prep_one(event)
            except Exception:
                log.exception("meeting_prep: prepping %r failed", event.get("summary"))

    # ───── selection (pure SQL + code) ─────

    def _due_events(self, *, lead_hours: int) -> list[dict]:
        """Timed, non-cancelled events starting within the lead window that
        have at least one non-self attendee and no prep row yet."""
        # calendar_events.start is stored as UTC isoformat without
        # microseconds (_stored_datetime); match the shape for the string
        # comparison below.
        now = datetime.now(UTC).replace(microsecond=0)
        horizon = now + timedelta(hours=lead_hours)
        with connect() as conn:
            rows = conn.execute(
                """SELECT ce.id, ce.summary, ce.start, ce.end, ce.location,
                          ce.attendees_json, ce.description
                   FROM calendar_events ce
                   LEFT JOIN meeting_preps mp
                     ON mp.event_id = ce.id AND mp.start = ce.start
                   WHERE mp.event_id IS NULL
                     AND ce.all_day = 0
                     AND COALESCE(ce.status, '') != 'cancelled'
                     AND ce.attendees_json IS NOT NULL
                     AND ce.start >= ? AND ce.start <= ?
                   ORDER BY ce.start ASC LIMIT 10""",
                (now.isoformat(), horizon.isoformat()),
            ).fetchall()
        due = []
        for r in rows:
            event = dict(r)
            try:
                attendees = json.loads(event["attendees_json"] or "[]")
            except json.JSONDecodeError:
                attendees = []
            others = [a for a in attendees if not a.get("self")]
            if not others:
                continue
            event["attendees"] = attendees
            event["others"] = others
            due.append(event)
        return due

    # ───── deterministic context assembly ─────

    @staticmethod
    def _resolve_person(conn, attendee: dict) -> dict | None:
        """Email-exact against facts.email first, then unambiguous casefolded
        name match. Never semantic — a wrong match poisons the brief."""
        email = (attendee.get("email") or "").strip().casefold()
        if email:
            row = conn.execute(
                """SELECT slug, name, facts_json, follow_up_at FROM people
                   WHERE deleted_at IS NULL
                     AND LOWER(COALESCE(json_extract(facts_json, '$.email'), '')) = ?""",
                (email,),
            ).fetchone()
            if row:
                return dict(row)
        display = (attendee.get("displayName") or "").strip()
        if display:
            matches = conn.execute(
                """SELECT slug, name, facts_json, follow_up_at FROM people
                   WHERE deleted_at IS NULL AND LOWER(name) = ?""",
                (display.casefold(),),
            ).fetchall()
            if len(matches) == 1:
                return dict(matches[0])
        return None

    def _assemble_context(self, event: dict) -> tuple[str, list[dict]]:
        """Returns (context markdown, resolved roster). Pure reads."""
        lines: list[str] = [f"Meeting: {event['summary']}"]
        lines.append(f"When: {event['start']} – {event['end']}")
        if event.get("location"):
            lines.append(f"Where: {event['location']}")
        if event.get("description"):
            lines.append(f"Agenda:\n{event['description'][:1500]}")

        roster: list[dict] = []
        with connect() as conn:
            for attendee in event["others"]:
                person = self._resolve_person(conn, attendee)
                label = attendee.get("displayName") or attendee.get("email")
                entry: dict[str, Any] = {"label": label, "attendee": attendee, "person": person}
                roster.append(entry)
                if not person:
                    lines.append(f"\nAttendee: {label} (no People note)")
                    continue
                lines.append(f"\nAttendee: {person['name']} (People/{person['slug']})")
                try:
                    facts = json.loads(person.get("facts_json") or "{}")
                except json.JSONDecodeError:
                    facts = {}
                for k, v in list(facts.items())[:8]:
                    lines.append(f"- {k}: {v}")
                if person.get("follow_up_at"):
                    lines.append(f"- open follow-up due {person['follow_up_at']}")
                interactions = conn.execute(
                    """SELECT ts, text FROM interactions WHERE person_slug = ?
                       ORDER BY ts DESC LIMIT 5""",
                    (person["slug"],),
                ).fetchall()
                if interactions:
                    lines.append("- recent interactions:")
                    lines += [f"  - {r['ts']} {r['text'][:200]}" for r in interactions]
        return "\n".join(lines), roster

    # ───── prep generation ─────

    async def _prep_one(self, event: dict) -> None:
        context, roster = self._assemble_context(event)

        brief = ""
        try:
            prompt = resolve_prompt("meeting_prep", "brief", BRIEF_PROMPT).format(
                context=context,
            )
            resp, _provider = await intelligence.run_intelligence(prompt, timeout_s=120)
            brief = (resp.get("text") or "").strip()
        except Exception as e:
            log.warning("meeting_prep: brief generation failed for %r: %s",
                        event.get("summary"), e)

        note_path = self._write_note(event, roster, brief)
        with connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO meeting_preps (event_id, start, note_path, brief)
                   VALUES (?, ?, ?, ?)""",
                (event["id"], event["start"], note_path, brief or None),
            )
        self.emit_feed(
            verb="prepped", obj=(event["summary"] or "meeting")[:80], kind="meeting",
            touched=1,
            payload={
                "event_id": event["id"], "start": event["start"],
                "known_attendees": sum(1 for r in roster if r["person"]),
                "attendees": len(roster),
            },
        )

    def _write_note(self, event: dict, roster: list[dict], brief: str) -> str:
        tz = ZoneInfo(get_settings().capture.default_timezone)
        day = (event.get("start") or "")[:10] or datetime.now(tz).date().isoformat()
        slug = slugify(event.get("summary") or "meeting")[:60] or "meeting"
        rel = f"meetings/prep/{slug}-{day}.md"
        path = vault_dir() / rel

        lines = [
            "---",
            "source: meeting-prep",
            f'title: "Prep: {(event.get("summary") or "Meeting").replace(chr(34), chr(39))}"',
            f"event_id: {event['id']}",
            f"start: {event['start']}",
            f"generated_at: {datetime.now(tz).isoformat()}",
            "---",
            "",
            f"# Prep: {event.get('summary') or 'Meeting'}",
            "",
        ]
        if brief:
            lines += ["## What matters", "", brief, ""]
        if event.get("description"):
            lines += ["## Agenda", "", event["description"][:1500], ""]
        lines += ["## Who's coming", ""]
        for r in roster:
            if r["person"]:
                lines.append(f"- [[{r['person']['name']}]] ({r['person']['slug']})")
            else:
                lines.append(f"- {r['label']} _(no People note yet)_")
        content = "\n".join(lines).rstrip() + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return rel
