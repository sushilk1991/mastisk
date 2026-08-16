"""CLI command tests.

Uses typer's CliRunner to invoke commands in-process. Each test asserts on
DB state rather than stdout text — match existing style where practical.
"""
from __future__ import annotations

import json
import tomllib
from datetime import datetime

import httpx
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


def test_ask_command_uses_intelligent_api_and_emits_json(cli, monkeypatch):
    runner, app = cli
    observed: dict = {}
    monkeypatch.delenv("MASTISK_URL", raising=False)
    monkeypatch.delenv("MASTISK_PORT", raising=False)

    class FakeClient:
        def __init__(self, **kwargs):
            observed["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, path: str, **kwargs):
            observed["path"] = path
            observed["request"] = kwargs
            return httpx.Response(
                200,
                request=httpx.Request("POST", f"http://mastisk.test{path}"),
                json={
                    "answer": "The durable rule still applies. [S1]",
                    "provider": "test",
                    "mode": "wiki",
                    "research_status": "not_requested",
                    "sources": [{"id": "rule-1", "title": "Durable rule"}],
                },
            )

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = runner.invoke(
        app,
        ["ask", "What have I learned about retry safety?", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert observed["client"]["base_url"] == "http://127.0.0.1:5555"
    assert observed["path"] == "/api/ask"
    assert observed["request"]["json"] == {
        "question": "What have I learned about retry safety?",
        "intelligent": True,
    }
    assert json.loads(result.output)["answer"] == "The durable rule still applies. [S1]"


def test_ask_command_reads_complete_task_from_stdin(cli, monkeypatch):
    runner, app = cli
    observed: dict = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, path: str, **kwargs):
            observed["path"] = path
            observed["request"] = kwargs
            return httpx.Response(
                200,
                request=httpx.Request("POST", f"http://mastisk.test{path}"),
                json={"answer": "Use the verified retry receipt.", "sources": []},
            )

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = runner.invoke(
        app,
        ["ask", "-", "--json"],
        input="Review the current retry design against prior incidents.\n",
    )

    assert result.exit_code == 0, result.output
    assert observed["request"]["json"] == {
        "question": "Review the current retry design against prior incidents.",
        "intelligent": True,
    }


def test_ask_command_reports_daemon_unavailable_without_local_db_fallback(
    cli, monkeypatch,
):
    runner, app = cli

    class OfflineClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, path: str, **_kwargs):
            request = httpx.Request("POST", f"http://127.0.0.1:5555{path}")
            raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr("httpx.Client", OfflineClient)

    result = runner.invoke(app, ["ask", "What matters?"])

    assert result.exit_code == 1
    assert "daemon is unavailable" in result.output.lower()
    assert "127.0.0.1:5555" in result.output


def test_ingest_command_sends_text_through_notes_api(cli, monkeypatch):
    runner, app = cli
    observed: dict = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, path: str, **kwargs):
            observed["path"] = path
            observed["request"] = kwargs
            return httpx.Response(
                201,
                request=httpx.Request("POST", f"http://mastisk.test{path}"),
                json={"id": 42, "slug": "durable-learning", "created_at": "2026-08-09"},
            )

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = runner.invoke(
        app,
        ["ingest", "Retry receipts must be checked end to end.", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert observed["path"] == "/api/notes"
    assert observed["request"]["json"] == {
        "text": "Retry receipts must be checked end to end.",
        "source": "cli",
    }
    assert json.loads(result.output)["id"] == 42


def test_ingest_command_uploads_existing_file_to_document_pipeline(
    cli, monkeypatch, tmp_path,
):
    runner, app = cli
    document = tmp_path / "architecture.pdf"
    document.write_bytes(b"%PDF-intelligent-memory")
    observed: dict = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, path: str, **kwargs):
            observed["path"] = path
            upload = kwargs["files"]["file"]
            observed["filename"] = upload[0]
            observed["body"] = upload[1].read()
            return httpx.Response(
                202,
                request=httpx.Request("POST", f"http://mastisk.test{path}"),
                json={"job_id": 77, "status": "queued", "queued": True},
            )

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = runner.invoke(app, ["ingest", str(document), "--json"])

    assert result.exit_code == 0, result.output
    assert observed == {
        "path": "/api/ingest/document",
        "filename": "architecture.pdf",
        "body": b"%PDF-intelligent-memory",
    }
    assert json.loads(result.output)["job_id"] == 77


def test_ingest_command_queues_exact_url_through_listener_pipeline(cli, monkeypatch):
    runner, app = cli
    observed: dict = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, path: str, **kwargs):
            observed["path"] = path
            observed["request"] = kwargs
            return httpx.Response(
                200,
                request=httpx.Request("POST", f"http://mastisk.test{path}"),
                json={"job_id": 91, "status": "queued", "kind": "transcribe"},
            )

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = runner.invoke(
        app,
        ["ingest", "https://example.com/current-agent-guidance", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert observed["path"] == "/api/listen"
    assert observed["request"]["json"] == {
        "url": "https://example.com/current-agent-guidance",
        "exact_url": True,
    }
    assert json.loads(result.output)["job_id"] == 91


def test_ask_command_human_output_shows_retrieved_sources_when_none_were_cited(
    cli, monkeypatch,
):
    runner, app = cli

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, path: str, **_kwargs):
            return httpx.Response(
                200,
                request=httpx.Request("POST", f"http://mastisk.test{path}"),
                json={
                    "answer": "The provider omitted citation markers.",
                    "sources": [],
                    "retrieved_sources": [
                        {"id": "memory-7", "title": "Retry incident"},
                    ],
                },
            )

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = runner.invoke(app, ["ask", "What matters?"])

    assert result.exit_code == 0, result.output
    assert "Retrieved sources (not explicitly cited):" in result.output
    assert "memory-7: Retry incident" in result.output


def test_ingest_command_human_output_preserves_job_receipt(cli, monkeypatch):
    runner, app = cli

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, path: str, **_kwargs):
            return httpx.Response(
                202,
                request=httpx.Request("POST", f"http://mastisk.test{path}"),
                json={"job_id": 91, "status": "queued", "duplicate": False},
            )

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = runner.invoke(
        app,
        ["ingest", "https://example.com/current-agent-guidance"],
    )

    assert result.exit_code == 0, result.output
    assert "job 91" in result.output
    assert "queued" in result.output


def test_ingest_command_reads_stdin(cli, monkeypatch):
    runner, app = cli
    observed: dict = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, path: str, **kwargs):
            observed["path"] = path
            observed["request"] = kwargs
            return httpx.Response(
                201,
                request=httpx.Request("POST", f"http://mastisk.test{path}"),
                json={"id": 52, "slug": "stdin-learning"},
            )

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = runner.invoke(
        app,
        ["ingest", "-", "--json"],
        input="The API changed in version 4; recheck before reuse.\n",
    )

    assert result.exit_code == 0, result.output
    assert observed["path"] == "/api/notes"
    assert observed["request"]["json"]["text"] == (
        "The API changed in version 4; recheck before reuse."
    )


def test_ingest_command_does_not_treat_long_learning_as_a_file_path(
    cli, monkeypatch,
):
    runner, app = cli
    learning = "A" * 600
    observed: dict = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, path: str, **kwargs):
            observed["path"] = path
            observed["request"] = kwargs
            return httpx.Response(
                201,
                request=httpx.Request("POST", f"http://mastisk.test{path}"),
                json={"id": 53, "slug": "long-learning"},
            )

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = runner.invoke(app, ["ingest", learning, "--json"])

    assert result.exit_code == 0, result.output
    assert observed["path"] == "/api/notes"
    assert observed["request"]["json"]["text"] == learning
