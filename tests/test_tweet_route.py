from __future__ import annotations

import json
from datetime import datetime

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


def test_post_tweet_thread_creates_row_and_job(client, db):
    r = client.post(
        "/api/tweet-threads",
        json={
            "theme": "agent browsers",
            "url": "https://example.com/post",
            "window_days": 7,
            "include_web": True,
            "use_browser_context": True,
        },
    )
    assert r.status_code == 202, r.text
    body = r.json()
    row = db.execute(
        "SELECT * FROM tweet_threads WHERE id=?", (body["id"],),
    ).fetchone()
    assert row["theme"] == "agent browsers"
    assert row["url"] == "https://example.com/post"
    assert row["window_days"] == 7
    assert row["include_web"] == 1
    assert row["use_browser_context"] == 1
    job = db.execute(
        "SELECT * FROM jobs WHERE agent='tweet_writer' AND kind='draft'",
    ).fetchone()
    assert json.loads(job["payload_json"])["tweet_thread_id"] == body["id"]


def test_post_tweet_thread_rejects_invalid_window(client):
    r = client.post("/api/tweet-threads", json={"window_days": 90})
    assert r.status_code == 422


def test_get_tweet_thread_shapes_json_fields(client, db):
    from mastisk.db.queries import create_tweet_thread, update_tweet_thread_done

    thread_id = create_tweet_thread(
        db,
        theme="t",
        url=None,
        window_days=7,
        include_web=True,
        use_browser_context=False,
    )
    update_tweet_thread_done(
        db,
        thread_id=thread_id,
        title="T",
        angle="A",
        model="claude",
        thread_json=json.dumps(["one", "two"]),
        sources_json=json.dumps([{"kind": "web", "title": "Example"}]),
        warnings_json=json.dumps(["thin evidence"]),
    )
    r = client.get(f"/api/tweet-threads/{thread_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "T"
    assert body["thread"] == ["one", "two"]
    assert body["sources"][0]["title"] == "Example"
    assert body["warnings"] == ["thin evidence"]


def test_list_tweet_threads_recent_first(client, db):
    from mastisk.db.queries import create_tweet_thread

    ids = [
        create_tweet_thread(
            db,
            theme=f"t{i}",
            url=None,
            window_days=7,
            include_web=True,
            use_browser_context=False,
        )
        for i in range(3)
    ]
    db.execute(
        "UPDATE tweet_threads SET created_at=? WHERE id=?",
        (datetime.now().astimezone().isoformat(), ids[-1]),
    )
    r = client.get("/api/tweet-threads")
    assert r.status_code == 200
    returned = [row["id"] for row in r.json()]
    assert returned == list(reversed(ids))
