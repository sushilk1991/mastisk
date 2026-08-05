"""yt-dlp Python API wrapper. No binary-on-PATH dependency.

Three entry points: metadata fetch, subtitle fetch (plain text), and audio
download. All sync under the hood (yt-dlp is sync); callers `await` via
`asyncio.to_thread` so the scheduler tick isn't blocked.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("mastisk.youtube")


def canonicalize_url(url: str) -> str:
    """Collapse single-video YouTube URL variants to one stable watch URL.

    yt-dlp reports ``/watch?v=...`` as ``webpage_url`` even when the browser
    submitted an embed, Shorts, mobile, or youtu.be URL. Normalizing before a
    Listener job is created keeps queue identity aligned with the eventual
    ``sources.url`` identity and drops tracking/playlist parameters that do not
    identify the video itself. Non-YouTube URLs are returned unchanged.
    """
    raw = (url or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw

    host = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    video_id = ""
    if host == "youtu.be":
        video_id = path_parts[0] if path_parts else ""
    elif (
        host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtube-nocookie.com"
        or host.endswith(".youtube-nocookie.com")
    ):
        if parsed.path.rstrip("/").lower() == "/watch":
            video_id = (parse_qs(parsed.query).get("v") or [""])[0]
        elif len(path_parts) >= 2 and path_parts[0].lower() in {
            "embed",
            "live",
            "shorts",
            "v",
        }:
            video_id = path_parts[1]

    if not video_id or re.fullmatch(r"[A-Za-z0-9_-]+", video_id) is None:
        return raw
    return f"https://www.youtube.com/watch?v={video_id}"


async def fetch_metadata(url: str) -> dict:
    """Return normalized video metadata. Raises RuntimeError on failure."""
    try:
        info = await asyncio.to_thread(_extract_info, url, False)
    except Exception as e:
        raise RuntimeError(f"yt-dlp: {e}") from e
    _require_single_media(info)
    upload = info.get("upload_date") or ""
    upload_iso = f"{upload[:4]}-{upload[4:6]}-{upload[6:8]}" if len(upload) == 8 else None
    return {
        "id": info.get("id") or "",
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or "",
        "channel": info.get("channel") or "",
        "upload_date": upload_iso,
        "duration_sec": int(info.get("duration") or 0),
        "description": info.get("description") or "",
        "thumbnail": info.get("thumbnail") or "",
        "webpage_url": info.get("webpage_url") or url,
    }


async def fetch_subtitles(url: str, out_dir: Path) -> str | None:
    """Fetch best English subtitle (human > auto) and return path to a .txt
    containing concatenated dedup'd lines. None if no subs available.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        vtt_path = await asyncio.to_thread(_download_subs, url, out_dir)
    except Exception as e:
        raise RuntimeError(f"yt-dlp: {e}") from e
    if not vtt_path or not vtt_path.exists():
        return None
    text = _vtt_to_plaintext(vtt_path.read_text(encoding="utf-8", errors="replace"))
    if not text.strip():
        return None
    txt_path = vtt_path.with_suffix(".txt")
    txt_path.write_text(text, encoding="utf-8")
    return str(txt_path)


async def download_audio(url: str, out_dir: Path) -> Path:
    """Download best audio track, convert to m4a if needed. Returns path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        path = await asyncio.to_thread(_download_audio, url, out_dir)
    except Exception as e:
        raise RuntimeError(f"yt-dlp: {e}") from e
    if not path or not Path(path).exists():
        raise RuntimeError(f"yt-dlp: audio download produced no file for {url}")
    return Path(path)


# ───── sync yt-dlp helpers (run under asyncio.to_thread) ─────


def _extract_info(url: str, download: bool) -> dict:
    from yt_dlp import YoutubeDL
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": not download,
        "noplaylist": True,
        "playlist_items": "1",
    }
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=download) or {}


def _require_single_media(info: dict) -> None:
    if info.get("_type") in {"playlist", "multi_video"}:
        raise RuntimeError(
            "yt-dlp: playlists and channel pages are not supported; "
            "use a single video or episode URL"
        )


def _download_subs(url: str, out_dir: Path) -> Path | None:
    """Write best available en subtitle as .vtt into out_dir. Return the path
    or None if nothing was written. Prefers human-authored over automatic."""
    from yt_dlp import YoutubeDL
    outtmpl = str(out_dir / "%(id)s.%(ext)s")
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "subtitleslangs": ["en", "en-US", "en-GB"],
        "subtitlesformat": "vtt",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "playlist_items": "1",
    }
    # First pass: human subs only.
    opts_human = {**base_opts, "writesubtitles": True, "writeautomaticsub": False}
    with YoutubeDL(opts_human) as ydl:
        info = ydl.extract_info(url, download=True) or {}
    _require_single_media(info)
    vid = info.get("id") or ""
    found = _find_vtt(out_dir, vid)
    if found:
        return found
    # Fallback: automatic captions.
    opts_auto = {**base_opts, "writesubtitles": False, "writeautomaticsub": True}
    with YoutubeDL(opts_auto) as ydl:
        info = ydl.extract_info(url, download=True) or {}
    _require_single_media(info)
    vid = info.get("id") or vid
    return _find_vtt(out_dir, vid)


def _find_vtt(out_dir: Path, vid: str) -> Path | None:
    """yt-dlp names subs like <id>.en.vtt, <id>.en-US.vtt etc. Prefer exact en."""
    if not vid:
        return None
    for lang in ("en", "en-US", "en-GB"):
        p = out_dir / f"{vid}.{lang}.vtt"
        if p.exists():
            return p
    # Last resort: any .vtt starting with this id
    for p in out_dir.glob(f"{vid}.*.vtt"):
        return p
    return None


def _download_audio(url: str, out_dir: Path) -> str | None:
    from yt_dlp import YoutubeDL
    outtmpl = str(out_dir / "%(id)s.%(ext)s")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "playlist_items": "1",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"},
        ],
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True) or {}
    _require_single_media(info)
    vid = info.get("id") or ""
    # After postprocessing the file should be <id>.m4a. Fall back to any file.
    cand = out_dir / f"{vid}.m4a"
    if cand.exists():
        return str(cand)
    for ext in ("m4a", "mp3", "webm", "opus", "ogg", "wav"):
        cand = out_dir / f"{vid}.{ext}"
        if cand.exists():
            return str(cand)
    for p in out_dir.glob(f"{vid}.*"):
        return str(p)
    return None


# ───── vtt parsing ─────

_TS_RE = re.compile(r"^\s*\d{2}:\d{2}[:.]\d{2}[.,]\d{3}\s*-->")
_TAG_RE = re.compile(r"<[^>]+>")


def _vtt_to_plaintext(vtt: str) -> str:
    """Strip WEBVTT header, drop cue timings, collapse consecutive duplicates."""
    out: list[str] = []
    last: str | None = None
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE") or line.startswith("Kind:") \
                or line.startswith("Language:") or line.startswith("STYLE"):
            continue
        if _TS_RE.match(line) or "-->" in line:
            continue
        # Cue identifier lines are pure digits or a hash; skip if followed by a timestamp.
        # Simpler: skip lines with no word characters. Unicode-aware so we keep
        # Japanese/Chinese/Korean/Cyrillic/Arabic/etc. content.
        if not re.search(r"\w", line, re.UNICODE):
            continue
        clean = _TAG_RE.sub("", line).strip()
        if not clean or clean == last:
            continue
        out.append(clean)
        last = clean
    return "\n".join(out)
