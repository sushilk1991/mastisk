"""Listener — ingests remote video, YouTube, and podcast audio.

Pipeline per job:
  1. classify URL (youtube | rss | direct_audio | article | spotify | unknown)
  2. obtain transcript (yt-dlp subs → mlx-whisper fallback, or direct whisper)
  3. extract entities via Claude (summary, books, people, concepts)
  4. enrich books via OpenLibrary, pre-create Entity articles for each resolved book
  5. write raw_path file with wiki-linked mentions
  6. insert sources row, enqueue compiler/compile

We don't write the main article — that's the Compiler's job. We just stage a
rich raw_path so the Compiler's prompt has named books/people/concepts to
hang wiki-links onto.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from pathlib import Path

import httpx
from slugify import slugify

from mastisk.agents.base import Agent, enqueue
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.integrations import (
    article as article_extractor,
)
from mastisk.integrations import (
    extract,
    openlibrary,
    podcasts,
    whisper,
    youtube,
)
from mastisk.integrations import (
    twitter as twitter_extractor,
)
from mastisk.integrations.podcasts import UnsupportedPlatformError
from mastisk.paths import raw_dir, tmp_dir, vault_dir
from mastisk.vault_io import write_vault_text

log = logging.getLogger("mastisk.listener")


class Listener(Agent):
    name = "listener"
    tick_seconds = 180

    async def _handle(self, job: dict) -> None:
        payload = json.loads(job.get("payload_json") or "{}")
        kind = job.get("kind") or "transcribe"
        if kind == "transcribe_audio":
            await self._handle_audio_episode(payload)
            return
        # Default: kind == 'transcribe'
        url = (payload.get("url") or "").strip()
        if not url:
            raise RuntimeError("listener: no url in job payload")
        media_type = payload.get("media_type")
        if media_type not in ("video", "podcast"):
            media_type = None
        media_scope = payload.get("media_scope")
        if media_scope not in ("episode", "show"):
            media_scope = None
        await self._handle_transcribe(
            url,
            media_type=media_type,
            media_scope=media_scope,
        )

    # ───── transcribe (remote video / YouTube / direct audio / rss) ─────

    async def _handle_transcribe(
        self,
        url: str,
        *,
        media_type: str | None = None,
        media_scope: str | None = None,
    ) -> None:
        # Show pages may resolve to an advertised RSS feed. Episode pages keep
        # their exact URL so a show feed cannot silently substitute its newest
        # entry. The route normally pre-resolves too; we repeat classification
        # for jobs queued through other paths or retried from older payloads.
        original_url = url
        if media_type == "podcast" and media_scope == "episode":
            cls = await podcasts.classify(url)
            resolved_url = url
        else:
            cls, resolved_url = await podcasts.classify_and_resolve(url)
        log.info(
            "listener: classified %s as %s (media_type=%s, media_scope=%s)",
            resolved_url,
            cls,
            media_type,
            media_scope,
        )

        if cls == "spotify":
            raise UnsupportedPlatformError(
                "Spotify podcasts are DRM-protected and can't be ingested. "
                "Try the podcast's RSS feed URL or Apple Podcasts link."
            )

        # The browser can observe the rendered DOM, so its primary-media hint is
        # stronger evidence than a server-side HEAD/HTML sniff. yt-dlp supports
        # far more than YouTube (Vimeo, Loom, Apple Podcasts, direct media, etc.),
        # and the existing YouTube integration is already a thin yt-dlp adapter.
        # Keep the original page URL for video: an unrelated site-wide RSS link
        # may have changed resolved_url above.
        if media_type == "video":
            source_kind = "youtube" if cls == "youtube" else "video"
            await self._ingest_youtube(original_url, source_kind=source_kind)
            return
        if media_type == "podcast" and cls not in ("rss", "direct_audio", "youtube"):
            await self._ingest_youtube(original_url, source_kind="podcast")
            return

        url = resolved_url
        if cls == "unknown":
            raise RuntimeError(f"can't ingest {url} — unknown type")

        if cls in ("article", "twitter"):
            await self._ingest_article(url, source_kind="blog" if cls == "article" else "twitter")
            return

        if cls == "rss":
            # Enqueue a per-episode job for the latest episode. Caller is free
            # to enqueue more via resolve_rss_episode if they want a backfill.
            episodes = await podcasts.resolve_rss_episode(url, max_episodes=1)
            if not episodes:
                raise RuntimeError(f"no episodes found in feed {url}")
            ep = episodes[0]
            feed_title = ep.get("author") or ""
            enqueue("listener", kind="transcribe_audio", payload={
                "audio_url": ep["audio_url"],
                "episode_title": ep.get("title") or "",
                "show_title": feed_title,
                "published_at": ep.get("published_at"),
                "feed_url": url,
                "image": ep.get("image"),
            })
            self.emit_feed(
                verb="queued",
                obj=(ep.get("title") or feed_title or url)[:80],
                kind="podcast",
                payload={"feed_url": url, "episode_title": ep.get("title")},
            )
            return

        if cls == "youtube":
            await self._ingest_youtube(url, source_kind="youtube")
            return

        if cls == "direct_audio":
            await self._ingest_direct_audio(url)
            return

    async def _ingest_youtube(self, url: str, *, source_kind: str = "youtube") -> None:
        """Ingest any yt-dlp-supported media page.

        The historical name stays private to avoid a noisy module rename, but
        ``source_kind`` preserves whether the input was YouTube, another video,
        or a podcast page.
        """
        # yt-dlp canonicalizes embed/Shorts/mobile URLs to /watch?v=..., but
        # metadata extraction itself is a network call that can be rate-limited.
        # Normalize and dedupe first so repeated extension saves do no remote
        # work at all once the source exists.
        canonical_url = youtube.canonicalize_url(url)
        existing = self._source_for_url(canonical_url)
        if existing:
            log.info("listener: %s %s already ingested, skipping", source_kind, canonical_url)
            self.emit_feed(
                verb="duplicate",
                obj=canonical_url[:80],
                kind=source_kind,
                payload={"source_id": existing["id"], "url": canonical_url},
            )
            return

        meta = await youtube.fetch_metadata(canonical_url)
        canonical_url = youtube.canonicalize_url(meta.get("webpage_url") or canonical_url)
        existing = self._source_for_url(canonical_url)
        if existing:
            log.info("listener: %s %s already ingested, skipping", source_kind, canonical_url)
            self.emit_feed(
                verb="duplicate",
                obj=(meta.get("title") or canonical_url)[:80],
                kind=source_kind,
                payload={"source_id": existing["id"], "url": canonical_url},
            )
            return

        work_dir = tmp_dir() / f"media-{meta['id'] or _hash16(canonical_url)}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            transcript = ""
            segments: list[whisper.TranscriptSegment] = []
            sub_path = await youtube.fetch_subtitles(canonical_url, work_dir)
            if sub_path:
                transcript = Path(sub_path).read_text(encoding="utf-8", errors="replace")
            if not transcript.strip():
                # Fall back to whisper. Fail loud with a useful hint if it's missing.
                if not whisper.is_available():
                    raise RuntimeError(
                        f"no subtitles or transcript on {url} and mlx-whisper is not installed. "
                        "Install with: uv tool install --force --reinstall --with mlx-whisper mastisk"
                    )
                audio_path = await youtube.download_audio(canonical_url, work_dir)
                result = await whisper.transcribe(audio_path)
                transcript = result.text
                segments = result.segments

            src_context = {
                "title": meta["title"],
                "author": meta.get("uploader") or meta.get("channel"),
                "published_at": meta.get("upload_date"),
                "source_kind": source_kind,
                "url": canonical_url,
                "duration_sec": meta.get("duration_sec"),
                "hero_image_url": meta.get("thumbnail") or None,
            }
            await self._finalize_ingest(
                transcript=transcript,
                source_context=src_context,
                source_kind=source_kind,
                segments=segments,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _ingest_direct_audio(self, url: str) -> None:
        src_id = _hash16(url)
        if self._source_exists(src_id):
            log.info("listener: audio %s already ingested, skipping", url)
            self.emit_feed(
                verb="duplicate",
                obj=url[:80],
                kind="podcast",
                payload={"source_id": src_id, "url": url},
            )
            return
        work_dir = tmp_dir() / f"audio-{_hash16(url)}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            audio_path = await _download_to(url, work_dir)
            if not whisper.is_available():
                raise RuntimeError(
                    "mlx-whisper is not installed. "
                    "Install with: uv tool install --force --reinstall --with mlx-whisper mastisk"
                )
            result = await whisper.transcribe(audio_path)
            title = Path(url).stem or url
            src_context = {
                "title": title,
                "author": None,
                "published_at": None,
                "source_kind": "podcast",
                "url": url,
                "duration_sec": None,
            }
            await self._finalize_ingest(
                transcript=result.text,
                source_context=src_context,
                source_kind="podcast",
                segments=result.segments,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _handle_audio_episode(self, payload: dict) -> None:
        """Handle a single RSS-resolved episode. Downloads + transcribes."""
        audio_url = (payload.get("audio_url") or "").strip()
        if not audio_url:
            raise RuntimeError("listener: transcribe_audio missing audio_url")
        title = payload.get("episode_title") or payload.get("show_title") or audio_url
        show = payload.get("show_title") or ""
        published_at = payload.get("published_at")
        feed_url = payload.get("feed_url")
        episode_image = payload.get("image") or None

        src_id = _hash16(audio_url)
        if self._source_exists(src_id):
            log.info("listener: episode %s already ingested, skipping", audio_url)
            self.emit_feed(
                verb="duplicate",
                obj=title[:80],
                kind="podcast",
                payload={"source_id": src_id, "url": audio_url},
            )
            return
        work_dir = tmp_dir() / f"podcast-{_hash16(audio_url)}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            audio_path = await _download_to(audio_url, work_dir)
            if not whisper.is_available():
                raise RuntimeError(
                    "mlx-whisper is not installed. "
                    "Install with: uv tool install --force --reinstall --with mlx-whisper mastisk"
                )
            result = await whisper.transcribe(audio_path)

            src_context = {
                "title": title,
                "author": show,
                "published_at": published_at,
                "source_kind": "podcast",
                "url": audio_url,
                "duration_sec": None,
                "feed_url": feed_url,
                "hero_image_url": episode_image,
            }
            await self._finalize_ingest(
                transcript=result.text,
                source_context=src_context,
                source_kind="podcast",
                segments=result.segments,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    # ───── article (HTML page) ingestion ─────

    async def _ingest_article(self, url: str, *, source_kind: str = "blog") -> None:
        """Universal HTML page → wiki-article path.

        Uses a source-specific extractor to pull main text + hero/inline
        images; persists a ``sources`` row with ``kind=source_kind`` (defaults
        to 'blog' for generic web pages, 'twitter' for x.com URLs); enqueues
        the Compiler which turns the source into a structured wiki article in
        the same way it handles RSS clippings from Scout.
        """
        # Dedup by canonical URL hash. The article extractor follows redirects
        # and returns the canonical URL, so the hash lines up across paste
        # variants (with/without query string, http/https, www/no-www only when
        # the server actually canonicalises).
        try:
            if source_kind == "twitter":
                data = await twitter_extractor.fetch_and_extract(url)
            else:
                data = await article_extractor.fetch_and_extract(url)
        except Exception as e:
            log.info("listener: %s fetch failed for %s: %s", source_kind, url, e)
            raise RuntimeError(f"{source_kind} fetch failed: {e}") from e

        canonical_url = data.url
        src_id = _hash16(canonical_url)
        existing = self._source_row(src_id)
        if existing:
            if source_kind == "twitter":
                existing_raw = _read_existing_raw(existing["raw_path"])
                if twitter_extractor.needs_refresh(existing_raw, data):
                    raw_path = Path(existing["raw_path"] or (raw_dir() / f"{src_id}.txt"))
                    _write_article_raw(raw_path, title=data.title, url=canonical_url, text=data.text)
                    with connect() as conn, q.txn(conn):
                        conn.execute(
                            """UPDATE sources
                                  SET kind = ?, url = ?, title = ?, published_at = ?,
                                      fetched_at = CURRENT_TIMESTAMP, raw_path = ?,
                                      author = ?, hero_image_url = ?, media_json = ?
                                WHERE id = ?""",
                            (
                                source_kind,
                                canonical_url,
                                data.title,
                                data.published_at,
                                str(raw_path),
                                data.author,
                                data.hero_image_url,
                                json.dumps(data.inline_media) if data.inline_media else None,
                                src_id,
                            ),
                        )
                        conn.execute(
                            "INSERT INTO jobs (agent, kind, payload_json) VALUES (?, ?, ?)",
                            ("compiler", "compile", json.dumps({"source_id": src_id})),
                        )
                    log.info("listener: refreshed twitter source %s", canonical_url)
                    self.emit_feed(
                        verb="refreshed",
                        obj=(data.title or canonical_url)[:80],
                        kind=source_kind,
                        payload={
                            "source_id": src_id,
                            "url": canonical_url,
                            "chars": len(data.text),
                            "via": "listener",
                        },
                    )
                    return

            log.info("listener: %s %s already ingested, skipping", source_kind, canonical_url)
            self.emit_feed(
                verb="duplicate",
                obj=(data.title or canonical_url)[:80],
                kind=source_kind,
                payload={"source_id": src_id, "url": canonical_url},
            )
            return

        raw_path = raw_dir() / f"{src_id}.txt"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        # Mirror Scout's raw-file shape so the Compiler sees a familiar layout:
        # the title is the first line, then the URL, then the extracted body.
        # Compiler doesn't depend on this exact format, but consistency makes
        # debugging easier when comparing Scout-clipped vs Listener-pasted
        # sources side-by-side.
        _write_article_raw(raw_path, title=data.title, url=canonical_url, text=data.text)

        with connect() as conn, q.txn(conn):
            cur = conn.execute(
                """INSERT OR IGNORE INTO sources
                     (id, kind, url, title, published_at, raw_path, author,
                      hero_image_url, media_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    src_id,
                    source_kind,
                    canonical_url,
                    data.title,
                    data.published_at,
                    str(raw_path),
                    data.author,
                    data.hero_image_url,
                    json.dumps(data.inline_media) if data.inline_media else None,
                ),
            )
            inserted = (cur.rowcount or 0) > 0
            if inserted:
                conn.execute(
                    "INSERT INTO jobs (agent, kind, payload_json) VALUES (?, ?, ?)",
                    ("compiler", "compile", json.dumps({"source_id": src_id})),
                )

        self.emit_feed(
            verb="clipped",
            obj=(data.title or canonical_url)[:80],
            kind=source_kind,
            payload={
                "source_id": src_id,
                "url": canonical_url,
                "chars": len(data.text),
                "via": "listener",
            },
        )

    # ───── shared finish path ─────

    async def _finalize_ingest(
        self,
        *,
        transcript: str,
        source_context: dict,
        source_kind: str,
        segments: list[whisper.TranscriptSegment] | None = None,
    ) -> None:
        extracted = await extract.extract_entities(transcript, source_context)

        # Enrich books via OpenLibrary, pre-create Entity articles. Books
        # are looked up in parallel — 10 books * 10s timeout serially is 100s
        # worst case, which stalls the whole ingest tick.
        resolved_books: list[dict] = []
        created_book_slugs: list[str] = []
        raw_books = [b for b in (extracted.get("books") or []) if (b.get("title") or "").strip()]
        if raw_books:
            async with httpx.AsyncClient(
                timeout=10.0, headers={"User-Agent": "Mastisk/0.1 (knowledge-wiki; personal use)"}
            ) as client:
                ol_results = await asyncio.gather(
                    *(
                        openlibrary.search_book(
                            (b.get("title") or "").strip(),
                            (b.get("author") or "").strip() or None,
                            client=client,
                        )
                        for b in raw_books
                    ),
                    return_exceptions=True,
                )
            for b, ol in zip(raw_books, ol_results, strict=True):
                if isinstance(ol, BaseException):
                    log.info("openlibrary lookup failed for %r: %s", b.get("title"), ol)
                    resolved_books.append({**b, "ol": None})
                    continue
                if not ol:
                    resolved_books.append({**b, "ol": None})
                    continue
                slug = self._ensure_book_article(
                    title=ol["title"],
                    ol_data=ol,
                    mention_context={
                        "source_title": source_context.get("title") or "",
                        "mention": b.get("context") or "",
                    },
                )
                if slug:
                    created_book_slugs.append(slug)
                resolved_books.append({**b, "ol": ol, "slug": slug})

        url = source_context.get("url") or ""
        src_id = _hash16(url or (source_context.get("title") or ""))

        # Insert source row + enqueue compile job in a single transaction so
        # the row can't exist without a matching job (or vice-versa).
        # INSERT OR IGNORE (not REPLACE): sources.url is UNIQUE and src_id is
        # deterministic from the URL hash, so re-ingest of the same URL should
        # skip — a REPLACE would CASCADE-delete any prior article_sources
        # relationships via the FK.
        with connect() as conn, q.txn(conn):
            cur = conn.execute(
                """INSERT OR IGNORE INTO sources
                   (id, kind, url, title, published_at, raw_path, author, hero_image_url,
                    duration_sec, feed_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    src_id,
                    source_kind,
                    url,
                    source_context.get("title") or "",
                    source_context.get("published_at"),
                    str(raw_dir() / f"{src_id}.txt"),
                    source_context.get("author") or None,
                    source_context.get("hero_image_url") or None,
                    source_context.get("duration_sec"),
                    source_context.get("feed_url") or None,
                ),
            )
            inserted = (cur.rowcount or 0) > 0
            # Only enqueue the compile job for fresh sources. Re-enqueuing on a
            # duplicate would re-run Claude against the same raw file and could
            # clobber curation other agents (Linter, Synthesizer) did since.
            if inserted:
                conn.execute(
                    "INSERT INTO jobs (agent, kind, payload_json) VALUES (?, ?, ?)",
                    ("compiler", "compile", json.dumps({"source_id": src_id})),
                )
                # Persist whisper-derived segments so the PodcastView can render
                # the transcript clickable, time-anchored, and notable. Only
                # written for fresh inserts — same idempotency story as the raw
                # file below; the backfill command rewrites these for existing
                # sources separately.
                if segments:
                    conn.executemany(
                        """INSERT INTO source_transcript_segments
                             (source_id, idx, start_sec, end_sec, text)
                           VALUES (?, ?, ?, ?, ?)""",
                        [
                            (src_id, s.idx, s.start_sec, s.end_sec, s.text)
                            for s in segments
                        ],
                    )

        # Only write the raw file for fresh inserts. If the URL was already
        # ingested, the existing raw file is still valid — don't clobber it
        # (the transcript may match, but re-writing risks losing a version
        # the Compiler has already consumed).
        if inserted:
            raw_path = raw_dir() / f"{src_id}.txt"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(
                _render_raw(
                    source_context=source_context,
                    extracted=extracted,
                    resolved_books=resolved_books,
                    transcript=transcript,
                ),
                encoding="utf-8",
            )

        self.emit_feed(
            verb="transcribed",
            obj=(source_context.get("title") or url)[:80],
            kind=source_kind,
            touched=len(created_book_slugs),
            payload={
                "source_id": src_id,
                "books": len(resolved_books),
                "people": len(extracted.get("people") or []),
                "concepts": len(extracted.get("concepts") or []),
                "duplicate": not inserted,
            },
        )

    # ───── dedup helper ─────

    def _source_exists(self, src_id: str) -> bool:
        return self._source_row(src_id) is not None

    def _source_row(self, src_id: str):
        with connect() as conn:
            return conn.execute(
                "SELECT * FROM sources WHERE id = ? LIMIT 1", (src_id,)
            ).fetchone()

    def _source_for_url(self, url: str):
        src_id = _hash16(url)
        with connect() as conn:
            return conn.execute(
                "SELECT * FROM sources WHERE id = ? OR url = ? LIMIT 1",
                (src_id, url),
            ).fetchone()

    # ───── book Entity articles (pre-stub) ─────

    def _ensure_book_article(
        self,
        *,
        title: str,
        ol_data: dict,
        mention_context: dict,
    ) -> str:
        authors = ol_data.get("authors") or []
        first_author = authors[0] if authors else ""
        slug = slugify(f"{title} {first_author}")[:80] or slugify(title)[:80]
        if not slug:
            return ""

        # If an article already exists, don't overwrite — Compiler may have
        # already authored it from another source.
        with connect() as conn:
            existing = conn.execute(
                "SELECT id FROM articles WHERE id = ?", (slug,)
            ).fetchone()
        if existing:
            return slug

        year = ol_data.get("year")
        cover = ol_data.get("cover_url") or ""
        subjects = ol_data.get("subjects") or []
        about_bits: list[str] = []
        if cover:
            about_bits.append(f'<img src="{cover}" alt="{title} cover" />')
        authors_line = ", ".join(authors) if authors else "Unknown author"
        year_line = f" ({year})" if year else ""
        about_bits.append(f"<p><strong>{authors_line}</strong>{year_line}</p>")
        if subjects:
            about_bits.append(
                "<p><em>Subjects:</em> " + ", ".join(s for s in subjects[:8]) + "</p>"
            )
        source_title = mention_context.get("source_title") or ""
        if source_title:
            about_bits.append(f"<p>First mentioned in: {source_title}</p>")
        about_html = "\n".join(about_bits)

        summary = f"{authors_line}{year_line}".strip() or (subjects[0] if subjects else "")
        body_md = (
            f"## About\n\n"
            f"{authors_line}{year_line}\n\n"
            + (f"Subjects: {', '.join(subjects[:8])}\n\n" if subjects else "")
            + (f"First mentioned in: {source_title}\n" if source_title else "")
        )
        vault_path = vault_dir() / "entities" / f"{slug}.md"

        with connect() as conn, q.txn(conn):
            q.upsert_article(conn, {
                "id": slug,
                "kind": "Entity",
                "title": title,
                "slug": slug,
                "aka": [],
                "summary": summary,
                "body_md": body_md,
                "confidence": 0.75,
                "reading_minutes": 1,
                "updated_by": "listener",
                "vault_path": str(vault_path),
                "hero_image_url": cover or None,
            })
            q.replace_sections(conn, slug, [
                {"h": "About", "body": about_html, "kind": "section"},
            ])

        # Mirror to vault. Mirrors Compiler._render_markdown shape lightly —
        # full rendering is the Compiler's job if it later writes over us.
        vault_path.parent.mkdir(parents=True, exist_ok=True)
        write_vault_text(
            vault_path,
            "\n".join([
                "---",
                f"id: {slug}",
                "kind: Entity",
                f"title: {title}",
                "confidence: 0.75",
                "reading_minutes: 1",
                "updated_by: listener",
                "---",
                "",
                f"# {title}",
                "",
                f"*{summary}*",
                "",
                body_md,
            ]),
            encoding="utf-8",
        )
        return slug


# ───── module helpers ─────


def _hash16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _write_article_raw(path: Path, *, title: str, url: str, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n{url}\n\n{text}", encoding="utf-8")


def _read_existing_raw(raw_path: str | None) -> str:
    if not raw_path:
        return ""
    try:
        return Path(raw_path).read_text(encoding="utf-8")
    except OSError:
        return ""


async def _download_to(url: str, out_dir: Path) -> Path:
    """Stream a remote file into out_dir. Returns the saved path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Derive a filename from the URL path; fall back to a hash if unclear.
    name = Path(url.split("?", 1)[0]).name or f"audio-{_hash16(url)}.bin"
    dest = out_dir / name
    try:
        async with (
            httpx.AsyncClient(follow_redirects=True, timeout=60.0) as c,
            c.stream("GET", url) as resp,
        ):
            if resp.status_code >= 400:
                raise RuntimeError(f"download {url} returned {resp.status_code}")
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(1 << 16):
                    f.write(chunk)
    except Exception as e:
        raise RuntimeError(f"download failed: {e}") from e
    return dest


def _render_raw(
    *,
    source_context: dict,
    extracted: dict,
    resolved_books: list[dict],
    transcript: str,
) -> str:
    title = source_context.get("title") or "(untitled)"
    url = source_context.get("url") or ""
    source_kind = source_context.get("source_kind") or ""
    duration = source_context.get("duration_sec")
    published = source_context.get("published_at") or "-"
    author = source_context.get("author") or "-"
    dur_s = f"{duration}s" if duration else "-"
    header = (
        f"# {title}\n"
        f"{url}\n"
        f"{source_kind} · {dur_s} · {published} · {author}\n\n"
    )

    summary = (extracted.get("summary") or "").strip()
    summary_block = f"## Summary\n{summary}\n\n" if summary else ""

    book_lines: list[str] = []
    for b in resolved_books:
        ol = b.get("ol") or {}
        btitle = ol.get("title") or b.get("title") or ""
        authors = ol.get("authors") or ([b.get("author")] if b.get("author") else [])
        author_str = ", ".join(a for a in authors if a) or "Unknown"
        year = ol.get("year")
        year_str = f" ({year})" if year else ""
        slug = b.get("slug") or slugify(f"{btitle} {authors[0] if authors else ''}")[:80]
        ctx = (b.get("context") or "").strip()
        link = f"[[{btitle}|{slug}]]" if slug else btitle
        ctx_suffix = f" — {ctx}" if ctx else ""
        book_lines.append(f"- {link} — {author_str}{year_str}{ctx_suffix}")
    books_block = "## Books mentioned\n" + "\n".join(book_lines) + "\n\n" if book_lines else ""

    people_lines: list[str] = []
    for p in extracted.get("people") or []:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        role = (p.get("role") or "").strip() or "-"
        note = (p.get("note") or "").strip() or "-"
        people_lines.append(f"- {name} — {role} — {note}")
    people_block = "## People mentioned\n" + "\n".join(people_lines) + "\n\n" if people_lines else ""

    concepts = [c for c in (extracted.get("concepts") or []) if c]
    concepts_block = "## Concepts\n" + ", ".join(concepts) + "\n\n" if concepts else ""

    chapter_lines: list[str] = []
    for i, ch in enumerate(extracted.get("chapters") or [], start=1):
        heading = (ch.get("heading") or "").strip() or "Section"
        start_min = ch.get("start_min")
        gist = (ch.get("body_preview") or "").strip()
        mark = f" (~{start_min}m)" if isinstance(start_min, int) else ""
        chapter_lines.append(f"{i}. {heading}{mark}: {gist}")
    chapters_block = "## Chapters\n" + "\n".join(chapter_lines) + "\n\n" if chapter_lines else ""

    return (
        header
        + summary_block
        + books_block
        + people_block
        + concepts_block
        + chapters_block
        + "## Transcript\n"
        + (transcript or "").strip()
        + "\n"
    )
