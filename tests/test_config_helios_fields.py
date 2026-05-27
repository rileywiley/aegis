"""Tests for Helios-related Settings fields (Phase 3 Track 3F.5).

Verifies that the new Helios connection settings expose correct defaults,
honor environment-variable overrides, and keep `helios_token_path` as a
plain string (consumers handle `Path(...).expanduser()` themselves).

Also confirms deprecated Screenpipe fields are still functional during
the Phase 3 → Phase 4 transition window.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aegis.config import Settings


# ── Defaults ────────────────────────────────────────────────


def test_helios_defaults_match_spec() -> None:
    """HELIOS.md §16.7 defaults must be exposed verbatim."""
    s = Settings()
    assert s.helios_url == "http://127.0.0.1:3031"
    assert s.helios_token_path == "~/.aegis/capture.toml"
    assert s.helios_heartbeat_seconds == 60
    assert s.helios_heartbeat_timeout_seconds == 5


def test_helios_token_path_is_unexpanded_string() -> None:
    """Token path is stored as a raw string with `~` intact.

    Consumers (HeliosClient, heartbeat loop) must call
    `Path(s).expanduser()` themselves. We do NOT pre-expand in Settings
    because that would bake the build-time HOME into the value.
    """
    s = Settings()
    assert isinstance(s.helios_token_path, str)
    assert s.helios_token_path.startswith("~/")
    # Sanity: expanding it produces an absolute path under the user's home.
    expanded = Path(s.helios_token_path).expanduser()
    assert expanded.is_absolute()
    assert str(expanded).endswith("/.aegis/capture.toml")


# ── Env-var overrides ───────────────────────────────────────


def test_helios_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIOS_URL", "http://example:3031")
    s = Settings()
    assert s.helios_url == "http://example:3031"


def test_helios_token_path_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIOS_TOKEN_PATH", "/tmp/custom_capture.toml")
    s = Settings()
    assert s.helios_token_path == "/tmp/custom_capture.toml"


def test_helios_heartbeat_seconds_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIOS_HEARTBEAT_SECONDS", "120")
    s = Settings()
    assert s.helios_heartbeat_seconds == 120


def test_helios_heartbeat_timeout_seconds_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIOS_HEARTBEAT_TIMEOUT_SECONDS", "10")
    s = Settings()
    assert s.helios_heartbeat_timeout_seconds == 10


# ── Deprecated Screenpipe fields still functional ──────────


def test_deprecated_screenpipe_fields_still_load_defaults() -> None:
    """Phase 3 keeps the deprecated fields so existing callers don't break.
    They will be removed in Phase 4."""
    s = Settings()
    assert s.screenpipe_url == "http://localhost:3030"
    assert s.polling_screenpipe_seconds == 300


def test_deprecated_screenpipe_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCREENPIPE_URL", "http://legacy:3030")
    s = Settings()
    assert s.screenpipe_url == "http://legacy:3030"
