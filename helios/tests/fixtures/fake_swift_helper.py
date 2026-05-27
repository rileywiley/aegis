#!/usr/bin/env python3
"""Test double for ScreenCaptureHelper.

Speaks the same framed stdout protocol the real Swift binary uses
(see HELIOS.md §8.3 and §20):

  Frame: [1B type][8B float64 LE timestamp][4B uint32 LE length][payload]
  type = 0x01 audio (int16 PCM)
  type = 0x02 video (JPEG)

Stdin commands: ENABLE_AUDIO, DISABLE_AUDIO, ENABLE_VIDEO, DISABLE_VIDEO,
SET_DISPLAY <id>, QUIT. Acks go to stderr ("OK <command>" or
"ERR <reason>") so test code can synchronize on them if needed.

Configurable via env vars (set by the test before spawning):
  FAKE_HELPER_AUDIO_BLOCKS    — number of audio frames to emit on ENABLE_AUDIO
  FAKE_HELPER_BLOCK_SAMPLES   — samples per audio frame (default 1600 = 100ms @ 16kHz)
  FAKE_HELPER_BLOCK_INTERVAL  — seconds between emitted frames (default 0.02)
  FAKE_HELPER_EMIT_VIDEO      — if "1", emit one fake JPEG frame after audio
  FAKE_HELPER_EXIT_AFTER_AUDIO — if "1", exit after the audio stream finishes
  FAKE_HELPER_CRASH_AFTER     — if set to N, crash after N audio frames
"""

from __future__ import annotations

import os
import struct
import sys
import threading
import time

PACKET_TYPE_AUDIO = 0x01
PACKET_TYPE_VIDEO = 0x02


def write_packet(packet_type: int, ts: float, payload: bytes) -> None:
    header = struct.pack("<BdI", packet_type, ts, len(payload))
    sys.stdout.buffer.write(header + payload)
    sys.stdout.buffer.flush()


def ack(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


_audio_thread: threading.Thread | None = None
_audio_stop = threading.Event()
_quit = threading.Event()


def _audio_loop() -> None:
    n_blocks = int(os.environ.get("FAKE_HELPER_AUDIO_BLOCKS", "10"))
    block_samples = int(os.environ.get("FAKE_HELPER_BLOCK_SAMPLES", "1600"))
    interval = float(os.environ.get("FAKE_HELPER_BLOCK_INTERVAL", "0.02"))
    crash_after = os.environ.get("FAKE_HELPER_CRASH_AFTER")
    crash_after_n = int(crash_after) if crash_after else None

    block = b"\x00\x01" * block_samples  # alternating tiny int16 values
    for i in range(n_blocks):
        if _audio_stop.is_set() or _quit.is_set():
            return
        write_packet(PACKET_TYPE_AUDIO, ts=time.time(), payload=block)
        if crash_after_n is not None and (i + 1) >= crash_after_n:
            os._exit(7)
        time.sleep(interval)

    if os.environ.get("FAKE_HELPER_EMIT_VIDEO") == "1":
        write_packet(PACKET_TYPE_VIDEO, ts=time.time(), payload=b"\xff\xd8\xff\xd9")  # tiny JPEG-shaped blob

    if os.environ.get("FAKE_HELPER_EXIT_AFTER_AUDIO") == "1":
        os._exit(0)


def main() -> None:
    if "--version" in sys.argv:
        print("FakeScreenCaptureHelper 0.0.1")
        return

    while not _quit.is_set():
        line = sys.stdin.readline()
        if not line:
            break
        cmd = line.strip()
        if not cmd:
            continue
        parts = cmd.split(maxsplit=1)
        head = parts[0]
        if head == "ENABLE_AUDIO":
            global _audio_thread
            if _audio_thread is None or not _audio_thread.is_alive():
                _audio_stop.clear()
                _audio_thread = threading.Thread(target=_audio_loop, daemon=True)
                _audio_thread.start()
            ack("OK ENABLE_AUDIO")
        elif head == "DISABLE_AUDIO":
            _audio_stop.set()
            ack("OK DISABLE_AUDIO")
        elif head == "ENABLE_VIDEO":
            ack("OK ENABLE_VIDEO")
        elif head == "DISABLE_VIDEO":
            ack("OK DISABLE_VIDEO")
        elif head == "SET_DISPLAY":
            ack(f"OK SET_DISPLAY {parts[1] if len(parts) > 1 else ''}")
        elif head == "QUIT":
            _quit.set()
            _audio_stop.set()
            ack("OK QUIT")
            break
        else:
            ack(f"ERR unknown_command {head}")


if __name__ == "__main__":
    main()
