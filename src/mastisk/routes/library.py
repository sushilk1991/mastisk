"""Library API: books, quotes, and Kindle import."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from mastisk.library.kindle import (
    dismiss_review_block,
    import_clippings_text,
    list_review_blocks,
    retry_review_as_quote,
)
from mastisk.library.sync import (
    add_book_highlight,
    append_quote_thought,
    book_payload,
    create_book_file_with_lookup,
    create_quote_file,
    list_books,
    list_quotes,
    patch_book,
    quote_payload,
    refresh_book_metadata,
)

router = APIRouter(tags=["library"])
_MAX_KINDLE_UPLOAD_BYTES = 10 * 1024 * 1024


class BookCreate(BaseModel):
    title: str = Field(min_length=1)
    author: str | None = None
    isbn: str | None = None
    lookup: bool = False

    @field_validator("title")
    @classmethod
    def _title_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must be non-blank")
        return value.strip()


class BookPatch(BaseModel):
    status: Literal["want", "reading", "finished", "abandoned"] | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    started: str | None = None
    finished: str | None = None
    format: str | None = None
    summary: str | None = None
    cover_url: str | None = None
    isbn: str | None = None
    author: str | None = None


class HighlightCreate(BaseModel):
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _highlight_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("highlight text must be non-blank")
        return value.strip()


class QuoteCreate(BaseModel):
    text: str = Field(min_length=1)
    source_type: Literal["book", "article", "podcast", "conversation"] = "conversation"
    source_ref: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def _quote_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("quote text must be non-blank")
        return value.strip()


class ThoughtCreate(BaseModel):
    text: str = Field(min_length=1)
    ts: str | None = None

    @field_validator("text")
    @classmethod
    def _thought_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("thought text must be non-blank")
        return value.strip()


class ReviewRetryAsQuote(BaseModel):
    text: str | None = None
    source_type: Literal["book", "article", "podcast", "conversation"] = "conversation"
    source_ref: str | None = None
    tags: list[str] = Field(default_factory=list)


@router.get("/api/books")
async def list_books_endpoint(status: str | None = None) -> list[dict[str, Any]]:
    return list_books(status=status)


@router.post("/api/books", status_code=201)
async def create_book_endpoint(req: BookCreate) -> dict[str, Any]:
    try:
        return await create_book_file_with_lookup(**req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/books/{slug}")
async def get_book_endpoint(slug: str) -> dict[str, Any]:
    book = book_payload(slug)
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")
    return book


@router.patch("/api/books/{slug}")
async def patch_book_endpoint(slug: str, req: BookPatch) -> dict[str, Any]:
    updates = {
        key: value
        for key, value in req.model_dump().items()
        if key in req.model_fields_set
    }
    try:
        book = patch_book(slug, updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")
    return book


@router.post("/api/books/{slug}/refresh-metadata")
async def refresh_book_metadata_endpoint(slug: str) -> dict[str, Any]:
    book = await refresh_book_metadata(slug)
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")
    return book


@router.post("/api/books/{slug}/highlights", status_code=201)
async def add_book_highlight_endpoint(slug: str, req: HighlightCreate) -> dict[str, Any]:
    try:
        highlight = add_book_highlight(slug, req.text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if highlight is None:
        raise HTTPException(status_code=404, detail="book not found")
    return highlight


@router.get("/api/quotes")
async def list_quotes_endpoint(
    source_type: str | None = None,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    return list_quotes(source_type=source_type, tag=tag)


@router.post("/api/quotes", status_code=201)
async def create_quote_endpoint(req: QuoteCreate) -> dict[str, Any]:
    try:
        return create_quote_file(**req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/quotes/{quote_id}")
async def get_quote_endpoint(quote_id: str) -> dict[str, Any]:
    quote = quote_payload(quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="quote not found")
    return quote


@router.post("/api/quotes/{quote_id}/thoughts", status_code=201)
async def append_quote_thought_endpoint(quote_id: str, req: ThoughtCreate) -> dict[str, Any]:
    try:
        quote = append_quote_thought(quote_id, req.text, ts=req.ts)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if quote is None:
        raise HTTPException(status_code=404, detail="quote not found")
    return quote


@router.post("/api/import/kindle")
async def import_kindle_endpoint(file: Annotated[UploadFile, File(...)]) -> dict[str, int]:
    raw = await file.read(_MAX_KINDLE_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_KINDLE_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Kindle upload exceeds 10 MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("utf-8-sig", errors="replace")
    return import_clippings_text(text)


@router.get("/api/import/kindle/review")
async def list_kindle_review_endpoint() -> list[dict[str, Any]]:
    return list_review_blocks()


@router.post("/api/import/kindle/review/{item_id}/dismiss")
async def dismiss_kindle_review_endpoint(item_id: int) -> dict[str, Any]:
    item = dismiss_review_block(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="review item not found")
    return item


@router.post("/api/import/kindle/review/{item_id}/retry-as-quote", status_code=201)
async def retry_kindle_review_as_quote_endpoint(
    item_id: int,
    req: ReviewRetryAsQuote,
) -> dict[str, Any]:
    try:
        item = retry_review_as_quote(item_id, **req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="review item not found")
    return item
