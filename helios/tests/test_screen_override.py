"""Tests for the Phase-5B screen-capture override (HELIOS_BUILD_PLAN §5B / §12.6).

Coverage:

* ``POST /v1/capture/enable-screen-override`` updates BOTH
  ``capture_sessions.screen_capture_override_until`` AND the scheduler's
  in-memory ``ActiveSessionInfo.screen_capture_override_until``.
* The new value flows through ``GET /v1/status`` under
  ``active_session.screen_capture_override_until``.
* Idempotent / replace-with-latest semantics: a second call wins even
  when it shortens the window.
* 404 paths: no active session, mismatched ``session_id``, scheduler
  not tracking the session.
* The scheduler-level ``set_screen_capture_override`` API used by the
  route handler.
* ``queries.update_session_screen_override`` helper round-trips the
  column.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from helios.api import create_app
from helios.api.schemas import CaptureScreenOverrideRequest
from helios.clock import VirtualClock
from helios.config import HeliosConfig
from helios.db import queries
from helios.db.migrations import get_migrations_dir, run_migrations
from helios.scheduler.scheduler import Scheduler
from helios.state import DaemonStateMachine
from tests.conftest import synth_wav

_AUTH = {"Authorization": "Bearer test"}


# ---------------------------------------------------------------------------
# Lifespan helper + replay_env fixture — duplicated from test_api_capture so
# the file is self-contained (per the build-plan testing pattern).
# ---------------------------------------------------------------------------


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
        assert msg["type"] in (
            "lifespan.startup.complete",
            "lifespan.startup.failed",
        )
        if msg["type"] == "lifespan.startup.failed":
            raise RuntimeError(f"lifespan startup failed: {msg.get('message')}")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._recv_q.put({"type": "lifespan.shutdown"})
        msg = await self._send_q.get()
        assert msg["type"] in (
            "lifespan.shutdown.complete",
            "lifespan.shutdown.failed",
        )
        await self._app_task


@pytest.fixture
def replay_env(monkeypatch, tmp_path):
    """Enable HELIOS_REPLAY=1 with synth fixtures + per-test storage root."""
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
# Unit tests — schema validation
# ---------------------------------------------------------------------------


def test_request_schema_requires_a_duration_field():
    """The validator rejects bodies that omit both duration fields."""
    with pytest.raises(ValueError):
        CaptureScreenOverrideRequest()


def test_request_schema_rejects_both_duration_fields():
    """Belt-and-suspenders: providing both duration fields is ambiguous."""
    with pytest.raises(ValueError):
        CaptureScreenOverrideRequest(duration_seconds=60, duration_minutes=1)


def test_request_schema_accepts_duration_seconds():
    req = CaptureScreenOverrideRequest(duration_seconds=1800)
    assert req.effective_duration_seconds == 1800


def test_request_schema_accepts_legacy_duration_minutes():
    req = CaptureScreenOverrideRequest(duration_minutes=30)
    assert req.effective_duration_seconds == 30 * 60


def test_request_schema_accepts_session_id():
    req = CaptureScreenOverrideRequest(session_id=42, duration_seconds=600)
    assert req.session_id == 42
    assert req.effective_duration_seconds == 600


# ---------------------------------------------------------------------------
# Unit tests — scheduler API surface
# ---------------------------------------------------------------------------


class _StubOrchestrator:
    active_session_id: int | None = None

    async def start_session(self, kind, calendar_events=None):  # pragma: no cover
        raise NotImplementedError

    async def stop_session(self, session_id, reason):  # pragma: no cover
        raise NotImplementedError


class _StubCalendarSource:
    async def get_upcoming(self, horizon_seconds: int = 3600):
        return []


def _make_scheduler(clock: VirtualClock) -> Scheduler:
    return Scheduler(
        clock=clock,
        calendar_source=_StubCalendarSource(),
        orchestrator=_StubOrchestrator(),
        state_machine=DaemonStateMachine(),
        config=HeliosConfig(),
    )


async def test_set_screen_capture_override_updates_active_session_info(virtual_clock):
    """The new scheduler helper flips ActiveSessionInfo.screen_capture_override_until."""
    sched = _make_scheduler(virtual_clock)
    await sched.notify_session_started(7, "continuous")
    until_ts = virtual_clock.time() + 1800

    result = sched.set_screen_capture_override(7, until_ts)
    assert result == pytest.approx(until_ts)

    info = sched.get_active_session_info()
    assert info is not None
    assert info.screen_capture_override_until == pytest.approx(until_ts)


async def test_set_screen_capture_override_returns_none_for_unknown_session(
    virtual_clock,
):
    """Sessions the scheduler hasn't seen return None (route maps to 404)."""
    sched = _make_scheduler(virtual_clock)
    assert sched.set_screen_capture_override(999, virtual_clock.time() + 60) is None


async def test_set_screen_capture_override_replace_with_latest(virtual_clock):
    """Most recent call wins, even if it shortens the override (§5B)."""
    sched = _make_scheduler(virtual_clock)
    await sched.notify_session_started(11, "continuous")
    now = virtual_clock.time()

    sched.set_screen_capture_override(11, now + 3600)  # 1 hour
    sched.set_screen_capture_override(11, now + 300)  # then 5 min
    info = sched.get_active_session_info()
    assert info is not None
    assert info.screen_capture_override_until == pytest.approx(now + 300)


# ---------------------------------------------------------------------------
# Unit tests — queries.update_session_screen_override
# ---------------------------------------------------------------------------


async def _open_db(tmp_path: Path):
    """Open a fresh aiosqlite db with the real migrations applied."""
    import aiosqlite

    db_path = tmp_path / "index.db"
    db = await aiosqlite.connect(db_path)
    await db.execute("PRAGMA foreign_keys = ON")
    await run_migrations(db, get_migrations_dir())
    return db


async def test_update_session_screen_override_writes_column(tmp_path):
    db = await _open_db(tmp_path)
    try:
        sid = await queries.create_session(db, kind="continuous")

        ok = await queries.update_session_screen_override(db, sid, 1712345678.5)
        assert ok is True

        row = await queries.get_session_by_id(db, sid)
        assert row is not None
        assert row.screen_capture_override_until == pytest.approx(1712345678.5)
    finally:
        await db.close()


async def test_update_session_screen_override_returns_false_for_missing(tmp_path):
    db = await _open_db(tmp_path)
    try:
        ok = await queries.update_session_screen_override(db, 999, 1.0)
        assert ok is False
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Endpoint tests — full ASGI lifespan
# ---------------------------------------------------------------------------


async def test_enable_screen_override_flips_scheduler_and_db(replay_env):
    """Round trip: endpoint flips scheduler + DB; status mirrors the value."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            start = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            assert start.status_code == 200, start.text
            sid = start.json()["session_id"]

            resp = await client.post(
                "/v1/capture/enable-screen-override",
                json={"session_id": sid, "duration_seconds": 1800},
                headers=_AUTH,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["ok"] is True
            assert body["session_id"] == sid
            assert body["duration_seconds"] == 1800
            assert body["screen_capture_override_until"] is not None
            until_ts = body["screen_capture_override_until"]

            # /v1/status must reflect the new override on the next call.
            status_resp = await client.get("/v1/status", headers=_AUTH)
            assert status_resp.status_code == 200
            active = status_resp.json()["active_session"]
            assert active is not None
            assert active["id"] == sid
            assert active["screen_capture_override_until"] == pytest.approx(until_ts)

            # Scheduler bookkeeping mirrors what the response advertised.
            scheduler = app.state.scheduler
            info = scheduler.get_active_session_info()
            assert info is not None
            assert info.screen_capture_override_until == pytest.approx(until_ts)

            # The DB column persisted the value so a future OCR worker
            # can read it after a restart / from another process.
            db = app.state.db_pool.writer
            row = await queries.get_session_by_id(db, sid)
            assert row is not None
            assert row.screen_capture_override_until == pytest.approx(until_ts)

            await client.post("/v1/capture/stop", headers=_AUTH)


async def test_enable_screen_override_omitted_session_id_uses_active(replay_env):
    """Body without session_id targets the orchestrator's active session."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            start = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            sid = start.json()["session_id"]

            resp = await client.post(
                "/v1/capture/enable-screen-override",
                json={"duration_seconds": 600},
                headers=_AUTH,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["session_id"] == sid

            await client.post("/v1/capture/stop", headers=_AUTH)


async def test_enable_screen_override_legacy_duration_minutes(replay_env):
    """Legacy menu-bar clients sending duration_minutes still work."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )

            resp = await client.post(
                "/v1/capture/enable-screen-override",
                json={"duration_minutes": 30},
                headers=_AUTH,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["duration_minutes"] == 30
            assert body["duration_seconds"] == 30 * 60

            await client.post("/v1/capture/stop", headers=_AUTH)


async def test_enable_screen_override_no_active_session_returns_404(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/capture/enable-screen-override",
                json={"duration_seconds": 600},
                headers=_AUTH,
            )
            assert resp.status_code == 404, resp.text
            body = resp.json()
            assert body["error"] == "no_active_session"
            assert body["code"] == 404


async def test_enable_screen_override_wrong_session_id_returns_404(replay_env):
    """Specifying a session_id that doesn't match the active one returns 404."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            start = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            sid = start.json()["session_id"]

            resp = await client.post(
                "/v1/capture/enable-screen-override",
                json={"session_id": sid + 9999, "duration_seconds": 600},
                headers=_AUTH,
            )
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"] == "session_not_active"

            await client.post("/v1/capture/stop", headers=_AUTH)


async def test_enable_screen_override_replace_with_latest_via_endpoint(replay_env):
    """Two consecutive calls leave the most-recent value in place."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )

            r1 = await client.post(
                "/v1/capture/enable-screen-override",
                json={"duration_seconds": 3600},
                headers=_AUTH,
            )
            assert r1.status_code == 200
            long_until = r1.json()["screen_capture_override_until"]

            r2 = await client.post(
                "/v1/capture/enable-screen-override",
                json={"duration_seconds": 60},
                headers=_AUTH,
            )
            assert r2.status_code == 200
            short_until = r2.json()["screen_capture_override_until"]

            assert short_until < long_until
            # Status reflects the most recent (shorter) value.
            status_resp = await client.get("/v1/status", headers=_AUTH)
            active = status_resp.json()["active_session"]
            assert active["screen_capture_override_until"] == pytest.approx(
                short_until
            )

            await client.post("/v1/capture/stop", headers=_AUTH)


async def test_enable_screen_override_requires_auth(replay_env):
    """No Authorization header → 401 (envelope-tested in test_api.py)."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/capture/enable-screen-override",
                json={"duration_seconds": 600},
            )
            assert resp.status_code == 401
