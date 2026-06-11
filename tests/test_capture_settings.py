"""Tests for capture subsystem config."""
from __future__ import annotations


def test_capture_settings_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("MASTISK_HOME", str(tmp_path))
    from mastisk import paths

    paths.data_dir.cache_clear()
    from mastisk.settings import reload_settings

    s = reload_settings()
    assert s.capture.bearer_token is None
    assert s.capture.default_timezone


def test_capture_token_read_from_toml(tmp_path, monkeypatch):
    """A [capture] bearer_token in config.toml is loaded."""
    monkeypatch.setenv("MASTISK_HOME", str(tmp_path))
    from mastisk import paths

    paths.data_dir.cache_clear()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[capture]\nbearer_token = "abc123"\n')
    from mastisk.settings import reload_settings

    s = reload_settings()
    assert s.capture.bearer_token == "abc123"
