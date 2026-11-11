"""Query helpers for the roundtable tables. See src/mastisk/db/queries.py
roundtable section + spec §§5, 7."""
from __future__ import annotations


def test_create_roundtable_returns_id_and_defaults(db):
    from mastisk.db.queries import create_roundtable, get_roundtable

    rt_id = create_roundtable(
        db, input_type="prompt", input_ref="", prompt="hello world",
    )
    assert isinstance(rt_id, int) and rt_id > 0

    row = get_roundtable(db, rt_id)
    assert row is not None
    assert row["input_type"] == "prompt"
    assert row["input_ref"] == ""
    assert row["prompt"] == "hello world"
    assert row["status"] == "pending"
    assert row["synthesis"] is None
    assert row["finished_at"] is None


def test_get_roundtable_missing_returns_none(db):
    from mastisk.db.queries import get_roundtable

    assert get_roundtable(db, 99999) is None


def test_list_roundtables_newest_first(db):
    from mastisk.db.queries import create_roundtable, list_roundtables

    ids = [
        create_roundtable(
            db, input_type="prompt", input_ref="", prompt=f"q{i}"
        )
        for i in range(3)
    ]
    rows = list_roundtables(db, limit=10)
    assert [r["id"] for r in rows] == list(reversed(ids))


def test_list_roundtables_before_id_pagination(db):
    from mastisk.db.queries import create_roundtable, list_roundtables

    ids = [
        create_roundtable(
            db, input_type="prompt", input_ref="", prompt=f"q{i}"
        )
        for i in range(5)
    ]
    # Ask for pages before the middle id — should return ids smaller than it.
    rows = list_roundtables(db, limit=10, before_id=ids[2])
    returned = [r["id"] for r in rows]
    assert ids[2] not in returned
    for rid in returned:
        assert rid < ids[2]


def test_insert_roundtable_perspective_roundtrip(db):
    from mastisk.db.queries import (
        create_roundtable,
        insert_roundtable_perspective,
        list_roundtable_perspectives,
    )

    rt_id = create_roundtable(
        db, input_type="prompt", input_ref="", prompt="q",
    )
    # Success row.
    insert_roundtable_perspective(
        db, roundtable_id=rt_id, backend="claude",
        model="claude-sonnet-4-6", content="claude's take",
        error=None, latency_ms=1234,
        started_at="2026-04-22T10:00:00+00:00",
        finished_at="2026-04-22T10:00:01+00:00",
    )
    # Error row.
    insert_roundtable_perspective(
        db, roundtable_id=rt_id, backend="codex",
        model="gpt-5-codex", content=None,
        error="CodexError: boom", latency_ms=500,
        started_at=None, finished_at=None,
    )

    rows = list_roundtable_perspectives(db, rt_id)
    # Ordered by backend ASC: "claude" < "codex".
    assert [r["backend"] for r in rows] == ["claude", "codex"]
    assert rows[0]["content"] == "claude's take"
    assert rows[0]["error"] is None
    assert rows[0]["latency_ms"] == 1234
    assert rows[1]["content"] is None
    assert rows[1]["error"] == "CodexError: boom"


def test_update_roundtable_status_running(db):
    """Compact form — just flips status, doesn't touch finished_at/synthesis."""
    from mastisk.db.queries import create_roundtable, update_roundtable_status

    rt_id = create_roundtable(
        db, input_type="prompt", input_ref="", prompt="q",
    )
    update_roundtable_status(db, rt_id=rt_id, status="running")
    row = db.execute(
        "SELECT status, synthesis, finished_at FROM roundtables WHERE id=?",
        (rt_id,),
    ).fetchone()
    assert row["status"] == "running"
    assert row["synthesis"] is None
    assert row["finished_at"] is None


def test_update_roundtable_status_finished_done(db):
    """finished=True form stamps finished_at + writes synthesis fields."""
    from mastisk.db.queries import create_roundtable, update_roundtable_status

    rt_id = create_roundtable(
        db, input_type="prompt", input_ref="", prompt="q",
    )
    update_roundtable_status(
        db, rt_id=rt_id, status="done",
        synthesis="the synthesis text", synthesis_model="claude",
        finished=True,
    )
    row = db.execute(
        "SELECT status, synthesis, synthesis_model, finished_at FROM roundtables WHERE id=?",
        (rt_id,),
    ).fetchone()
    assert row["status"] == "done"
    assert row["synthesis"] == "the synthesis text"
    assert row["synthesis_model"] == "claude"
    assert row["finished_at"] is not None


def test_update_roundtable_status_failed_writes_error(db):
    from mastisk.db.queries import create_roundtable, update_roundtable_status

    rt_id = create_roundtable(
        db, input_type="prompt", input_ref="", prompt="q",
    )
    update_roundtable_status(
        db, rt_id=rt_id, status="failed",
        error="all backends failed", finished=True,
    )
    row = db.execute(
        "SELECT status, error, finished_at FROM roundtables WHERE id=?",
        (rt_id,),
    ).fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "all backends failed"
    assert row["finished_at"] is not None
