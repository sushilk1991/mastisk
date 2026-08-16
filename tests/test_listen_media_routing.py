from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_video_hint_queues_original_page_for_listener(db, monkeypatch):
    from mastisk.routes import listen_route

    url = "https://example.com/watch/primary-video"
    classify = AsyncMock(return_value="article")
    monkeypatch.setattr(listen_route.podcasts, "classify", classify)

    result = await listen_route.listen(
        listen_route.ListenIn(url=url, media_type="video")
    )

    classify.assert_awaited_once_with(url)
    row = db.execute(
        "SELECT agent, kind, payload_json FROM jobs WHERE id=?", (result["job_id"],)
    ).fetchone()
    assert row["agent"] == "listener"
    assert row["kind"] == "transcribe"
    assert json.loads(row["payload_json"]) == {"url": url, "media_type": "video"}


@pytest.mark.asyncio
async def test_exact_url_hint_queues_requested_article_not_advertised_feed(
    db, monkeypatch,
):
    from mastisk.routes import listen_route

    page_url = "https://example.com/specific-article"
    classify = AsyncMock(return_value="article")
    resolve = AsyncMock(return_value=("rss", "https://example.com/feed.xml"))
    monkeypatch.setattr(listen_route.podcasts, "classify", classify)
    monkeypatch.setattr(listen_route.podcasts, "classify_and_resolve", resolve)

    result = await listen_route.listen(
        listen_route.ListenIn(url=page_url, exact_url=True)
    )

    classify.assert_awaited_once_with(page_url)
    resolve.assert_not_awaited()
    row = db.execute(
        "SELECT payload_json FROM jobs WHERE id=?", (result["job_id"],)
    ).fetchone()
    assert json.loads(row["payload_json"]) == {
        "url": page_url,
        "exact_url": True,
    }


@pytest.mark.asyncio
async def test_exact_url_does_not_reuse_active_legacy_nonexact_job(db, monkeypatch):
    from mastisk.routes import listen_route

    page_url = "https://example.com/specific-article"
    monkeypatch.setattr(
        listen_route.podcasts,
        "classify_and_resolve",
        AsyncMock(return_value=("article", page_url)),
    )
    legacy = await listen_route.listen(listen_route.ListenIn(url=page_url))

    monkeypatch.setattr(
        listen_route.podcasts,
        "classify",
        AsyncMock(return_value="article"),
    )
    exact = await listen_route.listen(
        listen_route.ListenIn(url=page_url, exact_url=True)
    )

    assert exact["job_id"] != legacy["job_id"]
    assert exact["duplicate"] is False
    payload = db.execute(
        "SELECT payload_json FROM jobs WHERE id=?", (exact["job_id"],)
    ).fetchone()["payload_json"]
    assert json.loads(payload)["exact_url"] is True


@pytest.mark.asyncio
async def test_exact_rss_url_can_refresh_after_completed_job(db, monkeypatch):
    from mastisk.routes import listen_route

    feed_url = "https://feeds.example.com/show.rss"
    monkeypatch.setattr(
        listen_route.podcasts,
        "classify",
        AsyncMock(return_value="rss"),
    )
    first = await listen_route.listen(
        listen_route.ListenIn(url=feed_url, exact_url=True)
    )
    db.execute("UPDATE jobs SET status='done' WHERE id=?", (first["job_id"],))

    refreshed = await listen_route.listen(
        listen_route.ListenIn(url=feed_url, exact_url=True)
    )

    assert refreshed["job_id"] != first["job_id"]
    assert refreshed["duplicate"] is False


@pytest.mark.asyncio
async def test_repeated_youtube_saves_reuse_one_canonical_job(db, monkeypatch):
    from mastisk.routes import listen_route
    from mastisk.routes.sources_route import list_jobs

    canonical = "https://www.youtube.com/watch?v=abc123xyz89"
    classify = AsyncMock(return_value="youtube")
    monkeypatch.setattr(listen_route.podcasts, "classify", classify)

    first = await listen_route.listen(
        listen_route.ListenIn(
            url="https://www.youtube.com/embed/abc123xyz89",
            title="Actual video title",
            media_type="video",
        )
    )
    second = await listen_route.listen(
        listen_route.ListenIn(
            url=f"{canonical}&si=tracking-token",
            title="Actual video title",
            media_type="video",
        )
    )
    third = await listen_route.listen(
        listen_route.ListenIn(
            url="https://youtu.be/abc123xyz89?t=42",
            title="Actual video title",
            media_type="video",
        )
    )

    assert first["job_id"] == second["job_id"] == third["job_id"]
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert third["duplicate"] is True
    rows = db.execute(
        "SELECT payload_json FROM jobs WHERE agent='listener' AND kind='transcribe'"
    ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"]) == {
        "url": canonical,
        "media_type": "video",
        "title": "Actual video title",
    }
    assert list_jobs(limit=1)["jobs"][0]["detail"]["title"] == "Actual video title"
    assert [call.args[0] for call in classify.await_args_list] == [canonical] * 3


@pytest.mark.asyncio
async def test_failed_youtube_job_can_be_queued_again(db, monkeypatch):
    from mastisk.routes import listen_route

    url = "https://www.youtube.com/watch?v=abc123xyz89"
    monkeypatch.setattr(
        listen_route.podcasts,
        "classify",
        AsyncMock(return_value="youtube"),
    )
    first = await listen_route.listen(listen_route.ListenIn(url=url, media_type="video"))
    db.execute("UPDATE jobs SET status='failed' WHERE id=?", (first["job_id"],))

    retry = await listen_route.listen(listen_route.ListenIn(url=url, media_type="video"))

    assert retry["job_id"] != first["job_id"]
    assert retry["duplicate"] is False


def test_listener_enqueue_dedupe_is_atomic_across_connections(db):
    from mastisk.routes.listen_route import enqueue_listener_once

    payload = {
        "url": "https://www.youtube.com/watch?v=abc123xyz89",
        "media_type": "video",
        "title": "Actual video title",
    }
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(lambda _: enqueue_listener_once(payload, reuse_done=True), range(6))
        )

    assert len({result["job_id"] for result in results}) == 1
    assert sum(not result["duplicate"] for result in results) == 1
    count = db.execute(
        "SELECT COUNT(*) FROM jobs WHERE agent='listener' AND kind='transcribe'"
    ).fetchone()[0]
    assert count == 1


def test_listener_enqueue_reuses_and_upgrades_active_legacy_youtube_job(db):
    from mastisk.routes.listen_route import enqueue_listener_once

    legacy_id = db.execute(
        """INSERT INTO jobs (agent, kind, payload_json)
           VALUES ('listener', 'transcribe', ?)""",
        (json.dumps({
            "url": "https://www.youtube.com/embed/abc123xyz89?autoplay=1",
            "media_type": "video",
        }),),
    ).lastrowid
    canonical = "https://www.youtube.com/watch?v=abc123xyz89"

    result = enqueue_listener_once(
        {"url": canonical, "media_type": "video", "title": "Actual video title"},
        reuse_done=True,
    )

    assert result["job_id"] == legacy_id
    assert result["duplicate"] is True
    rows = db.execute(
        "SELECT payload_json FROM jobs WHERE agent='listener' AND kind='transcribe'"
    ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"]) == {
        "url": canonical,
        "media_type": "video",
        "title": "Actual video title",
    }


@pytest.mark.asyncio
async def test_completed_podcast_show_can_refresh_but_episode_is_reused(db, monkeypatch):
    from mastisk.routes import listen_route

    show_url = "https://example.com/show"
    feed_url = "https://feeds.example.com/show.rss"
    monkeypatch.setattr(
        listen_route.podcasts,
        "classify_and_resolve",
        AsyncMock(return_value=("rss", feed_url)),
    )
    first_show = await listen_route.listen(
        listen_route.ListenIn(
            url=show_url,
            media_type="podcast",
            media_scope="show",
        )
    )
    db.execute("UPDATE jobs SET status='done' WHERE id=?", (first_show["job_id"],))
    refreshed_show = await listen_route.listen(
        listen_route.ListenIn(
            url=show_url,
            media_type="podcast",
            media_scope="show",
        )
    )
    assert refreshed_show["job_id"] != first_show["job_id"]
    assert refreshed_show["duplicate"] is False

    episode_url = "https://example.com/episodes/7"
    monkeypatch.setattr(
        listen_route.podcasts,
        "classify",
        AsyncMock(return_value="article"),
    )
    first_episode = await listen_route.listen(
        listen_route.ListenIn(
            url=episode_url,
            media_type="podcast",
            media_scope="episode",
        )
    )
    db.execute("UPDATE jobs SET status='done' WHERE id=?", (first_episode["job_id"],))
    repeated_episode = await listen_route.listen(
        listen_route.ListenIn(
            url=episode_url,
            media_type="podcast",
            media_scope="episode",
        )
    )
    assert repeated_episode["job_id"] == first_episode["job_id"]
    assert repeated_episode["duplicate"] is True


def test_queue_uses_canonical_youtube_source_title_over_browser_fallback(db):
    from mastisk.routes.sources_route import list_jobs

    submitted = "https://www.youtube.com/embed/abc123xyz89"
    canonical = "https://www.youtube.com/watch?v=abc123xyz89"
    source_id = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    db.execute(
        "INSERT INTO sources (id, kind, url, title) VALUES (?, 'youtube', ?, ?)",
        (source_id, canonical, "Actual yt-dlp video title"),
    )
    db.execute(
        """INSERT INTO articles (id, kind, title, slug)
           VALUES ('video-article', 'Source', 'Compiled article', 'video-article')"""
    )
    db.execute(
        "INSERT INTO article_sources (article_id, source_id) VALUES ('video-article', ?)",
        (source_id,),
    )
    db.execute(
        """INSERT INTO jobs (agent, kind, payload_json, status)
           VALUES ('listener', 'transcribe', ?, 'done')""",
        (
            json.dumps(
                {
                    "url": submitted,
                    "media_type": "video",
                    "title": "Stale browser title",
                }
            ),
        ),
    )

    job = list_jobs(limit=1)["jobs"][0]

    assert job["detail"]["title"] == "Actual yt-dlp video title"
    assert job["detail"]["source_kind"] == "youtube"
    assert job["detail"]["article_id"] == "video-article"


@pytest.mark.asyncio
async def test_listener_skips_existing_youtube_before_metadata_fetch(db, monkeypatch):
    from mastisk.agents.listener import Listener

    canonical = "https://www.youtube.com/watch?v=abc123xyz89"
    source_id = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    db.execute(
        "INSERT INTO sources (id, kind, url, title) VALUES (?, 'youtube', ?, ?)",
        (source_id, canonical, "Already ingested"),
    )
    fetch_metadata = AsyncMock(side_effect=AssertionError("metadata must not be fetched"))
    monkeypatch.setattr(
        "mastisk.agents.listener.youtube.fetch_metadata",
        fetch_metadata,
    )

    await Listener()._ingest_youtube(
        "https://www.youtube.com/embed/abc123xyz89",
        source_kind="youtube",
    )

    fetch_metadata.assert_not_awaited()
    duplicate = db.execute(
        "SELECT verb, obj FROM feed WHERE agent='listener' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert duplicate["verb"] == "duplicate"
    assert duplicate["obj"] == canonical


@pytest.mark.asyncio
async def test_listener_persists_canonical_url_from_metadata(db, monkeypatch, tmp_path):
    from mastisk.agents.listener import Listener

    video_id = "abc123xyz89"
    canonical = f"https://www.youtube.com/watch?v={video_id}"
    subtitle = tmp_path / "subtitle.txt"
    subtitle.write_text("Transcript text")
    fetch_metadata = AsyncMock(
        return_value={
            "id": video_id,
            "title": "Canonical video",
            "webpage_url": f"https://m.youtube.com/shorts/{video_id}",
            "uploader": "Channel",
            "upload_date": "20260805",
            "duration_sec": 60,
            "thumbnail": None,
        }
    )
    fetch_subtitles = AsyncMock(return_value=subtitle)
    monkeypatch.setattr(
        "mastisk.agents.listener.youtube.fetch_metadata",
        fetch_metadata,
    )
    monkeypatch.setattr(
        "mastisk.agents.listener.youtube.fetch_subtitles",
        fetch_subtitles,
    )
    listener = Listener()
    finalize = AsyncMock()
    monkeypatch.setattr(listener, "_finalize_ingest", finalize)

    await listener._ingest_youtube(
        f"https://www.youtube.com/embed/{video_id}",
        source_kind="youtube",
    )

    assert finalize.await_args.kwargs["source_context"]["url"] == canonical
    assert fetch_subtitles.await_args.args[0] == canonical


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        (
            "https://www.youtube.com/watch?v=abc123xyz89&list=PL123&t=5",
            "https://www.youtube.com/watch?v=abc123xyz89",
        ),
        (
            "https://www.youtube.com/embed/abc123xyz89?autoplay=1",
            "https://www.youtube.com/watch?v=abc123xyz89",
        ),
        (
            "https://youtu.be/abc123xyz89?si=token",
            "https://www.youtube.com/watch?v=abc123xyz89",
        ),
        (
            "https://m.youtube.com/shorts/abc123xyz89",
            "https://www.youtube.com/watch?v=abc123xyz89",
        ),
        (
            "https://example.com/video#chapter",
            "https://example.com/video#chapter",
        ),
    ],
)
def test_canonicalize_media_url(raw, canonical):
    from mastisk.integrations.youtube import canonicalize_url

    assert canonicalize_url(raw) == canonical


@pytest.mark.asyncio
async def test_podcast_hint_keeps_discovered_feed_and_hint(db, monkeypatch):
    from mastisk.routes import listen_route

    page_url = "https://example.com/podcast/episode"
    feed_url = "https://feeds.example.com/show.rss"
    classify = AsyncMock(return_value=("rss", feed_url))
    monkeypatch.setattr(listen_route.podcasts, "classify_and_resolve", classify)

    result = await listen_route.listen(
        listen_route.ListenIn(
            url=page_url,
            media_type="podcast",
            media_scope="show",
        )
    )

    classify.assert_awaited_once_with(page_url)
    row = db.execute(
        "SELECT payload_json FROM jobs WHERE id=?", (result["job_id"],)
    ).fetchone()
    assert json.loads(row["payload_json"]) == {
        "url": feed_url,
        "media_type": "podcast",
        "media_scope": "show",
    }
    assert "auto-discovered feed" in result["message"]


@pytest.mark.asyncio
async def test_podcast_episode_hint_preserves_specific_page(db, monkeypatch):
    from mastisk.routes import listen_route

    page_url = "https://example.com/podcast/episodes/specific"
    classify = AsyncMock(return_value="article")
    monkeypatch.setattr(listen_route.podcasts, "classify", classify)

    result = await listen_route.listen(
        listen_route.ListenIn(
            url=page_url,
            media_type="podcast",
            media_scope="episode",
        )
    )

    classify.assert_awaited_once_with(page_url)
    row = db.execute(
        "SELECT payload_json FROM jobs WHERE id=?", (result["job_id"],)
    ).fetchone()
    assert json.loads(row["payload_json"]) == {
        "url": page_url,
        "media_type": "podcast",
        "media_scope": "episode",
    }


@pytest.mark.asyncio
async def test_listener_video_hint_overrides_unrelated_discovered_rss(monkeypatch):
    from mastisk.agents.listener import Listener

    page_url = "https://example.com/watch/primary-video"
    listener = Listener()
    classify = AsyncMock(return_value=("rss", "https://example.com/site-feed.xml"))
    ingest = AsyncMock()
    monkeypatch.setattr(
        "mastisk.agents.listener.podcasts.classify_and_resolve", classify
    )
    monkeypatch.setattr(listener, "_ingest_youtube", ingest)

    await listener._handle_transcribe(page_url, media_type="video")

    ingest.assert_awaited_once_with(page_url, source_kind="video")


@pytest.mark.asyncio
async def test_listener_exact_url_ingests_requested_article_not_discovered_feed(
    monkeypatch,
):
    from mastisk.agents.listener import Listener

    page_url = "https://example.com/specific-article"
    listener = Listener()
    classify = AsyncMock(return_value="article")
    resolve = AsyncMock(return_value=("rss", "https://example.com/feed.xml"))
    ingest = AsyncMock()
    monkeypatch.setattr("mastisk.agents.listener.podcasts.classify", classify)
    monkeypatch.setattr(
        "mastisk.agents.listener.podcasts.classify_and_resolve", resolve,
    )
    monkeypatch.setattr(listener, "_ingest_article", ingest)

    await listener._handle_transcribe(page_url, exact_url=True)

    classify.assert_awaited_once_with(page_url)
    resolve.assert_not_awaited()
    ingest.assert_awaited_once_with(page_url, source_kind="blog")


@pytest.mark.asyncio
async def test_listener_podcast_episode_hint_uses_original_page(monkeypatch):
    from mastisk.agents.listener import Listener

    page_url = "https://podcasts.apple.com/us/podcast/example/id123"
    listener = Listener()
    classify = AsyncMock(return_value="article")
    ingest = AsyncMock()
    monkeypatch.setattr(
        "mastisk.agents.listener.podcasts.classify", classify
    )
    monkeypatch.setattr(listener, "_ingest_youtube", ingest)

    await listener._handle_transcribe(
        page_url,
        media_type="podcast",
        media_scope="episode",
    )

    ingest.assert_awaited_once_with(page_url, source_kind="podcast")


@pytest.mark.asyncio
async def test_listener_preserves_youtube_kind_for_video_hint(monkeypatch):
    from mastisk.agents.listener import Listener

    url = "https://www.youtube.com/watch?v=abc"
    listener = Listener()
    monkeypatch.setattr(
        "mastisk.agents.listener.podcasts.classify_and_resolve",
        AsyncMock(return_value=("youtube", url)),
    )
    ingest = AsyncMock()
    monkeypatch.setattr(listener, "_ingest_youtube", ingest)

    await listener._handle_transcribe(url, media_type="video")

    ingest.assert_awaited_once_with(url, source_kind="youtube")


@pytest.mark.asyncio
async def test_listener_podcast_feed_hint_queues_latest_audio_episode(monkeypatch):
    from mastisk.agents import listener as listener_module
    from mastisk.agents.listener import Listener

    feed_url = "https://feeds.example.com/show.rss"
    episode = {
        "audio_url": "https://cdn.example.com/latest.mp3",
        "title": "Latest episode",
        "author": "Example show",
        "published_at": "2026-08-05 10:00:00",
        "image": None,
    }
    monkeypatch.setattr(
        listener_module.podcasts,
        "classify_and_resolve",
        AsyncMock(return_value=("rss", feed_url)),
    )
    resolve = AsyncMock(return_value=[episode])
    monkeypatch.setattr(listener_module.podcasts, "resolve_rss_episode", resolve)
    queued = []
    monkeypatch.setattr(
        listener_module,
        "enqueue",
        lambda agent, kind, payload: queued.append((agent, kind, payload)) or 42,
    )
    listener = Listener()
    monkeypatch.setattr(listener, "emit_feed", lambda **_kwargs: None)

    await listener._handle_transcribe(feed_url, media_type="podcast")

    resolve.assert_awaited_once_with(feed_url, max_episodes=1)
    assert queued == [
        (
            "listener",
            "transcribe_audio",
            {
                "audio_url": episode["audio_url"],
                "episode_title": episode["title"],
                "show_title": episode["author"],
                "published_at": episode["published_at"],
                "feed_url": feed_url,
                "image": None,
            },
        )
    ]


@pytest.mark.asyncio
async def test_ytdlp_entry_points_bound_and_reject_playlists(monkeypatch, tmp_path):
    from mastisk.integrations import youtube

    observed_options = []

    class FakeYoutubeDL:
        def __init__(self, options):
            observed_options.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            assert isinstance(download, bool)
            return {
                "_type": "playlist",
                "id": "PL123",
                "title": "A channel playlist",
                "entries": [{"id": "first"}],
            }

    monkeypatch.setitem(
        sys.modules,
        "yt_dlp",
        SimpleNamespace(YoutubeDL=FakeYoutubeDL),
    )

    with pytest.raises(RuntimeError, match="use a single video or episode URL"):
        await youtube.fetch_metadata("https://www.youtube.com/playlist?list=PL123")
    with pytest.raises(RuntimeError, match="use a single video or episode URL"):
        youtube._download_subs(
            "https://www.youtube.com/playlist?list=PL123",
            tmp_path / "subs",
        )
    with pytest.raises(RuntimeError, match="use a single video or episode URL"):
        youtube._download_audio(
            "https://www.youtube.com/playlist?list=PL123",
            tmp_path / "audio",
        )

    assert len(observed_options) == 3
    assert all(options["noplaylist"] is True for options in observed_options)
    assert all(options["playlist_items"] == "1" for options in observed_options)
