"""Tests for ``DELETE /v1/sessions/{session_id}`` — Phase 5 (Wave 5C).

Coverage:

* 404 when the session does not exist.
* 200 + ``SessionDeleteResponse`` on success — including the
  ``chunks_trashed`` count and ``was_active=false`` for an ended session.
* Active session deletion: stops the orchestrator + scheduler, returns
  ``was_active=true``, removes the row.
* WAVs are physically moved into ``<storage.root>/trash/`` rather than
  deleted outright (so the 24h trash hold still applies).
* DB cascade fires — chunks / segments rows for that session disappear.

Uses the same replay-env scaffolding as ``test_voice_note_endpoints.py``
so the FastAPI lifespan actually wires the orchestrator, scheduler, and
DB pool we depend on.
"""

from __future__ import annotations

import asyncio
import time
import wave
from pathlib import Path

import httpx
import numpy as np
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
    )
    from helios import config as helios_config

    monkeypatch.setattr(helios_config, "_CONFIG_PATH", config_path)
    monkeypatch.setattr(helios_config, "_cached_config", None)
    return storage_root


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Helpers — seed a session with a WAV directly in the DB so the test
# doesn't need to wait for the chunker to flush real audio.
# ---------------------------------------------------------------------------


def _write_silent_wav(path: Path, *, samples: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = np.zeros(samples, dtype=np.int16)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(frames.tobytes())


async def _seed_ended_session_with_chunks(
    app, storage_root: Path, *, n_chunks: int = 2
) -> tuple[int, list[int], list[Path]]:
    """Insert a fake ended session + ``n_chunks`` chunks with real WAVs.

    Returns (session_id, chunk_ids, wav_paths). Chunks are flipped to
    ``status='transcribed'`` so :func:`trash_session_audio` includes
    them when called via the DELETE path (it accepts any status as long
    as ``path`` is non-NULL).
    """
    db = app.state.db_pool.writer
    sid = await queries.create_session(db, "manual_screen", started_at=time.time())
    await queries.end_session(db, sid, end_reason="test_seed")
    chunk_ids: list[int] = []
    wav_paths: list[Path] = []
    for i in range(n_chunks):
        wav = storage_root / "audio" / f"sess{sid}_chunk{i}.wav"
        _write_silent_wav(wav)
        cid = await queries.insert_audio_chunk(
            db,
            session_id=sid,
            channel="mic" if i % 2 == 0 else "system",
            start_ts=time.time() - 60 + i,
            end_ts=time.time() - 30 + i,
            path=str(wav),
            samples=16000 * 30,
        )
        chunk_ids.append(cid)
        wav_paths.append(wav)
    return sid, chunk_ids, wav_paths


# ---------------------------------------------------------------------------
# 404 cases
# ---------------------------------------------------------------------------


async def test_delete_session_404_when_missing(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.delete("/v1/sessions/999999", headers=_AUTH)
            assert resp.status_code == 404, resp.text
            envelope = resp.json()
            assert envelope["error"] == "session_not_found"
            assert envelope["code"] == 404


# ---------------------------------------------------------------------------
# Happy path — non-active session
# ---------------------------------------------------------------------------


async def test_delete_ended_session_200_trashes_wavs(replay_env):
    storage_root: Path = replay_env
    app = create_app()
    async with _LifespanContext(app):
        sid, chunk_ids, wav_paths = await _seed_ended_session_with_chunks(
            app, storage_root, n_chunks=2
        )
        async with await _client(app) as client:
            resp = await client.delete(
                f"/v1/sessions/{sid}", headers=_AUTH
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["session_id"] == sid
            assert body["chunks_trashed"] == 2
            assert body["was_active"] is False

            # The session row is gone.
            resp_get = await client.get(
                f"/v1/sessions/{sid}", headers=_AUTH
            )
            assert resp_get.status_code == 404

        # WAVs moved into <storage_root>/trash/ rather than deleted.
        trash = storage_root / "trash"
        assert trash.exists()
        trash_files = list(trash.iterdir())
        assert len(trash_files) == 2
        # Filenames are prefixed with chunk_id.
        names = " ".join(p.name for p in trash_files)
        for cid in chunk_ids:
            assert str(cid) in names
        # And the originals are gone.
        for wav in wav_paths:
            assert not wav.exists()


# ---------------------------------------------------------------------------
# DB cascade — chunks rows go with the session.
# ---------------------------------------------------------------------------


async def test_delete_session_cascades_chunks(replay_env):
    storage_root: Path = replay_env
    app = create_app()
    async with _LifespanContext(app):
        db = app.state.db_pool.writer
        sid, chunk_ids, _wav_paths = await _seed_ended_session_with_chunks(
            app, storage_root, n_chunks=2
        )
        async with await _client(app) as client:
            resp = await client.delete(
                f"/v1/sessions/{sid}", headers=_AUTH
            )
            assert resp.status_code == 200, resp.text

        # Verify the audio_chunks rows are gone (ON DELETE CASCADE).
        rows = await queries.get_session_audio_chunks(db, sid)
        assert rows == []
        for cid in chunk_ids:
            assert await queries.get_chunk_by_id(db, cid) is None


# ---------------------------------------------------------------------------
# Active session — must be stopped before delete.
# ---------------------------------------------------------------------------


async def test_delete_active_session_stops_and_deletes(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/capture/start",
                json={"kind": "continuous"},
                headers=_AUTH,
            )
            assert r1.status_code == 200, r1.text
            sid = r1.json()["session_id"]

            # Sanity: orchestrator considers the session active.
            assert app.state.orchestrator.active_session_id == sid

            resp = await client.delete(
                f"/v1/sessions/{sid}", headers=_AUTH
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["session_id"] == sid
            assert body["was_active"] is True

            # Orchestrator has released the session.
            assert app.state.orchestrator.active_session_id != sid

            # And the row is gone.
            resp_get = await client.get(
                f"/v1/sessions/{sid}", headers=_AUTH
            )
            assert resp_get.status_code == 404


# ---------------------------------------------------------------------------
# Auth — DELETE rejects missing token (regression coverage; covered by
# the parametrized test in test_api.py but worth keeping local).
# ---------------------------------------------------------------------------


async def test_delete_session_requires_auth(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.delete("/v1/sessions/1")
            assert resp.status_code == 401
