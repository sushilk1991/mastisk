"""CLI command tests.

Uses typer's CliRunner to invoke commands in-process. Each test asserts on
DB state rather than stdout text — match existing style where practical.
"""
from __future__ import annotations

import json
import tomllib
from datetime import datetime

import pytest
from typer.testing import CliRunner


@pytest.fixture
def cli(db, vault_tmp, data_tmp):
    """Reload settings so the blog CLI picks up the tmp vault. db runs first
    so the schema is applied."""
    from mastisk.settings import reload_settings
    reload_settings()
    from mastisk.cli import app
    return CliRunner(), app


def _seed_note(db) -> None:
    """Seed one classified in-window note so the empty-pool gate passes."""
    now = datetime.now().astimezone().isoformat()
    db.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at,
                              classified_at, classification, summary, tags_json)
           VALUES ('n', '_notes/inbox/n.md', 'b', 'h', 'pwa', ?, ?,
                   'idea', 's', '[]')""",
        (now, now),
    )


def test_blog_command_creates_pending_row_and_enqueues(cli, db):
    _seed_note(db)
    runner, app = cli
    result = runner.invoke(app, ["blog", "test-time compute", "--window", "14"])
    assert result.exit_code == 0, result.output
    row = db.execute(
        "SELECT * FROM blog_posts ORDER BY id DESC LIMIT 1",
    ).fetchone()
    assert row is not None
    assert row["theme"] == "test-time compute"
    assert row["window_days"] == 14
    assert row["status"] == "pending"
    job = db.execute(
        "SELECT * FROM jobs WHERE agent='blog_writer' AND kind='draft'",
    ).fetchone()
    assert job is not None
    import json as _json
    assert _json.loads(job["payload_json"])["blog_post_id"] == row["id"]


def test_blog_command_without_theme_is_ok(cli, db):
    _seed_note(db)
    runner, app = cli
    result = runner.invoke(app, ["blog"])
    assert result.exit_code == 0, result.output
    row = db.execute(
        "SELECT * FROM blog_posts ORDER BY id DESC LIMIT 1",
    ).fetchone()
    # Empty theme is fine — daemon lets the writer pick a thread.
    assert row["theme"] == ""
    assert row["window_days"] == 14


def test_blog_command_rejects_bad_window(cli, db):
    runner, app = cli
    result = runner.invoke(app, ["blog", "t", "--window", "99"])
    assert result.exit_code != 0
    # No row inserted.
    n = db.execute("SELECT COUNT(*) AS c FROM blog_posts").fetchone()["c"]
    assert n == 0


def test_blog_command_rejects_empty_pool(cli, db):
    """Without any in-window sources the CLI should fail fast without
    enqueueing a doomed job — mirrors the HTTP 422 behavior."""
    runner, app = cli
    result = runner.invoke(app, ["blog", "t", "--window", "14"])
    assert result.exit_code != 0
    assert "no sources" in result.output.lower()
    n = db.execute("SELECT COUNT(*) AS c FROM blog_posts").fetchone()["c"]
    assert n == 0
    j = db.execute(
        "SELECT COUNT(*) AS c FROM jobs WHERE agent='blog_writer'"
    ).fetchone()["c"]
    assert j == 0


def test_add_youtube_canonicalizes_and_reuses_existing_job(cli, db):
    runner, app = cli

    first = runner.invoke(
        app,
        ["add-youtube", "https://www.youtube.com/embed/abc123xyz89?autoplay=1"],
    )
    second = runner.invoke(
        app,
        ["add-youtube", "https://youtu.be/abc123xyz89?t=30"],
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "already queued" in second.output.lower()
    rows = db.execute(
        "SELECT payload_json FROM jobs WHERE agent='listener' AND kind='transcribe'"
    ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"]) == {
        "url": "https://www.youtube.com/watch?v=abc123xyz89",
        "media_type": "video",
    }


def test_list_blogs_empty(cli, db):
    runner, app = cli
    result = runner.invoke(app, ["list-blogs"])
    assert result.exit_code == 0
    assert "no drafts yet" in result.output.lower()


def test_list_blogs_shows_recent_entries(cli, db):
    from mastisk.db.queries import create_blog_post, update_blog_post_done

    bp_id = create_blog_post(db, theme="t", window_days=14)
    update_blog_post_done(
        db, bp_id=bp_id, slug="s", path="p", title="My Title",
        tags_json="[]", model="claude", word_count=1234,
        body_preview="pre",
    )
    runner, app = cli
    result = runner.invoke(app, ["list-blogs"])
    assert result.exit_code == 0
    assert "My Title" in result.output
    assert "1234" in result.output
    assert f"#{bp_id}" in result.output


def test_capture_token_generates_refuses_and_rotates(cli, data_tmp):
    runner, app = cli

    result = runner.invoke(app, ["capture-token"])
    assert result.exit_code == 0, result.output
    cfg = data_tmp / "config.toml"
    data = tomllib.loads(cfg.read_text())
    token = data["capture"]["bearer_token"]
    assert token
    assert token in result.output
    assert f"Authorization: Bearer {token}" in result.output

    second = runner.invoke(app, ["capture-token"])
    assert second.exit_code == 1
    assert "already exists" in second.output
    assert tomllib.loads(cfg.read_text())["capture"]["bearer_token"] == token

    rotated = runner.invoke(app, ["capture-token", "--rotate"])
    assert rotated.exit_code == 0, rotated.output
    new_token = tomllib.loads(cfg.read_text())["capture"]["bearer_token"]
    assert new_token
    assert new_token != token
    assert f"Authorization: Bearer {new_token}" in rotated.output


def test_calendar_oauth_loopback_waits_past_speculative_request():
    from mastisk.cli import _wait_for_oauth_redirect

    result: dict[str, str] = {}

    class FakeServer:
        timeout: float | int | None = None

        def __init__(self) -> None:
            self.calls = 0

        def handle_request(self) -> None:
            self.calls += 1
            if self.calls == 2:
                result["code"] = "oauth-code"
                result["state"] = "state-ok"

    server = FakeServer()

    _wait_for_oauth_redirect(server, result, timeout_seconds=180)

    assert server.calls == 2
    assert result["code"] == "oauth-code"
