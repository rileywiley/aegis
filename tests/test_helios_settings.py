"""Tests for the Helios Settings page (Phase 6 Wave 3 — Track 6C).

Covers:

* GET /helios/settings renders with values pulled from capture.toml.
* POST /helios/settings writes TOML correctly (no real ~/.aegis touched —
  monkeypatched into ``tmp_path``).
* Pydantic validation rejects out-of-range values.
* Restart-required vs hot-reloadable classification surfaces in the
  response banner.
* Hotkey toggle without Accessibility shows the deep-link state.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aegis.db.engine import get_session
from aegis.main import app
from aegis.web.routes import _helios_settings_helpers as hsh


# ── Fixtures ───────────────────────────────────────────────


def _stub_helios_client() -> MagicMock:
    stub = MagicMock(name="HeliosClient")
    for name in (
        "health_check", "get_status", "get_diagnostics", "get_permissions",
        "get_session", "get_session_transcript", "get_ocr", "list_audio",
        "list_sessions", "delete_session", "restart_daemon",
        "flush_queues", "test_capture", "reload_component",
        "create_diagnostics_bundle", "re_transcribe_session",
        "re_diarize_session",
    ):
        setattr(stub, name, AsyncMock(return_value=None))
    stub.health_check = AsyncMock(return_value=True)
    return stub


@pytest.fixture
def stub_client():
    return _stub_helios_client()


@pytest.fixture
def client(stub_client):
    async def _fake_session():
        yield AsyncMock()

    app.dependency_overrides[get_session] = _fake_session
    try:
        with TestClient(app) as c:
            app.state.helios_client = stub_client
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def toml_path(tmp_path: Path, monkeypatch):
    """Redirect ``_capture_toml_path`` into a tmp file with a starter TOML."""
    p = tmp_path / "capture.toml"
    p.write_text(
        '[api]\nport = 3031\nbearer_token = "x"\n'
        '[capture]\ncalendar_pre_start_seconds = 60\n'
        'continuous_prompt_hours = 4.0\n'
        '[exclusion]\nkeywords = ["confidential", "HR"]\n'
        '[voice_note]\nenabled = true\nmax_duration_seconds = 300\n'
        'auto_save_timeout_seconds = 10\nhotkey_enabled = false\n'
        '[retention]\nraw_audio_days = 7\n'
        '[ocr]\nmeeting_apps = ["com.microsoft.teams2"]\n'
        'gate_by_allowlist = false\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "aegis.web.routes.helios._capture_toml_path", lambda: p
    )
    return p


# ── GET /helios/settings ───────────────────────────────────


class TestSettingsGet:
    def test_renders_with_current_toml_values(self, client, toml_path):
        resp = client.get("/helios/settings")
        assert resp.status_code == 200
        body = resp.text
        # Section labels
        for section in (
            "Capture mode", "Meeting exclusion keywords",
            "Meeting apps for OCR", "Retention policy",
            "Transcription", "Voice notes",
            "Speaker identification", "Advanced",
        ):
            assert section in body
        # Values from the TOML show up
        assert 'value="60"' in body          # calendar_pre_start_seconds
        assert "confidential" in body        # exclusion keyword chip
        assert "com.microsoft.teams2" in body  # OCR app chip
        # Form posts back to the same URL
        assert 'hx-post="/helios/settings"' in body
        # Wizard placeholder loads on demand
        assert "/helios/settings/diarization/step/" in body


# ── POST /helios/settings ───────────────────────────────────


class TestSettingsPost:
    def test_writes_toml_with_changed_values(self, client, toml_path):
        resp = client.post(
            "/helios/settings",
            data={
                "capture.calendar_pre_start_seconds": "120",
                "voice_note.max_duration_seconds": "600",
            },
        )
        assert resp.status_code == 200
        # Banner says it saved.
        assert "Saved" in resp.text
        # File on disk reflects the new values.
        import tomllib

        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        assert data["capture"]["calendar_pre_start_seconds"] == 120
        assert data["voice_note"]["max_duration_seconds"] == 600

    def test_chip_list_overwrites_keywords(self, client, toml_path):
        # Submit a totally different exclusion keyword list. httpx's
        # TestClient form-encoder requires the dict-with-list shape to
        # produce duplicate keys in the request body.
        resp = client.post(
            "/helios/settings",
            data={"exclusion.keywords[]": ["compensation", "salary"]},
        )
        assert resp.status_code == 200
        import tomllib

        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        assert data["exclusion"]["keywords"] == ["compensation", "salary"]

    def test_rejects_out_of_range(self, client, toml_path):
        # max_duration_seconds capped at 1800; 2000 must fail validation.
        resp = client.post(
            "/helios/settings",
            data={"voice_note.max_duration_seconds": "2000"},
        )
        assert resp.status_code == 200
        assert "Validation failed" in resp.text
        # File on disk must not have been mutated.
        import tomllib

        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        assert data["voice_note"]["max_duration_seconds"] == 300

    def test_restart_required_surfaces_in_banner(self, client, toml_path):
        resp = client.post(
            "/helios/settings",
            data={"api.port": "3099"},  # api.port is restart-required
        )
        assert resp.status_code == 200
        body = resp.text
        assert "Restart Daemon" in body
        assert "api.port" in body

    def test_hot_reloadable_does_not_prompt_restart(
        self, client, toml_path
    ):
        resp = client.post(
            "/helios/settings",
            data={"retention.raw_audio_days": "14"},
        )
        assert resp.status_code == 200
        # Banner doesn't include a restart button.
        assert "Restart Daemon" not in resp.text


# ── Hotkey permission flow ─────────────────────────────────


class TestHotkeyPermission:
    def test_not_granted_shows_deep_link(self, client, toml_path):
        # check_accessibility_granted defaults to False per spec.
        resp = client.post("/helios/settings/hotkey-permission-check")
        assert resp.status_code == 200
        body = resp.text
        assert "Accessibility permission required" in body
        # The macOS deep-link URL
        assert (
            "x-apple.systempreferences:com.apple.preference.security?"
            "Privacy_Accessibility"
        ) in body

    def test_granted_shows_success(self, client, toml_path):
        with patch(
            "aegis.web.routes._helios_settings_helpers.check_accessibility_granted",
            AsyncMock(return_value=True),
        ):
            # Patch the local re-bind in helios.py too:
            with patch(
                "aegis.web.routes.helios.check_accessibility_granted",
                AsyncMock(return_value=True),
            ):
                resp = client.post(
                    "/helios/settings/hotkey-permission-check"
                )
        assert resp.status_code == 200
        assert "Accessibility granted" in resp.text


# ── Lower-level helper tests ────────────────────────────────


class TestHelpers:
    def test_hot_reloadable_label_function(self):
        assert hsh.field_label("retention", "raw_audio_days") == hsh.HOT_RELOADABLE_LABEL
        assert hsh.field_label("api", "port") == hsh.RESTART_REQUIRED_LABEL

    def test_apply_form_updates_coerces_types(self, tmp_path):
        current = {"capture": {"calendar_pre_start_seconds": 60}}
        new, changed = hsh.apply_form_updates(
            current,
            {"capture.calendar_pre_start_seconds": "90"},
        )
        assert new["capture"]["calendar_pre_start_seconds"] == 90
        assert changed == [("capture", "calendar_pre_start_seconds")]

    def test_apply_form_updates_preserves_unrelated_sections(self):
        current = {
            "capture": {"calendar_pre_start_seconds": 60},
            "logging": {"level": "info"},
        }
        new, _ = hsh.apply_form_updates(
            current, {"capture.calendar_pre_start_seconds": "120"}
        )
        # The logging section was untouched.
        assert new["logging"] == {"level": "info"}

    def test_apply_form_updates_ignores_unknown_fields(self):
        new, changed = hsh.apply_form_updates(
            {}, {"unknown.field": "value"}
        )
        assert changed == []
        assert "unknown" not in new

    def test_write_capture_toml_is_atomic(self, tmp_path):
        path = tmp_path / "out.toml"
        hsh.write_capture_toml(path, {"api": {"port": 3031}})
        assert path.exists()
        # 0600
        assert oct(path.stat().st_mode)[-3:] == "600"
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8"))
        assert data["api"]["port"] == 3031
        # No leftover tmp files.
        leftovers = [
            p for p in tmp_path.iterdir()
            if p.name != "out.toml" and not p.name.startswith(".")
        ]
        assert leftovers == [], f"unexpected leftovers: {leftovers}"

    def test_split_changes_by_reload(self):
        # Warning #1 (Wave 4): voice_note.* and capture.* are NOT
        # hot-reloadable per HELIOS.md §5.4 — only the enumerated set
        # (exclusion, ocr, retention, notifications, logging) applies
        # immediately. Everything else requires a daemon restart.
        hot, cold = hsh.split_changes_by_reload([
            ("retention", "raw_audio_days"),
            ("api", "port"),
            ("voice_note", "max_duration_seconds"),
            ("capture", "calendar_pre_start_seconds"),
            ("logging", "level"),
        ])
        assert ("retention", "raw_audio_days") in hot
        assert ("logging", "level") in hot
        assert ("voice_note", "max_duration_seconds") in cold
        assert ("capture", "calendar_pre_start_seconds") in cold
        assert ("api", "port") in cold

    @pytest.mark.asyncio
    async def test_check_accessibility_returns_false_by_default(self):
        # Stub today — see helpers module docstring.
        assert await hsh.check_accessibility_granted() is False

    def test_apply_form_updates_blocks_bearer_token(self):
        """Warning #6 — generic POSTs must NOT overwrite api.bearer_token."""
        current = {"api": {"port": 3031, "bearer_token": "secret-original"}}
        form = {
            "api.bearer_token": "attacker-controlled-value",
            "api.port": "3032",
        }
        new, changed = hsh.apply_form_updates(current, form)
        # Port change went through.
        assert ("api", "port") in changed
        assert new["api"]["port"] == 3032
        # Bearer token did NOT change.
        assert ("api", "bearer_token") not in changed
        assert new["api"]["bearer_token"] == "secret-original"

    def test_apply_form_updates_allows_bearer_with_flag(self):
        """A future Regenerate flow MAY pass ``allow_bearer_overwrite``."""
        current = {"api": {"bearer_token": "old"}}
        form = {"api.bearer_token": "new"}
        new, changed = hsh.apply_form_updates(
            current, form, allow_bearer_overwrite=True
        )
        assert ("api", "bearer_token") in changed
        assert new["api"]["bearer_token"] == "new"
