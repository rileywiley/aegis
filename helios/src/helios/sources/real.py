"""Real audio sources: macOS microphone (sounddevice) + system audio (Swift helper subprocess).

Per HELIOS.md §8.2 (mic) and §8.3 (system audio). Both implementations
satisfy the `AudioSource` / `SystemAudioSource` protocols in
`helios.sources.interface`. The real source factory in `helios.sources`
returns these for production daemon runs; the replay factory is used in
tests and headless dev (set HELIOS_REPLAY=1).

Both sources push emitted samples onto an internal queue backing the
`samples()` async iterator (the public consumer surface used by the stream
manager). The constructor-supplied `queue` parameter is accepted for
forward-compat parity with the replay source signature but is not written
to — see Wave 6Re review fix.
"""

from __future__ import annotations

import asyncio
import struct
import time
from pathlib import Path
from typing import AsyncIterator, Literal

import numpy as np

try:
    import sounddevice as _sd
    _sd_import_error: Exception | None = None
except (ImportError, OSError) as exc:  # pragma: no cover - PortAudio unavailable in headless / mis-bundled envs
    _sd = None
    _sd_import_error = exc

from helios.clock import Clock
from helios.log import get_logger
from helios.sources._helper_path import get_helper_path
from helios.sources.interface import AudioSample, VideoFrame

_log = get_logger("sources.real")

# Sentinel pushed onto the iterator queue to signal clean termination.
_STOP_SENTINEL: object = object()

# Frame layout from the Swift helper:
# [1B type][8B float64 LE timestamp][4B uint32 LE length][payload]
_HEADER_LEN = 13
_PACKET_TYPE_AUDIO = 0x01
_PACKET_TYPE_VIDEO = 0x02


# ---------------------------------------------------------------------------
# MicSource
# ---------------------------------------------------------------------------


class MicSource:
    """Microphone capture via sounddevice (PortAudio).

    Opens an InputStream at 16 kHz mono int16 with a 100 ms blocksize. The
    PortAudio callback runs on a non-asyncio audio thread; samples are
    marshaled to the event loop via `run_coroutine_threadsafe`.

    Idempotent: a second `start()` while running is a no-op; `stop()` is
    safe to call multiple times.
    """

    is_running: bool

    def __init__(
        self,
        queue: asyncio.Queue[AudioSample],
        clock: Clock,
        start_ts: float | None = None,
        samplerate: int = 16000,
        blocksize: int = 1600,
        sd_module=None,
    ) -> None:
        del queue  # accepted for factory signature parity; unused — consume via samples()
        del start_ts  # mic source uses wall-clock from sounddevice; start_ts is for replay
        self._clock = clock
        self._samplerate = samplerate
        self._blocksize = blocksize
        # Allow tests to inject a mock sounddevice module. Production uses the import.
        self._sd = sd_module if sd_module is not None else _sd
        self._stream = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._iter_queue: asyncio.Queue[AudioSample | object] = asyncio.Queue()
        self.is_running = False

    # ------------------------------------------------------------------ public

    async def start(self) -> None:
        if self.is_running:
            return
        if self._sd is None:
            raise RuntimeError(
                f"sounddevice (PortAudio) is not available: {_sd_import_error!r}"
            )
        self._loop = asyncio.get_running_loop()
        try:
            self._stream = self._sd.InputStream(
                samplerate=self._samplerate,
                channels=1,
                dtype="int16",
                blocksize=self._blocksize,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            self._loop = None
            _log.error("mic_stream_start_failed", error=str(exc))
            raise
        self.is_running = True
        _log.info("mic_source_started", samplerate=self._samplerate, blocksize=self._blocksize)

    async def stop(self) -> None:
        if not self.is_running and self._stream is None:
            return
        self.is_running = False
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:  # pragma: no cover - defensive
                _log.warning("mic_stream_stop_failed", error=str(exc))
        # Unblock any consumer awaiting samples().
        await self._iter_queue.put(_STOP_SENTINEL)
        _log.info("mic_source_stopped")

    async def samples(self) -> AsyncIterator[AudioSample]:
        while True:
            item = await self._iter_queue.get()
            if item is _STOP_SENTINEL:
                break
            yield item  # type: ignore[misc]

    # ------------------------------------------------------------------ internal

    def _callback(self, indata, frames, time_info, status) -> None:
        """PortAudio callback. Runs on the audio thread, NOT the event loop.

        `indata` is a numpy ndarray shape (frames, channels). We take channel
        0 (mono input), copy out of the PortAudio-owned buffer, build an
        AudioSample, and marshal it to the event loop's iterator queue.
        """
        if status:
            # Log device-change / overflow / underflow flags. We do not try
            # to recover here — the stream manager's watchdog detects a stall
            # and triggers a clean restart.
            _log.warning("mic_callback_status", flags=str(status))
        if self._loop is None or not self.is_running:
            return
        try:
            ts = self._clock.time() - (frames / self._samplerate)
            samples = indata[:, 0].copy()  # detach from PortAudio buffer
            sample = AudioSample(channel="mic", ts=ts, samples=samples)
            asyncio.run_coroutine_threadsafe(
                self._iter_queue.put(sample), self._loop
            )
        except Exception as exc:  # pragma: no cover - audio thread must not raise
            _log.error("mic_callback_error", error=str(exc))


# ---------------------------------------------------------------------------
# SCKSystemAudioSource
# ---------------------------------------------------------------------------


class SCKSystemAudioSource:
    """System audio capture via the Swift ScreenCaptureHelper subprocess.

    Spawns the helper, sends ENABLE_AUDIO on stdin, and parses framed packets
    from stdout. Audio packets feed the iterator queue; video packets feed a
    separate video queue (consumed by the OCR worker — Phase 1 does not use
    video, but the queue is wired so OCR can plug in later).

    Idempotent start/stop. On subprocess exit, the read loop logs and
    terminates cleanly; the stream manager's watchdog handles restarts.
    """

    is_running: bool

    def __init__(
        self,
        queue: asyncio.Queue[AudioSample],
        clock: Clock,
        start_ts: float | None = None,
        helper_path: Path | None = None,
        samplerate: int = 16000,
        startup_timeout_seconds: float = 5.0,
        shutdown_timeout_seconds: float = 5.0,
        enable_video: bool = False,
    ) -> None:
        del queue  # accepted for factory parity; unused — consume via samples()
        del start_ts  # SCK timestamps come from the helper (mach-time → UTC)
        self._clock = clock
        self._samplerate = samplerate
        self._helper_path = Path(helper_path) if helper_path else get_helper_path()
        self._startup_timeout = startup_timeout_seconds
        self._shutdown_timeout = shutdown_timeout_seconds
        self._enable_video = enable_video

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._iter_queue: asyncio.Queue[AudioSample | object] = asyncio.Queue()
        self._video_queue: asyncio.Queue[VideoFrame | object] = asyncio.Queue()
        self.is_running = False

    # ------------------------------------------------------------------ public

    async def start(self) -> None:
        if self.is_running:
            return
        if not self._helper_path.exists():
            raise FileNotFoundError(
                f"ScreenCaptureHelper not found at {self._helper_path}"
            )
        try:
            self._proc = await asyncio.create_subprocess_exec(
                str(self._helper_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await self._send_command("ENABLE_AUDIO")
            if self._enable_video:
                # The helper's ``enableVideo()`` requires a display to
                # have been registered first (``currentDisplay != nil``
                # guard in ScreenCaptureHelper.swift). Resolve the
                # main display via Quartz; lazy-import so non-mac test
                # hosts can still import this module.
                display_id = self._resolve_main_display_id()
                if display_id is not None:
                    await self._send_command(f"SET_DISPLAY {display_id}")
                    # Helper acks with ``OK ENABLE_VIDEO`` and then
                    # multiplexes JPEG-encoded frames on the same
                    # stdout pipe alongside audio packets.
                    await self._send_command("ENABLE_VIDEO")
                else:
                    _log.warning(
                        "sck_enable_video_skipped_no_display",
                        helper=str(self._helper_path),
                    )
        except Exception as exc:
            _log.error("sck_helper_start_failed", error=str(exc))
            await self._cleanup_proc()
            raise
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="sck-stdout-reader"
        )
        self._stderr_task = asyncio.create_task(
            self._stderr_loop(), name="sck-stderr-reader"
        )
        self.is_running = True
        _log.info("sck_source_started", helper=str(self._helper_path))

    async def stop(self) -> None:
        if not self.is_running and self._proc is None:
            return
        self.is_running = False
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                await self._send_command("QUIT")
                await asyncio.wait_for(proc.wait(), timeout=self._shutdown_timeout)
            except asyncio.TimeoutError:
                _log.warning("sck_helper_quit_timeout", pid=proc.pid)
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except Exception as exc:  # pragma: no cover - defensive
                _log.warning("sck_helper_quit_error", error=str(exc))
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._reader_task = None
        self._stderr_task = None
        self._proc = None
        # Unblock any consumer awaiting samples()/video_frames().
        await self._iter_queue.put(_STOP_SENTINEL)
        await self._video_queue.put(_STOP_SENTINEL)
        _log.info("sck_source_stopped")

    async def samples(self) -> AsyncIterator[AudioSample]:
        while True:
            item = await self._iter_queue.get()
            if item is _STOP_SENTINEL:
                break
            yield item  # type: ignore[misc]

    async def video_frames(self) -> AsyncIterator[VideoFrame]:
        while True:
            item = await self._video_queue.get()
            if item is _STOP_SENTINEL:
                break
            yield item  # type: ignore[misc]

    # ------------------------------------------------------------------ internal

    @staticmethod
    def _resolve_main_display_id() -> int | None:
        """Return the main display's CG ID, or ``None`` on non-mac /
        missing PyObjC.

        The Swift helper's ``ENABLE_VIDEO`` requires a prior
        ``SET_DISPLAY <id>`` and ID is a ``CGDirectDisplayID`` (uint32).
        ``Quartz.CGMainDisplayID()`` is the canonical lookup; it's
        constant per boot and matches the helper's
        ``SCShareableContent`` display list 1:1.
        """
        try:
            import Quartz  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001 — non-mac, missing PyObjC
            return None
        try:
            return int(Quartz.CGMainDisplayID())
        except Exception:  # noqa: BLE001 — defensive
            return None

    async def _send_command(self, cmd: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            raise RuntimeError("helper stdin not available")
        proc.stdin.write((cmd + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def _read_loop(self) -> None:
        """Parse framed packets from helper stdout until EOF or cancel."""
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        reader = proc.stdout
        try:
            while True:
                header = await reader.readexactly(_HEADER_LEN)
                packet_type = header[0]
                ts, length = struct.unpack("<dI", header[1:])
                payload = await reader.readexactly(length)
                if packet_type == _PACKET_TYPE_AUDIO:
                    samples = np.frombuffer(payload, dtype=np.int16).copy()
                    await self._iter_queue.put(
                        AudioSample(channel="system", ts=ts, samples=samples)
                    )
                elif packet_type == _PACKET_TYPE_VIDEO:
                    await self._video_queue.put(VideoFrame(ts=ts, jpeg=bytes(payload)))
                else:
                    _log.warning("sck_unknown_packet_type", type=packet_type, length=length)
        except asyncio.IncompleteReadError:
            _log.warning(
                "sck_helper_stream_ended",
                returncode=proc.returncode,
            )
            # Push sentinel so consumers awaiting samples() unblock cleanly.
            await self._iter_queue.put(_STOP_SENTINEL)
            await self._video_queue.put(_STOP_SENTINEL)
            self.is_running = False
        except asyncio.CancelledError:
            raise

    async def _stderr_loop(self) -> None:
        """Drain helper stderr; log ack lines so test runs surface helper errors."""
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        reader = proc.stderr
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text.startswith("ERR"):
                    _log.warning("sck_helper_err", line=text)
                elif text.startswith("OK"):
                    _log.debug("sck_helper_ack", line=text)
                else:
                    _log.debug("sck_helper_stderr", line=text)
        except asyncio.CancelledError:
            raise

    async def _cleanup_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:  # pragma: no cover - already gone
                pass
            try:
                await proc.wait()
            except Exception:  # pragma: no cover - best effort
                pass


# ---------------------------------------------------------------------------
# RealSourceFactory
# ---------------------------------------------------------------------------


class RealSourceFactory:
    """Produces real Mic + SCK sources for production daemon runs.

    Calendar source still raises NotImplementedError — Phase 2 wires the
    real Microsoft Graph calendar client.
    """

    def __init__(
        self,
        helper_path: Path | None = None,
        samplerate: int = 16000,
        enable_video: bool = False,
    ) -> None:
        self._helper_path = helper_path
        self._samplerate = samplerate
        self._enable_video = enable_video

    def make_mic_source(
        self,
        queue: asyncio.Queue[AudioSample],
        clock: Clock,
        start_ts: float | None = None,
    ) -> MicSource:
        return MicSource(
            queue=queue,
            clock=clock,
            start_ts=start_ts,
            samplerate=self._samplerate,
        )

    def make_system_source(
        self,
        queue: asyncio.Queue[AudioSample],
        clock: Clock,
        start_ts: float | None = None,
    ) -> SCKSystemAudioSource:
        return SCKSystemAudioSource(
            queue=queue,
            clock=clock,
            start_ts=start_ts,
            helper_path=self._helper_path,
            samplerate=self._samplerate,
            enable_video=self._enable_video,
        )

    def make_calendar_source(self, clock: Clock):  # noqa: ARG002
        raise NotImplementedError(
            "Real calendar source arrives in Phase 2 (Microsoft Graph)"
        )


__all__ = [
    "MicSource",
    "SCKSystemAudioSource",
    "RealSourceFactory",
]
