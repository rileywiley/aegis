"""HeliosClient — typed HTTP client for the Helios daemon.

Replaces the old ScreenpipeClient at ``aegis/ingestion/screenpipe.py``
(deleted in Phase 3 / Wave 3J). Used by:

* ``aegis/ingestion/helios.py`` for transcript / audio / OCR fetches
* ``aegis/ingestion/poller.py`` for the heartbeat loop (Track 3F.6)
* ``aegis/main.py`` app startup (Track 3F.7) — instance stored on ``app.state``
* ``aegis/web/routes/`` Phase 6 dashboard pages (Wave 2 — Track 6B/6D)

Bearer token loaded from ``~/.aegis/capture.toml`` at construction. On 401,
the client reloads the token (in case it rotated) and retries ONCE.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class HeliosClient:
    """Typed HTTP client for the Helios daemon.

    Use the async methods from coroutines / FastAPI handlers. The
    ``http`` argument is an externally-managed ``httpx.AsyncClient`` —
    the client does NOT close it on its own.

    Returns ``None`` for "Helios is unreachable" so callers can treat
    daemon-down as a degraded-but-not-broken state. Returns typed
    response data on success.
    """

    def __init__(
        self,
        base_url: str,
        token_path: str | Path,
        http: httpx.AsyncClient,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_path = Path(token_path).expanduser()
        self._http = http
        self._timeout = timeout_seconds
        self._token: str | None = None  # lazily loaded

    # ------------------------------------------------------------------ public

    async def health_check(self) -> bool:
        """``GET /v1/health`` (no auth). True if 2xx, False on any error."""
        url = f"{self._base_url}/v1/health"
        try:
            response = await self._http.get(url, timeout=self._timeout)
        except (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
        ):
            return False
        return 200 <= response.status_code < 300

    async def get_status(self) -> dict | None:
        """``GET /v1/status``. Returns the parsed JSON, or None if unreachable."""
        return await self._authed_get_json("/v1/status")

    async def get_diagnostics(self) -> dict | None:
        """``GET /v1/diagnostics``. Full daemon state for the dashboard."""
        return await self._authed_get_json("/v1/diagnostics")

    async def get_permissions(self) -> dict | None:
        """``GET /v1/permissions``. macOS TCC + screen-recording state."""
        return await self._authed_get_json("/v1/permissions")

    async def get_transcript_for_meeting(
        self,
        start_time: datetime,
        end_time: datetime,
        *,
        include_words: bool = False,
    ) -> dict | None:
        """``GET /v1/audio?start=&end=&include_words=``.

        Returns transcript segments spanning ``[start_time, end_time]`` across
        all sessions in that window. None if Helios unreachable.
        """
        params = {
            "start": start_time.timestamp(),
            "end": end_time.timestamp(),
            "include_words": str(include_words).lower(),
        }
        return await self._authed_get_json("/v1/audio", params=params)

    async def get_session_transcript(
        self, session_id: int, *, include_words: bool = False
    ) -> dict | None:
        """``GET /v1/sessions/{id}/transcript``."""
        params = {"include_words": str(include_words).lower()}
        return await self._authed_get_json(
            f"/v1/sessions/{session_id}/transcript", params=params
        )

    async def get_session(self, session_id: int) -> dict | None:
        """``GET /v1/sessions/{id}``. Session metadata for detail page header."""
        return await self._authed_get_json(f"/v1/sessions/{session_id}")

    async def list_sessions(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        date: str | None = None,
        start_ts_gte: float | None = None,
        start_ts_lte: float | None = None,
        limit: int = 50,
    ) -> dict | None:
        """``GET /v1/sessions`` with filter params.

        ``date`` is an ISO ``YYYY-MM-DD`` string the dashboard uses to
        scope the sessions page. The daemon translates it into start/end
        bounds; the client simply passes it through.
        """
        params: dict[str, Any] = {"limit": limit}
        if kind is not None:
            params["kind"] = kind
        if status is not None:
            params["status"] = status
        if date is not None:
            params["date"] = date
        if start_ts_gte is not None:
            params["start_ts_gte"] = start_ts_gte
        if start_ts_lte is not None:
            params["start_ts_lte"] = start_ts_lte
        return await self._authed_get_json("/v1/sessions", params=params)

    async def get_ocr(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        *,
        session_id: int | None = None,
    ) -> dict | None:
        """``GET /v1/ocr?session_id=&start=&end=``.

        Either a ``session_id`` or a time window is accepted; the daemon
        validates the combination. Returns OCR frames as JSON, or None
        when Helios is unreachable.
        """
        params: dict[str, Any] = {}
        if session_id is not None:
            params["session_id"] = session_id
        if start_time is not None:
            params["start"] = start_time.timestamp()
        if end_time is not None:
            params["end"] = end_time.timestamp()
        return await self._authed_get_json("/v1/ocr", params=params)

    async def list_audio(self, *, session_id: int) -> dict | None:
        """``GET /v1/sessions/{id}/audio-chunks``. Chunk list for the audio tab.

        Returns the daemon's :class:`SessionAudioChunksResponse` shape:
        ``{"session_id": int, "chunks": [AudioChunkResponse, ...]}``.
        Use :meth:`audio_chunk_url` to build the per-chunk stream URL.
        """
        return await self._authed_get_json(
            f"/v1/sessions/{session_id}/audio-chunks"
        )

    async def stream_audio_chunk(
        self, *, session_id: int, chunk_id: int
    ) -> tuple[int, bytes, str] | None:
        """Fetch a single WAV chunk's bytes with bearer auth.

        Browsers can't send Authorization on an ``<audio>`` tag, so the
        Aegis dashboard proxies the daemon endpoint through its own
        ``/helios/sessions/{sid}/audio/{cid}`` route, which calls this
        method. Returns ``(status_code, body_bytes, content_type)`` or
        None if the daemon is unreachable.
        """
        url = f"{self._base_url}/v1/sessions/{session_id}/audio/{chunk_id}"
        if self._token is None:
            self._token = self._load_token()
        if self._token is None:
            return None
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            response = await self._http.get(
                url, headers=headers, timeout=self._timeout
            )
        except (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
        ):
            return None
        return (
            response.status_code,
            response.content,
            response.headers.get("content-type", "audio/wav"),
        )

    # -------- writes ----------------------------------------------------

    async def delete_session(self, session_id: int) -> dict | None:
        """``DELETE /v1/sessions/{id}``. Per-meeting/manual delete."""
        return await self._authed_request_json(
            "DELETE", f"/v1/sessions/{session_id}"
        )

    async def start_capture(
        self,
        *,
        kind: str = "manual_screen",
        title: str | None = None,
    ) -> dict | None:
        """``POST /v1/capture/start``. Manual capture session.

        ``kind`` matches the daemon's ``SessionKind`` enum
        (``calendar``, ``continuous``, ``manual_screen``, ``voice_note``).
        """
        body: dict[str, Any] = {"kind": kind}
        if title is not None:
            body["title"] = title
        return await self._authed_request_json(
            "POST", "/v1/capture/start", json_body=body
        )

    async def stop_capture(self) -> dict | None:
        """``POST /v1/capture/stop``. Stop the currently-active session."""
        return await self._authed_request_json("POST", "/v1/capture/stop")

    async def screen_override(
        self, session_id: int, *, action: str
    ) -> dict | None:
        """``POST /v1/capture/{sid}/screen-override`` (Track 5B).

        ``action`` is one of ``enable`` / ``disable`` per the daemon's
        ``CaptureScreenOverrideRequest`` contract.
        """
        body = {"action": action}
        return await self._authed_request_json(
            "POST",
            f"/v1/capture/{session_id}/screen-override",
            json_body=body,
        )

    async def restart_daemon(self) -> dict | None:
        """``POST /v1/diagnostics/restart``.

        Backed by Track 6D in Phase 6 Wave 2.
        Returns a ``DiagnosticsAcceptedResponse``-shaped JSON: ``{"status":
        "queued", "detail": ...}`` (HTTP 202 stub today; real restart later).
        """
        return await self._authed_request_json("POST", "/v1/diagnostics/restart")

    async def flush_queues(self) -> dict | None:
        """``POST /v1/diagnostics/flush-queues``.

        Track 6D shape (``FlushQueuesResponse``):
        ``{"transcription_flushed": int, "diarization_flushed": int}``.
        """
        return await self._authed_request_json(
            "POST", "/v1/diagnostics/flush-queues"
        )

    async def test_capture(self) -> dict | None:
        """``POST /v1/diagnostics/test-capture`` (60-second self-test).

        Track 6D shape (``TestCaptureStartResponse``):
        ``{"job_id": str, "status": "queued"}``. Poll
        ``get_test_capture_status(job_id)`` for the per-step
        ``TestCaptureStatusResponse`` until ``status`` is ``complete`` /
        ``failed``.
        """
        return await self._authed_request_json(
            "POST", "/v1/diagnostics/test-capture"
        )

    async def get_test_capture_status(self, job_id: str) -> dict | None:
        """``GET /v1/diagnostics/test-capture/{job_id}``.

        Track 6D shape (``TestCaptureStatusResponse``):
        ``{"job_id": str, "status": "queued|running|complete|failed",
            "steps": [{"name": str, "ok": bool, "detail": str}],
            "session_id": int | None}``.
        """
        return await self._authed_request_json(
            "GET", f"/v1/diagnostics/test-capture/{job_id}"
        )

    async def reload_component(self, component: str) -> dict | None:
        """``POST /v1/diagnostics/reload-component``.

        Body: ``{"component": "<name>"}`` per ``ReloadComponentRequest``.
        Track 6D shape (``ReloadComponentResponse``):
        ``{"component": str, "ok": bool, "detail": str}``. An unknown
        component name returns 200 with ``ok=false`` rather than 4xx.
        """
        body = {"component": component}
        return await self._authed_request_json(
            "POST", "/v1/diagnostics/reload-component", json_body=body
        )

    async def create_diagnostics_bundle(self) -> dict | None:
        """``POST /v1/diagnostics/bundle`` (tar.gz creation).

        Track 6D shape (``BundleResponse``):
        ``{"bundle_path": str, "filename": str, "download_url": str,
            "size_bytes": int, "expires_at": iso8601 str}``.
        """
        return await self._authed_request_json(
            "POST", "/v1/diagnostics/bundle"
        )

    async def re_transcribe_session(self, session_id: int) -> dict | None:
        """``POST /v1/sessions/{id}/re-transcribe``.

        Track 6D shape (``ReTranscribeResponse``):
        ``{"session_id": int, "chunks_requeued": int}``.
        """
        return await self._authed_request_json(
            "POST", f"/v1/sessions/{session_id}/re-transcribe"
        )

    async def re_diarize_session(self, session_id: int) -> dict | None:
        """``POST /v1/sessions/{id}/re-diarize``.

        Track 6D shape (``ReDiarizeResponse``):
        ``{"session_id": int, "jobs_requeued": int}``. A value of 0
        signals diarization is unavailable (component disabled / not
        configured) and the UI should surface a warning pill.
        """
        return await self._authed_request_json(
            "POST", f"/v1/sessions/{session_id}/re-diarize"
        )

    # ------------------------------------------------------------------ internal

    def _load_token(self) -> str | None:
        """Read ``bearer_token`` from ``capture.toml``. Returns None on any failure."""
        try:
            import tomllib
        except ImportError:  # pragma: no cover — Python <3.11 fallback
            import tomli as tomllib  # type: ignore[no-redef]
        try:
            with open(self._token_path, "rb") as fh:
                data = tomllib.load(fh)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            logger.warning("helios_token_load_failed", extra={"error": str(exc)})
            return None
        except Exception as exc:  # malformed TOML, etc.
            logger.warning("helios_token_parse_failed", extra={"error": str(exc)})
            return None
        api = data.get("api") or {}
        token = api.get("bearer_token")
        if isinstance(token, str) and token.strip():
            return token.strip()
        return None

    async def _authed_get_json(
        self, path: str, *, params: dict | None = None
    ) -> dict | None:
        """Bearer-authed GET that returns parsed JSON, or None when unreachable.

        On 401, reloads the token from disk and retries ONCE.
        """
        return await self._authed_request_json("GET", path, params=params)

    async def _authed_request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict | None:
        """Bearer-authed request helper used by every authed method.

        Generalizes the original GET-only path so POST/DELETE flows share
        the same token-reload-on-401 retry behavior. Accepts 200/201/202
        as success; 202 is the common "queued" response from the daemon's
        diagnostic stubs (see ``DiagnosticsAcceptedResponse``).
        """
        url = f"{self._base_url}{path}"
        for attempt in range(2):
            if self._token is None:
                self._token = self._load_token()
            if self._token is None:
                # No token available; cannot authenticate.
                return None
            headers = {"Authorization": f"Bearer {self._token}"}
            try:
                response = await self._http.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=self._timeout,
                )
            except (
                httpx.ConnectError,
                httpx.TimeoutException,
                httpx.RemoteProtocolError,
            ):
                return None
            if response.status_code == 401 and attempt == 0:
                # Token may have rotated — reload and retry.
                self._token = None
                continue
            if 200 <= response.status_code < 300:
                # 204 No Content has no body; treat as empty success dict.
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError:
                    return None
            logger.warning(
                "helios_request_non_2xx",
                extra={"path": path, "method": method, "status": response.status_code},
            )
            return None
        return None
