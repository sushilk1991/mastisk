"""MeetingPrep tests: attendee normalization, resolution, prep flow, dedup."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from mastisk.agents.meeting_prep import MeetingPrep


@pytest.fixture
def prep(db):
    return MeetingPrep()


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def _seed_event(db, *, event_id="ev1", summary="Pilot sync", hours_from_now=2.0,
                attendees=None, status=None, all_day=0, description=None):
    start = datetime.now(UTC) + timedelta(hours=hours_from_now)
    end = start + timedelta(hours=1)
    db.execute(
        """INSERT INTO calendar_events
             (id, calendar_id, summary, start, end, all_day, status, synced_at,
              attendees_json, description)
           VALUES (?, 'primary', ?, ?, ?, ?, ?, datetime('now'), ?, ?)""",
        (event_id, summary, _iso(start), _iso(end), all_day, status,
         json.dumps(attendees) if attendees else None, description),
    )
    return _iso(start)


def _seed_person(db, *, name="Sarah Chen", email="sarah@acme.com"):
    db.execute(
        """INSERT INTO people (slug, name, facts_json, path)
           VALUES (?, ?, ?, ?)""",
        ("sarah-chen", name, json.dumps({"email": email, "role": "CTO at Acme"}),
         "_people/sarah-chen.md"),
    )
    db.execute(
        "INSERT INTO interactions (person_slug, ts, text) VALUES (?, ?, ?)",
        ("sarah-chen", "2026-07-01 10:00", "Agreed pilot scope; pricing open."),
    )


ATTENDEES = [
    {"email": "me@self.com", "displayName": "Me", "self": True, "responseStatus": "accepted"},
    {"email": "sarah@acme.com", "displayName": "Sarah Chen", "self": False, "responseStatus": "accepted"},
]


def test_normalize_attendees_drops_rooms_and_keeps_people():
    from mastisk.google_calendar import _normalize_attendees
    raw = [
        {"email": "sarah@acme.com", "displayName": "Sarah", "self": False},
        {"email": "room-4@resource.calendar.google.com", "displayName": "Room 4"},
        {"email": "me@self.com", "self": True},
        "garbage",
        {"displayName": "no email"},
    ]
    parsed = json.loads(_normalize_attendees(raw))
    assert [a["email"] for a in parsed] == ["sarah@acme.com", "me@self.com"]
    assert _normalize_attendees([]) is None
    assert _normalize_attendees(None) is None


def test_due_events_selects_upcoming_with_other_attendees(prep, db):
    _seed_event(db, event_id="soon", hours_from_now=2, attendees=ATTENDEES)
    _seed_event(db, event_id="far", hours_from_now=20, attendees=ATTENDEES)
    _seed_event(db, event_id="solo", hours_from_now=2,
                attendees=[{"email": "me@self.com", "self": True}])
    _seed_event(db, event_id="cancelled", hours_from_now=2, attendees=ATTENDEES,
                status="cancelled")
    _seed_event(db, event_id="allday", hours_from_now=2, attendees=ATTENDEES, all_day=1)

    due = prep._due_events(lead_hours=6)
    assert [e["id"] for e in due] == ["soon"]


def test_resolve_person_email_first_then_unambiguous_name(prep, db):
    _seed_person(db)
    with_email = prep._resolve_person(db, {"email": "SARAH@acme.com"})
    assert with_email["slug"] == "sarah-chen"
    by_name = prep._resolve_person(db, {"email": "other@x.com", "displayName": "sarah chen"})
    assert by_name["slug"] == "sarah-chen"
    # Ambiguous name → no match.
    db.execute(
        "INSERT INTO people (slug, name, facts_json, path) VALUES ('sarah-chen-2', 'Sarah Chen', '{}', 'p2.md')",
    )
    assert prep._resolve_person(db, {"email": "x@y.com", "displayName": "Sarah Chen"}) is None


def test_prep_one_writes_note_row_and_feed(prep, db, vault_tmp):
    _seed_person(db)
    start = _seed_event(db, attendees=ATTENDEES, description="Decide pricing tier")

    reply = {"text": "- Decide pricing: scope agreed 2026-07-01, pricing still open."}
    with patch(
        "mastisk.agents.meeting_prep.intelligence.run_intelligence",
        new_callable=AsyncMock, return_value=(reply, "claude"),
    ) as mock_int:
        asyncio.run(prep.run_once())

    assert mock_int.call_count == 1
    prompt = mock_int.call_args[0][0]
    # Deterministic context made it into the prompt: facts + interactions + agenda.
    assert "CTO at Acme" in prompt
    assert "Agreed pilot scope" in prompt
    assert "Decide pricing tier" in prompt

    row = db.execute("SELECT * FROM meeting_preps WHERE event_id='ev1'").fetchone()
    assert row["start"] == start
    assert "pricing still open" in row["brief"]

    note = vault_tmp / row["note_path"]
    assert note.exists()
    content = note.read_text()
    assert "# Prep: Pilot sync" in content
    assert "[[Sarah Chen]]" in content
    assert "What matters" in content

    feed = db.execute("SELECT * FROM feed WHERE agent='meeting_prep' AND verb='prepped'").fetchall()
    assert len(feed) == 1

    # Second tick: dedup — no new LLM call, no second row.
    with patch(
        "mastisk.agents.meeting_prep.intelligence.run_intelligence",
        new_callable=AsyncMock,
    ) as mock_again:
        asyncio.run(prep.run_once())
    assert mock_again.call_count == 0


def test_prep_survives_llm_outage_with_deterministic_note(prep, db, vault_tmp):
    from mastisk.bridges.intelligence import IntelligenceUnavailable
    _seed_person(db)
    _seed_event(db, attendees=ATTENDEES)

    with patch(
        "mastisk.agents.meeting_prep.intelligence.run_intelligence",
        new_callable=AsyncMock, side_effect=IntelligenceUnavailable("all tiers down"),
    ):
        asyncio.run(prep.run_once())

    row = db.execute("SELECT * FROM meeting_preps WHERE event_id='ev1'").fetchone()
    assert row is not None
    assert row["brief"] is None
    note = vault_tmp / row["note_path"]
    assert note.exists()
    assert "What matters" not in note.read_text()  # no fabricated brief section


def test_events_for_day_returns_attendees_and_prep(db):
    from mastisk import google_calendar as gc
    # events_for_day is gated on a connected calendar: fake the token file
    # (data_tmp is already active via the db fixture) and the state row.
    gc.token_file_path().parent.mkdir(parents=True, exist_ok=True)
    gc.token_file_path().write_text('{"access_token": "t", "refresh_token": "r"}')
    _seed_event(db, attendees=ATTENDEES)
    db.execute(
        "INSERT INTO meeting_preps (event_id, start, note_path, brief) "
        "SELECT id, start, 'meetings/prep/x.md', 'focus on pricing' FROM calendar_events WHERE id='ev1'",
    )
    db.execute(
        """INSERT INTO calendar_state (id, last_synced_at, status)
           VALUES (1, datetime('now'), 'connected')
           ON CONFLICT(id) DO UPDATE SET last_synced_at=excluded.last_synced_at, status=excluded.status""",
    )
    day = datetime.now(UTC).astimezone().date()
    events = gc.events_for_day(day)
    ev = next((e for e in events if e["id"] == "ev1"), None)
    if ev is None:
        # Event may start after local midnight; widen to tomorrow.
        events = gc.events_for_day(day + timedelta(days=1))
        ev = next(e for e in events if e["id"] == "ev1")
    assert [a["email"] for a in ev["attendees"]] == ["me@self.com", "sarah@acme.com"]
    assert ev["prep"] == {"brief": "focus on pricing", "note_path": "meetings/prep/x.md"}
