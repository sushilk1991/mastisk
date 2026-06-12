"""Ask drawer — retrieves wiki context and streams a cited answer."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from mastisk.db import queries as q
from mastisk.db.queries import connect

router = APIRouter(tags=["ask"])


class AskRequest(BaseModel):
    question: str
    selection: str | None = None
    article_id: str | None = None


@router.post("/ask")
async def ask(req: AskRequest):
    from mastisk.bridges import ollama_bridge

    # Simple retrieval: FTS over the wiki + optional current article
    with connect() as conn:
        hits = q.search_articles(conn, req.question, limit=5)
        personal_hits = q.search_personal_os_context(conn, req.question, per_kind=2)
        article = q.get_article(conn, req.article_id) if req.article_id else None
        if req.article_id:
            q.add_signal(conn, article_id=req.article_id, kind="asked",
                         value={"q": req.question, "selection": req.selection})

    context_chunks = []
    for h in hits:
        context_chunks.append(f"## {h['title']}\n{h.get('summary', '')}\n")
    if article:
        context_chunks.insert(0, f"# Currently reading: {article['title']}\n{article.get('summary', '')}\n")
    if personal_hits:
        personal_lines = ["# Personal OS context"]
        for hit in personal_hits[:18]:
            label = str(hit["kind"]).replace("_", " ").title()
            excerpt = (hit.get("excerpt") or hit.get("snippet") or "").strip()
            personal_lines.append(f"## {label}: {hit['title']}\n{excerpt[:220]}")
        context_chunks.append("\n".join(personal_lines))
    context = "\n\n".join(context_chunks) or "(no wiki context found)"

    # Load identity for personalization
    identity = _load_identity()

    prompt = (
        f"You are Mastisk's Ask assistant. Answer the user's question using only the Mastisk context below. "
        f"Cite page titles inline in [[brackets]]. If the context is insufficient, say so plainly.\n\n"
        f"{identity}\n\n"
        f"# Mastisk context\n{context}\n\n"
        f"# Question\n{req.question}\n"
    )

    try:
        answer = await ollama_bridge.chat(prompt, cheap=True)
    except Exception as e:
        answer = f"_(Ollama unavailable: {e})_\n\nHere's what the wiki says:\n\n{context[:600]}"

    cites = [h["title"] for h in hits] + [h["title"] for h in personal_hits]
    return {"answer": answer, "cites": cites, "hits": [*hits, *personal_hits]}


def _load_identity() -> str:
    from mastisk.paths import self_dir
    parts = []
    for name in ("identity", "interests", "dislikes", "style", "learnings"):
        p = self_dir() / f"{name}.md"
        if p.exists():
            txt = p.read_text().strip()
            if txt:
                parts.append(f"## {name}\n{txt}")
    return "# About the user\n" + "\n\n".join(parts) if parts else ""
