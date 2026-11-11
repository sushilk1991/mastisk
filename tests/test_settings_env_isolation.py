"""Nested config sections must not read unprefixed environment variables.

Only the top-level ``Settings`` is a pydantic-settings class. When each
section was one too, generic variables such as ``PAT``, ``BACKEND`` or
``ENABLED`` in the daemon's environment silently overrode config.toml.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError


@pytest.fixture
def stray_env(monkeypatch):
    monkeypatch.setenv("PAT", "env-pat")
    monkeypatch.setenv("BACKEND", "env-backend")
    monkeypatch.setenv("SCOUT", "7")
    monkeypatch.setenv("ENABLED", "false")


def test_nested_sections_ignore_unprefixed_env_defaults(data_tmp, stray_env):
    from mastisk.settings import reload_settings

    s = reload_settings()
    assert s.github.pat == ""
    assert s.notify.backend == "none"
    assert s.budget.scout == 500
    assert s.automations.enabled is True


def test_nested_sections_keep_toml_values_despite_env(data_tmp, stray_env):
    from mastisk.settings import reload_settings

    (data_tmp / "config.toml").write_text(
        '[github]\npat = "toml-pat"\n\n[budget]\nscout = 3\n\n[automations]\nenabled = true\n'
    )
    s = reload_settings()
    assert s.github.pat == "toml-pat"
    assert s.budget.scout == 3
    assert s.automations.enabled is True


def test_top_level_env_alias_still_applies(data_tmp, monkeypatch):
    from mastisk.settings import reload_settings

    monkeypatch.setenv("MASTISK_PORT", "6001")
    assert reload_settings().port == 6001


def test_unknown_key_handling_is_unchanged(data_tmp):
    """Unknown top-level keys are ignored; unknown keys inside a section are rejected."""
    from mastisk.settings import reload_settings

    (data_tmp / "config.toml").write_text('mystery = 1\n\n[github]\npat = "x"\n')
    assert reload_settings().github.pat == "x"

    (data_tmp / "config.toml").write_text("[github]\nmystery = 1\n")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        reload_settings()
