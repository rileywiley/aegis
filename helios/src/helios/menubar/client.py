"""Synchronous HTTP client for the Helios daemon.

Used by the menu bar (rumps' run-loop is sync, so we deliberately use
``httpx.Client`` not ``httpx.AsyncClient``). Per HELIOS.md §12 the menu
bar polls ``/v1/status`` every few seconds and issues capture / voice-
note RPCs in response to user clicks. Every endpoint except
``/v1/health`` requires the bearer token from
``~/.aegis/capture.toml`` (HELIOS.md §7.1) — this client always sends
one and re-reads from disk on a single 401 retry so a freshly rotated
token (e.g., user just regenerated capture.toml) doesn't require a menu
bar restart.

Public exception surface:

* :class:`DaemonUnreachable` — connection refused (daemon not running).
* :class:`DaemonTimeout`     — request exceeded ``timeout`` seconds.
* :class:`DaemonAuthFailed`  — 401 even after re-reading the token; the
  on-disk token is wrong / has been rotated to something the user
  hasn't given us.

Logging is PII-safe: only HTTP status codes, endpoint paths, and
durations are logged. Bodies are never logged here, and the structured
logger (helios.log) redacts ``bearer_token`` automatically.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from helios.api.schemas import (
    ActiveVoiceNoteResponse,
    CapturePauseUntilResponse,
    CaptureResumeResponse,
    CaptureStartResponse,
    CaptureStopResponse,
    StatusResponse,
    VoiceNoteCancelResponse,
    VoiceNoteStartResponse,
    VoiceNoteStopResponse,
)
from helios.log import get_logger

_log = get_logger("menubar.client")


# Public alias — the build plan refers to the status payload as
# ``DaemonStatus``. Reuse :class:`StatusResponse` (the daemon's wire
# contract) rather than duplicating fields. Re-exported below.
DaemonStatus = StatusResponse


class DaemonUnreachable(Exception):
    """The daemon's HTTP socket isn't accepting connections."""


class DaemonTimeout(Exception):
    """The daemon accepted the connection but didn't respond in time."""


class DaemonAuthFailed(Exception):
    """The bearer token on disk is rejected by the daemon (401 after retry)."""


def _load_token(token_path: Path) -> str:
    """Read the bearer token from ``capture.toml``.

    Mirrors :func:`helios.config.load_config` minimally — we only need
    ``[api].bearer_token`` and want to avoid pulling the full pydantic
    config (which would re-create the file with a new token if it's
    missing, which is wrong for a client).
    """
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]

    if not token_path.exists():
        # Treat as auth failure — the daemon owns config creation. A
        # menu bar talking to a non-existent config means the daemon
        # has never run on this machine.
        raise DaemonAuthFailed(f"capture.toml missing at {token_path}")
    data = tomllib.loads(token_path.read_text())
    api = data.get("api", {})
    token = api.get("bearer_token", "")
    if not isinstance(token, str) or not token:
        raise DaemonAuthFailed("bearer_token missing or empty in capture.toml")
    return token


class HeliosClient:
    """Sync HTTP client for the Helios daemon REST API.

    Parameters
    ----------
    base_url:
        Daemon URL, e.g. ``http://127.0.0.1:3031``.
    token_path:
        Path to ``~/.aegis/capture.toml`` (or a test fixture).
    timeout:
        Per-request timeout in seconds. Default 5.0 — keeps the rumps
        run-loop responsive when the daemon is wedged.
    transport:
        Optional ``httpx.BaseTransport`` for testing (``MockTransport``).
        Default ``None`` lets ``httpx.Client`` pick its real socket
        transport.
    """

    def __init__(
        self,
        base_url: str,
        token_path: Path,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_path = token_path
        self._timeout = timeout
        # Lazy: load the token on first request so the menu bar can
        # construct the client even when capture.toml hasn't been
        # written yet (the daemon will write it on its next start).
        self._token: str | None = None
        client_kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            client_kwargs["transport"] = transport
        self._http = httpx.Client(**client_kwargs)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._http.close()

    def __enter__(self) -> "HeliosClient":
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal request plumbing
    # ------------------------------------------------------------------

    def _ensure_token(self) -> str:
        if self._token is None:
            self._token = _load_token(self._token_path)
        return self._token

    def _refresh_token(self) -> str:
        """Force-reload the token from disk (called on 401)."""
        self._token = _load_token(self._token_path)
        return self._token

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Send a single request with bearer token, retrying once on 401.

        Network failures map to :class:`DaemonUnreachable` /
        :class:`DaemonTimeout`. A second 401 after re-reading the token
        raises :class:`DaemonAuthFailed`.

        ``timeout`` overrides the client's default for this single call.
        Voice-note stop runs synchronous transcription server-side and
        can legitimately take 30s+ on a partial chunk.
        """
        url = f"{self._base_url}{path}"
        started = time.monotonic()

        def _send(token: str) -> httpx.Response:
            headers = {"Authorization": f"Bearer {token}"}
            try:
                kwargs: dict[str, Any] = {"json": json, "headers": headers}
                if timeout is not None:
                    kwargs["timeout"] = timeout
                return self._http.request(method, url, **kwargs)
            except httpx.TimeoutException as exc:
                raise DaemonTimeout(f"{method} {path} timed out") from exc
            except httpx.ConnectError as exc:
                raise DaemonUnreachable(
                    f"cannot connect to {self._base_url}: {exc}"
                ) from exc

        # First attempt with cached token.
        token = self._ensure_token()
        resp = _send(token)
        if resp.status_code == 401:
            # Re-read token from disk and try once more. If the file
            # itself is the problem (missing / empty), _refresh_token
            # raises DaemonAuthFailed directly.
            new_token = self._refresh_token()
            resp = _send(new_token)
            if resp.status_code == 401:
                raise DaemonAuthFailed(
                    f"401 from {path} even after token refresh"
                )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        _log.info(
            "menubar_client_request",
            method=method,
            path=path,
            status=resp.status_code,
            elapsed_ms=elapsed_ms,
        )
        return resp

    @staticmethod
    def _parse[T: BaseModel](resp: httpx.Response, model: type[T]) -> T:
        """Validate ``resp.json()`` against a Pydantic model."""
        try:
            payload = resp.json()
        except ValueError as exc:
            raise DaemonUnreachable(f"non-JSON response: {exc}") from exc
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            # Schema mismatch is a daemon-version mismatch — surface
            # as Unreachable so the menu bar shows a degraded state
            # rather than crashing the run-loop.
            raise DaemonUnreachable(
                f"schema mismatch on {model.__name__}: {exc.errors()[:2]}"
            ) from exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_status(self) -> DaemonStatus:
        """``GET /v1/status`` — snapshot for menu bar polling."""
        resp = self._request("GET", "/v1/status")
        resp.raise_for_status()
        return self._parse(resp, StatusResponse)

    def get_active_voice_note(self) -> ActiveVoiceNoteResponse:
        """``GET /v1/voice-note/active`` — current voice-note state."""
        resp = self._request("GET", "/v1/voice-note/active")
        resp.raise_for_status()
        return self._parse(resp, ActiveVoiceNoteResponse)

    def post_capture_start(
        self,
        kind: str,
        duration_minutes: int | None = None,
    ) -> CaptureStartResponse:
        """``POST /v1/capture/start``.

        ``duration_minutes`` is required when ``kind="manual_screen"``
        (the daemon enforces this with a 422). Continuous mode passes
        ``None`` and the body omits the field.
        """
        body: dict[str, Any] = {"kind": kind}
        if duration_minutes is not None:
            body["duration_minutes"] = duration_minutes
        resp = self._request("POST", "/v1/capture/start", json=body)
        resp.raise_for_status()
        return self._parse(resp, CaptureStartResponse)

    def post_capture_stop(self) -> CaptureStopResponse | None:
        """``POST /v1/capture/stop``.

        Returns ``None`` when the daemon reports nothing was active
        (``session_id`` is null in the response). Otherwise returns the
        full :class:`CaptureStopResponse` with the stopped session id.
        """
        resp = self._request("POST", "/v1/capture/stop")
        resp.raise_for_status()
        parsed = self._parse(resp, CaptureStopResponse)
        if parsed.session_id is None:
            return None
        return parsed

    def post_capture_pause_until(
        self, until_ts: float
    ) -> CapturePauseUntilResponse:
        """``POST /v1/capture/pause-until`` with ``{"until_ts": ...}``."""
        resp = self._request(
            "POST", "/v1/capture/pause-until", json={"until_ts": until_ts}
        )
        resp.raise_for_status()
        return self._parse(resp, CapturePauseUntilResponse)

    def post_capture_resume(self) -> CaptureResumeResponse:
        """``POST /v1/capture/resume`` — clears any active pause window."""
        resp = self._request("POST", "/v1/capture/resume")
        resp.raise_for_status()
        return self._parse(resp, CaptureResumeResponse)

    def post_voice_note_start(
        self, triggered_by: str = "menu_bar"
    ) -> VoiceNoteStartResponse:
        """``POST /v1/voice-note/start`` with ``{"triggered_by": ...}``."""
        resp = self._request(
            "POST",
            "/v1/voice-note/start",
            json={"triggered_by": triggered_by},
        )
        resp.raise_for_status()
        return self._parse(resp, VoiceNoteStartResponse)

    def post_voice_note_stop(self) -> VoiceNoteStopResponse:
        """``POST /v1/voice-note/stop`` — returns transcript when ready.

        Server-side synchronous transcription on the final partial chunk
        runs WhisperX, which can take 10-30s. The 5s default timeout
        used to be enough only because transcription failed instantly
        on a missing-ffmpeg ImportError — now that the path works end
        to end, we need to give the daemon real wall-clock budget.
        """
        resp = self._request(
            "POST", "/v1/voice-note/stop", timeout=60.0
        )
        resp.raise_for_status()
        return self._parse(resp, VoiceNoteStopResponse)

    def post_voice_note_cancel(self) -> VoiceNoteCancelResponse:
        """``POST /v1/voice-note/cancel`` — discard without saving."""
        resp = self._request("POST", "/v1/voice-note/cancel")
        resp.raise_for_status()
        return self._parse(resp, VoiceNoteCancelResponse)


__all__ = [
    "DaemonAuthFailed",
    "DaemonStatus",
    "DaemonTimeout",
    "DaemonUnreachable",
    "HeliosClient",
]
