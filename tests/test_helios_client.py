"""Tests for the Aegis HeliosClient HTTP wrapper.

All cases use ``httpx.MockTransport`` — never a real network call. The
client itself is exercised end-to-end (token load → bearer header →
JSON parse → unreachable handling).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
import pytest

from aegis.clients.helios import HeliosClient


# ─── Helpers ──────────────────────────────────────────────────────────


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    tmp_path: Path,
    token: str | None = "test-token",
    token_body: str | None = None,
) -> tuple[HeliosClient, httpx.AsyncClient, Path]:
    """Return a (client, http, token_path) triple wired to a mocked transport.

    ``token`` controls the simple happy-path body. Set ``token_body`` to write
    arbitrary TOML content (used for malformed/empty cases). ``token=None``
    leaves the token file missing.
    """
    if token_body is not None:
        token_file = tmp_path / "capture.toml"
        token_file.write_text(token_body)
    elif token is not None:
        token_file = tmp_path / "capture.toml"
        token_file.write_text(f'[api]\nbearer_token = "{token}"\n')
    else:
        token_file = tmp_path / "no_capture.toml"  # missing on purpose
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = HeliosClient(
        base_url="http://helios.test",
        token_path=str(token_file),
        http=http,
        timeout_seconds=2.0,
    )
    return client, http, token_file


def _start_end() -> tuple[datetime, datetime]:
    start = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)
    return start, end


# ─── health_check ─────────────────────────────────────────────────────


class TestHealthCheck:
    async def test_health_200(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/health"
            # Health is unauthenticated — bearer must NOT be required.
            return httpx.Response(200, json={"status": "ok"})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.health_check() is True
        finally:
            await http.aclose()

    async def test_health_503(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "down"})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.health_check() is False
        finally:
            await http.aclose()

    async def test_health_connection_refused(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.health_check() is False
        finally:
            await http.aclose()

    async def test_health_timeout(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timeout")

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.health_check() is False
        finally:
            await http.aclose()


# ─── get_status ───────────────────────────────────────────────────────


class TestGetStatus:
    async def test_status_200(self, tmp_path):
        payload = {"daemon": "running", "mode": "armed", "scheduler_running": True}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/status"
            assert request.headers["Authorization"] == "Bearer test-token"
            return httpx.Response(200, json=payload)

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.get_status() == payload
        finally:
            await http.aclose()

    async def test_status_unreachable(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("daemon down")

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.get_status() is None
        finally:
            await http.aclose()

    async def test_status_401_then_200_retry_with_reloaded_token(self, tmp_path):
        """401 → client reloads token → second call succeeds."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("Authorization", "")
            calls.append(auth)
            if len(calls) == 1:
                # First call: token-old is rejected.
                return httpx.Response(401, json={"error": "unauthorized"})
            # Second call: client should have reloaded; we don't care
            # about the new value, just that a retry happened.
            return httpx.Response(200, json={"daemon": "running"})

        # Initial token write.
        client, http, token_path = _make_client(
            handler, tmp_path=tmp_path, token="token-old"
        )
        try:
            # Rotate the token on disk *before* the first call so that
            # the reload picks up a different value.
            token_path.write_text('[api]\nbearer_token = "token-new"\n')
            # Force a reload on the first attempt by clearing cache —
            # but the constructor already left _token unset, so the
            # first call uses the new file too. To exercise the rotation
            # path explicitly, prime the cached token to the old value.
            client._token = "token-old"

            result = await client.get_status()
            assert result == {"daemon": "running"}
            # Two calls: first with stale token, second after reload.
            assert len(calls) == 2
            assert calls[0] == "Bearer token-old"
            assert calls[1] == "Bearer token-new"
        finally:
            await http.aclose()

    async def test_status_401_twice_gives_up(self, tmp_path):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(401, json={"error": "unauthorized"})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.get_status() is None
            # 1 retry max → exactly 2 attempts.
            assert call_count == 2
        finally:
            await http.aclose()

    async def test_status_no_token_file(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("HTTP should not be called when no token is loadable")
            return httpx.Response(200)

        client, http, _ = _make_client(handler, tmp_path=tmp_path, token=None)
        try:
            assert await client.get_status() is None
        finally:
            await http.aclose()


# ─── get_transcript_for_meeting ───────────────────────────────────────


class TestGetTranscriptForMeeting:
    async def test_calls_audio_endpoint_with_unix_ts(self, tmp_path):
        start, end = _start_end()
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"chunks": []})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            result = await client.get_transcript_for_meeting(start, end)
            assert result == {"chunks": []}
            assert captured["path"] == "/v1/audio"
            assert captured["params"]["start"] == str(start.timestamp())
            assert captured["params"]["end"] == str(end.timestamp())
            assert captured["params"]["include_words"] == "false"
        finally:
            await http.aclose()

    async def test_include_words_true(self, tmp_path):
        start, end = _start_end()
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"chunks": []})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            await client.get_transcript_for_meeting(start, end, include_words=True)
            assert captured["params"]["include_words"] == "true"
        finally:
            await http.aclose()


# ─── get_session_transcript ───────────────────────────────────────────


class TestGetSessionTranscript:
    async def test_path_includes_session_id(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200, json={"session_id": 42, "segments": []}
            )

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            result = await client.get_session_transcript(42)
            assert result == {"session_id": 42, "segments": []}
            assert captured["path"] == "/v1/sessions/42/transcript"
            assert captured["params"]["include_words"] == "false"
        finally:
            await http.aclose()


# ─── list_sessions ────────────────────────────────────────────────────


class TestListSessions:
    async def test_filters_passed_through(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"sessions": [], "total": 0})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            await client.list_sessions(kind="calendar", limit=10)
            assert captured["path"] == "/v1/sessions"
            assert captured["params"]["kind"] == "calendar"
            assert captured["params"]["limit"] == "10"
            # Optional unset filters must not be sent.
            assert "start_ts_gte" not in captured["params"]
            assert "start_ts_lte" not in captured["params"]
        finally:
            await http.aclose()

    async def test_optional_time_filters(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"sessions": [], "total": 0})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            await client.list_sessions(
                start_ts_gte=100.0, start_ts_lte=200.0, limit=5
            )
            assert captured["params"]["start_ts_gte"] == "100.0"
            assert captured["params"]["start_ts_lte"] == "200.0"
        finally:
            await http.aclose()


# ─── get_ocr ──────────────────────────────────────────────────────────


class TestGetOcr:
    async def test_calls_ocr_endpoint(self, tmp_path):
        start, end = _start_end()
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"frames": []})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            result = await client.get_ocr(start, end)
            assert result == {"frames": []}
            assert captured["path"] == "/v1/ocr"
            assert captured["params"]["start"] == str(start.timestamp())
            assert captured["params"]["end"] == str(end.timestamp())
        finally:
            await http.aclose()


# ─── token loading ────────────────────────────────────────────────────


class TestTokenLoading:
    async def test_reads_bearer_token_from_capture_toml(self, tmp_path):
        token_file = tmp_path / "capture.toml"
        token_file.write_text('[api]\nbearer_token = "abc-123"\n')

        # Don't need real HTTP for this — just load + assert.
        client = HeliosClient(
            base_url="http://helios.test",
            token_path=str(token_file),
            http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
            timeout_seconds=2.0,
        )
        try:
            assert client._load_token() == "abc-123"
        finally:
            await client._http.aclose()

    async def test_missing_file_returns_none(self, tmp_path):
        client = HeliosClient(
            base_url="http://helios.test",
            token_path=str(tmp_path / "does_not_exist.toml"),
            http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
            timeout_seconds=2.0,
        )
        try:
            assert client._load_token() is None
        finally:
            await client._http.aclose()

    async def test_empty_token_returns_none(self, tmp_path):
        token_file = tmp_path / "capture.toml"
        token_file.write_text('[api]\nbearer_token = ""\n')

        client = HeliosClient(
            base_url="http://helios.test",
            token_path=str(token_file),
            http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
            timeout_seconds=2.0,
        )
        try:
            assert client._load_token() is None
        finally:
            await client._http.aclose()

    async def test_missing_api_section_returns_none(self, tmp_path):
        token_file = tmp_path / "capture.toml"
        token_file.write_text('[other]\nfoo = "bar"\n')

        client = HeliosClient(
            base_url="http://helios.test",
            token_path=str(token_file),
            http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
            timeout_seconds=2.0,
        )
        try:
            assert client._load_token() is None
        finally:
            await client._http.aclose()

    async def test_malformed_toml_returns_none(self, tmp_path):
        token_file = tmp_path / "capture.toml"
        token_file.write_text("this is = = not valid toml [[[\n")

        client = HeliosClient(
            base_url="http://helios.test",
            token_path=str(token_file),
            http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
            timeout_seconds=2.0,
        )
        try:
            assert client._load_token() is None
        finally:
            await client._http.aclose()


# ─── bearer header on authed calls ────────────────────────────────────


class TestBearerHeader:
    async def test_bearer_header_sent_on_authed_call(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={})

        client, http, _ = _make_client(
            handler, tmp_path=tmp_path, token="my-secret-token"
        )
        try:
            await client.get_status()
            assert captured["auth"] == "Bearer my-secret-token"
        finally:
            await http.aclose()


# ─── Phase 6 Wave 1 additions (Track 6A) ──────────────────────────────
#
# Each new method needs a happy-path test and a 401-retry test. Diagnostic
# write methods mock the daemon's ``DiagnosticsAcceptedResponse`` shape
# (202 + ``{"status": "queued", "detail": ...}``).


def _queued_response(detail: str | None = None) -> httpx.Response:
    body: dict = {"status": "queued"}
    if detail is not None:
        body["detail"] = detail
    return httpx.Response(202, json=body)


class TestGetDiagnostics:
    async def test_diagnostics_200(self, tmp_path):
        payload = {"components": {}, "queues": {}, "storage": {}}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/diagnostics"
            assert request.headers["Authorization"] == "Bearer test-token"
            return httpx.Response(200, json=payload)

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.get_diagnostics() == payload
        finally:
            await http.aclose()

    async def test_diagnostics_401_then_200(self, tmp_path):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers.get("Authorization", ""))
            if len(calls) == 1:
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json={"ok": True})

        client, http, token_path = _make_client(
            handler, tmp_path=tmp_path, token="t-old"
        )
        try:
            token_path.write_text('[api]\nbearer_token = "t-new"\n')
            client._token = "t-old"
            assert await client.get_diagnostics() == {"ok": True}
            assert calls == ["Bearer t-old", "Bearer t-new"]
        finally:
            await http.aclose()


class TestGetPermissions:
    async def test_permissions_200(self, tmp_path):
        payload = {"microphone": "granted", "screen_recording": "granted"}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/permissions"
            return httpx.Response(200, json=payload)

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.get_permissions() == payload
        finally:
            await http.aclose()

    async def test_permissions_401_retry(self, tmp_path):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(401)
            return httpx.Response(200, json={"microphone": "denied"})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.get_permissions() == {"microphone": "denied"}
            assert calls == 2
        finally:
            await http.aclose()


class TestGetSession:
    async def test_path_includes_session_id(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            return httpx.Response(200, json={"id": 7, "kind": "calendar"})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.get_session(7) == {"id": 7, "kind": "calendar"}
            assert captured["path"] == "/v1/sessions/7"
        finally:
            await http.aclose()

    async def test_get_session_401_retry(self, tmp_path):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(401)
            return httpx.Response(200, json={"id": 9})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.get_session(9) == {"id": 9}
            assert calls == 2
        finally:
            await http.aclose()


class TestListSessionsDashboardFilters:
    async def test_status_and_date_passed_through(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"sessions": [], "total": 0})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            await client.list_sessions(
                kind="manual_screen",
                status="complete",
                date="2026-05-15",
                limit=25,
            )
            assert captured["params"]["kind"] == "manual_screen"
            assert captured["params"]["status"] == "complete"
            assert captured["params"]["date"] == "2026-05-15"
            assert captured["params"]["limit"] == "25"
        finally:
            await http.aclose()


class TestListAudio:
    async def test_audio_for_session(self, tmp_path):
        """``list_audio`` calls the session-scoped chunks endpoint.

        Bug fix during the §12.7 smoke: the old ``/v1/audio?session_id=``
        was actually a cross-session transcript endpoint that required
        start+end. The session detail page's Audio tab needs raw chunk
        rows, not transcript segments, so we route to a new daemon
        endpoint at ``/v1/sessions/{id}/audio-chunks``.
        """
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200, json={"session_id": 42, "chunks": []}
            )

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            result = await client.list_audio(session_id=42)
            assert result == {"session_id": 42, "chunks": []}
            assert captured["path"] == "/v1/sessions/42/audio-chunks"
            assert captured["params"] == {}
        finally:
            await http.aclose()

    async def test_audio_chunk_url_format(self, tmp_path):
        """``stream_audio_chunk`` hits the per-chunk streaming endpoint."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, content=b"RIFF...", headers={"content-type": "audio/wav"})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            result = await client.stream_audio_chunk(session_id=7, chunk_id=99)
            assert result is not None
            status, body, ctype = result
            assert status == 200
            assert body == b"RIFF..."
            assert ctype == "audio/wav"
            assert captured["path"] == "/v1/sessions/7/audio/99"
            assert captured["auth"].startswith("Bearer ")
        finally:
            await http.aclose()

    async def test_audio_401_retry(self, tmp_path):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(401)
            return httpx.Response(200, json={"chunks": []})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.list_audio(session_id=3) == {"chunks": []}
            assert calls == 2
        finally:
            await http.aclose()


class TestOcrBySession:
    async def test_session_id_param(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"frames": []})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            result = await client.get_ocr(session_id=11)
            assert result == {"frames": []}
            assert captured["path"] == "/v1/ocr"
            assert captured["params"]["session_id"] == "11"
            # start/end omitted when not provided
            assert "start" not in captured["params"]
            assert "end" not in captured["params"]
        finally:
            await http.aclose()


class TestDeleteSession:
    async def test_delete_204(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            return httpx.Response(204)

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            result = await client.delete_session(99)
            # 204 maps to an empty dict (success, no body).
            assert result == {}
            assert captured["method"] == "DELETE"
            assert captured["path"] == "/v1/sessions/99"
        finally:
            await http.aclose()

    async def test_delete_200_with_body(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "DELETE"
            return httpx.Response(200, json={"status": "deleted", "id": 99})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.delete_session(99) == {"status": "deleted", "id": 99}
        finally:
            await http.aclose()

    async def test_delete_401_retry(self, tmp_path):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(401)
            return httpx.Response(204)

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.delete_session(1) == {}
            assert calls == 2
        finally:
            await http.aclose()


class TestStartCapture:
    async def test_post_with_body(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["body"] = request.read()
            return httpx.Response(200, json={"session_id": 5, "kind": "manual_screen"})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            result = await client.start_capture(kind="manual_screen", title="ad-hoc")
            assert result == {"session_id": 5, "kind": "manual_screen"}
            assert captured["method"] == "POST"
            assert captured["path"] == "/v1/capture/start"
            assert b'"kind":"manual_screen"' in captured["body"].replace(b" ", b"")
            assert b'"title":"ad-hoc"' in captured["body"].replace(b" ", b"")
        finally:
            await http.aclose()

    async def test_start_capture_401_retry(self, tmp_path):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(401)
            return httpx.Response(200, json={"session_id": 6})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.start_capture() == {"session_id": 6}
            assert calls == 2
        finally:
            await http.aclose()


class TestStopCapture:
    async def test_stop_200(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            return httpx.Response(200, json={"status": "stopped"})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.stop_capture() == {"status": "stopped"}
            assert captured["method"] == "POST"
            assert captured["path"] == "/v1/capture/stop"
        finally:
            await http.aclose()

    async def test_stop_401_retry(self, tmp_path):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(401)
            return httpx.Response(200, json={"status": "stopped"})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.stop_capture() == {"status": "stopped"}
            assert calls == 2
        finally:
            await http.aclose()


class TestScreenOverride:
    async def test_override_post(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["body"] = request.read()
            return httpx.Response(200, json={"status": "ok", "action": "enable"})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            result = await client.screen_override(42, action="enable")
            assert result == {"status": "ok", "action": "enable"}
            assert captured["method"] == "POST"
            assert captured["path"] == "/v1/capture/42/screen-override"
            assert b'"action":"enable"' in captured["body"].replace(b" ", b"")
        finally:
            await http.aclose()

    async def test_override_401_retry(self, tmp_path):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(401)
            return httpx.Response(200, json={"status": "ok"})

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.screen_override(1, action="disable") == {"status": "ok"}
            assert calls == 2
        finally:
            await http.aclose()


# ─── Diagnostic stubs (202 "queued" — Track 6D will implement) ────────


class TestDiagnosticStubs:
    """Each diagnostic write returns 202 ``DiagnosticsAcceptedResponse`` today.

    Tests assert the method posts to the right path and parses the 202 body.
    """

    async def test_restart_daemon(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            return _queued_response("restart scheduled")

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            result = await client.restart_daemon()
            assert result == {"status": "queued", "detail": "restart scheduled"}
            assert captured["method"] == "POST"
            assert captured["path"] == "/v1/diagnostics/restart"
        finally:
            await http.aclose()

    async def test_flush_queues(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            return _queued_response()

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.flush_queues() == {"status": "queued"}
            assert captured["path"] == "/v1/diagnostics/flush-queues"
        finally:
            await http.aclose()

    async def test_test_capture(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            return _queued_response("60s test started")

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.test_capture() == {
                "status": "queued",
                "detail": "60s test started",
            }
            assert captured["path"] == "/v1/diagnostics/test-capture"
        finally:
            await http.aclose()

    async def test_reload_component(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = request.read()
            return _queued_response()

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.reload_component("transcriber") == {"status": "queued"}
            assert captured["path"] == "/v1/diagnostics/reload-component"
            assert b'"component":"transcriber"' in captured["body"].replace(b" ", b"")
        finally:
            await http.aclose()

    async def test_create_diagnostics_bundle(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            return _queued_response("bundle queued")

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.create_diagnostics_bundle() == {
                "status": "queued",
                "detail": "bundle queued",
            }
            assert captured["path"] == "/v1/diagnostics/bundle"
        finally:
            await http.aclose()

    async def test_re_transcribe(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["method"] = request.method
            return _queued_response()

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.re_transcribe_session(7) == {"status": "queued"}
            assert captured["method"] == "POST"
            assert captured["path"] == "/v1/sessions/7/re-transcribe"
        finally:
            await http.aclose()

    async def test_re_diarize(self, tmp_path):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            return _queued_response()

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.re_diarize_session(7) == {"status": "queued"}
            assert captured["path"] == "/v1/sessions/7/re-diarize"
        finally:
            await http.aclose()

    async def test_diagnostic_stub_401_retry(self, tmp_path):
        """Token-rotation behavior applies uniformly to write endpoints too."""
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(401)
            return _queued_response()

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.flush_queues() == {"status": "queued"}
            assert calls == 2
        finally:
            await http.aclose()

    async def test_diagnostic_stub_unreachable(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("daemon down")

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.restart_daemon() is None
        finally:
            await http.aclose()


class TestGetTestCaptureStatus:
    """Critical #3 (Wave 4) — new method to poll the test-capture job."""

    async def test_get_test_capture_status_path(self, tmp_path):
        payload = {
            "job_id": "job-abc",
            "status": "running",
            "steps": [{"name": "mic_open", "ok": True, "detail": ""}],
            "session_id": None,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.path == "/v1/diagnostics/test-capture/job-abc"
            assert request.headers["Authorization"] == "Bearer test-token"
            return httpx.Response(200, json=payload)

        client, http, _ = _make_client(handler, tmp_path=tmp_path)
        try:
            assert await client.get_test_capture_status("job-abc") == payload
        finally:
            await http.aclose()
