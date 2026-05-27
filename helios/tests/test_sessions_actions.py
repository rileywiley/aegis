"""Tests for Wave 6D session-action endpoints + list filters.

Coverage:

* ``POST /v1/sessions/{id}/re-transcribe`` happy path + 404.
* ``POST /v1/sessions/{id}/re-diarize`` happy path (without worker
  wired) + 404 + segments-cleared invariant.
* ``GET /v1/sessions?date=YYYY-MM-DD`` filter — local-tz day window.
* ``GET /v1/sessions?status=active`` filter — survives the extension.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path

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
    mic = synth_wav(tmp_path / "fx" / "mic.wav", duration_s=5.0, freq_hz=440.0)
    sysw = synth_wav(tmp_path / "fx" / "sys.wav", duration_s=5.0, freq_hz=880.0)
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
# Helpers — seed sessions + chunks directly via queries.
# ---------------------------------------------------------------------------


async def _seed_session_with_chunks_and_segments(
    db, *, started_at: float, n_chunks: int = 2
) -> tuple[int, list[int]]:
    sid = await queries.create_session(db, "continuous", started_at=started_at)
    await queries.update_session_ended(
        db, session_id=sid, ended_at=started_at + 60, end_reason="test"
    )
    chunk_ids: list[int] = []
    for i in range(n_chunks):
        cid = await queries.insert_audio_chunk(
            db,
            session_id=sid,
            channel="mic" if i % 2 == 0 else "system",
            start_ts=started_at + i,
            end_ts=started_at + i + 30,
            path=f"/tmp/seed_s{sid}_c{i}.wav",
            samples=16000 * 30,
        )
        chunk_ids.append(cid)
        await queries.mark_chunk_transcribed(db, cid)
        await queries.insert_transcript_segment(
            db,
            chunk_id=cid,
            segment_index=0,
            start_ts=started_at + i,
            end_ts=started_at + i + 5,
            text="seed",
            speaker="user",
        )
    return sid, chunk_ids


# ---------------------------------------------------------------------------
# re-transcribe
# ---------------------------------------------------------------------------


async def test_re_transcribe_clears_segments_and_requeues(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        db = app.state.db_pool.writer
        sid, chunk_ids = await _seed_session_with_chunks_and_segments(
            db, started_at=time.time() - 600
        )

        async with await _client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{sid}/re-transcribe", headers=_AUTH
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["session_id"] == sid
            assert body["chunks_requeued"] == len(chunk_ids)

        # Segments cleared.
        segs = await queries.get_segments_for_session(db, sid)
        assert segs == []
        # Chunks back to status='recorded' with no transcribed_at.
        chunks = await queries.get_session_audio_chunks(db, sid)
        for c in chunks:
            assert c.status == "recorded"
            assert c.transcribed_at is None


async def test_re_transcribe_404(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/sessions/999999/re-transcribe", headers=_AUTH
            )
            assert resp.status_code == 404
            assert resp.json()["error"] == "session_not_found"


# ---------------------------------------------------------------------------
# re-diarize
# ---------------------------------------------------------------------------


async def test_re_diarize_resets_status_and_clears_turns(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        db = app.state.db_pool.writer
        # Seed an ended session with a diarization_turn.
        sid = await queries.create_session(db, "continuous")
        await queries.update_session_ended(
            db,
            session_id=sid,
            ended_at=time.time(),
            end_reason="test",
        )
        await queries.update_session_diarization_status(
            db, sid, status="complete"
        )
        await queries.insert_diarization_turn(
            db,
            session_id=sid,
            speaker_label="SPEAKER_00",
            start_ts=time.time(),
            end_ts=time.time() + 10,
            embedding=None,
        )

        async with await _client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{sid}/re-diarize", headers=_AUTH
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["session_id"] == sid
            # Default config has diarization disabled, so the worker is
            # wired but enqueue still succeeds — ``jobs_requeued`` will
            # be 1 because the queue accepts the id even when the loop
            # is idle.
            assert body["jobs_requeued"] in (0, 1)

        # Turns cleared + status reset.
        turns = await queries.get_diarization_turns_for_session(db, sid)
        assert turns == []
        row = await queries.get_session_by_id(db, sid)
        assert row.diarization_status == "pending"


async def test_re_diarize_404(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/sessions/999999/re-diarize", headers=_AUTH
            )
            assert resp.status_code == 404


async def test_re_diarize_handles_missing_worker(replay_env):
    """When diarization_worker is None, jobs_requeued == 0."""
    app = create_app()
    async with _LifespanContext(app):
        db = app.state.db_pool.writer
        sid = await queries.create_session(db, "continuous")
        await queries.update_session_ended(
            db, session_id=sid, ended_at=time.time(), end_reason="test"
        )
        # Force the worker to None.
        app.state.diarization_worker = None

        async with await _client(app) as client:
            resp = await client.post(
                f"/v1/sessions/{sid}/re-diarize", headers=_AUTH
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["jobs_requeued"] == 0


# ---------------------------------------------------------------------------
# /v1/sessions list — date + status filters
# ---------------------------------------------------------------------------


async def test_list_sessions_status_active_filter(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        db = app.state.db_pool.writer
        # One ended, one active.
        sid_ended = await queries.create_session(
            db, "continuous", started_at=time.time() - 3600
        )
        await queries.update_session_ended(
            db,
            session_id=sid_ended,
            ended_at=time.time() - 1800,
            end_reason="test",
        )
        sid_active = await queries.create_session(
            db, "continuous", started_at=time.time() - 60
        )

        async with await _client(app) as client:
            resp = await client.get(
                "/v1/sessions?status=active", headers=_AUTH
            )
            assert resp.status_code == 200, resp.text
            ids = {s["id"] for s in resp.json()["sessions"]}
            assert sid_active in ids
            assert sid_ended not in ids


async def test_list_sessions_date_filter(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        db = app.state.db_pool.writer

        today_local = datetime.now().astimezone()
        # Build "today at 10am" and "yesterday at 10am" in local tz.
        today_10am = today_local.replace(
            hour=10, minute=0, second=0, microsecond=0
        ).timestamp()
        yesterday_10am = today_10am - 24 * 3600

        sid_today = await queries.create_session(
            db, "continuous", started_at=today_10am
        )
        sid_yesterday = await queries.create_session(
            db, "continuous", started_at=yesterday_10am
        )
        # End both so they're not "active".
        await queries.update_session_ended(
            db, session_id=sid_today, ended_at=today_10am + 60,
            end_reason="test",
        )
        await queries.update_session_ended(
            db, session_id=sid_yesterday, ended_at=yesterday_10am + 60,
            end_reason="test",
        )

        today_str = today_local.strftime("%Y-%m-%d")

        async with await _client(app) as client:
            resp = await client.get(
                f"/v1/sessions?date={today_str}", headers=_AUTH
            )
            assert resp.status_code == 200, resp.text
            ids = {s["id"] for s in resp.json()["sessions"]}
            assert sid_today in ids
            assert sid_yesterday not in ids


async def test_list_sessions_invalid_date_returns_422(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get(
                "/v1/sessions?date=not-a-date", headers=_AUTH
            )
            assert resp.status_code == 422
            assert resp.json()["error"] == "invalid_date"


async def test_list_sessions_no_filters_returns_all(replay_env):
    """Backwards compat: omitting status + date returns the full list."""
    app = create_app()
    async with _LifespanContext(app):
        db = app.state.db_pool.writer
        sid = await queries.create_session(db, "continuous")
        async with await _client(app) as client:
            resp = await client.get("/v1/sessions", headers=_AUTH)
            assert resp.status_code == 200
            ids = {s["id"] for s in resp.json()["sessions"]}
            assert sid in ids
