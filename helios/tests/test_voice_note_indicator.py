"""Tests for the floating voice-note recording indicator (Track 4G).

The indicator owns a PyObjC NSWindow on macOS, but the test environment
mocks AppKit/Foundation so we can run on any platform. Tests focus on
LOGIC — polling cadence, state transitions, color flag, drag
persistence — not pixel-rendering.

The daemon-side ``current_rms`` field is also covered here because the
menu-bar indicator depends on it. Three integration cases hit the
FastAPI route via ``httpx.ASGITransport`` — same pattern as
``tests/test_voice_note_endpoints.py``.
"""

from __future__ import annotations

import asyncio
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import numpy as np
import pytest

from helios.api import create_app
from helios.api.routes import voice_note as voice_note_route
from helios.api.schemas import ActiveVoiceNote, ActiveVoiceNoteResponse
from helios.db import queries
from helios.menubar.client import (
    DaemonTimeout,
    DaemonUnreachable,
)
from helios.menubar.voice_note_indicator import (
    VoiceNoteIndicator,
    _resolve_position,
)
from tests.conftest import synth_wav

_AUTH = {"Authorization": "Bearer test"}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_config(position: Any = "top_right", last_position: Any = None) -> Any:
    """Construct a minimal HeliosConfig stub with voice_note.indicator."""
    indicator = SimpleNamespace(
        floating_pill_position=position,
        last_position=last_position,
        show_elapsed_time=True,
        show_audio_level=True,
    )
    voice_note = SimpleNamespace(indicator=indicator, max_duration_seconds=300)
    return SimpleNamespace(voice_note=voice_note)


def _make_active_response(
    *,
    elapsed: float = 5.0,
    rms: float = 0.0,
    approaching_cap: bool = False,
) -> ActiveVoiceNoteResponse:
    return ActiveVoiceNoteResponse(
        active=ActiveVoiceNote(
            voice_note_id=1,
            session_id=1,
            started_at=time.time() - elapsed,
            elapsed_seconds=elapsed,
            is_excerpt=False,
            max_duration_seconds=300,
            approaching_cap=approaching_cap,
            current_rms=rms,
        )
    )


def _stub_appkit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``_import_appkit`` with a stub that returns mock modules.

    The stub's namespaces are loose ``MagicMock``s — every attribute
    access returns another ``MagicMock``, so calls like
    ``ak.NSWindow.alloc().init...()`` all return mocks. That's plenty
    for the logic tests; we never assert pixels.
    """
    from helios.menubar import voice_note_indicator as mod

    appkit = MagicMock(name="AppKit")
    foundation = MagicMock(name="Foundation")
    objc = MagicMock(name="objc")

    bundle = mod._PyObjCImports()
    bundle.AppKit = appkit
    bundle.Foundation = foundation
    bundle.objc = objc

    def _fake_import() -> mod._PyObjCImports:
        return bundle

    monkeypatch.setattr(mod, "_import_appkit", _fake_import)
    # Also pre-stub ``objc.selector`` since the production code does a
    # late import in :meth:`_start_polling`.
    objc_module = MagicMock(name="objc_module")
    objc_module.selector = lambda fn, signature=b"v@:@": fn
    monkeypatch.setitem(sys.modules, "objc", objc_module)


# ---------------------------------------------------------------------------
# 1) show() starts polling at 250ms intervals
# ---------------------------------------------------------------------------


def test_show_starts_polling_timer(monkeypatch):
    _stub_appkit(monkeypatch)
    client = MagicMock()
    client.get_active_voice_note.return_value = _make_active_response()

    config = _make_config()
    indicator = VoiceNoteIndicator(client=client, config=config)
    indicator.show()

    assert indicator.visible is True
    assert indicator._poll_timer is not None
    # Verify the timer was scheduled at 250ms.
    appkit = indicator._appkit.AppKit
    call = appkit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_
    # First call is the poll timer.
    assert call.call_args_list[0].args[0] == 0.25
    indicator.dismiss()


# ---------------------------------------------------------------------------
# 2) dismiss() stops polling and closes window
# ---------------------------------------------------------------------------


def test_dismiss_stops_polling_and_closes(monkeypatch):
    _stub_appkit(monkeypatch)
    client = MagicMock()
    client.get_active_voice_note.return_value = _make_active_response()

    config = _make_config()
    indicator = VoiceNoteIndicator(client=client, config=config)
    indicator.show()
    poll_timer = indicator._poll_timer

    indicator.dismiss()

    assert indicator.visible is False
    assert indicator._poll_timer is None
    assert indicator._window is None
    poll_timer.invalidate.assert_called()


# ---------------------------------------------------------------------------
# 3) Polling response with active=null → triggers dismiss
# ---------------------------------------------------------------------------


def test_poll_with_inactive_dismisses(monkeypatch):
    _stub_appkit(monkeypatch)
    client = MagicMock()
    client.get_active_voice_note.return_value = ActiveVoiceNoteResponse(
        active=None
    )

    config = _make_config()
    indicator = VoiceNoteIndicator(client=client, config=config)
    indicator.show()
    indicator.handle_poll_tick()

    assert indicator.visible is False


# ---------------------------------------------------------------------------
# 4) Polling response with active set → updates elapsed + rms
# ---------------------------------------------------------------------------


def test_poll_updates_elapsed_and_rms(monkeypatch):
    _stub_appkit(monkeypatch)
    client = MagicMock()
    client.get_active_voice_note.return_value = _make_active_response(
        elapsed=42.5, rms=0.6
    )

    config = _make_config()
    indicator = VoiceNoteIndicator(client=client, config=config)
    indicator.show()
    indicator.handle_poll_tick()

    assert indicator.elapsed_seconds == 42.5
    assert indicator.current_rms == 0.6
    indicator.dismiss()


# ---------------------------------------------------------------------------
# 5) approaching_cap=True → color flag flips amber
# ---------------------------------------------------------------------------


def test_approaching_cap_turns_color_amber(monkeypatch):
    _stub_appkit(monkeypatch)
    client = MagicMock()
    client.get_active_voice_note.return_value = _make_active_response(
        approaching_cap=True
    )

    indicator = VoiceNoteIndicator(client=client, config=_make_config())
    indicator.show()
    indicator.handle_poll_tick()

    assert indicator.color_state == "amber"
    assert indicator.approaching_cap is True
    indicator.dismiss()


# ---------------------------------------------------------------------------
# 6) approaching_cap=False → color stays red
# ---------------------------------------------------------------------------


def test_approaching_cap_false_stays_red(monkeypatch):
    _stub_appkit(monkeypatch)
    client = MagicMock()
    client.get_active_voice_note.return_value = _make_active_response(
        approaching_cap=False
    )

    indicator = VoiceNoteIndicator(client=client, config=_make_config())
    indicator.show()
    indicator.handle_poll_tick()

    assert indicator.color_state == "red"
    assert indicator.approaching_cap is False
    indicator.dismiss()


# ---------------------------------------------------------------------------
# 7) Daemon unreachable → dismiss called
# ---------------------------------------------------------------------------


def test_poll_handles_daemon_unreachable(monkeypatch):
    _stub_appkit(monkeypatch)
    client = MagicMock()
    client.get_active_voice_note.side_effect = DaemonUnreachable("nope")

    indicator = VoiceNoteIndicator(client=client, config=_make_config())
    indicator.show()
    assert indicator.visible is True

    indicator.handle_poll_tick()

    assert indicator.visible is False


def test_poll_handles_daemon_timeout(monkeypatch):
    _stub_appkit(monkeypatch)
    client = MagicMock()
    client.get_active_voice_note.side_effect = DaemonTimeout("slow")

    indicator = VoiceNoteIndicator(client=client, config=_make_config())
    indicator.show()
    indicator.handle_poll_tick()

    assert indicator.visible is False


# ---------------------------------------------------------------------------
# 8) Initial position from config (top-right default)
# ---------------------------------------------------------------------------


def test_default_top_right_position():
    # 1440×900 screen: indicator (220×60) at top-right with 10px inset
    # → x = 1440 - 220 - 10 = 1210, y = 900 - 60 - 10 = 830.
    x, y = _resolve_position("top_right", screen_size=(1440, 900))
    assert x == 1210
    assert y == 830


def test_resolve_position_top_left():
    x, y = _resolve_position("top_left", screen_size=(1440, 900))
    assert x == 10
    assert y == 830


def test_resolve_position_bottom_right():
    x, y = _resolve_position("bottom_right", screen_size=(1440, 900))
    assert x == 1210
    assert y == 10


def test_resolve_position_unknown_string_falls_back_to_top_right():
    x, y = _resolve_position("middle_of_nowhere", screen_size=(1440, 900))
    assert (x, y) == (1210, 830)


# ---------------------------------------------------------------------------
# 9) Custom config position {"x", "y"} → window placed at those coords
# ---------------------------------------------------------------------------


def test_custom_dict_position_used():
    x, y = _resolve_position(
        {"x": 100, "y": 200}, screen_size=(1440, 900)
    )
    assert (x, y) == (100, 200)


def test_show_records_last_position_from_dict_config(monkeypatch):
    _stub_appkit(monkeypatch)
    client = MagicMock()
    client.get_active_voice_note.return_value = _make_active_response()

    config = _make_config(position={"x": 100, "y": 200})
    indicator = VoiceNoteIndicator(client=client, config=config)
    indicator.show()

    assert indicator.last_position == {"x": 100, "y": 200}
    indicator.dismiss()


# ---------------------------------------------------------------------------
# 10) Drag-end → persistence callback fired with new position
# ---------------------------------------------------------------------------


def test_drag_end_invokes_persist_callback(monkeypatch):
    _stub_appkit(monkeypatch)
    client = MagicMock()
    client.get_active_voice_note.return_value = _make_active_response()

    captured: list[dict[str, int]] = []

    def _persist(pos: dict[str, int]) -> None:
        captured.append(pos)

    indicator = VoiceNoteIndicator(
        client=client, config=_make_config(), persist_position=_persist
    )
    indicator.show()
    indicator.handle_drag_end(500, 600)

    assert captured == [{"x": 500, "y": 600}]
    assert indicator.last_position == {"x": 500, "y": 600}
    indicator.dismiss()


def test_drag_end_no_callback_does_not_raise(monkeypatch):
    _stub_appkit(monkeypatch)
    client = MagicMock()
    indicator = VoiceNoteIndicator(
        client=client, config=_make_config(), persist_position=None
    )
    indicator.show()
    # Should not raise.
    indicator.handle_drag_end(50, 60)
    assert indicator.last_position == {"x": 50, "y": 60}
    indicator.dismiss()


# ---------------------------------------------------------------------------
# 11) show() called twice → idempotent, single window
# ---------------------------------------------------------------------------


def test_show_is_idempotent(monkeypatch):
    _stub_appkit(monkeypatch)
    client = MagicMock()
    client.get_active_voice_note.return_value = _make_active_response()

    indicator = VoiceNoteIndicator(client=client, config=_make_config())
    indicator.show()
    first_window = indicator._window
    first_timer = indicator._poll_timer

    indicator.show()  # second call

    assert indicator._window is first_window
    assert indicator._poll_timer is first_timer
    assert indicator.visible is True
    indicator.dismiss()


# ---------------------------------------------------------------------------
# 12) PyObjC ImportError → show() raises clean RuntimeError on non-mac
# ---------------------------------------------------------------------------


def test_show_on_non_mac_raises_runtime_error(monkeypatch):
    from helios.menubar import voice_note_indicator as mod

    def _broken_import() -> Any:
        raise RuntimeError(
            "PyObjC (AppKit/Foundation) not available — VoiceNoteIndicator "
            "requires macOS"
        )

    monkeypatch.setattr(mod, "_import_appkit", _broken_import)

    indicator = VoiceNoteIndicator(client=MagicMock(), config=_make_config())
    with pytest.raises(RuntimeError, match="requires macOS"):
        indicator.show()


# ---------------------------------------------------------------------------
# Stop button → calls client.post_voice_note_stop and dismisses
# ---------------------------------------------------------------------------


def test_stop_button_sends_stop_and_dismisses(monkeypatch):
    _stub_appkit(monkeypatch)
    client = MagicMock()
    client.get_active_voice_note.return_value = _make_active_response()

    indicator = VoiceNoteIndicator(client=client, config=_make_config())
    indicator.show()
    indicator.handle_stop_clicked()

    client.post_voice_note_stop.assert_called_once()
    assert indicator.visible is False


def test_stop_button_swallows_client_failure(monkeypatch):
    _stub_appkit(monkeypatch)
    client = MagicMock()
    client.post_voice_note_stop.side_effect = DaemonUnreachable("nope")

    indicator = VoiceNoteIndicator(client=client, config=_make_config())
    indicator.show()
    # Should not raise.
    indicator.handle_stop_clicked()
    assert indicator.visible is False


# ---------------------------------------------------------------------------
# Daemon-side: GET /v1/voice-note/active populates current_rms
# ---------------------------------------------------------------------------


# Test 13-15 reuse the route module's RMS helper unit-style. We test
# the helper directly (cheap) and via the route (with a mocked queries
# layer so we don't have to spin up the full daemon lifespan).


def test_compute_recent_rms_with_silent_wav(tmp_path: Path):
    """A silent WAV → RMS very close to 0.0."""
    p = synth_wav(tmp_path / "silent.wav", duration_s=1.0, silent=True)
    rms = voice_note_route._compute_recent_rms(str(p))
    assert rms == 0.0


def test_compute_recent_rms_with_loud_wav(tmp_path: Path):
    """A 0.3-amplitude tone → RMS in (0.0, 1.0)."""
    p = synth_wav(
        tmp_path / "loud.wav", duration_s=1.0, freq_hz=440.0, amplitude=0.3
    )
    rms = voice_note_route._compute_recent_rms(str(p))
    # sin-wave RMS = amplitude / sqrt(2). 0.3/√2 ≈ 0.212.
    assert 0.1 < rms < 0.4


def test_compute_recent_rms_with_none_path():
    assert voice_note_route._compute_recent_rms(None) == 0.0


def test_compute_recent_rms_with_missing_file(tmp_path: Path):
    assert voice_note_route._compute_recent_rms(str(tmp_path / "nope.wav")) == 0.0


def test_compute_recent_rms_with_empty_wav(tmp_path: Path):
    """Zero-frame WAV → 0.0 without raising."""
    p = tmp_path / "empty.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        # No frames written.
    assert voice_note_route._compute_recent_rms(str(p)) == 0.0


def test_compute_recent_rms_clamps_to_unit_interval(tmp_path: Path):
    """Even a max-amplitude tone stays inside [0.0, 1.0]."""
    p = synth_wav(
        tmp_path / "max.wav", duration_s=1.0, freq_hz=440.0, amplitude=1.0
    )
    rms = voice_note_route._compute_recent_rms(str(p))
    assert 0.0 <= rms <= 1.0


# ---------------------------------------------------------------------------
# Route-level: GET /v1/voice-note/active returns current_rms
# ---------------------------------------------------------------------------


class _LifespanContext:
    """Mirror of tests/test_voice_note_endpoints.py — lifespan harness."""

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
    """Same env helper used by test_voice_note_endpoints.py."""
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


async def test_active_returns_current_rms_zero_when_no_active(replay_env):
    """GET /v1/voice-note/active → active=null when no note is running."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get("/v1/voice-note/active", headers=_AUTH)
            assert resp.status_code == 200
            assert resp.json()["active"] is None


async def test_active_returns_zero_rms_when_no_chunks(replay_env, monkeypatch):
    """Active note with zero audio chunks → current_rms = 0.0."""
    app = create_app()
    async with _LifespanContext(app):
        # Plant an active-voice-note state directly so we don't have to
        # boot the orchestrator. The route reads from app.state and from
        # queries.get_session_audio_chunks; we monkeypatch the latter
        # to return an empty list.
        app.state.voice_note_active = {
            "voice_note_id": 99,
            "session_id": 99,
            "started_at": time.time() - 1.0,
            "is_excerpt": False,
            "parent_session_id": None,
            "max_duration_seconds": 300,
            "triggered_by": "menu_bar",
        }
        # The /v1/voice-note/active endpoint cross-checks the
        # orchestrator's active_session_id against the planted state
        # (cycle-2 stale-state fix). Force the helper to report owned
        # so the response reports active != null without us having to
        # spin up a real orchestrator-tracked session.
        from helios.api.routes import voice_note as _vn_routes

        monkeypatch.setattr(
            _vn_routes, "_orchestrator_owns", lambda *_args, **_kw: True
        )

        async def _no_chunks(_db, _sid):
            return []

        monkeypatch.setattr(queries, "get_session_audio_chunks", _no_chunks)

        async with await _client(app) as client:
            resp = await client.get("/v1/voice-note/active", headers=_AUTH)
            assert resp.status_code == 200, resp.text
            body = resp.json()["active"]
            assert body is not None
            assert body["current_rms"] == 0.0


async def test_active_returns_rms_when_chunks_exist(
    replay_env, tmp_path, monkeypatch
):
    """Active note with a non-silent mic chunk → current_rms in (0, 1]."""
    app = create_app()
    async with _LifespanContext(app):
        # Write a non-silent fixture WAV that the RMS helper will read.
        wav_path = synth_wav(
            tmp_path / "chunk.wav",
            duration_s=1.0,
            freq_hz=440.0,
            amplitude=0.3,
        )

        app.state.voice_note_active = {
            "voice_note_id": 77,
            "session_id": 77,
            "started_at": time.time() - 2.0,
            "is_excerpt": False,
            "parent_session_id": None,
            "max_duration_seconds": 300,
            "triggered_by": "menu_bar",
        }
        from helios.api.routes import voice_note as _vn_routes

        monkeypatch.setattr(
            _vn_routes, "_orchestrator_owns", lambda *_a, **_kw: True
        )

        # Stand up a fake AudioChunkRow-shaped object — the route only
        # reads ``.channel`` and ``.path``.
        from helios.db.rows import AudioChunkRow

        chunk = AudioChunkRow(
            id=1,
            session_id=77,
            channel="mic",
            start_ts=time.time() - 2.0,
            end_ts=time.time() - 1.0,
            path=str(wav_path),
            samples=16000,
            partial=False,
            status="recorded",
            transcription_attempts=0,
        )

        async def _with_chunk(_db, _sid):
            return [chunk]

        monkeypatch.setattr(queries, "get_session_audio_chunks", _with_chunk)

        async with await _client(app) as client:
            resp = await client.get("/v1/voice-note/active", headers=_AUTH)
            assert resp.status_code == 200, resp.text
            body = resp.json()["active"]
            assert body is not None
            assert 0.0 < body["current_rms"] <= 1.0


async def test_active_skips_system_chunks_for_rms(
    replay_env, tmp_path, monkeypatch
):
    """System-channel chunks must NOT contribute to the live mic RMS."""
    app = create_app()
    async with _LifespanContext(app):
        wav_path = synth_wav(
            tmp_path / "sys.wav",
            duration_s=1.0,
            freq_hz=880.0,
            amplitude=0.3,
        )

        app.state.voice_note_active = {
            "voice_note_id": 88,
            "session_id": 88,
            "started_at": time.time() - 2.0,
            "is_excerpt": False,
            "parent_session_id": None,
            "max_duration_seconds": 300,
            "triggered_by": "menu_bar",
        }
        from helios.api.routes import voice_note as _vn_routes

        monkeypatch.setattr(
            _vn_routes, "_orchestrator_owns", lambda *_a, **_kw: True
        )

        from helios.db.rows import AudioChunkRow

        sys_chunk = AudioChunkRow(
            id=2,
            session_id=88,
            channel="system",
            start_ts=time.time() - 2.0,
            end_ts=time.time() - 1.0,
            path=str(wav_path),
            samples=16000,
            partial=False,
            status="recorded",
            transcription_attempts=0,
        )

        async def _system_only(_db, _sid):
            return [sys_chunk]

        monkeypatch.setattr(queries, "get_session_audio_chunks", _system_only)

        async with await _client(app) as client:
            resp = await client.get("/v1/voice-note/active", headers=_AUTH)
            assert resp.status_code == 200
            body = resp.json()["active"]
            assert body is not None
            # No mic chunk → 0.0 even though system audio exists.
            assert body["current_rms"] == 0.0
