"""Tests for the HuggingFace speaker-ID wizard (Phase 6 Wave 3 — Track 6C).

HELIOS.md §15.5. Five steps:
1. HF account creation (passive link).
2. Token generation (passive link).
3. License acceptance for 3 models (checkboxes).
4. Token validation (huggingface_hub.whoami — mocked).
5. Model download trigger (keyring + reload-component — mocked).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aegis.db.engine import get_session
from aegis.main import app


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
    p = tmp_path / "capture.toml"
    p.write_text(
        '[api]\nport = 3031\nbearer_token = "x"\n'
        '[diarization]\nenabled = false\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "aegis.web.routes.helios._capture_toml_path", lambda: p
    )
    return p


# ── Wizard ───────────────────────────────────────────────


class TestWizardSteps:
    def test_step1_renders(self, client, toml_path):
        resp = client.get("/helios/settings/diarization/step/1")
        assert resp.status_code == 200
        assert "Step 1 of 5" in resp.text
        assert "huggingface.co/join" in resp.text

    def test_step1_continue_advances_to_step2(self, client, toml_path):
        resp = client.post("/helios/settings/diarization/step1")
        assert resp.status_code == 200
        assert "Step 2 of 5" in resp.text
        # Setup progress persisted.
        import tomllib

        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        assert (
            data["diarization"]["setup_progress"]["step1_completed"] is True
        )

    def test_step2_continue_advances_to_step3(self, client, toml_path):
        resp = client.post("/helios/settings/diarization/step2")
        assert resp.status_code == 200
        assert "Step 3 of 5" in resp.text
        # Step 3 lists all three models.
        assert "pyannote/segmentation-3.0" in resp.text
        assert "pyannote/wespeaker-voxceleb-resnet34-LM" in resp.text
        assert "pyannote/speaker-diarization-3.1" in resp.text

    def test_step3_requires_all_licenses(self, client, toml_path):
        # Only one model checked — should re-render step 3 with error.
        resp = client.post(
            "/helios/settings/diarization/step3",
            data={"accept.pyannote/segmentation-3.0": "1"},
        )
        assert resp.status_code == 200
        assert "Step 3 of 5" in resp.text
        assert "all three model licenses" in resp.text

    def test_step3_all_licenses_advances_to_step4(self, client, toml_path):
        resp = client.post(
            "/helios/settings/diarization/step3",
            data={
                "accept.pyannote/segmentation-3.0": "1",
                "accept.pyannote/wespeaker-voxceleb-resnet34-LM": "1",
                "accept.pyannote/speaker-diarization-3.1": "1",
            },
        )
        assert resp.status_code == 200
        assert "Step 4 of 5" in resp.text
        # State updated.
        import tomllib

        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        sp = data["diarization"]["setup_progress"]
        assert sp["step3_completed"] is True
        assert len(sp["licenses_accepted"]) == 3


class TestWizardTokenValidation:
    def test_empty_token_rejected(self, client, toml_path):
        resp = client.post(
            "/helios/settings/diarization/validate-token",
            data={"hf_token": ""},
        )
        assert resp.status_code == 200
        assert "Paste a token first" in resp.text

    def test_valid_token_succeeds(self, client, toml_path):
        # Mock the validate_hf_token helper directly.
        with patch(
            "aegis.web.routes.helios.validate_hf_token",
            AsyncMock(return_value=(True, "test-user")),
        ):
            resp = client.post(
                "/helios/settings/diarization/validate-token",
                data={"hf_token": "hf_abc123"},
            )
        assert resp.status_code == 200
        body = resp.text
        assert "Token valid" in body
        assert "test-user" in body
        assert "Continue to download" in body

    def test_invalid_token_shows_error(self, client, toml_path):
        with patch(
            "aegis.web.routes.helios.validate_hf_token",
            AsyncMock(return_value=(False, "401 Unauthorized")),
        ):
            resp = client.post(
                "/helios/settings/diarization/validate-token",
                data={"hf_token": "bad_token"},
            )
        assert resp.status_code == 200
        body = resp.text
        assert "Token validation failed" in body
        assert "401 Unauthorized" in body
        assert "Continue to download" not in body

    def test_token_validation_helper_uses_whoami(self):
        """The helper must call huggingface_hub.whoami(token=...) in a thread."""
        import sys
        from unittest.mock import MagicMock as _MagicMock

        from aegis.web.routes._helios_settings_helpers import validate_hf_token

        fake_hub = _MagicMock()
        fake_hub.whoami = _MagicMock(return_value={"name": "alice"})
        sys.modules["huggingface_hub"] = fake_hub
        try:
            import asyncio

            ok, detail = asyncio.run(validate_hf_token("hf_xyz"))
        finally:
            sys.modules.pop("huggingface_hub", None)
        assert ok is True
        assert detail == "alice"
        fake_hub.whoami.assert_called_once_with(token="hf_xyz")


class TestWizardDownloadModels:
    def test_download_step_persists_token_in_keyring(
        self, client, toml_path, stub_client
    ):
        """Step 5 must call keyring.set_password and flip diarization.enabled."""
        # Track 6D ``ReloadComponentResponse`` shape: ok=True.
        stub_client.reload_component = AsyncMock(
            return_value={"component": "diarization", "ok": True,
                          "detail": "scheduled"}
        )

        # Patch the helper used by the route (re-bound).
        with patch(
            "aegis.web.routes._helios_settings_helpers.store_hf_token_in_keychain",
            return_value=True,
        ) as keyring_stub:
            resp = client.post(
                "/helios/settings/diarization/download-models",
                data={"hf_token": "hf_abc123"},
            )
        assert resp.status_code == 200
        assert "Speaker identification enabled" in resp.text
        keyring_stub.assert_called_once_with("hf_abc123")
        # TOML now has diarization.enabled = true.
        import tomllib

        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        assert data["diarization"]["enabled"] is True
        assert data["diarization"]["hf_token_location"] == "keychain"
        # And reload-component was called with "diarization".
        stub_client.reload_component.assert_awaited_once_with("diarization")

    def test_download_step_handles_keyring_failure(
        self, client, toml_path
    ):
        with patch(
            "aegis.web.routes._helios_settings_helpers.store_hf_token_in_keychain",
            return_value=False,
        ):
            resp = client.post(
                "/helios/settings/diarization/download-models",
                data={"hf_token": "hf_abc123"},
            )
        assert resp.status_code == 200
        assert "Could not store token" in resp.text
        # And the TOML wasn't mutated.
        import tomllib

        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        assert data["diarization"]["enabled"] is False

    def test_download_step_handles_daemon_unreachable(
        self, client, toml_path, stub_client
    ):
        stub_client.reload_component = AsyncMock(return_value=None)
        with patch(
            "aegis.web.routes._helios_settings_helpers.store_hf_token_in_keychain",
            return_value=True,
        ):
            resp = client.post(
                "/helios/settings/diarization/download-models",
                data={"hf_token": "hf_abc123"},
            )
        assert resp.status_code == 200
        body = resp.text
        # Still reports success — token is saved, config is set, daemon
        # picks up on next start.
        assert "Token saved" in body or "✓" in body

    def test_keyring_helper_stores_under_helios_service(self):
        """Helper must write the HF token where the daemon reads it.

        The daemon's ``helios/keychain.py`` calls
        ``keyring.get_password("helios", "huggingface")``. The wizard
        previously wrote to ``("aegis-helios", "hf_token")`` — a real
        bug surfaced during the §12.7 smoke that left diarization
        silently in ``token_missing`` after a "successful" wizard run.
        """
        import sys
        from unittest.mock import MagicMock as _MagicMock

        from aegis.web.routes._helios_settings_helpers import (
            store_hf_token_in_keychain,
        )

        fake = _MagicMock()
        sys.modules["keyring"] = fake
        try:
            ok = store_hf_token_in_keychain("tok_123")
        finally:
            sys.modules.pop("keyring", None)
        assert ok is True
        fake.set_password.assert_called_once_with(
            "helios", "huggingface", "tok_123"
        )

    def test_download_step_surfaces_reload_failure(
        self, client, toml_path, stub_client
    ):
        """Warning #5 — wizard step 5 must check ``ok`` from
        ReloadComponentResponse, not the legacy ``status`` field. When
        the daemon returns ``ok=False`` the result partial should NOT
        claim success.
        """
        stub_client.reload_component = AsyncMock(return_value={
            "component": "diarization",
            "ok": False,
            "detail": "model download failed",
        })
        with patch(
            "aegis.web.routes._helios_settings_helpers.store_hf_token_in_keychain",
            return_value=True,
        ):
            resp = client.post(
                "/helios/settings/diarization/download-models",
                data={"hf_token": "hf_abc123"},
            )
        assert resp.status_code == 200
        body = resp.text
        # Error pill / message surfaced (not the success "Speaker
        # identification enabled" branch).
        assert "Speaker identification enabled" not in body
        assert "model download failed" in body
