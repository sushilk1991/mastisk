"""Inventory API."""
from __future__ import annotations

import csv
import io
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from mastisk.inventory.sync import (
    archive_inventory,
    create_inventory_file,
    inventory_payload,
    list_inventory,
    patch_inventory,
    total_value,
)

router = APIRouter(prefix="/api/inventory", tags=["inventory"])

InventoryStatus = Literal["owned", "sold", "discarded"]


class InventoryCreate(BaseModel):
    name: str = Field(min_length=1)
    acquired: str | None = None
    value: float | None = Field(default=None, ge=0)
    status: InventoryStatus = "owned"
    location: str | None = None
    photo: str | None = None
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def _name_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must be non-blank")
        return value.strip()


class InventoryPatch(BaseModel):
    name: str | None = None
    acquired: str | None = None
    value: float | None = Field(default=None, ge=0)
    status: InventoryStatus | None = None
    location: str | None = None
    photo: str | None = None
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def _optional_name_non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("name must be non-blank")
        return value.strip() if value is not None else None


@router.get("")
async def list_inventory_endpoint(
    status: InventoryStatus | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    items = list_inventory(status=status, location=location)
    return {"items": items, "total_value": total_value(items)}


@router.post("", status_code=201)
async def create_inventory_endpoint(req: InventoryCreate) -> dict[str, Any]:
    try:
        return create_inventory_file(**req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/export")
async def export_inventory_endpoint() -> Response:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["name", "acquired", "value", "status", "location"])
    for item in list_inventory():
        writer.writerow([
            _csv_safe_cell(item["name"]),
            _csv_safe_cell(item.get("acquired") or ""),
            "" if item.get("value") is None else item["value"],
            _csv_safe_cell(item["status"]),
            _csv_safe_cell(item.get("location") or ""),
        ])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="inventory.csv"'},
    )


@router.get("/{item_id}")
async def get_inventory_endpoint(item_id: str) -> dict[str, Any]:
    item = inventory_payload(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="inventory item not found")
    return item


def _csv_safe_cell(value: object) -> object:
    if not isinstance(value, str) or not value:
        return value
    if value[0] in {"=", "+", "-", "@"}:
        return f"'{value}"
    return value


@router.delete("/{item_id}")
async def delete_inventory_endpoint(item_id: str) -> dict[str, Any]:
    item = archive_inventory(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="inventory item not found")
    return item


@router.patch("/{item_id}")
async def patch_inventory_endpoint(item_id: str, req: InventoryPatch) -> dict[str, Any]:
    updates = {
        key: value
        for key, value in req.model_dump().items()
        if key in req.model_fields_set
    }
    try:
        item = patch_inventory(item_id, updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="inventory item not found")
    return item
