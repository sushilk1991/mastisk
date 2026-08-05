from __future__ import annotations

import json
import sys
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
