"""Domains API."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator
from slugify import slugify

from mastisk.db.queries import connect
from mastisk.settings import get_settings

router = APIRouter(prefix="/api/domains", tags=["domains"])


class DomainCreate(BaseModel):
    name: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must be non-blank")
        return value.strip()


@router.get("")
async def list_domains_endpoint() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM domains WHERE deleted_at IS NULL ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_domain_endpoint(req: DomainCreate) -> dict:
    return _upsert_domain(req.name)


def sync_config_domains() -> None:
    for row in config_domain_rows():
        with connect() as conn:
            conn.execute(
                """INSERT INTO domains (slug, name, deleted_at)
                   VALUES (?, ?, NULL)
                   ON CONFLICT(slug) DO NOTHING""",
                (row["slug"], row["name"]),
            )


def config_domain_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for name in get_settings().domains.names:
        clean = str(name).strip()
        if not clean:
            continue
        slug = slugify(clean)[:80] or "domain"
        if slug in seen:
            continue
        seen.add(slug)
        rows.append({"slug": slug, "name": clean})
    return rows


def _upsert_domain(name: str) -> dict:
    slug = slugify(name)[:80] or "domain"
    with connect() as conn:
        conn.execute(
            """INSERT INTO domains (slug, name, deleted_at)
               VALUES (?, ?, NULL)
               ON CONFLICT(slug) DO UPDATE SET
                 name=excluded.name,
                 deleted_at=NULL""",
            (slug, name.strip()),
        )
        row = conn.execute("SELECT * FROM domains WHERE slug = ?", (slug,)).fetchone()
    return dict(row)
