"""Podcasts route tests — list + detail + transcript_anchor on note capture."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(db, vault_tmp):
    from mastisk.app import create_app
    app = create_app()
    return TestClient(app)


def _seed_source_and_article(
    db,
    *,
    article_id: str = "ep-foo",
    source_id: str = "src-foo",
    kind: str = "podcast",
    raw_text: str = "intro\n## Transcript\nHello world. This is the show.",
    duration_sec: int | None = 1800,
    audio_url: str = "https://example.com/episode.mp3",
    feed_url: str | None = "https://example.com/rss",
    raw_path: Path | None = None,
) -> None:
    """Insert a podcast/youtube source + an article linked to it. The raw_path
    file is written so get_podcast_view's transcript-extraction path can run."""
    if raw_path is None:
        raw_path = Path("/tmp") / f"{source_id}.txt"
    raw_path.write_text(raw_text, encoding="utf-8")

    db.execute(
        """INSERT INTO sources (id, kind, url, title, raw_path, author,
                                duration_sec, feed_url, hero_image_url)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_id, kind, audio_url, "Episode 42",
         str(raw_path), "Show Host", duration_sec, feed_url, None),
    )
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, aka_json, summary,
                                 body_md, confidence, reading_minutes, updated_by)
           VALUES (?, 'Source', 'Episode 42', ?, '[]', 'an episode',
                   'body', 0.5, 5, 'Compiler')""",
        (article_id, article_id),
    )
    db.execute(
        "INSERT INTO article_sources (article_id, source_id) VALUES (?, ?)",
        (article_id, source_id),
    )


def test_list_returns_podcast_articles(client, db):
    _seed_source_and_article(db, article_id="a1", source_id="s1", kind="podcast")
    _seed_source_and_article(db, article_id="a2", source_id="s2", kind="youtube",
                             audio_url="https://youtube.com/watch?v=foo")
    _seed_source_and_article(db, article_id="a3", source_id="s3", kind="video",
                             audio_url="https://vimeo.com/123")
    # An article with no podcast source — should NOT appear in the list.
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, aka_json, summary,
                                 body_md, confidence, reading_minutes, updated_by)
           VALUES ('plain', 'Concept', 'Plain article', 'plain', '[]',
                   '', '', 0.5, 1, 'Compiler')""",
    )

    r = client.get("/api/podcasts")
    assert r.status_code == 200
    items = r.json()["items"]
    ids = [i["article_id"] for i in items]
    assert set(ids) == {"a1", "a2", "a3"}
    assert "plain" not in ids


def test_detail_returns_joined_view_with_transcript(client, db, tmp_path):
    raw = tmp_path / "ep.txt"
    _seed_source_and_article(
        db, article_id="ep-x", source_id="src-x", kind="podcast", raw_path=raw,
        raw_text="Episode preamble.\n## Transcript\nVerbatim transcript here.",
    )

    r = client.get("/api/podcasts/ep-x")
    assert r.status_code == 200
    body = r.json()
    assert body["article"]["id"] == "ep-x"
    assert body["source"]["kind"] == "podcast"
    assert body["source"]["duration_sec"] == 1800
    # Transcript-extraction lifts everything after the "## Transcript\n" sentinel.
    assert body["transcript_text"] == "Verbatim transcript here."
    assert body["segments"] == []
    assert body["anchored_notes"] == []


def test_detail_404s_for_non_podcast_article(client, db):
    """Articles without an attached podcast/youtube source should 404 at this
    endpoint so the frontend can fall back to the generic ArticleView."""
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, aka_json, summary,
                                 body_md, confidence, reading_minutes, updated_by)
           VALUES ('plain', 'Concept', 'Plain article', 'plain', '[]',
                   '', '', 0.5, 1, 'Compiler')""",
    )
    r = client.get("/api/podcasts/plain")
    assert r.status_code == 404


def test_detail_returns_segments_when_present(client, db, tmp_path):
    raw = tmp_path / "ep.txt"
    _seed_source_and_article(
        db, article_id="ep-seg", source_id="src-seg", kind="podcast", raw_path=raw,
    )
    db.executemany(
        """INSERT INTO source_transcript_segments (source_id, idx, start_sec, end_sec, text)
           VALUES (?, ?, ?, ?, ?)""",
        [
            ("src-seg", 0, 0.0, 4.5, "Welcome to the show."),
            ("src-seg", 1, 4.5, 9.2, "Today we're talking about agents."),
        ],
    )

    r = client.get("/api/podcasts/ep-seg")
    assert r.status_code == 200
    segs = r.json()["segments"]
    assert len(segs) == 2
    assert segs[0] == {"idx": 0, "start_sec": 0.0, "end_sec": 4.5, "text": "Welcome to the show."}


def test_detail_returns_anchored_notes(client, db, tmp_path):
    """Notes whose transcript_anchor_json points at this source's id should
    surface in the detail payload, parsed back into a TranscriptAnchor dict."""
    raw = tmp_path / "ep.txt"
    _seed_source_and_article(
        db, article_id="ep-notes", source_id="src-notes", kind="podcast", raw_path=raw,
    )
    db.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at,
                              classification, summary, confidence, transcript_anchor_json)
           VALUES ('n1', '_notes/n1.md', 'an idea', 'sha-1', 'pwa',
                   '2026-05-04T12:00:00+00:00', 'idea', 'sum', 0.7, ?)""",
        (json.dumps({"source_id": "src-notes", "segment_idx": 7, "start_sec": 42.5}),),
    )
    # Another note pointing at a different source — must NOT show up here.
    db.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at,
                              transcript_anchor_json)
           VALUES ('n2', '_notes/n2.md', 'unrelated', 'sha-2', 'pwa',
                   '2026-05-04T12:00:00+00:00', ?)""",
        (json.dumps({"source_id": "other", "segment_idx": 0, "start_sec": 0}),),
    )

    r = client.get("/api/podcasts/ep-notes")
    assert r.status_code == 200
    notes = r.json()["anchored_notes"]
    assert len(notes) == 1
    assert notes[0]["body"] == "an idea"
    assert notes[0]["transcript_anchor"] == {
        "source_id": "src-notes", "segment_idx": 7, "start_sec": 42.5,
    }


def test_capture_note_with_transcript_anchor_persists_anchor(client, db, tmp_path):
    """The notes capture route should accept transcript_anchor and round-trip
    it through to notes.transcript_anchor_json so the PodcastView can render
    the note inline beneath its segment on next load."""
    raw = tmp_path / "ep.txt"
    _seed_source_and_article(
        db, article_id="ep-cap", source_id="src-cap", kind="podcast", raw_path=raw,
    )

    payload = {
        "text": "this was the key insight",
        "source": "pwa",
        "context": {"article_id": "ep-cap", "section_heading": "Transcript @ 1:23"},
        "transcript_anchor": {
            "source_id": "src-cap", "segment_idx": 12, "start_sec": 83.4,
        },
    }
    r = client.post("/api/notes", json=payload)
    assert r.status_code == 201
    note_id = r.json()["id"]

    row = db.execute(
        "SELECT transcript_anchor_json FROM notes WHERE id=?", (note_id,),
    ).fetchone()
    anchor = json.loads(row["transcript_anchor_json"])
    assert anchor == {"source_id": "src-cap", "segment_idx": 12, "start_sec": 83.4}

    # And it should now appear on the podcast detail.
    r2 = client.get("/api/podcasts/ep-cap")
    assert r2.status_code == 200
    notes = r2.json()["anchored_notes"]
    assert len(notes) == 1
    assert notes[0]["body"].endswith("the key insight")
