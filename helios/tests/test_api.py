"""Endpoint coverage tests for the full Phase-2 HTTP API (Track 2E.2).

Mirrors HELIOS.md §7.3:

* every endpoint except ``/v1/health`` requires bearer auth (401
  without a token, 401 with a wrong token);
* validation errors return the §7.2 envelope with ``error =
  "validation_error"`` and ``code = 422``;
* error responses always carry the flat ``{"error", "detail", "code"}``
  shape.

These tests exercise the routes through ``httpx.ASGITransport`` so the
real lifespan + state assembly run. The replay source factory keeps
audio capture deterministic and side-effect free.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from helios.api import create_app
from tests.conftest import synth_wav

_AUTH = {"Authorization": "Bearer test"}
_BAD_AUTH = {"Authorization": "Bearer not-the-right-token"}


class _LifespanContext:
    """Minimal stand-in for asgi-lifespan's LifespanManager."""

    def __init__(self, app):
        self._app = app

    async def __aenter__(self):
        self._scope = {"type": "lifespan"}
        self._send_q: asyncio.Queue = asyncio.Queue()
        self._recv_q: asyncio.Queue = asyncio.Queue()

        async def receive():
            return await self._recv_q.get()

        async def send(message):
            await self._send_q.put(message)

        self._app_task = asyncio.create_task(
            self._app(self._scope, receive, send)
        )
        await self._recv_q.put({"type": "lifespan.startup"})
        msg = await self._send_q.get()
        if msg["type"] == "lifespan.startup.failed":
            raise RuntimeError(f"lifespan startup failed: {msg.get('message')}")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._recv_q.put({"type": "lifespan.shutdown"})
        await self._send_q.get()
        await self._app_task


@pytest.fixture
def replay_env(monkeypatch, tmp_path):
    """HELIOS_REPLAY=1 + tmp config + bearer_token='test'."""
    mic = synth_wav(tmp_path / "fx" / "mic.wav", duration_s=10.0, freq_hz=440.0)
    sysw = synth_wav(tmp_path / "fx" / "sys.wav", duration_s=10.0, freq_hz=880.0)
    cal = tmp_path / "fx" / "cal.json"
    cal.write_text("[]")

    monkeypatch.setenv("HELIOS_REPLAY", "1")
    monkeypatch.setenv("HELIOS_REPLAY_MIC_FIXTURE", str(mic))
    monkeypatch.setenv("HELIOS_REPLAY_SYSTEM_FIXTURE", str(sysw))
    monkeypatch.setenv("HELIOS_REPLAY_CAL_FIXTURE", str(cal))
    monkeypatch.setenv("HELIOS_REPLAY_SPEED", "20.0")

    config_path = tmp_path / "capture.toml"
    storage_root = tmp_path / "storage"
    config_path.write_text(
        "[storage]\n"
        f'root = "{storage_root}"\n'
        "[api]\n"
        'bearer_token = "test"\n'
    )

    from helios import config as helios_config

    monkeypatch.setattr(helios_config, "_CONFIG_PATH", config_path)
    monkeypatch.setattr(helios_config, "_cached_config", None)
    return storage_root


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# /v1/health  — public
# ---------------------------------------------------------------------------


async def test_health_no_auth_required(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get("/v1/health")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ok"
            assert "version" in body
            assert "uptime_seconds" in body


async def test_openapi_schema_disabled(replay_env):
    """``/openapi.json`` must 404 — only /v1/health is unauthenticated.

    Wave-4 Warning: leaving the OpenAPI dump on leaks the entire endpoint
    surface (including parameter names) to anyone hitting loopback.
    """
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get("/openapi.json")
            assert resp.status_code == 404
            resp = await client.get("/openapi.json", headers=_AUTH)
            assert resp.status_code == 404


def test_aegis_base_url_raises_on_empty():
    """Empty / host-less URLs raise so misconfig surfaces at startup."""
    from helios.api import _aegis_base_url

    import pytest as _pytest

    with _pytest.raises(ValueError):
        _aegis_base_url("")
    with _pytest.raises(ValueError):
        _aegis_base_url("http://")
    # Sanity: a real URL still passes.
    assert _aegis_base_url("http://localhost:8000/api/meetings/upcoming") == (
        "http://localhost:8000"
    )
    # Bare hostname (no scheme, no port) gets http:// prefix and parses.
    assert _aegis_base_url("aegis.local/api/x") == "http://aegis.local"


# ---------------------------------------------------------------------------
# Auth — every authed endpoint rejects missing + wrong tokens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/v1/status", None),
        ("GET", "/v1/permissions", None),
        ("GET", "/v1/diagnostics", None),
        ("POST", "/v1/capture/start", {"kind": "continuous"}),
        ("POST", "/v1/capture/stop", None),
        ("POST", "/v1/capture/pause-until", {"until_ts": 1.0}),
        ("POST", "/v1/capture/resume", None),
        ("POST", "/v1/capture/enable-screen-override", {"duration_minutes": 5}),
        ("POST", "/v1/capture/prompt-response", {"continue": True}),
        ("GET", "/v1/sessions", None),
        ("GET", "/v1/sessions/1", None),
        ("GET", "/v1/sessions/1/transcript", None),
        ("POST", "/v1/sessions/1/re-transcribe", None),
        ("POST", "/v1/sessions/1/re-diarize", None),
        ("DELETE", "/v1/sessions/1", None),
        ("GET", "/v1/audio", None),
        ("GET", "/v1/ocr", None),
        ("POST", "/v1/diagnostics/restart", None),
        ("POST", "/v1/diagnostics/flush-queues", None),
        ("POST", "/v1/diagnostics/test-capture", None),
        ("POST", "/v1/diagnostics/reload-component", {"component": "transcription"}),
        ("POST", "/v1/voice-note/start", {"triggered_by": "menu_bar"}),
        ("POST", "/v1/voice-note/stop", None),
        ("POST", "/v1/voice-note/cancel", None),
        ("GET", "/v1/voice-note/active", None),
    ],
)
async def test_endpoints_require_auth(replay_env, method, path, body):
    """Every authed endpoint returns 401 without a bearer token."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.request(method, path, json=body)
            assert resp.status_code == 401, f"{method} {path}: {resp.status_code} {resp.text}"
            envelope = resp.json()
            assert envelope["error"] == "unauthorized"
            assert envelope["code"] == 401


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/v1/status", None),
        ("POST", "/v1/capture/stop", None),
    ],
)
async def test_endpoints_reject_wrong_token(replay_env, method, path, body):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.request(method, path, json=body, headers=_BAD_AUTH)
            assert resp.status_code == 401, resp.text
            envelope = resp.json()
            assert envelope["error"] == "unauthorized"


# ---------------------------------------------------------------------------
# /v1/status  — happy path + shape
# ---------------------------------------------------------------------------


async def test_status_returns_full_shape(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get("/v1/status", headers=_AUTH)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["daemon"] == "running"
            assert body["mode"] in {"armed", "recording", "paused", "error", "not_running"}
            # HELIOS.md §7.3 nested fields.
            assert "components" in body
            assert isinstance(body["components"], dict)
            assert "active_session" in body  # null when nothing running
            assert body["active_session"] is None
            assert "next_calendar_event" in body
            # Backwards-compat legacy field.
            assert "active_session_id" in body
            assert "paused_until" in body
            assert isinstance(body["aegis_unreachable"], bool)
            assert isinstance(body["component_errors"], dict)
            assert isinstance(body["queue"], dict)
            assert "scheduler_running" in body
            assert "permissions" in body
            assert {"mic_granted", "screen_recording_granted", "last_checked_at"} <= set(
                body["permissions"]
            )


async def test_status_active_session_populated_after_start(replay_env):
    """Per HELIOS.md §7.3 active_session = {id, kind, calendar_event_ids,
    started_at, screen_capture_override_until}."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            sid = r1.json()["session_id"]

            resp = await client.get("/v1/status", headers=_AUTH)
            body = resp.json()
            active = body["active_session"]
            assert active is not None
            assert active["id"] == sid
            assert active["kind"] == "continuous"
            assert active["calendar_event_ids"] == []
            assert isinstance(active["started_at"], (int, float))
            assert active["screen_capture_override_until"] is None

            await client.post("/v1/capture/stop", headers=_AUTH)


# ---------------------------------------------------------------------------
# /v1/permissions
# ---------------------------------------------------------------------------


async def test_permissions_no_data_returns_defaults(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get("/v1/permissions", headers=_AUTH)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert "mic_granted" in body
            assert "screen_recording_granted" in body
            assert "last_checked_at" in body


# ---------------------------------------------------------------------------
# /v1/diagnostics
# ---------------------------------------------------------------------------


async def test_diagnostics_returns_full_shape(replay_env):
    """Wire shape per HELIOS.md §7.3 ``GET /v1/diagnostics``."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get("/v1/diagnostics", headers=_AUTH)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            for key in (
                "version",
                "pid",
                "uptime_seconds",
                "memory_mb",
                "cpu_5min_avg_pct",
                "permissions",
                "active_session",
                "next_event",
                "last_chunks",
                "transcription_throughput_realtime_multiple",
                "active_session_id",
                "queues",
                "storage",
                "component_status",
                "recent_events",
            ):
                assert key in body, f"missing key {key!r}"
            assert isinstance(body["component_status"], list)
            assert isinstance(body["recent_events"], list)
            # last_chunks has mic + system slots.
            assert "mic" in body["last_chunks"]
            assert "system" in body["last_chunks"]


# ---------------------------------------------------------------------------
# /v1/capture/start  — validation
# ---------------------------------------------------------------------------


async def test_start_with_continuous_kind(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["kind"] == "continuous"

            await client.post("/v1/capture/stop", headers=_AUTH)


async def test_start_manual_screen_requires_duration(replay_env):
    """kind=manual_screen without duration_minutes ⇒ 422 validation_error."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/capture/start",
                json={"kind": "manual_screen"},
                headers=_AUTH,
            )
            assert resp.status_code == 422, resp.text
            body = resp.json()
            assert body["error"] == "validation_error"
            assert body["code"] == 422


async def test_start_manual_screen_with_duration_succeeds(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/capture/start",
                json={"kind": "manual_screen", "duration_minutes": 30},
                headers=_AUTH,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["kind"] == "manual_screen"

            await client.post("/v1/capture/stop", headers=_AUTH)


# ---------------------------------------------------------------------------
# /v1/capture/pause-until + resume
# ---------------------------------------------------------------------------


async def test_pause_until_and_resume(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/capture/pause-until",
                json={"until_ts": 9_999_999_999.0},
                headers=_AUTH,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["paused_until"] == 9_999_999_999.0

            resp = await client.post("/v1/capture/resume", headers=_AUTH)
            assert resp.status_code == 200
            assert resp.json()["paused"] is False


async def test_pause_until_rejects_negative(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/capture/pause-until",
                json={"until_ts": -5.0},
                headers=_AUTH,
            )
            assert resp.status_code == 422
            assert resp.json()["error"] == "validation_error"


# ---------------------------------------------------------------------------
# /v1/capture/enable-screen-override
# ---------------------------------------------------------------------------


async def test_enable_screen_override(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            # Phase-5 wiring (§5B / §12.6): the override now requires an
            # active session because it flips an in-memory + DB column
            # the OCR worker reads. Start a session first.
            start = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            assert start.status_code == 200, start.text

            resp = await client.post(
                "/v1/capture/enable-screen-override",
                json={"duration_minutes": 15},
                headers=_AUTH,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["duration_minutes"] == 15
            assert body["screen_capture_override_until"] is not None

            await client.post("/v1/capture/stop", headers=_AUTH)


# ---------------------------------------------------------------------------
# /v1/capture/prompt-response
# ---------------------------------------------------------------------------


async def test_prompt_response_continue(replay_env):
    """`continue` keyword aliased to `continue_session` in schema."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/capture/prompt-response",
                json={"continue": True},
                headers=_AUTH,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["ok"] is True


async def test_prompt_response_stop(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/capture/prompt-response",
                json={"continue": False},
                headers=_AUTH,
            )
            assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# /v1/sessions
# ---------------------------------------------------------------------------


async def test_sessions_list_empty(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get("/v1/sessions", headers=_AUTH)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["sessions"] == []
            assert body["total"] == 0
            assert body["offset"] == 0
            assert body["limit"] == 50


async def test_sessions_list_after_start(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            sid = r1.json()["session_id"]
            await client.post("/v1/capture/stop", headers=_AUTH)

            resp = await client.get("/v1/sessions", headers=_AUTH)
            assert resp.status_code == 200
            body = resp.json()
            assert body["total"] >= 1
            ids = [s["id"] for s in body["sessions"]]
            assert sid in ids


async def test_sessions_list_filters_by_kind(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            sid = r1.json()["session_id"]
            await client.post("/v1/capture/stop", headers=_AUTH)

            resp = await client.get(
                "/v1/sessions?kind=continuous", headers=_AUTH
            )
            assert resp.status_code == 200
            assert resp.json()["total"] >= 1

            resp = await client.get("/v1/sessions?kind=calendar", headers=_AUTH)
            assert resp.status_code == 200
            assert sid not in [s["id"] for s in resp.json()["sessions"]]


async def test_session_detail_404(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get("/v1/sessions/999999", headers=_AUTH)
            assert resp.status_code == 404
            body = resp.json()
            assert body["error"] == "session_not_found"
            assert body["code"] == 404


async def test_session_detail_happy_path(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            sid = r1.json()["session_id"]
            await client.post("/v1/capture/stop", headers=_AUTH)

            resp = await client.get(f"/v1/sessions/{sid}", headers=_AUTH)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["id"] == sid
            assert body["kind"] == "continuous"
            assert body["ended_at"] is not None
            assert body["duration_seconds"] is not None


async def test_session_transcript_empty_when_no_segments(replay_env):
    """Session with no transcript_segments rows yet → empty list."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            sid = r1.json()["session_id"]
            await client.post("/v1/capture/stop", headers=_AUTH)

            resp = await client.get(
                f"/v1/sessions/{sid}/transcript", headers=_AUTH
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["session_id"] == sid
            assert body["segments"] == []
            assert "coverage" in body
            cov = body["coverage"]
            assert "captured_seconds" in cov
            assert "unavailable_ranges" in cov
            assert "transcription_pending_seconds" in cov


async def test_session_transcript_404(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get("/v1/sessions/999999/transcript", headers=_AUTH)
            assert resp.status_code == 404


async def test_re_transcribe_returns_202(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            sid = r1.json()["session_id"]
            await client.post("/v1/capture/stop", headers=_AUTH)

            resp = await client.post(
                f"/v1/sessions/{sid}/re-transcribe", headers=_AUTH
            )
            assert resp.status_code == 202
            body = resp.json()
            assert body["status"] == "queued"
            assert body["session_id"] == sid


async def test_re_diarize_returns_202(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            sid = r1.json()["session_id"]
            await client.post("/v1/capture/stop", headers=_AUTH)

            resp = await client.post(
                f"/v1/sessions/{sid}/re-diarize", headers=_AUTH
            )
            assert resp.status_code == 202
            assert resp.json()["status"] == "queued"


async def test_delete_session_200_then_404(replay_env):
    """Phase 5 changed the contract: 200 + SessionDeleteResponse, not 204.

    See HELIOS_BUILD_PLAN §5C / §17 and tests/test_session_delete.py for
    the full coverage. We keep a minimal smoke check here so the
    parametrized auth tests above stay accurate against the live route.
    """
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            sid = r1.json()["session_id"]
            await client.post("/v1/capture/stop", headers=_AUTH)

            resp = await client.delete(f"/v1/sessions/{sid}", headers=_AUTH)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["session_id"] == sid
            assert body["was_active"] is False

            # Subsequent GET 404s
            resp = await client.get(f"/v1/sessions/{sid}", headers=_AUTH)
            assert resp.status_code == 404


async def test_delete_session_404_when_missing(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.delete("/v1/sessions/999999", headers=_AUTH)
            assert resp.status_code == 404
            assert resp.json()["error"] == "session_not_found"


async def test_delete_active_session_stops_and_deletes(replay_env):
    """Phase 5: deleting an active session stops it first instead of 409."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            sid = r1.json()["session_id"]

            resp = await client.delete(f"/v1/sessions/{sid}", headers=_AUTH)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["was_active"] is True
            assert body["session_id"] == sid


# ---------------------------------------------------------------------------
# /v1/audio + /v1/ocr  — placeholders
# ---------------------------------------------------------------------------


async def test_audio_empty_when_no_segments_in_window(replay_env):
    """HELIOS.md §7.3 wire contract uses ``?start=&end=`` (no _ts suffix).

    Wave 3I made this real; Wave 6 aligned the response with the §9.6
    spec — it now returns ``TranscriptResponse`` (segments + coverage),
    not ``AudioListResponse``. The legacy ``chunks`` field is gone.
    """
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get(
                "/v1/audio?start=1&end=2", headers=_AUTH
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["segments"] == []
            assert "coverage" in body
            assert body["coverage"]["captured_seconds"] == 0.0
            assert "chunks" not in body  # removed in Wave 6


async def test_audio_rejects_inverted_range(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get(
                "/v1/audio?start=20&end=10", headers=_AUTH
            )
            assert resp.status_code == 400
            assert resp.json()["error"] == "invalid_range"


async def test_ocr_placeholder(replay_env):
    """HELIOS.md §7.3 wire contract uses ``?start=&end=`` (no _ts suffix)."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get(
                "/v1/ocr?start=1&end=2", headers=_AUTH
            )
            assert resp.status_code == 200
            assert resp.json() == {"frames": []}


# ---------------------------------------------------------------------------
# /v1/diagnostics/* action endpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,body",
    [
        ("/v1/diagnostics/restart", None),
        ("/v1/diagnostics/flush-queues", None),
        ("/v1/diagnostics/test-capture", None),
        ("/v1/diagnostics/reload-component", {"component": "transcription"}),
    ],
)
async def test_diagnostics_actions_return_202(replay_env, path, body):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(path, json=body, headers=_AUTH)
            assert resp.status_code == 202, resp.text
            envelope = resp.json()
            assert envelope["status"] == "queued"


async def test_reload_component_validation(replay_env):
    """Empty / missing ``component`` body field ⇒ 422."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/diagnostics/reload-component",
                json={},
                headers=_AUTH,
            )
            assert resp.status_code == 422
            assert resp.json()["error"] == "validation_error"


# ---------------------------------------------------------------------------
# Auth-not-configured failure mode
# ---------------------------------------------------------------------------


async def test_auth_not_configured_returns_503(monkeypatch, tmp_path):
    """Empty bearer_token in config ⇒ 503 auth_not_configured (fail closed)."""
    mic = synth_wav(tmp_path / "fx" / "mic.wav", duration_s=2.0)
    sysw = synth_wav(tmp_path / "fx" / "sys.wav", duration_s=2.0)
    cal = tmp_path / "fx" / "cal.json"
    cal.write_text("[]")
    monkeypatch.setenv("HELIOS_REPLAY", "1")
    monkeypatch.setenv("HELIOS_REPLAY_MIC_FIXTURE", str(mic))
    monkeypatch.setenv("HELIOS_REPLAY_SYSTEM_FIXTURE", str(sysw))
    monkeypatch.setenv("HELIOS_REPLAY_CAL_FIXTURE", str(cal))
    monkeypatch.setenv("HELIOS_REPLAY_SPEED", "20.0")

    config_path = tmp_path / "capture.toml"
    storage_root = tmp_path / "storage"
    config_path.write_text(
        "[storage]\n"
        f'root = "{storage_root}"\n'
        "[api]\n"
        'bearer_token = ""\n'
    )
    from helios import config as helios_config

    monkeypatch.setattr(helios_config, "_CONFIG_PATH", config_path)
    monkeypatch.setattr(helios_config, "_cached_config", None)

    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get("/v1/status", headers={"Authorization": "Bearer x"})
            assert resp.status_code == 503
            body = resp.json()
            assert body["error"] == "auth_not_configured"
            assert body["code"] == 503


# ---------------------------------------------------------------------------
# /v1/sessions/{id}/transcript + /v1/audio  — real data flow (Wave 3I)
# ---------------------------------------------------------------------------


async def _seed_session_with_segments(
    app, *, started_at: float, ended_at: float, segments: list[dict]
) -> int:
    """Insert a session + chunk + segments into the live test DB."""
    from helios.db import queries

    db = app.state.db_pool.writer
    sid = await queries.create_session(db, "continuous", started_at=started_at)
    await queries.update_session_ended(
        db, session_id=sid, ended_at=ended_at, end_reason="test"
    )
    # Insert one system-channel chunk that covers the whole window.
    chunk_id = await queries.insert_audio_chunk(
        db,
        session_id=sid,
        channel="system",
        start_ts=started_at,
        end_ts=ended_at,
        path=f"/tmp/test_{sid}.wav",
        samples=int((ended_at - started_at) * 16000),
    )
    await queries.mark_chunk_transcribed(db, chunk_id)
    for i, seg in enumerate(segments):
        await queries.insert_transcript_segment(
            db,
            chunk_id=chunk_id,
            segment_index=i,
            start_ts=seg["start"],
            end_ts=seg["end"],
            text=seg["text"],
            speaker=seg.get("speaker"),
            words=seg.get("words"),
        )
    return sid


async def test_session_transcript_returns_real_segments(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        sid = await _seed_session_with_segments(
            app,
            started_at=2000.0,
            ended_at=2030.0,
            segments=[
                {"start": 2002.0, "end": 2005.0, "text": "alpha",
                 "speaker": "user"},
                {"start": 2010.0, "end": 2015.0, "text": "beta",
                 "speaker": "SPEAKER_00"},
                {"start": 2020.0, "end": 2025.0, "text": "gamma",
                 "speaker": None},
            ],
        )
        async with await _client(app) as client:
            resp = await client.get(
                f"/v1/sessions/{sid}/transcript", headers=_AUTH
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["session_id"] == sid
            segs = body["segments"]
            assert len(segs) == 3
            # Ordered by start_ts ASC.
            assert segs[0]["start"] == 2002.0
            assert segs[1]["start"] == 2010.0
            assert segs[2]["start"] == 2020.0
            assert segs[0]["text"] == "alpha"
            assert segs[0]["speaker"] == "user"
            assert segs[2]["speaker"] is None
            # Words are not requested → omitted / null.
            assert segs[0].get("words") in (None, [])


async def test_session_transcript_include_words_populates_field(replay_env):
    """include_words=true expands the JSON-encoded words column."""
    import json

    app = create_app()
    async with _LifespanContext(app):
        sid = await _seed_session_with_segments(
            app,
            started_at=3000.0,
            ended_at=3010.0,
            segments=[
                {
                    "start": 3001.0,
                    "end": 3004.0,
                    "text": "hello world",
                    "speaker": "user",
                    "words": json.dumps([
                        {"start": 3001.0, "end": 3002.0, "word": "hello"},
                        {"start": 3002.5, "end": 3004.0, "word": "world"},
                    ]),
                }
            ],
        )
        async with await _client(app) as client:
            resp = await client.get(
                f"/v1/sessions/{sid}/transcript?include_words=true",
                headers=_AUTH,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert len(body["segments"]) == 1
            words = body["segments"][0]["words"]
            assert words is not None
            assert len(words) == 2
            assert words[0]["word"] == "hello"


async def test_session_transcript_coverage_captured_seconds(replay_env):
    """Coverage reflects sum of recorded chunks (samples / sample_rate)."""
    from helios.db import queries

    app = create_app()
    async with _LifespanContext(app):
        db = app.state.db_pool.writer
        sid = await queries.create_session(db, "continuous", started_at=4000.0)
        await queries.update_session_ended(
            db, session_id=sid, ended_at=4060.0, end_reason="test"
        )
        # 30s worth of recorded samples at 16kHz.
        chunk_id = await queries.insert_audio_chunk(
            db,
            session_id=sid,
            channel="system",
            start_ts=4000.0,
            end_ts=4030.0,
            path="/tmp/seed.wav",
            samples=16000 * 30,
        )
        await queries.mark_chunk_transcribed(db, chunk_id)
        async with await _client(app) as client:
            resp = await client.get(
                f"/v1/sessions/{sid}/transcript", headers=_AUTH
            )
            body = resp.json()
            assert body["coverage"]["captured_seconds"] == pytest.approx(30.0)
            assert body["coverage"]["transcription_pending_seconds"] == 0.0
            assert body["coverage"]["unavailable_ranges"] == []


async def test_session_transcript_coverage_unavailable_ranges(replay_env):
    """Unavailable chunks surface in coverage.unavailable_ranges."""
    from helios.db import queries

    app = create_app()
    async with _LifespanContext(app):
        db = app.state.db_pool.writer
        sid = await queries.create_session(db, "continuous", started_at=5000.0)
        await queries.update_session_ended(
            db, session_id=sid, ended_at=5060.0, end_reason="test"
        )
        await queries.insert_unavailable_chunk(
            db,
            session_id=sid,
            channel="system",
            start_ts=5010.0,
            end_ts=5020.0,
            samples=0,
            reason="watchdog_stall",
        )
        async with await _client(app) as client:
            resp = await client.get(
                f"/v1/sessions/{sid}/transcript", headers=_AUTH
            )
            body = resp.json()
            ranges = body["coverage"]["unavailable_ranges"]
            assert len(ranges) == 1
            assert ranges[0]["start"] == 5010.0
            assert ranges[0]["end"] == 5020.0
            assert ranges[0]["reason"] == "watchdog_stall"


async def test_session_transcript_coverage_pending_seconds(replay_env):
    """Recorded-but-not-transcribed chunks count as pending."""
    from helios.db import queries

    app = create_app()
    async with _LifespanContext(app):
        db = app.state.db_pool.writer
        sid = await queries.create_session(db, "continuous", started_at=6000.0)
        await queries.update_session_ended(
            db, session_id=sid, ended_at=6060.0, end_reason="test"
        )
        # Status='recorded' AND transcribed_at is NULL ⇒ pending.
        await queries.insert_audio_chunk(
            db,
            session_id=sid,
            channel="system",
            start_ts=6000.0,
            end_ts=6020.0,
            path="/tmp/p.wav",
            samples=16000 * 20,
        )
        async with await _client(app) as client:
            resp = await client.get(
                f"/v1/sessions/{sid}/transcript", headers=_AUTH
            )
            body = resp.json()
            cov = body["coverage"]
            assert cov["captured_seconds"] == pytest.approx(20.0)
            assert cov["transcription_pending_seconds"] == pytest.approx(20.0)


async def test_audio_returns_segments_across_sessions(replay_env):
    """Cross-session time-window query — typical case for back-to-back sessions."""
    app = create_app()
    async with _LifespanContext(app):
        # Session A covers 7000-7030, session B covers 7030-7060.
        await _seed_session_with_segments(
            app,
            started_at=7000.0,
            ended_at=7030.0,
            segments=[
                {"start": 7005.0, "end": 7015.0, "text": "from-a",
                 "speaker": "user"},
            ],
        )
        await _seed_session_with_segments(
            app,
            started_at=7030.0,
            ended_at=7060.0,
            segments=[
                {"start": 7035.0, "end": 7045.0, "text": "from-b",
                 "speaker": "SPEAKER_00"},
            ],
        )
        # Window 7010-7040 catches both segments (they overlap the window).
        async with await _client(app) as client:
            resp = await client.get(
                "/v1/audio?start=7010&end=7040", headers=_AUTH
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            segs = body["segments"]
            assert len(segs) == 2
            # Ordered by start_ts.
            assert segs[0]["start"] == 7005.0
            assert segs[1]["start"] == 7035.0


async def test_audio_filters_segments_outside_window(replay_env):
    """Segments entirely outside the requested window are excluded."""
    app = create_app()
    async with _LifespanContext(app):
        await _seed_session_with_segments(
            app,
            started_at=8000.0,
            ended_at=8030.0,
            segments=[
                {"start": 8005.0, "end": 8010.0, "text": "early",
                 "speaker": "user"},
                {"start": 8020.0, "end": 8025.0, "text": "late",
                 "speaker": "user"},
            ],
        )
        async with await _client(app) as client:
            # Window only covers the early segment.
            resp = await client.get(
                "/v1/audio?start=8003&end=8012", headers=_AUTH
            )
            body = resp.json()
            assert len(body["segments"]) == 1
            assert body["segments"][0]["text"] == "early"
