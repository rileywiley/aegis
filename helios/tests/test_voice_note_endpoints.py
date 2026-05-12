"""Voice-note endpoint tests (Track 2E.5).

Wave 2E (Track 3B) plumbed real transcription through ``stop`` —
``transcribe_synchronously`` is now invoked. Because whisperx is an
optional dependency that may not be installed in the test environment,
these tests force the component reporter to ``ok`` and stub the
transcription worker's model so the synchronous path returns an empty
result deterministically. (The transcription worker has its own test
suite covering the WhisperX integration in detail.)

Tests cover:

* start happy path → 200 + voice_note_id
* start during active continuous session → is_excerpt=true,
  parent_session_id set
* start while another voice note is active → 409
* start with mic permission denied → 403
* start when transcription is unavailable → 503
* stop → 200 with transcript shape (segments may be empty)
* stop with no active note → 404
* cancel → 200
* GET active when none → null
* GET active when active and within cap window → approaching_cap=true
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from helios.api import create_app
from helios.db import queries
from tests.conftest import synth_wav

_AUTH = {"Authorization": "Bearer test"}


class _LifespanContext:
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
        "[voice_note]\n"
        "max_duration_seconds = 300\n"
    )
    from helios import config as helios_config

    monkeypatch.setattr(helios_config, "_CONFIG_PATH", config_path)
    monkeypatch.setattr(helios_config, "_cached_config", None)
    return storage_root


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# start happy path
# ---------------------------------------------------------------------------


async def _grant_mic(app):
    """Insert a permission_checks row with mic_granted=True so the voice-note
    permission gate passes. The PermissionChecker background loop also writes
    rows; this helper just ensures the LATEST row sees mic granted at the
    moment of the next voice-note start."""
    db = app.state.db_pool.writer
    await queries.insert_permission_check(
        db,
        checked_at=time.time(),
        mic_granted=True,
        screen_recording_granted=True,
    )


async def _force_transcription_ready(app):
    """Force the transcription component to ``ok`` and pre-load a no-op model.

    Wave 2E gates ``/v1/voice-note/start`` on transcription availability.
    The lifespan-spawned worker reports ``unavailable(model_loading)``
    initially and may flip to ``unavailable(whisperx_not_installed)`` if
    the optional extras aren't installed in the test env. We override
    both so these endpoint tests can focus on lifecycle semantics.
    """
    reporter = app.state.component_reporter
    await reporter.set("transcription", "ok")
    worker = app.state.transcription_worker
    # Mark model "loaded" so transcribe_synchronously() doesn't block on
    # _model_ready. The fake model returns an empty segment list, which
    # the synchronous path then writes to the DB as zero rows.

    class _NoopModel:
        def transcribe(self, *_args, **_kwargs):
            return {"segments": []}

    worker._model = _NoopModel()
    worker._model_ready.set()


async def test_voice_note_start_happy_path(replay_env):
    """No active capture ⇒ creates a voice_note session, is_excerpt=false."""
    app = create_app()
    async with _LifespanContext(app):
        await _grant_mic(app)
        await _force_transcription_ready(app)
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/voice-note/start",
                json={"triggered_by": "menu_bar"},
                headers=_AUTH,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["voice_note_id"] >= 1
            assert body["session_id"] >= 1
            assert body["is_excerpt"] is False
            assert body["parent_session_id"] is None
            assert body["max_duration_seconds"] == 300

            # Cleanup.
            await client.post("/v1/voice-note/stop", headers=_AUTH)


# ---------------------------------------------------------------------------
# start during active continuous session  → excerpt
# ---------------------------------------------------------------------------


async def test_voice_note_start_during_continuous_creates_excerpt(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        await _grant_mic(app)
        await _force_transcription_ready(app)
        async with await _client(app) as client:
            cap = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            cap_sid = cap.json()["session_id"]

            resp = await client.post(
                "/v1/voice-note/start",
                json={"triggered_by": "hotkey"},
                headers=_AUTH,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["is_excerpt"] is True
            assert body["parent_session_id"] == cap_sid
            assert body["session_id"] == cap_sid

            # Cleanup — cancel voice note (since it has no real session row),
            # then stop the underlying capture.
            await client.post("/v1/voice-note/cancel", headers=_AUTH)
            await client.post("/v1/capture/stop", headers=_AUTH)


# ---------------------------------------------------------------------------
# start while another voice note active → 409
# ---------------------------------------------------------------------------


async def test_voice_note_start_while_active_409(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        await _grant_mic(app)
        await _force_transcription_ready(app)
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/voice-note/start",
                json={"triggered_by": "menu_bar"},
                headers=_AUTH,
            )
            assert r1.status_code == 200

            r2 = await client.post(
                "/v1/voice-note/start",
                json={"triggered_by": "menu_bar"},
                headers=_AUTH,
            )
            assert r2.status_code == 409, r2.text
            body = r2.json()
            assert body["error"] == "voice_note_already_active"
            assert body["code"] == 409

            await client.post("/v1/voice-note/stop", headers=_AUTH)


# ---------------------------------------------------------------------------
# start with mic permission denied → 403
# ---------------------------------------------------------------------------


async def test_voice_note_start_mic_denied_403(replay_env):
    """Insert a permission-check row with mic_granted=False; expect 403."""
    app = create_app()
    async with _LifespanContext(app):
        # Inject the denied permission BEFORE the request.
        db = app.state.db_pool.writer
        await queries.insert_permission_check(
            db,
            checked_at=time.time(),
            mic_granted=False,
            screen_recording_granted=True,
        )

        async with await _client(app) as client:
            resp = await client.post(
                "/v1/voice-note/start",
                json={"triggered_by": "menu_bar"},
                headers=_AUTH,
            )
            assert resp.status_code == 403, resp.text
            body = resp.json()
            assert body["error"] == "permission_denied"
            assert body["code"] == 403


# ---------------------------------------------------------------------------
# start when transcription unavailable → 503 (Wave 2E gate)
# ---------------------------------------------------------------------------


async def test_voice_note_start_transcription_unavailable_503(replay_env):
    """Reporter status ``unavailable(whisperx_not_installed)`` ⇒ 503."""
    app = create_app()
    async with _LifespanContext(app):
        await _grant_mic(app)
        # Force the reporter into the "really unavailable" state (not the
        # transient ``model_loading`` one — that is allowed through).
        reporter = app.state.component_reporter
        await reporter.set(
            "transcription",
            "unavailable",
            reason="whisperx_not_installed",
            detail="No module named 'whisperx'",
        )

        async with await _client(app) as client:
            resp = await client.post(
                "/v1/voice-note/start",
                json={"triggered_by": "menu_bar"},
                headers=_AUTH,
            )
            assert resp.status_code == 503, resp.text
            body = resp.json()
            assert body["error"] == "component_unavailable"
            assert body["code"] == 503


# ---------------------------------------------------------------------------
# stop happy path → 200 with transcript shape (segments may be empty)
# ---------------------------------------------------------------------------


async def test_voice_note_stop_returns_transcript_shape(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        await _grant_mic(app)
        await _force_transcription_ready(app)
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/voice-note/start",
                json={"triggered_by": "menu_bar"},
                headers=_AUTH,
            )
            sid = r1.json()["session_id"]
            vid = r1.json()["voice_note_id"]

            resp = await client.post("/v1/voice-note/stop", headers=_AUTH)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["voice_note_id"] == vid
            assert body["session_id"] == sid
            # Wave 2E: transcript is a TranscriptResponse object (or null
            # when transcription failed). With the noop model installed by
            # _force_transcription_ready, segments is an empty list.
            transcript = body["transcript"]
            assert transcript is not None
            assert transcript["session_id"] == sid
            assert transcript["segments"] == []
            assert body["ended_at"] >= body["started_at"]
            assert body["duration_seconds"] >= 0


# ---------------------------------------------------------------------------
# stop with no active note → 404
# ---------------------------------------------------------------------------


async def test_voice_note_stop_when_idle_404(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post("/v1/voice-note/stop", headers=_AUTH)
            assert resp.status_code == 404, resp.text
            body = resp.json()
            assert body["error"] == "voice_note_not_active"


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


async def test_voice_note_cancel_returns_id(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        await _grant_mic(app)
        await _force_transcription_ready(app)
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/voice-note/start",
                json={"triggered_by": "menu_bar"},
                headers=_AUTH,
            )
            vid = r1.json()["voice_note_id"]

            resp = await client.post("/v1/voice-note/cancel", headers=_AUTH)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["cancelled_voice_note_id"] == vid

            # Cancel-when-idle returns 404.
            resp = await client.post("/v1/voice-note/cancel", headers=_AUTH)
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET active when none → null
# ---------------------------------------------------------------------------


async def test_voice_note_active_when_none(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get("/v1/voice-note/active", headers=_AUTH)
            assert resp.status_code == 200, resp.text
            assert resp.json()["active"] is None


# ---------------------------------------------------------------------------
# GET active reports approaching_cap when within 30s of max
# ---------------------------------------------------------------------------


async def test_voice_note_active_approaching_cap(replay_env, monkeypatch):
    """Force ``started_at`` into the past so elapsed_seconds > max - 30s."""
    app = create_app()
    async with _LifespanContext(app):
        await _grant_mic(app)
        await _force_transcription_ready(app)
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/voice-note/start",
                json={"triggered_by": "menu_bar"},
                headers=_AUTH,
            )
            assert r1.status_code == 200

            # Backdate the in-memory started_at into the cap window.
            state = app.state.voice_note_active
            assert state is not None
            max_dur = state["max_duration_seconds"]
            # 5 seconds before the max; well inside the 30s warning window.
            state["started_at"] = time.time() - (max_dur - 5)

            resp = await client.get("/v1/voice-note/active", headers=_AUTH)
            assert resp.status_code == 200, resp.text
            body = resp.json()["active"]
            assert body is not None
            assert body["approaching_cap"] is True
            assert body["elapsed_seconds"] >= max_dur - 30

            await client.post("/v1/voice-note/stop", headers=_AUTH)


# ---------------------------------------------------------------------------
# GET active reports approaching_cap=False when fresh
# ---------------------------------------------------------------------------


async def test_voice_note_active_not_approaching_when_fresh(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        await _grant_mic(app)
        await _force_transcription_ready(app)
        async with await _client(app) as client:
            await client.post(
                "/v1/voice-note/start",
                json={"triggered_by": "menu_bar"},
                headers=_AUTH,
            )

            resp = await client.get("/v1/voice-note/active", headers=_AUTH)
            assert resp.status_code == 200, resp.text
            body = resp.json()["active"]
            assert body is not None
            assert body["approaching_cap"] is False

            await client.post("/v1/voice-note/stop", headers=_AUTH)


async def test_voice_note_active_null_after_orchestrator_force_stop(replay_env):
    """After a scheduler force-stop the orchestrator's active session is
    cleared but ``app.state.voice_note_active`` is preserved so the
    ``/voice-note/stop`` endpoint can still serve the menu bar a
    transcript response. Meanwhile ``/v1/voice-note/active`` must
    report ``null`` so the floating indicator dismisses on its next
    250ms poll.
    """
    app = create_app()
    async with _LifespanContext(app):
        await _grant_mic(app)
        await _force_transcription_ready(app)
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/voice-note/start",
                json={"triggered_by": "menu_bar"},
                headers=_AUTH,
            )
            assert r1.status_code == 200

            # Sanity: active reports the in-flight note.
            resp = await client.get("/v1/voice-note/active", headers=_AUTH)
            assert resp.json()["active"] is not None

            # Simulate scheduler-side force-stop: orchestrator's active
            # session disappears, but the API's voice_note_active dict
            # is still populated (the /stop endpoint never ran).
            assert app.state.voice_note_active is not None
            await app.state.orchestrator.stop_session(
                app.state.voice_note_active["session_id"],
                reason="voice_note_cap_reached",
            )

            # /active reports null (indicator dismisses) ...
            resp = await client.get("/v1/voice-note/active", headers=_AUTH)
            assert resp.status_code == 200, resp.text
            assert resp.json()["active"] is None

            # ... but the raw state is still in app.state so /stop can
            # build a response from it on the post-force-stop call.
            assert app.state.voice_note_active is not None


async def test_voice_note_stop_after_force_stop_returns_response(replay_env):
    """``/voice-note/stop`` accepts a call after a scheduler force-stop
    and returns a normal ``VoiceNoteStopResponse`` so the menu bar can
    show the save window even when the user never clicked Stop.
    """
    app = create_app()
    async with _LifespanContext(app):
        await _grant_mic(app)
        await _force_transcription_ready(app)
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/voice-note/start",
                json={"triggered_by": "menu_bar"},
                headers=_AUTH,
            )
            assert r1.status_code == 200
            start_body = r1.json()

            # Force-stop via orchestrator.
            await app.state.orchestrator.stop_session(
                app.state.voice_note_active["session_id"],
                reason="voice_note_cap_reached",
            )

            r2 = await client.post("/v1/voice-note/stop", headers=_AUTH)
            assert r2.status_code == 200, r2.text
            body = r2.json()
            assert body["voice_note_id"] == start_body["voice_note_id"]
            assert body["session_id"] == start_body["session_id"]
            # State cleared after the (idempotent) stop response.
            assert app.state.voice_note_active is None
