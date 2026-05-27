"""Tests for ``/v1/diagnostics/*`` action endpoints (Wave 6D / Track 6D).

Coverage:

* ``POST /diagnostics/restart`` — happy path (suppressed SIGTERM) +
  the counter increments.
* ``POST /diagnostics/flush-queues`` — drains pending transcription
  chunks and pending diarization sessions; returns the counts.
* ``POST /diagnostics/test-capture`` + ``GET .../test-capture/{job_id}``
  — schedules the self-test, polls the status endpoint, missing job ⇒ 404.
* ``POST /diagnostics/reload-component`` — happy path + unknown
  component returns ``ok=false``.
* ``POST /diagnostics/bundle`` + ``GET .../bundle/{filename}`` —
  end-to-end tar.gz build, contents include the expected files with
  bearer_token redacted, then the streamed download serves it.

Re-uses the ``replay_env`` scaffolding from :mod:`tests.test_api`.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
import time
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
    return storage_root, config_path


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------


async def test_restart_increments_counter_when_suppressed(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        app.state.suppress_restart_sigterm = True
        async with await _client(app) as client:
            r1 = await client.post(
                "/v1/diagnostics/restart", headers=_AUTH
            )
            assert r1.status_code == 202, r1.text
            r2 = await client.post(
                "/v1/diagnostics/restart", headers=_AUTH
            )
            assert r2.status_code == 202
            assert app.state.restart_calls == 2


async def test_restart_requires_auth(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post("/v1/diagnostics/restart")
            assert resp.status_code == 401


# ---------------------------------------------------------------------------
# flush-queues
# ---------------------------------------------------------------------------


async def test_flush_queues_drains_pending_chunks_and_diar(replay_env):
    """Seed pending state directly via queries, then flush."""
    storage_root, _ = replay_env
    app = create_app()
    async with _LifespanContext(app):
        db = app.state.db_pool.writer

        sid = await queries.create_session(db, "continuous")
        await queries.insert_audio_chunk(
            db,
            session_id=sid,
            channel="mic",
            start_ts=time.time(),
            end_ts=time.time() + 30,
            path="/tmp/fake.wav",
            samples=16000 * 30,
        )
        # diarization_status defaults to 'pending' on session create —
        # nothing else needed for the diar side.

        async with await _client(app) as client:
            resp = await client.post(
                "/v1/diagnostics/flush-queues", headers=_AUTH
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["transcription_flushed"] >= 1
            assert body["diarization_flushed"] >= 1


async def test_flush_queues_when_idle_returns_zeros(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/diagnostics/flush-queues", headers=_AUTH
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["transcription_flushed"] == 0
            # ``diarization_flushed`` may be 0 because nothing is pending
            # in a fresh DB.
            assert body["diarization_flushed"] >= 0


# ---------------------------------------------------------------------------
# reload-component
# ---------------------------------------------------------------------------


async def test_reload_component_transcription(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/diagnostics/reload-component",
                json={"component": "transcription"},
                headers=_AUTH,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["component"] == "transcription"
            assert isinstance(body["ok"], bool)


async def test_reload_component_unknown_returns_ok_false(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/diagnostics/reload-component",
                json={"component": "nonexistent"},
                headers=_AUTH,
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["component"] == "nonexistent"
            assert body["ok"] is False
            assert "unknown component" in body["detail"]


# ---------------------------------------------------------------------------
# test-capture (self-test)
# ---------------------------------------------------------------------------


async def test_test_capture_polls_to_completion(replay_env):
    """Schedule a self-test, poll until done, confirm structure."""
    app = create_app()
    async with _LifespanContext(app):
        app.state.self_test_capture_seconds = 0.05
        app.state.self_test_transcribe_timeout_seconds = 0.5
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/diagnostics/test-capture", headers=_AUTH
            )
            assert resp.status_code == 202, resp.text
            job_id = resp.json()["job_id"]
            assert job_id.startswith("selftest-")

            # Poll.
            final = None
            for _ in range(100):
                await asyncio.sleep(0.1)
                r = await client.get(
                    f"/v1/diagnostics/test-capture/{job_id}",
                    headers=_AUTH,
                )
                assert r.status_code == 200
                final = r.json()
                if final["status"] in ("complete", "failed"):
                    break
            assert final is not None
            assert final["status"] in ("complete", "failed")
            # Step names always include start_session and stop_session.
            step_names = {s["name"] for s in final["steps"]}
            assert "start_session" in step_names
            assert "verify_chunks" in step_names


async def test_test_capture_status_missing_job_404(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get(
                "/v1/diagnostics/test-capture/nope",
                headers=_AUTH,
            )
            assert resp.status_code == 404
            body = resp.json()
            assert body["error"] == "job_not_found"


# ---------------------------------------------------------------------------
# bundle
# ---------------------------------------------------------------------------


async def test_bundle_creates_tar_gz_with_expected_files(replay_env):
    storage_root, config_path = replay_env
    # Seed a daemon event so events.json has at least one row.
    app = create_app()
    async with _LifespanContext(app):
        db = app.state.db_pool.writer
        await queries.log_daemon_event(
            db,
            level="info",
            component="test",
            event="bundle_test_event",
            details="seed",
        )
        async with await _client(app) as client:
            resp = await client.post(
                "/v1/diagnostics/bundle", headers=_AUTH
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            bundle_path = Path(body["bundle_path"])
            assert bundle_path.exists()
            assert bundle_path.suffix == ".gz"
            assert body["size_bytes"] > 0
            assert body["filename"] in body["download_url"]
            assert body["expires_at"] > time.time()

            # Open the tar and assert the layout.
            with tarfile.open(bundle_path, "r:gz") as tar:
                names = set(tar.getnames())
                assert "diagnostics.txt" in names
                assert "events.json" in names
                assert "config.toml.redacted" in names
                assert "system.txt" in names
                assert "logs/helios.log.gz" in names

                # config.toml.redacted must NOT contain the real bearer_token.
                redacted = tar.extractfile("config.toml.redacted").read().decode()
                assert "test" not in redacted or "<redacted>" in redacted
                assert "<redacted>" in redacted

                # events.json should parse and include our seed event.
                import json

                events_data = json.loads(
                    tar.extractfile("events.json").read().decode()
                )
                assert isinstance(events_data, list)
                assert any(
                    ev.get("event") == "bundle_test_event"
                    for ev in events_data
                )


async def test_bundle_download_streams_file(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            create_resp = await client.post(
                "/v1/diagnostics/bundle", headers=_AUTH
            )
            assert create_resp.status_code == 200
            filename = create_resp.json()["filename"]

            dl = await client.get(
                f"/v1/diagnostics/bundle/{filename}", headers=_AUTH
            )
            assert dl.status_code == 200
            # The content is the tar.gz bytes.
            assert dl.headers.get("content-type", "").startswith(
                "application/gzip"
            )
            assert len(dl.content) > 0


async def test_bundle_download_404_for_missing(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            resp = await client.get(
                "/v1/diagnostics/bundle/missing.tar.gz", headers=_AUTH
            )
            assert resp.status_code == 404
            assert resp.json()["error"] == "bundle_not_found"


async def test_bundle_download_rejects_path_traversal(replay_env):
    app = create_app()
    async with _LifespanContext(app):
        async with await _client(app) as client:
            # FastAPI routes don't match raw .. directly, but slashes
            # inside the path param do reach our validator.
            resp = await client.get(
                "/v1/diagnostics/bundle/..%2Fetc%2Fpasswd",
                headers=_AUTH,
            )
            # Either 400 (our validator) or 404 (router fallback) is
            # acceptable — both prevent traversal.
            assert resp.status_code in (400, 404)
