"""Tests for the menu bar HTTP client (Track 4A).

These tests use ``httpx.MockTransport`` (the synchronous variant) to
simulate the Helios daemon without a live socket. The patterns here
mirror :mod:`tests.test_calendar_client` — see its module docstring for
the rationale on why mock-transport is preferred over patching at the
HTTP layer.

The client itself is **synchronous** because the menu bar's rumps
run-loop is sync. So every test here is ``def test_...`` rather than
``async def`` even though the project's ``pytest-asyncio`` runs in
``auto`` mode (auto-mode only picks up ``async def`` functions).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from helios.menubar.client import (
    DaemonAuthFailed,
    DaemonTimeout,
    DaemonUnreachable,
    HeliosClient,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_capture_toml(path: Path, token: str = "test-token-1") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""[api]
port = 3031
bearer_token = "{token}"
bind_address = "127.0.0.1"
"""
    )
    return path


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    return _write_capture_toml(tmp_path / "capture.toml", token="test-token-1")


def _make_client(
    token_path: Path,
    handler,
    *,
    timeout: float = 5.0,
) -> HeliosClient:
    transport = httpx.MockTransport(handler)
    return HeliosClient(
        base_url="http://127.0.0.1:3031",
        token_path=token_path,
        timeout=timeout,
        transport=transport,
    )


# Canonical /v1/status payload satisfying the StatusResponse schema.
_STATUS_PAYLOAD = {
    "daemon": "running",
    "mode": "armed",
    "components": {
        "audio_capture": "ok",
        "transcription": "unavailable",
        "diarization": "unavailable",
        "ocr": "unavailable",
    },
    "component_errors": {
        "transcription": {
            "component": "transcription",
            "message": "model loading",
            "occurred_at": 1700000000.0,
            "reason": "model_loading",
            "detail": "loading distil-large-v3",
            "action": "wait",
        },
    },
    "active_session": {
        "id": 7,
        "kind": "continuous",
        "calendar_event_ids": [],
        "started_at": 1700000100.0,
        "screen_capture_override_until": None,
    },
    "next_calendar_event": {
        "calendar_event_id": "evt-9",
        "title": "",  # title intentionally empty — PII discipline
        "starts_at": 1700001000.0,
        "pre_start_at": 1700000940.0,
    },
    "active_session_id": 7,
    "paused_until": None,
    "aegis_unreachable": False,
    "last_error": None,
    "queue": {
        "transcription_pending": 0,
        "transcription_failed_24h": 0,
        "diarization_pending": 0,
        "diarization_failed_24h": 0,
    },
    "scheduler_running": True,
    "permissions": {
        "mic_granted": True,
        "screen_recording_granted": True,
        "last_checked_at": 1700000000.0,
    },
}


# ---------------------------------------------------------------------------
# 1. get_status() happy path
# ---------------------------------------------------------------------------


def test_get_status_happy_path(token_path: Path) -> None:
    seen: dict[str, str | None] = {"path": None, "auth": None}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_STATUS_PAYLOAD)

    with _make_client(token_path, handler) as client:
        status = client.get_status()

    assert seen["path"] == "/v1/status"
    assert seen["auth"] == "Bearer test-token-1"
    assert status.mode == "armed"
    assert status.scheduler_running is True
    assert status.aegis_unreachable is False
    assert status.active_session is not None
    assert status.active_session.id == 7
    assert status.active_session_id == 7
    assert status.paused_until is None
    assert status.last_error is None
    assert status.next_calendar_event is not None
    assert status.next_calendar_event.calendar_event_id == "evt-9"
    assert status.permissions.mic_granted is True
    assert "transcription" in status.component_errors
    assert status.queue.transcription_pending == 0
    assert status.components["audio_capture"] == "ok"


# ---------------------------------------------------------------------------
# 2. 401 → re-read token, retry, succeed
# ---------------------------------------------------------------------------


def test_401_then_token_rotated_then_success(token_path: Path) -> None:
    """Daemon rejects the cached token; new token written to disk wins on retry."""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        auth = request.headers.get("authorization")
        if auth == "Bearer rotated-token":
            return httpx.Response(200, json=_STATUS_PAYLOAD)
        return httpx.Response(
            401,
            json={"error": "unauthorized", "detail": "invalid token", "code": 401},
        )

    with _make_client(token_path, handler) as client:
        # Force the cached token to load with the old value.
        assert client._ensure_token() == "test-token-1"
        # User rotates the token on disk between requests.
        _write_capture_toml(token_path, token="rotated-token")
        status = client.get_status()

    assert state["calls"] == 2  # initial 401 + retry
    assert status.mode == "armed"


# ---------------------------------------------------------------------------
# 3. 401 then 401 → DaemonAuthFailed
# ---------------------------------------------------------------------------


def test_persistent_401_raises_auth_failed(token_path: Path) -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        return httpx.Response(
            401,
            json={"error": "unauthorized", "detail": "invalid token", "code": 401},
        )

    with _make_client(token_path, handler) as client:
        with pytest.raises(DaemonAuthFailed):
            client.get_status()

    # Initial attempt + retry == 2 calls. No third attempt.
    assert state["calls"] == 2


# ---------------------------------------------------------------------------
# 4. ConnectError → DaemonUnreachable
# ---------------------------------------------------------------------------


def test_connection_refused_raises_unreachable(token_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with _make_client(token_path, handler) as client:
        with pytest.raises(DaemonUnreachable, match="cannot connect"):
            client.get_status()


# ---------------------------------------------------------------------------
# 5. TimeoutException → DaemonTimeout
# ---------------------------------------------------------------------------


def test_timeout_raises_daemon_timeout(token_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    with _make_client(token_path, handler) as client:
        with pytest.raises(DaemonTimeout, match="timed out"):
            client.get_status()


# ---------------------------------------------------------------------------
# 6. post_capture_start with kind="continuous", duration_minutes=None
# ---------------------------------------------------------------------------


def test_capture_start_continuous_omits_duration(token_path: Path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "session_id": 12,
                "kind": "continuous",
                "started_at": 1700000200.0,
            },
        )

    with _make_client(token_path, handler) as client:
        resp = client.post_capture_start(kind="continuous")

    import json as _json

    body = _json.loads(seen["body"])  # type: ignore[arg-type]
    assert seen["path"] == "/v1/capture/start"
    assert body == {"kind": "continuous"}
    # Duration must be omitted for continuous (the daemon's pydantic
    # schema rejects it with extra="forbid" if the client sends None
    # explicitly with strict callers, and HELIOS.md §7.3 says it's
    # ignored for continuous).
    assert "duration_minutes" not in body
    assert resp.session_id == 12
    assert resp.kind == "continuous"


# ---------------------------------------------------------------------------
# 7. post_capture_start with kind="manual_screen", duration_minutes=30
# ---------------------------------------------------------------------------


def test_capture_start_manual_screen_includes_duration(token_path: Path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "session_id": 13,
                "kind": "manual_screen",
                "started_at": 1700000300.0,
            },
        )

    with _make_client(token_path, handler) as client:
        resp = client.post_capture_start(
            kind="manual_screen", duration_minutes=30
        )

    import json as _json

    body = _json.loads(seen["body"])  # type: ignore[arg-type]
    assert body == {"kind": "manual_screen", "duration_minutes": 30}
    assert resp.kind == "manual_screen"


# ---------------------------------------------------------------------------
# 8. post_capture_stop happy path
# ---------------------------------------------------------------------------


def test_capture_stop_returns_response_when_session_was_active(
    token_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/capture/stop"
        # POST with no body — body field omitted, content-length is 0.
        return httpx.Response(
            200,
            json={
                "session_id": 7,
                "ended_at": 1700000900.0,
                "end_reason": "user_stop",
            },
        )

    with _make_client(token_path, handler) as client:
        resp = client.post_capture_stop()

    assert resp is not None
    assert resp.session_id == 7
    assert resp.end_reason == "user_stop"


def test_capture_stop_returns_none_when_idempotent(token_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"session_id": None, "ended_at": None, "end_reason": None},
        )

    with _make_client(token_path, handler) as client:
        resp = client.post_capture_stop()

    assert resp is None


# ---------------------------------------------------------------------------
# 9. post_capture_pause_until — sends until_ts in body
# ---------------------------------------------------------------------------


def test_pause_until_sends_until_ts_in_body(token_path: Path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(200, json={"paused_until": 1700099999.0})

    with _make_client(token_path, handler) as client:
        resp = client.post_capture_pause_until(1700099999.0)

    import json as _json

    body = _json.loads(seen["body"])  # type: ignore[arg-type]
    assert seen["path"] == "/v1/capture/pause-until"
    assert body == {"until_ts": 1700099999.0}
    assert resp.paused_until == 1700099999.0


def test_capture_resume_happy_path(token_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/capture/resume"
        return httpx.Response(200, json={"paused": False})

    with _make_client(token_path, handler) as client:
        resp = client.post_capture_resume()

    assert resp.paused is False


# ---------------------------------------------------------------------------
# 10. post_voice_note_start — body has triggered_by
# ---------------------------------------------------------------------------


def test_voice_note_start_passes_triggered_by(token_path: Path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "voice_note_id": 99,
                "session_id": 99,
                "started_at": 1700000400.0,
                "is_excerpt": False,
                "parent_session_id": None,
                "max_duration_seconds": 300,
            },
        )

    with _make_client(token_path, handler) as client:
        resp = client.post_voice_note_start(triggered_by="menu_bar")

    import json as _json

    body = _json.loads(seen["body"])  # type: ignore[arg-type]
    assert seen["path"] == "/v1/voice-note/start"
    assert body == {"triggered_by": "menu_bar"}
    assert resp.voice_note_id == 99
    assert resp.is_excerpt is False
    assert resp.max_duration_seconds == 300


# ---------------------------------------------------------------------------
# 11. post_voice_note_stop — returns transcript when present
# ---------------------------------------------------------------------------


def test_voice_note_stop_returns_transcript(token_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/voice-note/stop"
        return httpx.Response(
            200,
            json={
                "voice_note_id": 99,
                "session_id": 99,
                "started_at": 1700000400.0,
                "ended_at": 1700000460.0,
                "duration_seconds": 60.0,
                "transcript": {
                    "session_id": 99,
                    "started_at": 1700000400.0,
                    "ended_at": 1700000460.0,
                    "segments": [
                        {
                            "start": 0.0,
                            "end": 1.5,
                            "speaker": None,
                            "text": "hello world",
                            "words": None,
                        }
                    ],
                    "coverage": {
                        "captured_seconds": 60.0,
                        "unavailable_ranges": [],
                        "transcription_pending_seconds": 0.0,
                    },
                    "diarization_status": "not_applicable",
                },
                "is_excerpt": False,
            },
        )

    with _make_client(token_path, handler) as client:
        resp = client.post_voice_note_stop()

    assert resp.voice_note_id == 99
    assert resp.transcript is not None
    assert len(resp.transcript.segments) == 1
    assert resp.transcript.segments[0].text == "hello world"
    assert resp.duration_seconds == 60.0


def test_voice_note_stop_returns_response_with_null_transcript(
    token_path: Path,
) -> None:
    """Stop succeeded but transcription wasn't available — transcript=None."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "voice_note_id": 99,
                "session_id": 99,
                "started_at": 1700000400.0,
                "ended_at": 1700000460.0,
                "duration_seconds": 60.0,
                "transcript": None,
                "is_excerpt": False,
            },
        )

    with _make_client(token_path, handler) as client:
        resp = client.post_voice_note_stop()

    assert resp.transcript is None


# ---------------------------------------------------------------------------
# 12. post_voice_note_cancel — returns cancelled_voice_note_id
# ---------------------------------------------------------------------------


def test_voice_note_cancel_returns_id(token_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/voice-note/cancel"
        return httpx.Response(200, json={"cancelled_voice_note_id": 42})

    with _make_client(token_path, handler) as client:
        resp = client.post_voice_note_cancel()

    assert resp.cancelled_voice_note_id == 42


# ---------------------------------------------------------------------------
# 13. get_active_voice_note when none active (active=null)
# ---------------------------------------------------------------------------


def test_active_voice_note_when_none(token_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/voice-note/active"
        return httpx.Response(200, json={"active": None})

    with _make_client(token_path, handler) as client:
        resp = client.get_active_voice_note()

    assert resp.active is None


# ---------------------------------------------------------------------------
# 14. get_active_voice_note when active
# ---------------------------------------------------------------------------


def test_active_voice_note_when_active(token_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "active": {
                    "voice_note_id": 11,
                    "session_id": 11,
                    "started_at": 1700000400.0,
                    "elapsed_seconds": 12.5,
                    "is_excerpt": False,
                    "max_duration_seconds": 300,
                    "approaching_cap": False,
                },
            },
        )

    with _make_client(token_path, handler) as client:
        resp = client.get_active_voice_note()

    assert resp.active is not None
    assert resp.active.voice_note_id == 11
    assert resp.active.elapsed_seconds == 12.5
    assert resp.active.max_duration_seconds == 300
    assert resp.active.approaching_cap is False


# ---------------------------------------------------------------------------
# Token-loading edge cases
# ---------------------------------------------------------------------------


def test_missing_capture_toml_raises_auth_failed(tmp_path: Path) -> None:
    """A non-existent capture.toml is treated as auth-failed, not silently created."""
    missing = tmp_path / "does-not-exist.toml"

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, json={})

    with _make_client(missing, handler) as client:
        with pytest.raises(DaemonAuthFailed, match="missing"):
            client.get_status()


def test_empty_bearer_token_raises_auth_failed(tmp_path: Path) -> None:
    path = tmp_path / "capture.toml"
    path.write_text(
        '[api]\nport = 3031\nbearer_token = ""\nbind_address = "127.0.0.1"\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, json={})

    with _make_client(path, handler) as client:
        with pytest.raises(DaemonAuthFailed, match="missing or empty"):
            client.get_status()
