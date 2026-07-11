"""Shared pytest fixtures. Isolates DB + vault per test so nothing touches the real ~/Library paths."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_intelligence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the intelligence chain deterministic regardless of the host shell.

    A developer's ANTHROPIC_API_KEY would auto-prepend the anthropic tier and
    change provider ordering under test; the circuit breaker holds
    module-level state that would leak trip-status between tests.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from mastisk import settings
    settings.get_settings.cache_clear()
    from mastisk.bridges import intelligence
    intelligence.reset_breakers()


@pytest.fixture
def vault_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point MASTISK_VAULT at a tmp dir; clear the lru_cache so it takes effect."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("MASTISK_VAULT", str(vault))
    # Clear cached path resolvers
    from mastisk import paths
    paths.vault_dir.cache_clear()
    paths.data_dir.cache_clear()
    return vault


@pytest.fixture
def data_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point MASTISK_HOME at a tmp dir; clear the lru_cache so it takes effect."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("MASTISK_HOME", str(data))
    from mastisk import paths
    paths.data_dir.cache_clear()
    paths.vault_dir.cache_clear()
    return data


@pytest.fixture
def db(vault_tmp: Path, data_tmp: Path) -> Iterator[sqlite3.Connection]:
    """Fresh SQLite at data_tmp/mastisk.db, schema applied."""
    from mastisk.db.queries import connect, init_schema
    conn = connect()  # uses db_path() which reads data_dir() → data_tmp
    init_schema(conn)
    yield conn
    conn.close()
