from __future__ import annotations

import errno
from pathlib import Path

import pytest

from mastisk import vault_io


def _deadlock_error() -> OSError:
    return OSError(errno.EDEADLK, "Resource deadlock avoided")


def test_successful_self_read_updates_local_cache(
    vault_tmp: Path,
    data_tmp: Path,
) -> None:
    path = vault_tmp / "_self" / "identity.md"
    path.parent.mkdir()
    path.write_text("# Identity\n\nSushil")

    assert vault_io.read_vault_text(path) == "# Identity\n\nSushil"
    assert (
        data_tmp / "cache" / "vault-self" / "identity.md"
    ).read_text() == "# Identity\n\nSushil"


def test_self_read_uses_local_cache_after_icloud_deadlock(
    vault_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = vault_tmp / "_self" / "identity.md"
    path.parent.mkdir()
    path.write_text("# Identity\n\nSushil")
    assert vault_io.read_vault_text(path) == "# Identity\n\nSushil"

    original_read_text = Path.read_text
    attempts = 0

    def fail_vault_read(self: Path, **kwargs: str) -> str:
        nonlocal attempts
        if self == path:
            attempts += 1
            raise _deadlock_error()
        return original_read_text(self, **kwargs)

    monkeypatch.setattr(vault_io, "vault_is_icloud", lambda: True)
    monkeypatch.setattr(Path, "read_text", fail_vault_read)

    assert vault_io.read_vault_text(path) == "# Identity\n\nSushil"
    assert attempts == 2


def test_read_vault_text_uses_hydration_from_concurrent_reader(
    vault_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = vault_tmp / "_self" / "dislikes.md"
    reads = iter([_deadlock_error(), "available"])

    def fake_read_text(self: Path, **kwargs: str) -> str:
        result = next(reads)
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr(vault_io, "vault_is_icloud", lambda: True)
    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(vault_io, "_update_self_cache", lambda *args, **kwargs: None)

    assert vault_io.read_vault_text(path) == "available"


def test_non_self_deadlock_is_not_masked(
    vault_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = vault_tmp / "sources" / "placeholder.md"
    deadlock = _deadlock_error()

    def fail_read(self: Path, **kwargs: str) -> str:
        raise deadlock

    monkeypatch.setattr(vault_io, "vault_is_icloud", lambda: True)
    monkeypatch.setattr(Path, "read_text", fail_read)

    with pytest.raises(OSError) as exc_info:
        vault_io.read_vault_text(path)

    assert exc_info.value is deadlock


def test_write_vault_text_atomically_replaces_icloud_placeholder(
    vault_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = vault_tmp / "sources" / "placeholder.md"
    writes = 0
    original_write_text = Path.write_text

    def fail_placeholder_write(self: Path, content: str, **kwargs: str) -> int:
        nonlocal writes
        if self == path:
            writes += 1
            raise _deadlock_error()
        return original_write_text(self, content, **kwargs)

    monkeypatch.setattr(vault_io, "vault_is_icloud", lambda: True)
    monkeypatch.setattr(Path, "write_text", fail_placeholder_write)

    assert vault_io.write_vault_text(path, "hydrated") == 8
    assert path.read_text() == "hydrated"
    assert writes == 2


def test_self_write_refreshes_cache(
    vault_tmp: Path,
    data_tmp: Path,
) -> None:
    path = vault_tmp / "_self" / "style.md"
    path.parent.mkdir()

    assert vault_io.write_vault_text(path, "Plainspoken.") == 12
    assert (data_tmp / "cache" / "vault-self" / "style.md").read_text() == "Plainspoken."


def test_read_vault_text_does_not_mask_unrelated_errors(
    vault_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = vault_tmp / "_self" / "identity.md"
    denied = PermissionError(errno.EACCES, "Permission denied")

    def fail_read(self: Path, **kwargs: str) -> str:
        raise denied

    monkeypatch.setattr(vault_io, "vault_is_icloud", lambda: True)
    monkeypatch.setattr(Path, "read_text", fail_read)

    with pytest.raises(PermissionError) as exc_info:
        vault_io.read_vault_text(path)

    assert exc_info.value is denied


def test_read_vault_text_does_not_handle_local_vault_error(
    vault_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = vault_tmp / "_self" / "identity.md"
    deadlock = _deadlock_error()

    def fail_read(self: Path, **kwargs: str) -> str:
        raise deadlock

    monkeypatch.setattr(vault_io, "vault_is_icloud", lambda: False)
    monkeypatch.setattr(Path, "read_text", fail_read)

    with pytest.raises(OSError) as exc_info:
        vault_io.read_vault_text(path)

    assert exc_info.value is deadlock


def test_read_vault_text_does_not_handle_outside_vault(
    vault_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = vault_tmp.parent / "outside.md"
    deadlock = _deadlock_error()

    def fail_read(self: Path, **kwargs: str) -> str:
        raise deadlock

    monkeypatch.setattr(vault_io, "vault_is_icloud", lambda: True)
    monkeypatch.setattr(Path, "read_text", fail_read)

    with pytest.raises(OSError) as exc_info:
        vault_io.read_vault_text(path)

    assert exc_info.value is deadlock


def test_agent_identity_load_recovers_from_cached_self_file(
    vault_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mastisk.agents.base import Agent

    path = vault_tmp / "_self" / "identity.md"
    path.parent.mkdir()
    path.write_text("# Identity\n\nSushil")
    vault_io.read_vault_text(path)
    original_read_text = Path.read_text

    def fail_identity_read(self: Path, **kwargs: str) -> str:
        if self == path:
            raise _deadlock_error()
        return original_read_text(self, **kwargs)

    monkeypatch.setattr(vault_io, "vault_is_icloud", lambda: True)
    monkeypatch.setattr(Path, "read_text", fail_identity_read)

    assert "Sushil" in Agent.load_identity()


def test_sidebar_user_info_recovers_from_cached_self_file(
    db,
    vault_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mastisk.db import queries as q

    path = vault_tmp / "_self" / "identity.md"
    path.parent.mkdir()
    path.write_text("## Role\n- Sushil — engineer\n")
    vault_io.read_vault_text(path)
    original_read_text = Path.read_text

    def fail_identity_read(self: Path, **kwargs: str) -> str:
        if self == path:
            raise _deadlock_error()
        return original_read_text(self, **kwargs)

    monkeypatch.setattr(vault_io, "vault_is_icloud", lambda: True)
    monkeypatch.setattr(Path, "read_text", fail_identity_read)

    assert q.user_info(db)["name"] == "Sushil"


def test_compiler_mirror_uses_resilient_vault_writer(
    db,
    vault_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mastisk.agents import compiler as compiler_module

    writes: list[tuple[Path, str]] = []

    def fake_write(path: Path, content: str) -> int:
        writes.append((path, content))
        return len(content)

    monkeypatch.setattr(compiler_module, "write_vault_text", fake_write)
    monkeypatch.setattr(compiler_module.wiki_suggestions, "render_vault_file", lambda: None)

    compiler_module.Compiler()._persist_article(
        {
            "id": "icloud-write-test",
            "kind": "Source",
            "title": "iCloud write test",
            "sections": [{"h": "TL;DR", "body": "content"}],
        },
        source_id=None,
    )

    assert writes[0][0] == vault_tmp / "sources" / "icloud-write-test.md"
    assert "# iCloud write test" in writes[0][1]
