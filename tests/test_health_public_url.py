"""`[server] public_url`: config loading and the `/api/health` field."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

# Placeholder only. The repository is public: never a real tailnet name.
PUBLIC_URL = "https://mastisk-host.example-tailnet.ts.net:8443"


def _health(data_tmp, config: str | None) -> dict:
    if config is not None:
        (data_tmp / "config.toml").write_text(config, encoding="utf-8")
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    response = TestClient(create_app()).get("/api/health")
    assert response.status_code == 200, response.text
    return response.json()


def test_health_reports_null_public_url_when_unset(vault_tmp, data_tmp, db):
    assert _health(data_tmp, None) == {"ok": True, "public_url": None}


def test_health_reports_null_public_url_when_server_table_omits_it(vault_tmp, data_tmp, db):
    body = _health(data_tmp, '[server]\nallowed_origins = ["https://app.example"]\n')

    assert body == {"ok": True, "public_url": None}


def test_health_reports_configured_public_url(vault_tmp, data_tmp, db):
    body = _health(data_tmp, f'[server]\npublic_url = "{PUBLIC_URL}"\n')

    assert body == {"ok": True, "public_url": PUBLIC_URL}


def test_public_url_does_not_extend_the_host_allowlist(vault_tmp, data_tmp, db):
    """It is a label for links, not a trust decision: the allowlist is unchanged."""
    (data_tmp / "config.toml").write_text(
        f'[server]\npublic_url = "{PUBLIC_URL}"\n', encoding="utf-8"
    )
    from mastisk.settings import reload_settings

    settings = reload_settings()
    assert "mastisk-host.example-tailnet.ts.net" not in settings.server.local_hostnames

    from mastisk.app import create_app

    response = TestClient(create_app()).get(
        "/api/health", headers={"Host": "mastisk-host.example-tailnet.ts.net:8443"}
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (PUBLIC_URL, PUBLIC_URL),
        (f"  {PUBLIC_URL}/  ", PUBLIC_URL),
        (f"{PUBLIC_URL}///", PUBLIC_URL),
        ("http://100.64.0.10:5555", "http://100.64.0.10:5555"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_public_url_is_normalised(raw, expected):
    from mastisk.settings import ServerSettings

    assert ServerSettings(public_url=raw).public_url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "mastisk-host.example-tailnet.ts.net:8443",  # no scheme
        "ftp://mastisk-host.example-tailnet.ts.net",
        "https://",
        f"{PUBLIC_URL}/?next=1",
        f"{PUBLIC_URL}/#top",
    ],
)
def test_public_url_rejects_values_a_browser_could_not_open(raw):
    from mastisk.settings import ServerSettings

    with pytest.raises(ValidationError):
        ServerSettings(public_url=raw)


def test_config_toml_public_url_loads_into_settings(vault_tmp, data_tmp):
    (data_tmp / "config.toml").write_text(
        f'[server]\npublic_url = "{PUBLIC_URL}/"\n', encoding="utf-8"
    )
    from mastisk.settings import reload_settings

    settings = reload_settings()

    assert settings.server.public_url == PUBLIC_URL
    # Setting it leaves the trust-boundary defaults alone.
    assert settings.server.allowed_origins == []
    assert "localhost" in settings.server.local_hostnames
