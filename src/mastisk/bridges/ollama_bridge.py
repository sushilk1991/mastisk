"""Ollama bridge — hybrid cloud + local fallback.

Local:  http://localhost:11434  (native /api/chat + /api/embed)
Cloud:  https://ollama.com       (same endpoints; bearer auth)

Note: if the user is signed in to Ollama Cloud via `ollama signin`, local daemon
transparently proxies :cloud-tagged models. So "local only" can still reach cloud
models — which is what we want.
"""
from __future__ import annotations

import logging
import struct

import httpx

from mastisk.settings import get_settings

log = logging.getLogger("mastisk.ollama")

_TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=30.0, pool=5.0)


def _endpoints() -> list[tuple[str, str | None]]:
    s = get_settings()
    urls: list[tuple[str, str | None]] = []
    if s.ollama_cloud_key and not s.ollama_local_only:
        urls.append((s.ollama_cloud_url.rstrip("/"), s.ollama_cloud_key))
    urls.append((s.ollama_local_url.rstrip("/"), None))
    return urls


async def chat(prompt: str, *, cheap: bool = False, system: str | None = None) -> str:
    s = get_settings()
    model = s.summarize_model_cheap if cheap else s.summarize_model_heavy

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_err: Exception | None = None
    for base, key in _endpoints():
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                headers = {"Authorization": f"Bearer {key}"} if key else {}
                r = await client.post(
                    f"{base}/api/chat",
                    headers=headers,
                    json={"model": model, "messages": messages, "stream": False, "think": False},
                )
                r.raise_for_status()
                data = r.json()
                msg = data.get("message") or {}
                content = msg.get("content") or ""
                if content:
                    return content
                # Fallback: some cloud models return OpenAI-shaped responses
                choices = data.get("choices") or []
                if choices:
                    return choices[0].get("message", {}).get("content", "")
                raise RuntimeError(f"empty response from {base}: {data}")
        except Exception as e:
            log.info("ollama chat failed at %s model=%s: %s", base, model, e)
            last_err = e
            continue
    raise RuntimeError(f"all ollama endpoints failed: {last_err}")


async def run_ollama(
    prompt: str, model: str, *, system: str | None = None
) -> dict:
    """Run a single chat completion against ``model``, returning ``{"text": str, "raw": dict}``.

    Mirrors the ``claude_bridge.run_claude`` contract so callers can swap engines
    with the same shape. Unlike ``chat()``, which uses the globally-configured
    heavy/cheap models, this lets an agent pick a model per-call (e.g. the
    Notetaker's classify model). Tries cloud first then local — same endpoint
    fallback as ``chat()``.
    """
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_err: Exception | None = None
    for base, key in _endpoints():
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                headers = {"Authorization": f"Bearer {key}"} if key else {}
                r = await client.post(
                    f"{base}/api/chat",
                    headers=headers,
                    json={"model": model, "messages": messages, "stream": False, "think": False},
                )
                r.raise_for_status()
                data = r.json()
                msg = data.get("message") or {}
                content = msg.get("content") or ""
                if not content:
                    choices = data.get("choices") or []
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")
                if not content:
                    raise RuntimeError(f"empty response from {base}: {data}")
                return {"text": content, "raw": data}
        except Exception as e:
            log.info("ollama run_ollama failed at %s model=%s: %s", base, model, e)
            last_err = e
            continue
    raise RuntimeError(f"all ollama endpoints failed for model={model}: {last_err}")


async def embed(texts: list[str]) -> list[list[float]]:
    """Try /api/embed (0.1.x+). Returns [] on failure — caller should fail open."""
    s = get_settings()
    last_err: Exception | None = None
    for base, key in _endpoints():
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                headers = {"Authorization": f"Bearer {key}"} if key else {}
                r = await client.post(
                    f"{base}/api/embed",
                    headers=headers,
                    json={"model": s.embed_model, "input": texts},
                )
                if r.status_code == 404:
                    # Older Ollama: /api/embeddings, singular prompt
                    out = []
                    for t in texts:
                        r2 = await client.post(
                            f"{base}/api/embeddings",
                            headers=headers,
                            json={"model": s.embed_model, "prompt": t},
                        )
                        r2.raise_for_status()
                        out.append(r2.json().get("embedding", []))
                    return out
                r.raise_for_status()
                data = r.json()
                return data.get("embeddings") or []
        except httpx.HTTPStatusError as e:
            body = e.response.text[:200] if e.response is not None else ""
            if "does not support embeddings" in body:
                raise RuntimeError(
                    f"Ollama model '{s.embed_model}' doesn't support embeddings. "
                    f"Run: ollama pull nomic-embed-text  (or set embed_model in config.toml)"
                ) from e
            log.info("ollama embed http error at %s: %s %s", base, e.response.status_code if e.response else "?", body)
            last_err = e
            continue
        except Exception as e:
            log.info("ollama embed failed at %s: %s", base, e)
            last_err = e
            continue
    raise RuntimeError(f"all ollama endpoints failed: {last_err}")


def pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
