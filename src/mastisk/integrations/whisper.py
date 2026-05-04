"""mlx-whisper wrapper. Lazy import — mlx-whisper is an optional extra."""
from __future__ import annotations

import asyncio
import importlib
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("mastisk.whisper")

_INSTALL_HINT = (
    "Audio transcription needs mlx-whisper. "
    "Install with: uv tool install --force --reinstall --with mlx-whisper mastisk"
)


@dataclass
class TranscriptSegment:
    """One whisper-derived chunk. start/end are seconds from the start of the
    audio; ``text`` is the verbatim transcribed phrase. The Listener persists
    these to source_transcript_segments so the PodcastView can render a
    clickable, time-anchored transcript."""
    idx: int
    start_sec: float
    end_sec: float
    text: str


@dataclass
class TranscriptResult:
    text: str
    segments: list[TranscriptSegment]


def is_available() -> bool:
    try:
        importlib.import_module("mlx_whisper")
        return True
    except ImportError:
        return False


async def transcribe(
    audio_path: Path,
    model: str = "mlx-community/whisper-medium",
) -> TranscriptResult:
    """Transcribe an audio file with mlx-whisper.

    Returns a TranscriptResult containing both the joined text (preserved for
    callers that just want the raw transcript) and the structured segments
    (used by the PodcastView for time-anchored playback). Older callers can
    use ``result.text`` and ignore segments.

    Raises RuntimeError if mlx-whisper isn't installed — the message includes
    an install hint so the failed-job UI is actionable.
    """
    if not is_available():
        raise RuntimeError(_INSTALL_HINT)
    return await asyncio.to_thread(_run_mlx, audio_path, model)


def _run_mlx(audio_path: Path, model: str) -> TranscriptResult:
    import mlx_whisper  # type: ignore[import-untyped]
    result = mlx_whisper.transcribe(str(audio_path), path_or_hf_repo=model)
    if not isinstance(result, dict):
        return TranscriptResult(text=str(result).strip(), segments=[])
    text = (result.get("text") or "").strip()
    raw_segments = result.get("segments") or []
    segments: list[TranscriptSegment] = []
    for i, seg in enumerate(raw_segments):
        # mlx-whisper emits segment dicts with start, end, text. Defensive
        # coercion in case the upstream shape evolves — silently skip
        # malformed entries rather than failing the whole transcription.
        try:
            start = float(seg.get("start", 0.0))
            end = float(seg.get("end", start))
            seg_text = (seg.get("text") or "").strip()
        except (TypeError, ValueError):
            continue
        if not seg_text:
            continue
        segments.append(TranscriptSegment(
            idx=i, start_sec=start, end_sec=end, text=seg_text,
        ))
    return TranscriptResult(text=text, segments=segments)
