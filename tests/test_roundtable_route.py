"""Roundtable API tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings
    reload_settings()
    from mastisk.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_post_roundtable_with_prompt_creates_row_and_job(client, db):
    r = client.post("/api/roundtables", json={
        "input_type": "prompt",
        "input_ref": "",
        "prompt": "what's interesting about X?",
    })
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["id"] > 0
    assert body["status"] == "pending"
    # DB row
    row = db.execute("SELECT * FROM roundtables WHERE id = ?", (body["id"],)).fetchone()
    assert row["input_type"] == "prompt"
    assert row["prompt"] == "what's interesting about X?"
    # Job enqueued
    jrow = db.execute(
        "SELECT * FROM jobs WHERE agent='roundtable' AND kind='fan_out'"
    ).fetchone()
    assert jrow is not None
    import json as _json
    payload = _json.loads(jrow["payload_json"])
    assert payload["roundtable_id"] == body["id"]


def test_post_roundtable_with_note_validates_exists(client, db):
    db.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at)
           VALUES ('test-note', '_notes/inbox/test-note.md', 'body', 'sha', 'pwa', '2026-04-22T09:00:00')"""
    )
    note_id = db.execute("SELECT id FROM notes WHERE slug='test-note'").fetchone()["id"]
    r = client.post("/api/roundtables", json={
        "input_type": "note",
        "input_ref": str(note_id),
        "prompt": "roundtable on this note",
    })
    assert r.status_code == 202


def test_post_roundtable_with_unknown_note_returns_404(client):
    r = client.post("/api/roundtables", json={
        "input_type": "note",
        "input_ref": "999999",
        "prompt": "p",
    })
    assert r.status_code == 404


def test_post_roundtable_with_invalid_note_ref_returns_422(client):
    r = client.post("/api/roundtables", json={
        "input_type": "note",
        "input_ref": "not-an-int",
        "prompt": "p",
    })
    assert r.status_code == 422


def test_post_roundtable_rejects_empty_prompt(client):
    r = client.post("/api/roundtables", json={
        "input_type": "prompt", "input_ref": "", "prompt": "   ",
    })
    assert r.status_code == 422


def test_get_roundtable_detail_includes_perspectives(client, db):
    # Seed a completed roundtable + 2 perspectives
    cur = db.execute(
        """INSERT INTO roundtables (input_type, input_ref, prompt, status, synthesis, synthesis_model)
           VALUES ('prompt', '', 'test', 'done', 'the synthesis text', 'claude')"""
    )
    rt_id = cur.lastrowid
    db.execute(
        """INSERT INTO roundtable_perspectives (roundtable_id, backend, model, content, latency_ms)
           VALUES (?, 'claude', 'claude-sonnet-4-6', 'claude said this', 500)""",
        (rt_id,),
    )
    db.execute(
        """INSERT INTO roundtable_perspectives (roundtable_id, backend, model, content, latency_ms)
           VALUES (?, 'codex', 'gpt-5-codex', 'codex said this', 800)""",
        (rt_id,),
    )

    r = client.get(f"/api/roundtables/{rt_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == rt_id
    assert body["synthesis"] == "the synthesis text"
    assert len(body["perspectives"]) == 2


def test_get_roundtable_404_on_unknown(client):
    r = client.get("/api/roundtables/999999")
    assert r.status_code == 404


def test_list_roundtables(client, db):
    for i in range(3):
        db.execute(
            """INSERT INTO roundtables (input_type, input_ref, prompt, status)
               VALUES ('prompt', '', ?, 'done')""",
            (f"prompt-{i}",),
        )
    r = client.get("/api/roundtables")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) >= 3


def test_save_as_note_creates_note(client, db, vault_tmp):
    cur = db.execute(
        """INSERT INTO roundtables (input_type, input_ref, prompt, status, synthesis, synthesis_model)
           VALUES ('prompt', '', 'test', 'done', 'synthesis paragraph here', 'claude')"""
    )
    rt_id = cur.lastrowid

    r = client.post(f"/api/roundtables/{rt_id}/save-as-note", json={})
    assert r.status_code == 201
    body = r.json()
    note_id = body["note_id"]
    assert body["reused"] is False

    # DB note exists, linked back
    note = db.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    assert note is not None
    assert "synthesis paragraph here" in note["body"]
    assert f"From roundtable #{rt_id}" in note["body"]
    rt = db.execute("SELECT saved_as_note_id FROM roundtables WHERE id = ?", (rt_id,)).fetchone()
    assert rt["saved_as_note_id"] == note_id

    # Idempotency: second call returns the same note
    r2 = client.post(f"/api/roundtables/{rt_id}/save-as-note", json={})
    assert r2.status_code == 201
    assert r2.json()["note_id"] == note_id
    assert r2.json()["reused"] is True


def test_save_as_note_409_when_no_synthesis(client, db):
    cur = db.execute(
        """INSERT INTO roundtables (input_type, input_ref, prompt, status)
           VALUES ('prompt', '', 'test', 'pending')"""
    )
    rt_id = cur.lastrowid
    r = client.post(f"/api/roundtables/{rt_id}/save-as-note", json={})
    assert r.status_code == 409


def test_save_as_note_slug_collision_keeps_both_files(client, db, vault_tmp, monkeypatch):
    # Two saves landing on the same HHMMSS-slug: the second must get its own
    # file instead of overwriting and renaming the first note's.
    monkeypatch.setattr(
        "mastisk.routes.notes.derive_slug", lambda text, ts: "120000-same-prompt",
    )
    saved = []
    for synthesis in ("first synthesis", "second synthesis"):
        cur = db.execute(
            """INSERT INTO roundtables (input_type, input_ref, prompt, status, synthesis, synthesis_model)
               VALUES ('prompt', '', 'same prompt', 'done', ?, 'claude')""",
            (synthesis,),
        )
        db.commit()
        r = client.post(f"/api/roundtables/{cur.lastrowid}/save-as-note", json={})
        assert r.status_code == 201, r.text
        saved.append((r.json(), synthesis))

    assert [body["slug"] for body, _ in saved] == [
        "120000-same-prompt", "120000-same-prompt-2",
    ]
    for body, synthesis in saved:
        note = db.execute("SELECT path FROM notes WHERE id = ?", (body["note_id"],)).fetchone()
        assert synthesis in (vault_tmp / note["path"]).read_text(encoding="utf-8")
