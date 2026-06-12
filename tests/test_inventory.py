from __future__ import annotations

import csv
import io
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


def test_inventory_patch_preserves_hand_edited_unparseable_frontmatter(
    db, vault_tmp, data_tmp
):
    from mastisk.inventory.sync import scan_inventory

    path = vault_tmp / "inventory" / "hand-edited-item.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "name: Hand edited item\n"
        "acquired: Christmas 2024\n"
        "value: ~$200\n"
        "status: lent\n"
        "location: Closet\n"
        "---\n\n"
        "Keep the original receipt.\n",
        encoding="utf-8",
    )
    scan_inventory()
    before = path.read_text(encoding="utf-8")

    with _client(vault_tmp, data_tmp, db) as client:
        patched = client.patch(
            "/api/inventory/hand-edited-item",
            json={"location": "Hall closet"},
        )

    assert patched.status_code == 200, patched.text
    after = path.read_text(encoding="utf-8")
    for line in (
        "acquired: Christmas 2024",
        "value: ~$200",
        "status: lent",
    ):
        assert line in before
        assert line in after
    assert "location: Hall closet" in after
    assert "photo:" not in after

    row = db.execute(
        """SELECT acquired, value, status, location
           FROM inventory WHERE id = 'hand-edited-item'"""
    ).fetchone()
    assert dict(row) == {
        "acquired": None,
        "value": None,
        "status": "owned",
        "location": "Hall closet",
    }


def test_inventory_patch_supports_all_fields_and_notes_body(db, vault_tmp, data_tmp):
    with _client(vault_tmp, data_tmp, db) as client:
        created = client.post(
            "/api/inventory",
            json={
                "name": "Camera",
                "acquired": "2026-01-01",
                "value": 500,
                "status": "owned",
                "location": "Shelf",
                "photo": "attachments/camera.jpg",
                "notes": "Original notes.",
            },
        )
        assert created.status_code == 201, created.text
        item_id = created.json()["id"]

        patched = client.patch(
            f"/api/inventory/{item_id}",
            json={
                "name": "Travel camera",
                "acquired": "2026-02-03",
                "value": 450.75,
                "status": "sold",
                "location": "Gear box",
                "photo": "attachments/travel-camera.jpg",
                "notes": "Sold to Rahul.",
            },
        )

    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["name"] == "Travel camera"
    assert body["acquired"] == "2026-02-03"
    assert body["value"] == 450.75
    assert body["status"] == "sold"
    assert body["location"] == "Gear box"
    assert body["photo"] == "attachments/travel-camera.jpg"
    assert body["body"].strip() == "Sold to Rahul."


def test_inventory_patch_rejects_invalid_acquired_from_api(db, vault_tmp, data_tmp):
    with _client(vault_tmp, data_tmp, db) as client:
        created = client.post(
            "/api/inventory",
            json={"name": "Tripod", "acquired": "2026-01-01"},
        )
        assert created.status_code == 201, created.text

        patched = client.patch(
            f"/api/inventory/{created.json()['id']}",
            json={"acquired": "Christmas 2024"},
        )

    assert patched.status_code == 422, patched.text
    assert patched.json()["detail"] == "acquired must be YYYY-MM-DD"


def test_inventory_delete_archives_file_and_hides_item(db, vault_tmp, data_tmp):
    with _client(vault_tmp, data_tmp, db) as client:
        created = client.post(
            "/api/inventory",
            json={
                "name": "Old backpack",
                "acquired": "2024-05-01",
                "status": "discarded",
                "notes": "Broken zip.",
            },
        )
        assert created.status_code == 201, created.text
        item_id = created.json()["id"]

        deleted = client.delete(f"/api/inventory/{item_id}")

        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["status"] == "discarded"
        assert deleted.json()["archived"] is True
        assert client.get(f"/api/inventory/{item_id}").status_code == 404
        listed = client.get("/api/inventory")
        assert listed.status_code == 200, listed.text
        assert listed.json()["items"] == []

    row = db.execute(
        "SELECT status, deleted_at FROM inventory WHERE id = ?",
        (item_id,),
    ).fetchone()
    assert row["status"] == "discarded"
    assert row["deleted_at"] is not None
    file_text = (vault_tmp / created.json()["path"]).read_text(encoding="utf-8")
    assert "archived: true" in file_text


def test_inventory_scan_does_not_archive_quoted_false_frontmatter(db, vault_tmp):
    from mastisk.inventory.sync import scan_inventory

    path = vault_tmp / "inventory" / "quoted-false-2026-06-10.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "name: Quoted false\n"
        "acquired: 2026-06-10\n"
        'archived: "false"\n'
        "---\n",
        encoding="utf-8",
    )

    scan_inventory()

    row = db.execute(
        "SELECT deleted_at FROM inventory WHERE id = 'quoted-false-2026-06-10'"
    ).fetchone()
    assert row["deleted_at"] is None


def test_inventory_export_escapes_formula_cells(db, vault_tmp, data_tmp):
    with _client(vault_tmp, data_tmp, db) as client:
        created = client.post(
            "/api/inventory",
            json={
                "name": '=HYPERLINK("https://example.com","click")',
                "acquired": "2026-01-01",
                "location": "+Office",
            },
        )
        assert created.status_code == 201, created.text

        exported = client.get("/api/inventory/export")

    assert exported.status_code == 200, exported.text
    rows = list(csv.reader(io.StringIO(exported.text)))
    assert rows[1][0] == '\'=HYPERLINK("https://example.com","click")'
    assert rows[1][4] == "'+Office"


def test_inventory_full_scan_skips_soft_delete_when_file_appears_after_glob(
    db, vault_tmp, monkeypatch
):
    import mastisk.inventory.sync as inventory_sync

    path = vault_tmp / "inventory" / "concurrent-create-2026-06-10.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "name: Concurrent create\n"
        "acquired: 2026-06-10\n"
        "status: owned\n"
        "---\n",
        encoding="utf-8",
    )
    db.execute(
        """INSERT INTO inventory
           (id, path, name, acquired, status, deleted_at)
           VALUES (?, ?, ?, ?, ?, NULL)""",
        (
            "concurrent-create-2026-06-10",
            "inventory/concurrent-create-2026-06-10.md",
            "Concurrent create",
            "2026-06-10",
            "owned",
        ),
    )
    monkeypatch.setattr(inventory_sync, "_inventory_paths", lambda: [])

    inventory_sync.scan_inventory()

    row = db.execute(
        "SELECT deleted_at FROM inventory WHERE id = 'concurrent-create-2026-06-10'"
    ).fetchone()
    assert row["deleted_at"] is None
    assert inventory_sync.inventory_payload("concurrent-create-2026-06-10") is not None


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
