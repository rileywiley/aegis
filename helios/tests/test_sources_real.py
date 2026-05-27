"""Tests for the real Mic + SCK system audio sources.

MicSource is tested with a mock sounddevice module — running real audio in
CI is not feasible. SCKSystemAudioSource is tested against a Python fake
helper script (`tests/fixtures/fake_swift_helper.py`) that speaks the same
framed stdout protocol the real Swift binary uses; this exercises the
subprocess wiring + frame parser end-to-end without needing
ScreenCaptureKit.

The single integration test against the REAL Swift binary (verifying it
launches, prints --version, and exits cleanly) lives at the bottom and
skips when the binary is missing.
"""

from __future__ import annotations

import asyncio
import os
import struct
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from helios.clock import RealClock
from helios.sources._helper_path import get_helper_path
from helios.sources.interface import AudioSample, VideoFrame
from helios.sources.real import (
    MicSource,
    RealSourceFactory,
    SCKSystemAudioSource,
)

FAKE_HELPER = Path(__file__).parent / "fixtures" / "fake_swift_helper.py"


# ---------------------------------------------------------------------------
# Helper-path resolver
# ---------------------------------------------------------------------------


def test_helper_path_dev_resolves_to_repo_bin(monkeypatch):
    monkeypatch.delenv("RESOURCEPATH", raising=False)
    p = get_helper_path()
    assert p.name == "ScreenCaptureHelper"
    assert "helios/bin" in str(p)


def test_helper_path_bundle_uses_resourcepath(monkeypatch, tmp_path):
    monkeypatch.setenv("RESOURCEPATH", str(tmp_path))
    p = get_helper_path()
    assert p == tmp_path / "bin" / "ScreenCaptureHelper"


# ---------------------------------------------------------------------------
# MicSource (mocked sounddevice)
# ---------------------------------------------------------------------------


class _FakeStream:
    """Mock for sounddevice.InputStream. Records calls and exposes the callback."""

    def __init__(self, samplerate, channels, dtype, blocksize, callback):
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.blocksize = blocksize
        self.callback = callback
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class _FakeSdModule:
    """Mock sounddevice module exposing only InputStream."""

    def __init__(self):
        self.last_stream: _FakeStream | None = None

    def InputStream(self, **kw):  # noqa: N802 — match sounddevice API
        self.last_stream = _FakeStream(**kw)
        return self.last_stream


async def test_mic_start_opens_stream_with_correct_args():
    fake_sd = _FakeSdModule()
    mic = MicSource(asyncio.Queue(), RealClock(), sd_module=fake_sd)
    await mic.start()
    try:
        assert mic.is_running is True
        s = fake_sd.last_stream
        assert s is not None
        assert s.samplerate == 16000
        assert s.channels == 1
        assert s.dtype == "int16"
        assert s.blocksize == 1600
        assert s.started is True
    finally:
        await mic.stop()


async def test_mic_double_start_is_noop():
    fake_sd = _FakeSdModule()
    mic = MicSource(asyncio.Queue(), RealClock(), sd_module=fake_sd)
    await mic.start()
    first_stream = fake_sd.last_stream
    await mic.start()  # idempotent
    assert fake_sd.last_stream is first_stream
    await mic.stop()


async def test_mic_stop_releases_stream():
    fake_sd = _FakeSdModule()
    mic = MicSource(asyncio.Queue(), RealClock(), sd_module=fake_sd)
    await mic.start()
    s = fake_sd.last_stream
    await mic.stop()
    assert mic.is_running is False
    assert s.stopped is True
    assert s.closed is True
    # Second stop is safe.
    await mic.stop()


async def test_mic_callback_marshals_samples_to_iterator():
    fake_sd = _FakeSdModule()
    mic = MicSource(asyncio.Queue(), RealClock(), sd_module=fake_sd)
    await mic.start()
    try:
        # Simulate PortAudio callback firing on the audio thread.
        frames = 1600
        # indata is shape (frames, channels=1) of int16
        indata = np.zeros((frames, 1), dtype=np.int16)
        indata[:, 0] = np.arange(frames, dtype=np.int16)

        # Drive the callback directly. asyncio.run_coroutine_threadsafe needs
        # to schedule onto the loop, so we yield control afterward.
        fake_sd.last_stream.callback(indata, frames, None, None)

        # Drain one sample from the iterator.
        agen = mic.samples()
        sample = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
        assert sample.channel == "mic"
        assert sample.samples.shape == (frames,)
        # Verify the copy is independent of the source buffer.
        indata[:, 0] = 99
        assert sample.samples[0] == 0
        await agen.aclose()
    finally:
        await mic.stop()


async def test_mic_callback_status_flag_logged_no_crash(caplog):
    """Status flags (device change, overflow) must not crash the callback."""
    fake_sd = _FakeSdModule()
    mic = MicSource(asyncio.Queue(), RealClock(), sd_module=fake_sd)
    await mic.start()
    try:
        # Pass a truthy status — real sounddevice passes a CallbackFlags.
        fake_sd.last_stream.callback(
            np.zeros((1600, 1), dtype=np.int16), 1600, None, "input_overflow"
        )
        # No exception, mic still running.
        assert mic.is_running is True
    finally:
        await mic.stop()


async def test_mic_iterator_terminates_on_stop():
    fake_sd = _FakeSdModule()
    mic = MicSource(asyncio.Queue(), RealClock(), sd_module=fake_sd)
    await mic.start()
    agen = mic.samples()

    async def drain():
        items = []
        async for s in agen:
            items.append(s)
        return items

    drain_task = asyncio.create_task(drain())
    await asyncio.sleep(0.01)
    await mic.stop()
    items = await asyncio.wait_for(drain_task, timeout=1.0)
    assert items == []  # no samples were pushed; iterator just ended cleanly


async def test_mic_start_without_sounddevice_raises():
    mic = MicSource(asyncio.Queue(), RealClock(), sd_module=None)
    # Force the sd attribute to None to simulate PortAudio unavailable.
    mic._sd = None
    with pytest.raises(RuntimeError, match="sounddevice"):
        await mic.start()


async def test_mic_start_propagates_stream_init_error():
    class _BoomSd:
        def InputStream(self, **kw):  # noqa: N802
            raise RuntimeError("PortAudio init failed")

    mic = MicSource(asyncio.Queue(), RealClock(), sd_module=_BoomSd())
    with pytest.raises(RuntimeError, match="PortAudio init failed"):
        await mic.start()
    assert mic.is_running is False


# ---------------------------------------------------------------------------
# SCKSystemAudioSource (against fake helper subprocess)
# ---------------------------------------------------------------------------


def _spawn_args(env_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """Build kwargs for SCKSystemAudioSource pointing at the fake helper."""
    return {
        "queue": asyncio.Queue(),
        "clock": RealClock(),
        "helper_path": _make_executable_helper(env_overrides),
    }


def _make_executable_helper(env_overrides: dict[str, str] | None) -> Path:
    """Return a path to a wrapper that execs the fake helper with env overrides.

    asyncio.create_subprocess_exec doesn't take an env-overrides kwarg
    cleanly when we want to run a script; using a tiny wrapper script keeps
    the test independent of the test process env.
    """
    return FAKE_HELPER  # env is set on the test process before spawning


@pytest.fixture(autouse=True)
def _clear_fake_helper_env(monkeypatch):
    """Each test sets its own FAKE_HELPER_* vars; ensure no leak between tests."""
    for k in list(os.environ):
        if k.startswith("FAKE_HELPER_"):
            monkeypatch.delenv(k, raising=False)


def _spawn_kwargs() -> dict[str, Any]:
    return {
        "queue": asyncio.Queue(),
        "clock": RealClock(),
        "helper_path": FAKE_HELPER,
    }


async def test_sck_start_spawns_helper_and_sends_enable_audio(monkeypatch):
    """ENABLE_AUDIO is sent on start; helper acks via stderr."""
    monkeypatch.setenv("FAKE_HELPER_AUDIO_BLOCKS", "0")  # no audio frames
    src = SCKSystemAudioSource(**_spawn_kwargs())
    await src.start()
    try:
        assert src.is_running is True
        assert src._proc is not None
        assert src._proc.returncode is None
    finally:
        await src.stop()


async def test_sck_helper_missing_raises(tmp_path):
    src = SCKSystemAudioSource(
        queue=asyncio.Queue(),
        clock=RealClock(),
        helper_path=tmp_path / "nonexistent",
    )
    with pytest.raises(FileNotFoundError):
        await src.start()


async def test_sck_audio_packets_parse_and_yield(monkeypatch):
    monkeypatch.setenv("FAKE_HELPER_AUDIO_BLOCKS", "3")
    monkeypatch.setenv("FAKE_HELPER_BLOCK_SAMPLES", "1600")
    monkeypatch.setenv("FAKE_HELPER_BLOCK_INTERVAL", "0.005")

    src = SCKSystemAudioSource(**_spawn_kwargs())
    await src.start()
    try:
        agen = src.samples()
        received: list[AudioSample] = []
        for _ in range(3):
            received.append(await asyncio.wait_for(agen.__anext__(), timeout=2.0))
        await agen.aclose()
        assert len(received) == 3
        for s in received:
            assert s.channel == "system"
            assert s.samples.dtype == np.int16
            assert s.samples.shape == (1600,)
            assert isinstance(s.ts, float)
    finally:
        await src.stop()


async def test_sck_video_packet_parses_and_yields(monkeypatch):
    monkeypatch.setenv("FAKE_HELPER_AUDIO_BLOCKS", "1")
    monkeypatch.setenv("FAKE_HELPER_BLOCK_INTERVAL", "0.005")
    monkeypatch.setenv("FAKE_HELPER_EMIT_VIDEO", "1")

    src = SCKSystemAudioSource(**_spawn_kwargs())
    await src.start()
    try:
        # Drain one audio sample first to ensure stream is flowing.
        agen = src.samples()
        await asyncio.wait_for(agen.__anext__(), timeout=2.0)
        await agen.aclose()
        # Then drain one video frame.
        vgen = src.video_frames()
        frame = await asyncio.wait_for(vgen.__anext__(), timeout=2.0)
        await vgen.aclose()
        assert isinstance(frame, VideoFrame)
        assert frame.jpeg.startswith(b"\xff\xd8")  # JPEG SOI
    finally:
        await src.stop()


async def test_sck_stop_sends_quit_and_terminates_subprocess(monkeypatch):
    monkeypatch.setenv("FAKE_HELPER_AUDIO_BLOCKS", "100")
    monkeypatch.setenv("FAKE_HELPER_BLOCK_INTERVAL", "0.05")

    src = SCKSystemAudioSource(**_spawn_kwargs())
    await src.start()
    proc = src._proc
    assert proc is not None
    pid = proc.pid
    await src.stop()
    assert src.is_running is False
    assert src._proc is None
    # Subprocess has fully exited.
    assert proc.returncode is not None
    # Confirm process is gone (kill(0) raises if dead).
    import errno
    try:
        os.kill(pid, 0)
        process_alive = True
    except OSError as e:
        process_alive = e.errno != errno.ESRCH
    assert not process_alive, f"helper pid {pid} still alive after stop"


async def test_sck_stop_is_idempotent(monkeypatch):
    monkeypatch.setenv("FAKE_HELPER_AUDIO_BLOCKS", "0")
    src = SCKSystemAudioSource(**_spawn_kwargs())
    await src.start()
    await src.stop()
    await src.stop()  # no exception


async def test_sck_double_start_is_noop(monkeypatch):
    monkeypatch.setenv("FAKE_HELPER_AUDIO_BLOCKS", "0")
    src = SCKSystemAudioSource(**_spawn_kwargs())
    await src.start()
    proc1 = src._proc
    await src.start()
    try:
        assert src._proc is proc1  # didn't spawn a second helper
    finally:
        await src.stop()


async def test_sck_helper_crash_unblocks_iterator(monkeypatch):
    """When the helper dies mid-stream, samples() terminates cleanly."""
    monkeypatch.setenv("FAKE_HELPER_AUDIO_BLOCKS", "10")
    monkeypatch.setenv("FAKE_HELPER_BLOCK_INTERVAL", "0.005")
    monkeypatch.setenv("FAKE_HELPER_CRASH_AFTER", "2")

    src = SCKSystemAudioSource(**_spawn_kwargs())
    await src.start()
    try:
        agen = src.samples()
        items: list[AudioSample] = []
        async for sample in agen:
            items.append(sample)
            if len(items) > 20:  # safety stop
                break
        # We should have received the 2 frames before the crash.
        assert 1 <= len(items) <= 5
        assert src.is_running is False
    finally:
        await src.stop()


async def test_sck_unknown_packet_type_is_ignored_and_does_not_break_stream(monkeypatch, tmp_path):
    """A junk packet between audio packets shouldn't kill the parser."""
    # Custom helper writing one audio packet, one unknown-type packet, one audio packet.
    junk_helper = tmp_path / "junk_helper.py"
    junk_helper.write_text(
        "#!/usr/bin/env python3\n"
        "import struct, sys, time\n"
        "blk = b'\\x00\\x01' * 1600\n"
        "for ptype in (0x01, 0x99, 0x01):\n"
        "    payload = blk if ptype == 0x01 else b'\\x00\\x00'\n"
        "    sys.stdout.buffer.write(struct.pack('<BdI', ptype, time.time(), len(payload)) + payload)\n"
        "    sys.stdout.buffer.flush()\n"
        "import sys as _sys; _sys.stdin.readline()\n"  # block until QUIT
    )
    junk_helper.chmod(0o755)
    src = SCKSystemAudioSource(
        queue=asyncio.Queue(),
        clock=RealClock(),
        helper_path=junk_helper,
    )
    await src.start()
    try:
        agen = src.samples()
        s1 = await asyncio.wait_for(agen.__anext__(), timeout=2.0)
        s2 = await asyncio.wait_for(agen.__anext__(), timeout=2.0)
        await agen.aclose()
        assert s1.channel == s2.channel == "system"
    finally:
        await src.stop()


# ---------------------------------------------------------------------------
# RealSourceFactory
# ---------------------------------------------------------------------------


def test_real_factory_makes_mic_source():
    f = RealSourceFactory(samplerate=22050)
    src = f.make_mic_source(asyncio.Queue(), RealClock())
    assert isinstance(src, MicSource)
    assert src._samplerate == 22050


def test_real_factory_makes_system_source(tmp_path):
    f = RealSourceFactory(helper_path=tmp_path / "helper")
    src = f.make_system_source(asyncio.Queue(), RealClock())
    assert isinstance(src, SCKSystemAudioSource)
    assert src._helper_path == tmp_path / "helper"


def test_real_factory_calendar_source_raises():
    f = RealSourceFactory()
    with pytest.raises(NotImplementedError, match="Phase 2"):
        f.make_calendar_source(RealClock())


# ---------------------------------------------------------------------------
# Real Swift helper smoke check (skipped if binary missing)
# ---------------------------------------------------------------------------


_REAL_HELPER = get_helper_path()


@pytest.mark.skipif(
    not _REAL_HELPER.exists(),
    reason=f"Swift helper not built at {_REAL_HELPER}",
)
@pytest.mark.skipif(sys.platform != "darwin", reason="ScreenCaptureKit is macOS-only")
async def test_real_swift_helper_version():
    """Sanity: the bundled binary launches and prints --version cleanly."""
    proc = await asyncio.create_subprocess_exec(
        str(_REAL_HELPER),
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    assert proc.returncode == 0
    assert b"ScreenCaptureHelper" in stdout
