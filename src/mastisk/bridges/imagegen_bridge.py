"""Hero-image generation via the yoyo CLI (`yoyo imagegen`).

Detected, never required: ``available()`` checks for the yoyo binary on PATH
(augmented with common user-install locations, same launchd caveat as the
claude bridge). Generated images are written into the vault attachments dir
(hash-named, same convention as user uploads) and served through the existing
``/api/attachments/{name}`` route — no new serving surface.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import uuid

from mastisk.bridges.claude_bridge import _resolve_cmd
from mastisk.paths import attachments_dir, tmp_dir

log = logging.getLogger("mastisk.imagegen")


class ImagegenError(RuntimeError):
    pass


def available() -> bool:
    return shutil.which(_resolve_cmd("yoyo")) is not None


async def generate_hero(prompt: str, *, timeout_s: int = 180) -> str:
    """Generate one image, store it as a vault attachment.

    Returns the relative URL (``/api/attachments/<hash>.png``) to store in
    ``articles.hero_image_url``. Raises ImagegenError on any failure.
    """
    yoyo = _resolve_cmd("yoyo")
    if shutil.which(yoyo) is None:
        raise ImagegenError("yoyo binary not on PATH")

    out_path = tmp_dir() / f"hero-{uuid.uuid4().hex[:12]}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [yoyo, "imagegen", prompt, "--out", str(out_path)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise ImagegenError(f"yoyo not executable: {e}") from e
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise ImagegenError(f"yoyo imagegen timed out after {timeout_s}s") from e

    try:
        if proc.returncode != 0:
            msg = stderr.decode("utf-8", errors="replace").strip()[-400:]
            raise ImagegenError(msg or f"yoyo exited {proc.returncode}")
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise ImagegenError("yoyo imagegen produced no output file")

        raw = out_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        target = attachments_dir() / f"{digest[:32]}.png"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        return f"/api/attachments/{target.name}"
    finally:
        out_path.unlink(missing_ok=True)
