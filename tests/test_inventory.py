from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


def _client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    return TestClient(create_app())


def _capture(**overrides):
    from mastisk.capture.router import Capture

    data = {
        "type": "inventory",
        "confidence": 0.96,
        "title": "LG 5K Monitor",
        "body": "Work display for the office.",
        "domain": None,
        "project": None,
        "person": None,
        "routine": None,
        "due": None,
        "scheduled": None,
        "priority": None,
        "recurrence": None,
        "reminder_lead_minutes": None,
        "no_reminder": False,
        "review_at": None,
        "tags": [],
        "related": [],
        "command_detected": True,
    }
    data.update(overrides)
    return Capture(**data)


def test_scan_inventory_round_trips_handmade_file(db, vault_tmp):
    from mastisk.inventory.sync import scan_inventory

    path = vault_tmp / "inventory" / "lg-5k-monitor-2026-06-11.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "name: LG 5K Monitor\n"
        "acquired: 2026-06-11\n"
        "value: 1299.5\n"
        "status: owned\n"
        "location: Office\n"
        "photo: attachments/monitor.jpg\n"
        "---\n\n"
        "Serial number is in the box.\n",
        encoding="utf-8",
    )

    result = scan_inventory()

    assert result == {"upserted": 1}
    row = db.execute(
        "SELECT * FROM inventory WHERE id = 'lg-5k-monitor-2026-06-11'"
    ).fetchone()
    assert row["name"] == "LG 5K Monitor"
    assert row["acquired"] == "2026-06-11"
    assert row["value"] == 1299.5
    assert row["status"] == "owned"
    assert row["location"] == "Office"
    assert row["photo"] == "attachments/monitor.jpg"
    assert row["path"] == "inventory/lg-5k-monitor-2026-06-11.md"


def test_inventory_routes_create_patch_filter_and_export_csv(db, vault_tmp, data_tmp):
    with _client(vault_tmp, data_tmp, db) as client:
        first = client.post(
            "/api/inventory",
            json={
                "name": 'Desk, "Oak"',
                "acquired": "2026-06-10",
                "value": 250,
                "status": "owned",
                "location": "Studio",
                "notes": "Needs felt pads.",
            },
        )
        assert first.status_code == 201, first.text
        item_id = first.json()["id"]

        second = client.post(
            "/api/inventory",
            json={
                "name": "Old chair",
                "acquired": "2025-01-01",
                "value": 30,
                "status": "sold",
                "location": "Garage",
            },
        )
        assert second.status_code == 201, second.text

        patched = client.patch(
            f"/api/inventory/{item_id}",
            json={"value": 275.25, "location": "Home office"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["value"] == 275.25
        assert patched.json()["location"] == "Home office"

        listed = client.get("/api/inventory?status=owned&location=Home%20office")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["total_value"] == 275.25
        assert [item["id"] for item in body["items"]] == [item_id]

        detail = client.get(f"/api/inventory/{item_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["body"].strip() == "Needs felt pads."

        exported = client.get("/api/inventory/export")
        assert exported.status_code == 200, exported.text
        assert exported.headers["content-type"].startswith("text/csv")
        lines = exported.text.splitlines()
        assert lines[0] == "name,acquired,value,status,location"
        assert '"Desk, ""Oak"""' in lines[1]
        assert "2026-06-10,275.25,owned,Home office" in lines[1]


def test_capture_inventory_command_creates_item_and_medium_confidence_triages(
    db, vault_tmp, data_tmp
):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[capture]\nbearer_token = "test-token"\ndefault_timezone = "Asia/Kolkata"\n',
        encoding="utf-8",
    )
    with _client(vault_tmp, data_tmp, db) as client, patch(
        "mastisk.routes.capture.route_capture", new_callable=AsyncMock
    ) as router:
        router.return_value = _capture()
        saved = client.post(
            "/api/capture",
            json={
                "text": "add LG 5K Monitor to inventory",
                "source": "watch",
                "ts": "2026-06-12T09:00:00+05:30",
            },
            headers={"Authorization": "Bearer test-token"},
        )

        assert saved.status_code == 201, saved.text
        body = saved.json()
        assert body["type"] == "inventory"
        assert body["needs_triage"] is False
        assert body["id"].startswith("lg-5k-monitor-")
        item = client.get(f"/api/inventory/{body['id']}").json()
        assert item["acquired"] == "2026-06-12"
        file_text = (vault_tmp / body["destination"]).read_text(encoding="utf-8")
        assert "status: owned" in file_text

        router.return_value = _capture(
            confidence=0.7,
            command_detected=False,
            title="Vintage lamp",
            body="Vintage lamp for the den.",
        )
        triaged = client.post(
            "/api/capture",
            json={"text": "maybe inventory this vintage lamp", "source": "watch"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert triaged.status_code == 201, triaged.text
        assert triaged.json()["type"] == "inventory"
        assert triaged.json()["needs_triage"] is True
        queue = client.get("/api/triage").json()
        item_id = next(row["id"] for row in queue if row["detected_type"] == "inventory")
        accepted = client.post(
            f"/api/triage/{item_id}/reclassify",
            json={"type": "inventory"},
        )
        assert accepted.status_code == 200, accepted.text
        inventory_row = db.execute(
            "SELECT name, status FROM inventory WHERE name = ? AND deleted_at IS NULL",
            ("Vintage lamp",),
        ).fetchone()
        assert dict(inventory_row) == {"name": "Vintage lamp", "status": "owned"}


def test_create_inventory_defaults_acquired_to_today(db, vault_tmp, monkeypatch):
    import mastisk.inventory.sync as inventory_sync

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 6, 12)

    monkeypatch.setattr(inventory_sync, "date", FixedDate)

    item = inventory_sync.create_inventory_file(name="Passport holder")

    assert item["acquired"] == "2026-06-12"
    assert item["status"] == "owned"
