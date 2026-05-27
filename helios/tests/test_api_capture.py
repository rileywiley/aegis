"""Tests for ``helios.api.routes.capture`` (Phase-2 manual capture API).

Phase 2 (Wave 3 / Track 2E) reshaped these endpoints to match HELIOS.md
§7.3:

* every endpoint requires ``Authorization: Bearer <token>`` (the
  ``replay_env`` fixture writes ``bearer_token = "test"``);
* ``POST /v1/capture/start`` returns ``{session_id, kind, started_at}``
  and validates ``kind`` against the §7.3 enum (``continuous`` /
  ``manual_screen``);
* ``POST /v1/capture/stop`` is bodyless and idempotent — returns
  ``{session_id, ended_at, end_reason}`` with ``session_id=null`` when
  nothing was active.

These cases cover the orchestrator-RuntimeError → 409 mapping and the
idempotent-stop contract; full per-endpoint coverage lives in
``test_api.py``.
"""

from __future__ import annotations

import httpx
import pytest

from helios.api import create_app
from tests.conftest import synth_wav

# A second copy of LifespanContext + replay_env would be redundant; we
# reuse the ones in test_api.py via direct import once that file lands.
# For now, keep them local to this module so existing imports stay green.


class _LifespanContext:
    """Minimal stand-in for asgi-lifespan's LifespanManager."""

    def __init__(self, app):
        self._app = app

    async def __aenter__(self):
        self._scope = {"type": "lifespan"}
        import asyncio

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


_AUTH = {"Authorization": "Bearer test"}


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


async def test_start_capture_returns_session_id(replay_env):
    """POST /v1/capture/start returns 200 + session_id + kind + started_at."""
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
            assert isinstance(body["session_id"], int) and body["session_id"] >= 1
            assert body["kind"] == "continuous"
            assert isinstance(body["started_at"], (int, float))

            stop = await client.post("/v1/capture/stop", headers=_AUTH)
            assert stop.status_code == 200, stop.text


async def test_start_capture_while_active_returns_409(replay_env):
    """A second start while one is active returns 409 with §7.2 envelope."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            assert r1.status_code == 200

            r2 = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            assert r2.status_code == 409, r2.text
            body = r2.json()
            assert body["error"] == "session_already_active"
            assert body["code"] == 409
            assert "already active" in body["detail"]

            stop = await client.post("/v1/capture/stop", headers=_AUTH)
            assert stop.status_code == 200


async def test_stop_capture_returns_session_info(replay_env):
    """POST /v1/capture/stop with active session returns the stopped row."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            sid = r1.json()["session_id"]

            stop = await client.post("/v1/capture/stop", headers=_AUTH)
            assert stop.status_code == 200, stop.text
            body = stop.json()
            assert body["session_id"] == sid
            assert body["ended_at"] is not None
            assert body["end_reason"] == "user_stop"


async def test_stop_capture_when_idle_returns_null(replay_env):
    """Idempotent stop: no active session ⇒ session_id=null with 200."""
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            stop = await client.post("/v1/capture/stop", headers=_AUTH)
            assert stop.status_code == 200, stop.text
            body = stop.json()
            assert body["session_id"] is None
            assert body["ended_at"] is None
            assert body["end_reason"] is None
