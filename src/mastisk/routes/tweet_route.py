"""Tweet thread API - on-demand short-form drafts from recent context."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from mastisk.agents.base import enqueue
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.settings import get_settings

router = APIRouter(prefix="/api/tweet-threads", tags=["tweet_threads"])


class CreateTweetThreadRequest(BaseModel):
    theme: str = ""
    url: str | None = None
    window_days: int = Field(default=7)
    include_web: bool = True
    use_browser_context: bool = False

    @field_validator("theme")
    @classmethod
    def _theme_len(cls, v: str) -> str:
        if len(v) > 500:
            raise ValueError("theme must be <=500 chars")
        return v

    @field_validator("url")
    @classmethod
    def _url_len(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 2000:
            raise ValueError("url must be <=2000 chars")
        return v


class TweetThreadFeedbackRequest(BaseModel):
    body: str = Field(min_length=1, max_length=2000)
    target_tweet_index: int | None = Field(default=None, ge=0, le=99)
    rework: bool = True

    @field_validator("body")
    @classmethod
    def _body_trim(cls, v: str) -> str:
        text = v.strip()
        if not text:
            raise ValueError("feedback cannot be blank")
        return text


@router.post("", status_code=202)
async def create_tweet_thread_endpoint(req: CreateTweetThreadRequest) -> dict:
    settings = get_settings().tweet
    if req.window_days not in settings.allowed_window_days:
        raise HTTPException(
            status_code=422,
            detail=f"window_days must be one of {settings.allowed_window_days}",
        )
    url = (req.url or "").strip() or None
    theme = req.theme.strip()
    with connect() as conn:
        thread_id = q.create_tweet_thread(
            conn,
            theme=theme,
            url=url,
            window_days=req.window_days,
            include_web=req.include_web,
            use_browser_context=req.use_browser_context,
        )
    enqueue("tweet_writer", "draft", {"tweet_thread_id": thread_id})
    return {"id": thread_id, "status": "pending"}


@router.get("")
async def list_tweet_threads_endpoint(
    limit: int = 50,
    before: int | None = None,
) -> list[dict]:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be 1..500")
    with connect() as conn:
        rows = q.list_tweet_threads(conn, limit=limit, before_id=before)
    return [_shape(r) for r in rows]


@router.get("/{thread_id}")
async def get_tweet_thread_endpoint(thread_id: int) -> dict:
    with connect() as conn:
        row = q.get_tweet_thread(conn, thread_id)
    if row is None or row.get("deleted_at") is not None:
        raise HTTPException(status_code=404, detail="tweet thread not found")
    return _shape(row)


@router.post("/{thread_id}/regenerate", status_code=202)
async def regenerate_tweet_thread_endpoint(thread_id: int) -> dict:
    with connect() as conn:
        row = q.get_tweet_thread(conn, thread_id)
        if row is None or row.get("deleted_at") is not None:
            raise HTTPException(status_code=404, detail="tweet thread not found")
        if row["status"] not in ("done", "failed"):
            raise HTTPException(
                status_code=409,
                detail=f"cannot regenerate from status={row['status']}",
            )
        target_payload = json.dumps({"tweet_thread_id": thread_id})
        in_flight = conn.execute(
            """SELECT 1 FROM jobs
               WHERE agent = 'tweet_writer'
                 AND status IN ('queued', 'running')
                 AND payload_json = ?""",
            (target_payload,),
        ).fetchone()
        if in_flight is not None:
            raise HTTPException(
                status_code=409, detail="an earlier tweet job is still in flight",
            )
        q.reset_tweet_thread_for_regenerate(conn, thread_id)
    enqueue("tweet_writer", "draft", {"tweet_thread_id": thread_id})
    return {"id": thread_id, "status": "pending"}


@router.post("/{thread_id}/feedback", status_code=202)
async def create_tweet_thread_feedback_endpoint(
    thread_id: int, req: TweetThreadFeedbackRequest,
) -> dict:
    with connect() as conn:
        row = q.get_tweet_thread(conn, thread_id)
        if row is None or row.get("deleted_at") is not None:
            raise HTTPException(status_code=404, detail="tweet thread not found")

        tweets = _json_list(row.get("thread_json"))
        if req.target_tweet_index is not None and req.target_tweet_index >= len(tweets):
            raise HTTPException(status_code=422, detail="target tweet does not exist")

        if req.rework:
            if row["status"] not in ("done", "failed"):
                raise HTTPException(
                    status_code=409,
                    detail=f"cannot rework from status={row['status']}",
                )
            target_payload = json.dumps({"tweet_thread_id": thread_id})
            in_flight = conn.execute(
                """SELECT 1 FROM jobs
                   WHERE agent = 'tweet_writer'
                     AND status IN ('queued', 'running')
                     AND payload_json = ?""",
                (target_payload,),
            ).fetchone()
            if in_flight is not None:
                raise HTTPException(
                    status_code=409, detail="an earlier tweet job is still in flight",
                )

        feedback_id = q.create_tweet_thread_feedback(
            conn,
            thread_id=thread_id,
            target_tweet_index=req.target_tweet_index,
            body=req.body,
        )
        if req.rework:
            q.reset_tweet_thread_for_regenerate(conn, thread_id)

    if req.rework:
        enqueue("tweet_writer", "draft", {"tweet_thread_id": thread_id})
    return {
        "id": thread_id,
        "status": "pending" if req.rework else row["status"],
        "feedback_id": feedback_id,
        "rework_queued": req.rework,
    }


@router.delete("/{thread_id}", status_code=204)
async def delete_tweet_thread_endpoint(thread_id: int) -> None:
    with connect() as conn:
        row = q.get_tweet_thread(conn, thread_id)
        if row is None or row.get("deleted_at") is not None:
            raise HTTPException(status_code=404, detail="tweet thread not found")
        q.soft_delete_tweet_thread(conn, thread_id)


def _shape(row: dict) -> dict:
    with connect() as conn:
        feedback = q.list_tweet_thread_feedback(conn, int(row["id"]))
    return {
        "id": row["id"],
        "title": row.get("title"),
        "angle": row.get("angle"),
        "theme": row.get("theme") or "",
        "url": row.get("url"),
        "window_days": row["window_days"],
        "include_web": bool(row.get("include_web")),
        "use_browser_context": bool(row.get("use_browser_context")),
        "status": row["status"],
        "model": row.get("model"),
        "thread": _json_list(row.get("thread_json")),
        "sources": _json_list(row.get("sources_json")),
        "warnings": [str(w) for w in _json_list(row.get("warnings_json"))],
        "feedback": [_shape_feedback(f) for f in feedback],
        "error": row.get("error"),
        "created_at": row["created_at"],
        "finished_at": row.get("finished_at"),
    }


def _shape_feedback(row: dict) -> dict:
    return {
        "id": row["id"],
        "target_tweet_index": row.get("target_tweet_index"),
        "body": row.get("body") or "",
        "created_at": row["created_at"],
        "applied_at": row.get("applied_at"),
    }


def _json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []
