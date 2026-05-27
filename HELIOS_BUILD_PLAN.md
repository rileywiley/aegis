# HELIOS — Build Plan

> Save as `HELIOS_BUILD_PLAN.md` at repo root, alongside `HELIOS.md` and Aegis's `CLAUDE.md`.
> This document is the imperative companion to HELIOS.md. The spec describes *what* Helios is; this plan describes *what to do, in what order, to build it*. Claude Code executes phases sequentially; human verifies smoke tests before each phase advances.

---

## How to Use This Document

### For Claude Code

1. Read `HELIOS.md` fully before starting any phase. The spec is the source of truth for behavior; this plan describes the order of work.
2. Execute phases sequentially. Do not start Phase N+1 until Phase N's checkpoint is signed off by the human.
3. Within a phase, tracks can be worked in parallel (they're listed as parallel workstreams). Within a track, tasks are ordered.
4. When a task says "write tests first," do exactly that: write the test file, watch tests fail, then implement until they pass.
5. **When you hit ambiguity:**
   - If it changes external behavior, public API, or the spec → **stop and ask the human.** Do not decide unilaterally.
   - If it's pure implementation detail (variable naming, internal helper decomposition, choice of small utility library, error message wording) → **decide, document the decision in §13 (Running Decisions Log), and proceed.**
6. After each phase, write the checkpoint sign-off summary as the last commit of the phase. Wait for human approval before continuing.
7. Do not modify any file marked `HUMAN-MAINTAINED — do not modify via Claude Code`.

### For the human

1. Before Phase 0: complete the Prerequisites section.
2. After each phase's automated work completes: run the smoke test procedure for that phase (§12). Sign off the checkpoint by replying "Phase N approved" in chat or committing a `PHASE_N_APPROVED.md` marker.
3. If a smoke test fails, do not approve. Surface the failure to Claude Code; remediate before re-running.
4. Manual tasks (recording audio fixtures, drafting/committing the Swift helper, reviewing UX) are flagged inline as "Human task." Complete them when prompted.
5. Reasonable expectation: ~5 weeks of Claude Code work plus ~2-3 hours of your time per phase for smoke testing and approval.

### Conventions used in this plan

- **Track** — a parallel workstream within a phase. Tracks within a phase can be done concurrently.
- **Task** — a unit of work within a track. Tasks within a track are sequential.
- **Test-first** task — write tests before implementation. The task description lists the test cases; Claude Code converts them to actual test code.
- **Acceptance** — what must be true to consider a task complete.
- **Checkpoint** — gate at the end of a phase; lists what must be true to advance.
- **Smoke test** — manual verification on real hardware (§12).
- **Human task** — explicitly requires the human to do something Claude Code cannot.

---

## Prerequisites

Complete all of these before starting Phase 0. Most are one-time setup.

### Hardware and OS

- [ ] Mac with Apple Silicon (M1 or later) running macOS 13 or newer
- [ ] At least 30 GB free disk space on the boot volume
- [ ] At least 16 GB RAM

### Software

- [ ] Aegis is installed, running locally, and accessible at `http://127.0.0.1:8000`
- [ ] PostgreSQL with pgvector is running (Aegis's existing infrastructure)
- [ ] Python 3.13+ installed (`python3 --version` confirms)
- [ ] `uv` package manager installed (`brew install uv` or `pipx install uv`)
- [ ] Xcode Command Line Tools installed (`xcode-select --install`)
- [ ] ffmpeg installed (`brew install ffmpeg`, verify with `ffmpeg -version`)
- [ ] PyCharm with Claude Code CLI plugin configured for the Aegis repo
- [ ] No Screenpipe installation present (would conflict on port 3030; Helios uses 3031, but cleanliness matters for testing)

### Repository state

- [ ] Aegis repo is checked out at the working state described in `aegis_codebase_summary.md`
- [ ] `HELIOS.md` is committed at the repo root
- [ ] `HELIOS_BUILD_PLAN.md` (this file) is committed at the repo root
- [ ] A new git branch `helios/build` is created and checked out

### External accounts

Required:
- [ ] Anthropic API key (already configured for Aegis)
- [ ] OpenAI API key (already configured for Aegis)
- [ ] Microsoft Azure app registration (already configured for Aegis)

Optional (needed for diarization):
- [ ] HuggingFace account with read-scope access token
- [ ] License acceptance for `pyannote/speaker-diarization-3.1`, `pyannote/segmentation-3.0`, `pyannote/embedding`
  *Defer this if you're not enabling speaker identification immediately. The dashboard wizard handles it later.*

### Human task: source assets

- [ ] **Swift helper source.** Either draft `helios/swift/ScreenCaptureHelper.swift` from the sketch in HELIOS.md §20, or have Claude Code generate the first draft during Phase 0 and commit it as human-maintained going forward.
- [ ] **Audio fixture plan.** Identify how you'll produce the five test recordings required by Phase 1's checkpoint:
   1. Clean 2-speaker conversation (~10 min)
   2. Messy 3-speaker discussion (~30 min)
   3. Silence-heavy meeting (~5 min, mostly silent with occasional speech)
   4. Crosstalk-heavy meeting (~10 min, multiple speakers talking over each other)
   5. Poor-audio meeting (~10 min, intentionally degraded — phone speaker, background noise)
   These can be self-recorded with friends, scripted with multiple Mac voices, or sourced from existing recordings you have rights to. Need ~500 MB of WAV files plus a manually-corrected golden transcript for at least the first one.

---

## Phase 0 — Scaffolding

**Goal:** Empty Helios package builds, signs, installs, and serves `/v1/health`. Repository structure in place. No actual capture functionality yet.

**Estimated time:** 1-2 days of Claude Code work.

### Track 0A — Repository structure

**Task 0A.1: Create directory layout.**

Create the directory structure described in HELIOS.md §4:
- `helios/` (top-level Helios package directory)
- `helios/src/helios/` (Python package)
- `helios/src/helios/{api,sources,capture,scheduler,workers,menubar,db}/` (subpackages, each with `__init__.py`)
- `helios/src/helios/api/routes/` (subpackage)
- `helios/migrations/`
- `helios/swift/`
- `helios/bin/`
- `helios/icons/`
- `helios/tests/`
- `helios/tests/fixtures/{audio,calendar,ocr,transcripts}/`
- `shared/` and `shared/src/shared/`
- `scripts/` (already exists; add Helios-specific scripts here)

Create empty `__init__.py` files in every Python subpackage. Add `.gitkeep` files in directories that should exist but are otherwise empty (e.g., `helios/tests/fixtures/audio/`).

Add `.gitignore` entries:
```
helios/.venv/
helios/dist/
helios/build/
helios/tests/fixtures/audio/*.wav
helios/tests/fixtures/audio/*.json
!helios/tests/fixtures/audio/README.md
helios/__pycache__/
*.pyc
```

**Acceptance:** Directory structure matches HELIOS.md §4. `git status` shows clean working tree after commit.

**Task 0A.2: pyproject.toml for Helios.**

Create `helios/pyproject.toml`:

```toml
[project]
name = "helios"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "fastapi >= 0.110",
    "uvicorn[standard] >= 0.27",
    "pydantic >= 2.5",
    "pydantic-settings >= 2.1",
    "aiosqlite >= 0.19",
    "httpx >= 0.26",
    "tomli >= 2.0; python_version < '3.11'",
    "watchfiles >= 0.21",
    "structlog >= 24.1",
    "rumps >= 0.4",
    "pyobjc-core >= 10.1",
    "pyobjc-framework-AVFoundation >= 10.1",
    "pyobjc-framework-Cocoa >= 10.1",
    "pyobjc-framework-CoreAudio >= 10.1",
    "pyobjc-framework-Quartz >= 10.1",
    "pyobjc-framework-ScreenCaptureKit >= 10.1",
    "pyobjc-framework-UserNotifications >= 10.1",
    "pyobjc-framework-Vision >= 10.1",
    "sounddevice >= 0.4",
    "numpy >= 1.26",
    "soundfile >= 0.12",
    "imagehash >= 4.3",
    "pillow >= 10.2",
    "huggingface-hub >= 0.20",
    "keyring >= 24.3",
    "shared @ file://../shared",
]

[project.optional-dependencies]
transcription = [
    "whisperx >= 3.1",
    "pyannote.audio >= 3.1",
    "torch >= 2.1",
    "torchaudio >= 2.1",
]
dev = [
    "pytest >= 8.0",
    "pytest-asyncio >= 0.23",
    "pytest-cov >= 4.1",
    "ruff >= 0.1",
    "mypy >= 1.8",
]
build = [
    "py2app >= 0.28",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py313"
```

Create `shared/pyproject.toml`:

```toml
[project]
name = "shared"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["pydantic >= 2.5"]
```

**Acceptance:** `cd helios && uv sync` installs without errors. `cd shared && uv sync` installs without errors. Helios's `--extra transcription` and `--extra build` install separately.

**Task 0A.3: Update Aegis pyproject for shared dep.**

Modify Aegis's existing `pyproject.toml` to add the local-path dependency on `shared/`:

```toml
[project]
dependencies = [
    # ... existing dependencies ...
    "shared @ file://../shared",
]
```

Run `uv sync` in Aegis's venv to verify.

**Acceptance:** Aegis venv has `shared` installed. `python -c "from shared.meetings import UpcomingMeetingEvent"` works in the Aegis venv (after Task 0B.2 creates the schema).

### Track 0B — Shared schemas

Can run in parallel with 0A.

**Task 0B.1: Create shared package structure.**

`shared/src/shared/__init__.py`:
```python
"""Shared schemas for the Aegis-Helios contract.

Both Aegis and Helios depend on this package to ensure their HTTP contracts
stay in sync. Any change here requires updates in both packages."""
```

**Task 0B.2: Define meeting upcoming schema.**

`shared/src/shared/meetings.py`:

```python
from pydantic import BaseModel, Field
from typing import Literal

class UpcomingMeetingEvent(BaseModel):
    calendar_event_id: str
    title: str  # "(excluded)" if is_excluded and the title shouldn't leak
    starts_at: float  # UTC epoch seconds
    ends_at: float
    is_online_meeting: bool
    is_excluded: bool
    exclusion_reason: str | None
    series_master_id: str | None
    attendee_count: int

class UpcomingMeetingsResponse(BaseModel):
    events: list[UpcomingMeetingEvent]
    horizon_minutes: int
    fetched_at: float
```

**Task 0B.3: Define audio response schema.**

`shared/src/shared/audio.py`:

```python
from pydantic import BaseModel
from typing import Literal

class Word(BaseModel):
    word: str
    start: float
    end: float
    probability: float | None = None

class TranscriptSegment(BaseModel):
    start: float
    end: float
    speaker: str | None  # 'user', 'SPEAKER_00', etc., or None
    text: str
    words: list[Word] | None = None

class UnavailableRange(BaseModel):
    start: float
    end: float
    reason: str

class Coverage(BaseModel):
    captured_seconds: float
    unavailable_ranges: list[UnavailableRange]
    transcription_pending_seconds: float

class TranscriptResponse(BaseModel):
    session_id: int | None
    started_at: float
    ended_at: float | None
    segments: list[TranscriptSegment]
    coverage: Coverage
    diarization_status: Literal["pending", "running", "complete", "failed", "not_applicable"]

class OCRFrame(BaseModel):
    ts: float
    app_bundle: str
    display_id: int | None
    text: str
    confidence: float
    thumbnail_url: str | None

class OCRResponse(BaseModel):
    frames: list[OCRFrame]

# --- Voice notes ---

class SuggestedAttachments(BaseModel):
    person_ids: list[int] = []
    workstream_ids: list[int] = []
    ask_ids: list[int] = []

class ConfirmedAttachments(BaseModel):
    person_ids: list[int] = []
    workstream_ids: list[int] = []
    ask_ids: list[int] = []

class AttachmentMatch(BaseModel):
    type: Literal["person", "workstream", "ask"]
    id: int
    display_name: str
    match_text: str
    confidence: float

class AttachmentPreviewResponse(BaseModel):
    suggested_attachments: SuggestedAttachments
    matches: list[AttachmentMatch]

class VoiceNoteCreate(BaseModel):
    helios_voice_note_id: int
    helios_session_id: int
    started_at: float
    ended_at: float
    duration_seconds: float
    transcript_text: str
    triggered_by: Literal["menu_bar", "hotkey", "dashboard"]
    source_device: str = "mac"
    is_excerpt: bool
    excerpt_of_meeting_id: int | None = None
    suggested_attachments: SuggestedAttachments | None = None
    confirmed_attachments: ConfirmedAttachments
```

**Acceptance:** Both Aegis and Helios can `from shared.meetings import UpcomingMeetingEvent` and `from shared.audio import TranscriptResponse` after `uv sync`. mypy passes on the schemas.

### Track 0C — Config and logging

**Task 0C.1: Test-first — config loading.**

Write `helios/tests/test_config.py` with cases:

- TOML missing → loads defaults, generates bearer token, writes file with `chmod 600`
- TOML present with valid values → loads correctly
- TOML present with invalid value (e.g., `port = "not a number"`) → raises clear error
- Environment override (`HELIOS_API__PORT=3032`) → overrides TOML value
- All sections present and Pydantic-validated

**Task 0C.2: Implement config.**

Create `helios/src/helios/config.py` matching HELIOS.md §5.2 schema. Use `pydantic-settings` with `SettingsConfigDict(env_prefix="HELIOS_", env_nested_delimiter="__", toml_file=...)`. Auto-generate bearer token via `secrets.token_hex(32)` when missing.

The full schema includes sections for `api`, `capture`, `exclusion`, `audio`, `transcription`, `diarization`, `ocr`, `retention`, `storage`, `scheduler`, `notifications`, `voice_note`, `voice_note.indicator`, `logging`, `launchagent`, plus the test-only `replay` section.

The `voice_note` section includes: `enabled`, `max_duration_seconds`, `soft_cap_notification`, `auto_save_timeout_seconds`, `default_save_action`, `hotkey_enabled`, `hotkey_combo`. The `voice_note.indicator` subsection includes: `floating_pill_position`, `show_elapsed_time`, `show_audio_level`, `last_position`.

**Acceptance:** All Task 0C.1 tests pass. Manual test: delete `~/.aegis/capture.toml`, run config loader, file is created with correct permissions and a fresh bearer token.

**Task 0C.3: Test-first — logging.**

Write `helios/tests/test_logging.py` with cases:
- `log.info("event_name", session_id=42, foo="bar")` produces JSON line with `ts`, `level`, `event`, `session_id`, `foo`
- Sensitive fields (`bearer_token`, `transcript_text`, `text` on OCR) are filtered/redacted
- Log file rotates daily

**Task 0C.4: Implement structured logging.**

Create `helios/src/helios/logging.py` using `structlog` configured for JSON output. Two handlers: rotating file handler (`~/.aegis/capture/logs/helios.log`) and a SQLite handler for important events (added later in Phase 1 when DB exists).

For now, file-only. Sensitive-field filter as a structlog processor.

**Acceptance:** Tests pass. Logging from any Helios module produces valid JSON in the configured location.

### Track 0D — Initial daemon entry point

**Task 0D.1: Empty FastAPI app with /health.**

Create `helios/src/helios/api/__init__.py`:

```python
from fastapi import FastAPI
from helios.api.routes import health

def create_app() -> FastAPI:
    app = FastAPI(title="Helios", version="0.1.0")
    app.include_router(health.router, prefix="/v1")
    return app
```

Create `helios/src/helios/api/routes/health.py`:

```python
from fastapi import APIRouter
import time

router = APIRouter()
_started_at = time.time()

@router.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "0.1.0",
        "uptime_seconds": time.time() - _started_at,
    }
```

**Task 0D.2: Daemon entry point.**

Create `helios/src/helios/__main__.py`:

```python
import argparse
import asyncio
import sys

def main():
    parser = argparse.ArgumentParser(prog="helios")
    parser.add_argument("--daemon", action="store_true",
                        help="Run as background daemon (LaunchAgent mode)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging to stdout")
    args = parser.parse_args()

    if args.daemon:
        from helios.daemon import run_daemon
        asyncio.run(run_daemon(debug=args.debug))
    else:
        from helios.menubar.app import run_menubar
        run_menubar(debug=args.debug)

if __name__ == "__main__":
    main()
```

Create `helios/src/helios/daemon.py` with a stub `run_daemon` that loads config, configures logging, starts uvicorn:

```python
async def run_daemon(debug: bool = False):
    config = HeliosConfig.load()
    setup_logging(level="DEBUG" if debug else config.logging.level)
    log.info("daemon_started", version="0.1.0", api_port=config.api.port)
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(
        app, host=config.api.bind_address, port=config.api.port, log_level="warning"
    ))
    await server.serve()
```

Create `helios/src/helios/menubar/app.py` with a stub `run_menubar` that just prints "menu bar mode not yet implemented" and exits. Real implementation comes in Phase 4.

**Task 0D.3: Bearer token middleware (stubbed).**

Create `helios/src/helios/api/auth.py` with the bearer token dependency from HELIOS.md §7.1. The `/v1/health` endpoint does NOT require auth (it's the only public endpoint), but other endpoints (added later) will.

**Acceptance:** `cd helios && uv run python -m helios --daemon` starts a server. `curl http://127.0.0.1:3031/v1/health` returns 200 with the expected JSON. Server logs the startup event to file.

### Track 0E — Build and packaging scripts

**Task 0E.1: Swift helper build script.**

Create `scripts/build_swift_helper.sh` from HELIOS.md §20. Runs `swiftc` with universal binary targets, signs ad-hoc, places output in `helios/bin/ScreenCaptureHelper`.

**Task 0E.2: Helios helper source — first draft.**

**Human task:** Either draft `helios/swift/ScreenCaptureHelper.swift` yourself, or have Claude Code generate a first draft based on HELIOS.md §20 and the sketch we discussed earlier. Once committed, mark with `// HUMAN-MAINTAINED — do not modify via Claude Code` at the top.

The Swift source must implement the full contract: framed stdout protocol with packet types 0x01 (audio) and 0x02 (video), stdin command parser (ENABLE_AUDIO, DISABLE_AUDIO, ENABLE_VIDEO, DISABLE_VIDEO, SET_DISPLAY, QUIT), stderr acknowledgments (OK / ERR), `--version` flag, defensive resampling via AVAudioConverter, JPEG q85 video encoding, `minimumFrameInterval = 1.0` for video throttling.

**Task 0E.3: Build script for Helios.app.**

Create `scripts/build_helios.sh` from HELIOS.md §21. Runs py2app and ad-hoc signs the result.

Create `helios/setup.py` from HELIOS.md §21 with the full py2app config.

**Task 0E.4: Install script.**

Create `scripts/install_helios.sh` from HELIOS.md §21. Quits running menu bar, unloads daemon, copies bundle to `/Applications`, writes LaunchAgent plist, reloads daemon, opens app.

**Acceptance:**
- `bash scripts/build_swift_helper.sh` produces `helios/bin/ScreenCaptureHelper` (universal binary)
- `helios/bin/ScreenCaptureHelper --version` prints version and exits 0
- `bash scripts/build_helios.sh` produces `helios/dist/Helios.app`
- The .app launches without crashing (will exit immediately for now since menu bar is stubbed)
- `bash scripts/install_helios.sh` copies the bundle, writes plist, loads LaunchAgent
- After install, `curl http://127.0.0.1:3031/v1/health` works

### Track 0F — Migrations infrastructure

**Task 0F.1: Initial migration.**

Create `helios/migrations/001_initial.sql` with the full schema from HELIOS.md §6.2. Includes all tables (`schema_version`, `capture_sessions`, `session_calendar_links`, `audio_chunks`, `transcript_segments`, `diarization_turns`, `ocr_frames`, `permission_checks`, `component_status`, `daemon_events`, `voice_notes`) and indexes.

The `capture_sessions.kind` CHECK constraint must include `'voice_note'` from day one (per HELIOS.md §6.2). The `end_reason` field should accommodate voice-note-specific reasons (`voice_note_user_stop`, `voice_note_cap_reached`, `voice_note_cancelled`).

**Task 0F.2: Migration runner.**

Create `helios/src/helios/db/migrations.py` matching the implementation in HELIOS.md §6.3.

**Task 0F.3: Connection factory.**

Create `helios/src/helios/db/connection.py` with `DatabasePool` from HELIOS.md §6.4. WAL mode, foreign keys, busy_timeout pragmas.

**Task 0F.4: Test-first — migrations.**

Write `helios/tests/test_migrations.py`:
- Fresh database → all tables created, version = 1
- Re-run migrations → idempotent, no errors
- Connection pool opens/closes cleanly
- WAL mode active after open
- Multiple concurrent reads work

**Acceptance:** Tests pass. Manual test: delete `~/.aegis/capture/index.db`, start daemon, file is created with all tables.

### Track 0G — Placeholder icons

**Task 0G.1: Generate placeholder icons.**

Create the 12 menu bar PNG files (6 states × 2 resolutions: @1x 18×18, @2x 36×36) and the `Helios.icns`. For v1, generate ugly-but-functional placeholders programmatically using PIL:
- Not running: outline circle
- Armed: outline circle with center dot
- Recording: filled circle (record button)
- Recording voice note: microphone glyph (or filled circle with mic-style mark)
- Paused: outline circle with two vertical bars
- Error: outline circle with exclamation mark

Files must be template PNGs (black + alpha). Save with the `_template` suffix as required by macOS for menu bar template behavior. The voice note state file is named `helios_recording_voice_note_template.png` (and @2x variant).

Generate the .icns by combining 16, 32, 64, 128, 256, 512, 1024 PNG variants of a simple Helios shield logo.

**Acceptance:** All 13 image files exist in `helios/icons/`. macOS Preview can open them. The .icns file shows a recognizable icon.

### Phase 0 Checkpoint

Required for advancement:

- [ ] All Track 0A-0G tasks complete; tests pass
- [ ] `bash scripts/build_helios.sh` succeeds
- [ ] `bash scripts/install_helios.sh` succeeds
- [ ] Daemon runs and serves `/v1/health` after install
- [ ] Phase 0 smoke test (§12.1) signed off by human
- [ ] Last commit includes a `PHASE_0_CHECKPOINT.md` summary listing what was built

**Phase 0 smoke test summary** (full procedure in §12.1):

1. Build Helios.app from clean state
2. Install via the install script
3. Confirm menu bar icon appears (placeholder is fine)
4. Confirm daemon serves `/v1/health` from outside the build environment
5. Confirm SystemAudioCapture --version works from the bundled path
6. Confirm logs are being written to `~/.aegis/capture/logs/helios.log`

If any item fails: do not advance. Report findings, fix, re-run.

---

## Phase 1 — Capture Pipeline (Replay Mode)

**Goal:** Audio capture pipeline working end-to-end in replay mode. Reads fixture WAVs, produces 30s WAV chunks on disk, writes correct rows to SQLite. Real-hardware capture also works at the smoke test.

**Estimated time:** 1 week of Claude Code work.

### Track 1A — Clock abstraction

**Task 1A.1: Test-first — Clock protocol.**

Write `helios/tests/test_clock.py`:
- `RealClock.time()` returns increasing wall-clock values
- `VirtualClock.time()` returns the seeded initial value
- `VirtualClock.advance(N)` advances time, fires registered timers due before/at the new time
- `VirtualClock.sleep(N)` blocks until `advance` brings the clock past N
- Multiple coroutines sleeping on the same clock all wake correctly when time advances
- `call_later` schedules callbacks correctly

**Task 1A.2: Implement Clock.**

Create `helios/src/helios/clock.py` matching HELIOS.md §18.3. Both `RealClock` and `VirtualClock` implement the `Clock` protocol.

**Acceptance:** Tests pass. VirtualClock can drive a 30-minute simulation in under 100ms of wall-clock time.

### Track 1B — Source abstractions

**Task 1B.1: Define source protocol.**

Create `helios/src/helios/sources/interface.py` with the `AudioSample` named tuple, `AudioSource` protocol, and `SystemAudioSource` marker protocol from HELIOS.md §8.1. Also define a `VideoFrame` named tuple (timestamp, JPEG bytes) for use in OCR phase.

**Task 1B.2: Test-first — replay source.**

Write `helios/tests/test_sources_replay.py`:
- Reads a 60-second fixture WAV at 16kHz mono int16
- Emits `AudioSample` tuples at the configured `speed_multiplier` rate (10x for fast tests)
- `start()` and `stop()` lifecycle works correctly
- Two replay sources (mic + system) emit synchronized timestamps
- Calendar replay reads JSON fixture and exposes events via the same interface real calendar uses

**Task 1B.3: Implement replay source.**

Create `helios/src/helios/sources/replay.py`:

```python
class ReplayMicSource:
    def __init__(self, wav_path: Path, queue, clock: Clock, start_ts: float, speed: float = 1.0):
        ...

    async def start(self):
        # Read WAV, emit samples in chunks aligned to clock progression
        ...
```

For replay calendar source, read the JSON fixture and provide the same `get_upcoming` interface that `aegis/clients/helios.py`'s real calendar will provide later.

Source factory:

```python
# helios/src/helios/sources/__init__.py
def make_source_factory(config) -> SourceFactory:
    if os.environ.get("HELIOS_REPLAY") == "1":
        return ReplaySourceFactory(config.replay)
    return RealSourceFactory(config)
```

**Acceptance:** Replay tests pass. A test fixture WAV can be played through the replay mic source and produces correct AudioSample emissions.

**Task 1B.4: Implement real mic source.**

Create `helios/src/helios/sources/real.py` with `MicSource` from HELIOS.md §8.2. Use sounddevice. Include device-change handling (PortAudioError → log + restart).

**Task 1B.5: Implement real system audio source.**

In the same file, add `SCKSystemAudioSource` from HELIOS.md §8.3. Spawns the Swift helper subprocess, parses the framed stdout protocol, sends stdin commands.

The framed protocol parser reads: `[1B type][8B ts][4B length][payload]`. Type 0x01 → AudioSample to audio queue. Type 0x02 → VideoFrame to video queue.

**Acceptance:** Unit tests with mocked subprocess verify framing parser. Integration test (gated by smoke test, since it requires real Swift helper and screen recording permission).

### Track 1C — Database queries

**Task 1C.1: Row models.**

Create `helios/src/helios/db/rows.py` with Pydantic models for every table from HELIOS.md §6.5: `CaptureSessionRow`, `SessionCalendarLinkRow`, `AudioChunkRow`, `TranscriptSegmentRow`, `DiarizationTurnRow`, `OCRFrameRow`, `PermissionCheckRow`, `ComponentStatusRow`, `DaemonEventRow`.

**Task 1C.2: Test-first — query functions.**

Write `helios/tests/test_db_queries.py` with tests for every query function. Use a real SQLite database in a temp directory. Test cases:

- `insert_audio_chunk` and `get_chunk_by_id` round-trip
- `get_pending_chunks` returns only `status='recorded' AND transcribed_at IS NULL`
- `get_session_audio_chunks` returns chunks for a session ordered by start_ts
- `get_chunks_in_range` returns chunks overlapping a time window
- `mark_chunk_transcribed`, `mark_chunk_transcription_failed`, `mark_chunk_archived`
- Session queries: `insert_session`, `update_session_ended`, `get_active_session`, `get_sessions_overlapping`
- Junction table: `link_session_to_calendar_event`, `get_sessions_for_calendar_event`
- Foreign keys enforced

Aim for 90%+ coverage of `queries.py`.

**Task 1C.3: Implement query functions.**

Create `helios/src/helios/db/queries.py` with all functions tested in 1C.2. Pydantic row return types. No raw SQL outside this file.

**Acceptance:** All Task 1C.2 tests pass. Coverage of queries.py ≥ 90%.

### Track 1D — Chunker

**Task 1D.1: Test-first — chunker.**

Write `helios/tests/test_chunker.py`:
- 30s of samples produces one full chunk and writes WAV file
- 35s of samples produces one full chunk, 5s remains in buffer
- `stop_session` flushes remaining buffer as `partial=true`
- Silent chunk (RMS below threshold) creates row with `status='no_audio'`, no WAV file written
- Independent per-channel buffering (mic and system don't interfere)
- Concatenated samples produce correct WAV (read it back, verify length and sample rate)
- Database row matches WAV file: start_ts, end_ts, samples count

**Task 1D.2: Implement chunker.**

Create `helios/src/helios/capture/chunker.py` matching HELIOS.md §8.5. Use `wave` stdlib for WAV writing. Run blocking I/O in a thread pool executor to avoid blocking the event loop.

**Acceptance:** All chunker tests pass. WAV files are valid (can be opened in QuickTime).

### Track 1E — Stream manager

**Task 1E.1: Test-first — stream manager.**

Write `helios/tests/test_stream_manager.py`:
- `start()` initializes both mic and system streams
- `start()` raises `StreamStartError` if mic init fails
- `start()` cleans up mic stream if system init fails after mic succeeds
- Watchdog detects no-buffers-for-30s and triggers restart
- `stop()` cleanly stops both streams and watchdog
- Wake notification triggers stream restart

**Task 1E.2: Implement stream manager.**

Create `helios/src/helios/capture/stream_manager.py` matching HELIOS.md §8.4. Power assertion (`IOPMAssertionCreateWithName`) acquired on start, released on stop. NSWorkspace wake notification subscription via PyObjC.

For replay-mode integration, the StreamManager accepts source instances rather than constructing them — the source factory is injected via DI. This lets tests inject replay sources without subclassing.

**Acceptance:** Stream manager tests pass. Manual integration test: with replay sources, start manager, verify samples flow through to queues.

### Track 1F — Wire everything together

**Task 1F.1: Capture orchestrator.**

Create `helios/src/helios/capture/orchestrator.py`:

```python
class CaptureOrchestrator:
    """Coordinates session lifecycle: scheduler tells orchestrator to start/stop;
    orchestrator manages streams, chunker, session DB rows."""
    async def start_session(
        self, kind: str, calendar_events: list[CalendarEvent] | None = None,
        screen_override_until: float | None = None,
    ) -> int:
        # 1. Insert session row
        # 2. Insert session_calendar_links rows (if calendar_events)
        # 3. Start chunker
        # 4. Start stream manager (or no-op if already running for an adjacent session)
        # 5. Return session_id
        ...

    async def stop_session(self, session_id: int, reason: str) -> None:
        # 1. Stop chunker (partial flush)
        # 2. Stop stream manager (if no other active session needs it)
        # 3. Update session row: ended_at, end_reason
        # 4. (Phase 3) Enqueue diarization
        ...
```

**Task 1F.2: Test-first — orchestrator end-to-end.**

Write `helios/tests/test_capture_orchestrator.py`:
- Start session with replay sources → samples flow through chunker → WAV files appear → DB rows created
- Stop session → ended_at set, partial chunk flushed
- Two adjacent sessions reuse the stream (orchestrator doesn't restart streams between adjacent sessions)
- Crash recovery: simulate crash with active session, restart, session marked ended with reason `crash_recovery`

**Task 1F.3: Wire into daemon.**

Update `helios/src/helios/daemon.py` to instantiate and start the capture orchestrator. Wire the source factory based on `HELIOS_REPLAY` env var.

Add to `daemon.py` an MVP API endpoint `POST /v1/capture/start` (no auth yet, will add in Phase 2) so smoke test can trigger a capture without waiting for the scheduler.

**Acceptance:** All orchestrator tests pass. Manual integration: `HELIOS_REPLAY=1 python -m helios --daemon` reads fixture WAVs and produces correct chunk files in `~/.aegis/capture/<date>/{mic,system}/`.

### Track 1G — Power management and sleep handling

**Task 1G.1: Power assertion.**

In `stream_manager.py`, add `IOPMAssertionCreateWithName` calls via PyObjC. Acquire `kIOPMAssertionTypePreventUserIdleSystemSleep` on start, release on stop. Test by acquiring during a session and verifying the system doesn't go to sleep (manual smoke test only).

**Task 1G.2: Wake notification.**

Subscribe to `NSWorkspace.didWakeNotification` on stream manager start. On wake, log event and restart streams. Test via mock notification post (PyObjC supports this).

**Task 1G.3: Sleep gap handling.**

When streams stop delivering buffers due to system sleep (detected by watchdog after 30s), mark in-progress chunks as `unavailable` with reason `system_sleep` via `chunker.mark_unavailable()`.

**Acceptance:** Watchdog tests verify gap marking. Power assertion verified manually in smoke test (close laptop briefly, reopen, verify capture resumes).

### Phase 1 Checkpoint

Required for advancement:

- [ ] All Track 1A-1G tasks complete; tests pass
- [ ] Coverage on `chunker.py`, `stream_manager.py`, `db/queries.py` ≥ 85%
- [ ] Replay-mode end-to-end test produces correct chunks from fixture WAV
- [ ] Phase 1 smoke test (§12.2) signed off by human, including:
  - Real 60-second mic + system capture works
  - WAV files are valid and contain recognizable audio
  - SQLite has correct rows
  - Power assertion observed (laptop won't idle-sleep during active capture)
- [ ] **Human task complete:** at least one audio fixture (`meeting_2speaker_10min_*.wav`) recorded and committed (gitignored binary, fetch script in place)
- [ ] `PHASE_1_CHECKPOINT.md` summary committed

If any item fails: report, fix, re-run.

---

## Phase 2 — Scheduler and API

**Goal:** Calendar-driven session lifecycle. Aegis exposes upcoming meetings; Helios polls and schedules captures. Full HTTP API surface (except transcript-related endpoints, which come in Phase 3).

**Estimated time:** 1 week of Claude Code work.

### Track 2A — Aegis API additions

**Task 2A.1: Add `/api/meetings/upcoming` endpoint to Aegis.**

Modify Aegis: add a new route file or extend existing `aegis/web/routes/api.py` (Claude Code chooses based on Aegis's existing conventions; the summary notes there are several JSON endpoints already).

Implement `GET /api/meetings/upcoming` per HELIOS.md §16.5. Uses `shared.meetings.UpcomingMeetingsResponse` for the schema. No authentication required (loopback only, consistent with Aegis's existing read endpoints).

Logic:
1. Query Aegis's `meetings` table for events in the time window (now → now + horizon_minutes)
2. For each, determine `is_excluded`: true if `helios_exclude` is true (from Phase 6 column add — for now assume false), OR if Aegis's keyword match flags the meeting, OR if attendees list is empty/declined
3. Set `title` to `"(excluded)"` if excluded; otherwise the actual title

**Task 2A.2: Tests for new Aegis endpoint.**

Add tests to Aegis's existing test suite verifying:
- Empty calendar returns `events: []`
- Single upcoming meeting in window returns it correctly
- Excluded meeting has title `"(excluded)"` and `is_excluded: true`
- Past meetings not returned
- Meeting outside horizon not returned
- Recurring series instances correctly include `series_master_id`

**Acceptance:** Endpoint returns correct shape, all tests pass, manual `curl http://127.0.0.1:8000/api/meetings/upcoming?horizon_minutes=60` returns valid JSON.

**Task 2A.3: Aegis voice note endpoints (stubs).**

In Aegis, add stub endpoints that return placeholder responses. Real implementations come in Phase 3:

- `POST /api/voice-notes/preview-attachments` — returns empty `suggested_attachments` and empty `matches` for now
- `POST /api/voice-notes` — accepts `VoiceNoteCreate` body, returns `{"voice_note_id": <fake_id>, "extraction_status": "queued"}` without actually creating a row

Adding the stubs in Phase 2 lets the menu bar code (Phase 4) develop against a real endpoint surface even before extraction is wired up.

Tests verify the stubs accept valid request shapes and reject malformed bodies with 422.

### Track 2B — Calendar client in Helios

**Task 2B.1: Test-first — calendar client.**

Write `helios/tests/test_calendar_client.py`:
- Successful fetch returns parsed events
- Connection refused → raises `AegisUnreachable`
- 500 from Aegis → raises `AegisUnreachable`
- Invalid response shape → raises `AegisProtocolError`
- Mock httpx.MockTransport for all cases

**Task 2B.2: Implement calendar client.**

Create `helios/src/helios/scheduler/calendar.py`:

```python
class CalendarClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient, timeout: int):
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._timeout = timeout

    async def get_upcoming(self, horizon_minutes: int = 60) -> list[CalendarEvent]:
        try:
            r = await self._http.get(
                f"{self._base_url}/api/meetings/upcoming",
                params={"horizon_minutes": horizon_minutes},
                timeout=self._timeout,
            )
            r.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise AegisUnreachable(str(e)) from e
        data = UpcomingMeetingsResponse.model_validate(r.json())
        return [CalendarEvent.from_api(e) for e in data.events]
```

`CalendarEvent` is an internal Helios dataclass derived from the shared schema; it exposes the fields Helios cares about and adds derived fields like `pre_start_ts`.

**Acceptance:** Tests pass. Manual: with Aegis running, `helios.scheduler.calendar.CalendarClient` fetches real upcoming events.

### Track 2C — Scheduler

**Task 2C.1: Test-first — adjacency grouping.**

Write `helios/tests/test_scheduler.py`. First wave of tests covers pure adjacency logic with no clock or async:

- Two meetings 6 minutes apart → two separate groups
- Two meetings 5 minutes apart → one group (within adjacency window)
- Two meetings overlapping → one group
- Three meetings: A (14:00–14:30), B (14:30–15:00), C (16:00–16:30) → groups [(A,B), (C)]
- Nested: A (14:00–15:00), B (14:15–14:45) → one group (A,B)
- Excluded meetings filtered out before grouping

**Task 2C.2: Test-first — reconciliation.**

Second wave: reconciliation logic with VirtualClock:

- Empty event list, no timers → no-op
- New event added → timer scheduled
- Event removed → timer cancelled
- Event rescheduled earlier → timer rescheduled
- Active session for cancelled event → session continues (don't interrupt)
- Pause-until set → events during pause are not scheduled

**Task 2C.3: Test-first — hard stop and 4-hour prompt.**

Third wave: time-of-day logic:

- Active continuous session, clock advances to 17:30 local → session stopped with reason `hard_stop_530`
- Active calendar session at 17:30 → not stopped (calendar exempt from hard stop)
- Continuous session running 4 hours → 4-hour prompt notification fires
- Prompt response "continue" → next prompt scheduled for +4h
- Prompt response "stop" → session stops with reason `4hr_prompt_stop`
- Prompt timeout (300s no response) → session stops with reason `4hr_prompt_stop`

**Task 2C.4: Implement scheduler.**

Create `helios/src/helios/scheduler/scheduler.py` matching HELIOS.md §11.2 with the polished implementation. Heavy use of dependency injection: clock, calendar client, capture orchestrator, db, configs all injected.

Create `helios/src/helios/scheduler/timezone.py` with the timezone-aware time helpers (next morning at hour, next Monday, hard stop time today).

**Task 2C.5: Wire scheduler into daemon.**

Update `daemon.py`: instantiate scheduler with real (or replay) calendar client, start it as part of daemon startup.

In replay mode, the calendar client reads from a JSON fixture instead of HTTP.

**Task 2C.6: Voice note scheduling exemptions.**

Update scheduler to:
- Exclude voice note sessions (`kind='voice_note'`) from 5:30 PM hard stop check
- Exclude voice note sessions from the 4-hour prompt
- Allow voice note creation during pause-until (paused state doesn't block voice notes)
- Skip voice note sessions in calendar adjacency grouping

Tests:
- Voice note running at 5:30 PM does not stop
- Voice note running for 4+ hours does not get prompt (relevant if max_duration_seconds is increased above default)
- Voice note can be created while pause-until is active
- Voice note adjacent to calendar event does not merge into one session

**Task 2C.7: Voice note duration cap timer.**

When a voice note session starts (via `/v1/voice-note/start`), schedule:
1. `cap_warning` callback at `started_at + max_duration_seconds - 30` — fires soft cap notification
2. `force_stop` callback at `started_at + max_duration_seconds` — stops session with reason `voice_note_cap_reached`

If voice note stops normally (user action) before either fires, both callbacks are cancelled.

Tests with VirtualClock:
- Voice note runs to 4:30 → cap_warning fires → notification sent
- Voice note runs to 5:00 → force_stop fires → session ends with reason `voice_note_cap_reached`
- Voice note stopped at 0:30 → both timers cancelled cleanly
- Voice note cancelled at 2:00 → both timers cancelled, session marked `voice_note_cancelled`

**Acceptance:** All scheduler tests pass. Manual integration: with Aegis running, scheduler polls correctly. With virtual clock + fixture calendar, scheduler fires sessions at the right virtual times. Voice note timers fire correctly under VirtualClock.

### Track 2D — State machine

**Task 2D.1: Test-first — daemon state machine.**

Write `helios/tests/test_state.py`:
- Initial state: `armed`
- Capture starts → `recording`
- Capture stops → `armed`
- Pause-until set → `paused`
- Pause expires → `armed`
- Permission revoked → `error`
- Component fails to load → `error`
- Aegis unreachable → state has `aegis_unreachable=true` flag (not full error)

**Task 2D.2: Implement state machine.**

Create `helios/src/helios/state.py`:

```python
@dataclass
class DaemonState:
    mode: Literal["armed", "recording", "paused", "error", "not_running"]
    active_session_id: int | None
    paused_until: float | None
    aegis_unreachable: bool
    component_errors: dict[str, ComponentError]
    last_error: str | None

class DaemonStateMachine:
    def __init__(self):
        self._state = DaemonState(...)
        self._listeners: list[Callable] = []

    def transition(self, new_mode: str, **fields):
        ...
```

State transitions trigger listener callbacks (for menu bar updates, status component refresh).

**Acceptance:** State machine tests pass.

### Track 2E — Full HTTP API

**Task 2E.1: Bearer token auth.**

Update `helios/src/helios/api/auth.py` to enforce bearer token from config on all endpoints except `/v1/health`.

**Task 2E.2: Test-first — every endpoint.**

Write `helios/tests/test_api.py` covering every endpoint from HELIOS.md §7.3:

For each endpoint:
- 200 happy path
- 401 without bearer token
- 401 with wrong bearer token
- Validation errors return 422 with proper shape
- Error responses match the documented format

Specific endpoints:
- `GET /v1/health` (no auth)
- `GET /v1/status` — verify all fields, mocked queue counts
- `POST /v1/capture/start` with kind=continuous
- `POST /v1/capture/start` with kind=manual_screen and duration_minutes
- `POST /v1/capture/start` with kind=manual_screen missing duration → validation error
- `POST /v1/capture/stop` with active session → returns session info
- `POST /v1/capture/stop` with no active session → returns `session_id: null`
- `POST /v1/capture/pause-until` with timestamp
- `POST /v1/capture/resume`
- `POST /v1/capture/enable-screen-override`
- `GET /v1/sessions` with various filters
- `GET /v1/sessions/{id}` valid + 404
- `GET /v1/permissions`
- `GET /v1/diagnostics` — verify shape
- `POST /v1/diagnostics/restart` — returns 202, queues restart
- `POST /v1/diagnostics/flush-queues`
- `POST /v1/diagnostics/test-capture`
- `POST /v1/diagnostics/reload-component`
- Session-level: `re-transcribe`, `re-diarize`, `DELETE`

For Phase 2, `GET /v1/sessions/{id}/transcript` and `GET /v1/audio` and `GET /v1/ocr` can return placeholder responses (empty `segments: []`); they're fully implemented in Phase 3 and Phase 5 respectively.

**Task 2E.3: Implement endpoints.**

Create one file per endpoint group under `helios/src/helios/api/routes/`:
- `status.py` — `/v1/status`, `/v1/permissions`, `/v1/diagnostics`
- `capture.py` — all `/v1/capture/*`
- `sessions.py` — all `/v1/sessions/*`
- `audio.py` — `/v1/audio` (placeholder)
- `ocr.py` — `/v1/ocr` (placeholder)
- `diagnostics.py` — `/v1/diagnostics/*`

Each route module exports a `router: APIRouter`. Mount all in `api/__init__.py`.

**Task 2E.4: Schemas.**

Create `helios/src/helios/api/schemas.py` with all request/response Pydantic models from HELIOS.md §7.4. Reuse types from `shared/` where applicable.

**Task 2E.5: Voice note endpoints.**

Test-first. Write tests in `helios/tests/test_voice_note_endpoints.py`:

- `POST /v1/voice-note/start` happy path (no other capture active) → creates session, returns voice_note_id
- `POST /v1/voice-note/start` during active meeting (calendar/continuous) → creates excerpt voice note, references parent session, sets `is_excerpt=true`
- `POST /v1/voice-note/start` while another voice note active → 409 voice_note_already_active
- `POST /v1/voice-note/start` with transcription unavailable → 503 component_unavailable
- `POST /v1/voice-note/start` with mic permission denied → 403 permission_denied
- `POST /v1/voice-note/stop` → returns transcript synchronously
- `POST /v1/voice-note/stop` with no active note → 404 voice_note_not_active
- `POST /v1/voice-note/cancel` → discards in-progress note, returns cancelled_voice_note_id
- `GET /v1/voice-note/active` → returns active note state or null
- `GET /v1/voice-note/active` returns `approaching_cap=true` when within 30s of max duration

Implement endpoints in new `helios/src/helios/api/routes/voice_note.py`. The synchronous transcription path is implemented in Phase 3 (Track 3B). For Phase 2, the stop endpoint can return immediately with `transcript: null` and the test fixtures mock the transcription pipeline. The full synchronous path lights up in Phase 3.

**Acceptance:** All API tests pass. Manual: every endpoint can be hit with curl, returns documented shapes.

### Track 2F — Permission checks

**Task 2F.1: Test-first — permission checker.**

Write `helios/tests/test_permission_check.py`:
- Mocked `CGPreflightScreenCaptureAccess` and `AVCaptureDevice.authorizationStatus`
- Returns correct status for each combination
- Records to `permission_checks` table
- 5-minute periodic check fires correctly under VirtualClock

**Task 2F.2: Implement permission checker.**

Create `helios/src/helios/workers/permissions.py`:

```python
class PermissionChecker:
    async def check_now(self) -> PermissionState:
        mic = self._check_mic()  # AVCaptureDevice.authorizationStatus
        scr = CGPreflightScreenCaptureAccess()
        await queries.insert_permission_check(...)
        return PermissionState(mic_granted=mic, screen_recording_granted=scr)

    async def run_periodic(self):
        while True:
            await self.check_now()
            await self._clock.sleep(self._config.permission_check_minutes * 60)
```

Include detection of revocation: compare current check to last check; on transition `granted → not_granted`, fire notification and update daemon state.

**Acceptance:** Tests pass. Manual: revoke mic permission in System Settings, observe state transition within 5 minutes.

### Phase 2 Checkpoint

Required for advancement:

- [ ] All Track 2A-2F tasks complete; tests pass
- [ ] Coverage on `scheduler/scheduler.py` ≥ 85%
- [ ] All API endpoints tested and working
- [ ] Aegis's `/api/meetings/upcoming` works correctly
- [ ] Phase 2 smoke test (§12.3) signed off, including:
  - Test calendar event triggers a session at the right time
  - Adjacent meetings produce one continuous session
  - Pause-until prevents future scheduled sessions
  - 5:30 PM hard stop fires for continuous mode (test by temporarily setting hard stop to 1 minute from now)
  - Permission revocation detected within 5 minutes
- [ ] `PHASE_2_CHECKPOINT.md` summary committed

---

## Phase 3 — Transcription, Diarization, Aegis Integration

**Goal:** Audio becomes speaker-labeled transcripts. Aegis's HeliosClient replaces ScreenpipeClient. `meeting_detector` simplified. First real transcripts flow into Aegis's meetings table.

**Estimated time:** 1.5 weeks of Claude Code work.

### Track 3A — Whisper download script and onboarding helper

**Task 3A.1: Download script.**

Create `helios/src/helios/scripts/download_whisper.py` per HELIOS.md §15.3:

```python
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    args = parser.parse_args()

    check_disk_space(3 * 1024**3)
    snapshot_path = huggingface_hub.snapshot_download(
        repo_id=f"guillaumekln/faster-whisper-{args.model}",
        resume_download=True,
        tqdm_class=JSONProgressTqdm,
    )
    verify_with_synthetic_audio(snapshot_path)
    print(json.dumps({"type": "done", "path": snapshot_path}))
```

`JSONProgressTqdm` emits progress as JSON lines on stdout for the onboarding UI (Phase 4) to parse.

`verify_with_synthetic_audio` runs a 1-second sine wave through WhisperX to confirm the model loads.

**Task 3A.2: Component status reporter.**

Create `helios/src/helios/state.py` (or extend it) with `ComponentStatusReporter`:

```python
class ComponentStatusReporter:
    def set(self, component: str, status: str, *,
            reason: str | None = None, detail: str | None = None,
            action: str | None = None) -> None:
        self._current[component] = ComponentStatus(...)
        await queries.insert_component_status(...)
        # Trigger state machine update
```

### Track 3B — Transcription worker

**Task 3B.1: Test-first — transcription worker.**

Write `helios/tests/test_transcription_worker.py`:

Mocked WhisperX layer:
- `transcribe()` mocked to return canned segments
- Worker picks up pending chunks, calls model, writes segments
- Mic chunks get speaker='user' immediately
- System chunks get speaker=NULL
- Failed chunk after 3 attempts → `transcription_failed` status
- Model load timeout → component_status set to `unavailable`
- Import error → component_status set to `unavailable` with action message

Real WhisperX layer (slow, marked `@pytest.mark.slow`):
- Run on a fixture WAV (10s clean speech)
- Verify segments have non-empty text
- Verify timestamps are within chunk bounds

**Task 3B.2: Implement transcription worker.**

Create `helios/src/helios/workers/transcription.py` matching HELIOS.md §9.2.

`os.nice(10)` on the worker thread to lower priority. Run blocking model calls via `loop.run_in_executor`.

**Task 3B.3: Wire into daemon.**

In replay/test mode, transcription worker uses mocked WhisperX. In real mode, loads the real model in a background thread on daemon start (§9.2 background load pattern).

**Task 3B.4: Synchronous transcription path for voice notes.**

Add a `transcribe_synchronously` method to `TranscriptionWorker` per HELIOS.md §9.8:

```python
async def transcribe_synchronously(
    self, chunks: list[AudioChunkRow]
) -> TranscriptResult:
    """Run transcription on a specific list of chunks, bypassing the queue.
    Used for voice notes which need immediate transcript return."""
    await self._model_ready.wait()
    self._pause_normal_loop = True
    try:
        audio = await self._concatenate_chunks(chunks)
        segments = await asyncio.get_event_loop().run_in_executor(
            None, self._model.transcribe, audio,
            {"language": self._config.language, "word_timestamps": True,
             "vad_filter": self._config.vad_filter},
        )
        for chunk, chunk_segments in zip(chunks, segments_per_chunk):
            await self._store_segments(chunk, chunk_segments)
        return TranscriptResult(segments=all_segments)
    finally:
        self._pause_normal_loop = False
```

Tests:
- Sync path produces same transcript as queue path for the same input
- Normal worker is paused during sync (verify by checking queue isn't drained)
- Sync path returns within 60s for typical voice note audio (5 min mic input)
- Multiple sync calls serialize correctly (don't run concurrently)

Wire into `/v1/voice-note/stop` endpoint (replacing the Phase 2 placeholder behavior).

**Task 3B.5: Excerpt-mode transcription.**

When voice note is an excerpt (`is_excerpt=true`):
1. Don't capture new audio — the parent session's mic stream is already running
2. At voice-note stop, query for chunks in `[excerpt_start_ts, excerpt_end_ts]` from the parent session
3. Some chunks may already be transcribed (if normal worker got to them first); reuse those segments
4. Transcribe any not-yet-transcribed chunks via the sync path
5. Compose the response from the union of segments in the time range

Tests:
- Excerpt mode chunks remain owned by parent session (not duplicated)
- Voice note transcript correctly assembled from time-range query
- Mid-excerpt chunks transcribed by normal worker are reused (no duplicate transcription)
- Voice note's transcript_segments rows visible to parent meeting transcript queries (no carve-out)

**Acceptance:** All transcription tests pass. Slow tests pass on real audio (run via `pytest -m slow`). Voice note synchronous path returns within 10 seconds for a 30-second note on Apple Silicon.

### Track 3C — Diarization worker

**Task 3C.1: Keychain helper.**

Create `helios/src/helios/keychain.py`:

```python
def get_hf_token() -> str | None:
    try:
        return keyring.get_password("helios", "huggingface")
    except keyring.errors.KeyringError:
        return None

def set_hf_token(token: str) -> None:
    keyring.set_password("helios", "huggingface", token)

def clear_hf_token() -> None:
    keyring.delete_password("helios", "huggingface")
```

**Task 3C.2: Test-first — diarization worker.**

Write `helios/tests/test_diarization_worker.py`:

Mocked pyannote layer:
- Pipeline returns canned diarization with N speakers
- Worker writes turns with correct speaker labels
- Embeddings stored as BLOB
- Failure → session diarization_status set to `failed`
- Disabled in config → component status `unavailable` with reason `disabled`
- Token missing → component status `unavailable` with reason `token_missing`

Real pyannote layer (slow):
- Run on a fixture WAV with known 3 speakers
- Verify ~3 unique speaker labels
- Verify turn boundaries within ±500ms tolerance against golden

**Task 3C.3: Implement diarization worker.**

Create `helios/src/helios/workers/diarization.py` matching HELIOS.md §9.3. Concatenates session's system-channel WAVs into a temp file before pyannote runs; deletes temp file after.

Embedding extraction: pyannote 3.1's pipeline can yield speaker embeddings. Store as float32 BLOB.

### Track 3D — Merge worker

**Task 3D.1: Test-first — merge worker.**

Write `helios/tests/test_merge_worker.py`:
- Single turn covers entire segment → segment gets that speaker
- Two turns split a segment → segment gets the speaker with maximum overlap
- No turn overlaps a segment → speaker stays NULL
- Session with no diarization (disabled) → mic segments still have 'user', system stay NULL

**Task 3D.2: Implement merge worker.**

Create `helios/src/helios/workers/merge.py` matching HELIOS.md §9.4.

**Task 3D.3: Wire transcription → diarization → merge.**

When session ends, orchestrator enqueues to diarization worker. When diarization completes, it enqueues to merge worker. Each worker is an asyncio task with its own queue.

**Acceptance:** Full pipeline test (replay): fixture WAV → chunks → transcription → diarization → merge → final speaker-labeled segments in DB.

### Track 3E — Transcript serving endpoints

**Task 3E.1: Test-first — transcript endpoints.**

Update `helios/tests/test_api.py` with real implementations:

- `GET /v1/sessions/{id}/transcript` returns segments in correct order with coverage
- Coverage calculation: captured_seconds, unavailable_ranges, transcription_pending_seconds
- `GET /v1/audio?start=&end=` returns segments in time window across multiple sessions
- `include_words` parameter includes/excludes word-level data
- 404 for non-existent session

**Task 3E.2: Implement endpoints.**

Update `helios/src/helios/api/routes/sessions.py` and `audio.py` with the real implementations from HELIOS.md §9.5 and §9.6.

Coverage computation in a helper function in `helios/src/helios/capture/coverage.py`.

### Track 3F — Aegis integration

**Task 3F.1: Create `aegis/clients/helios.py`.**

Implement `HeliosClient` per HELIOS.md §16.1 and §16.2. Token loading from `~/.aegis/capture.toml`, 401 retry, typed responses using shared schemas.

**Task 3F.2: Test-first — HeliosClient.**

Write tests in Aegis's test suite:
- `health_check()` returns true/false
- `get_transcript_for_meeting()` calls `/v1/audio` with correct time window
- 401 retry: token reloaded, request retried once
- Daemon down: returns None, doesn't raise
- Mock httpx.MockTransport for all cases

**Task 3F.3: Rename and rewrite `aegis/ingestion/screenpipe.py` → `helios.py`.**

`aegis/ingestion/helios.py`:
- Imports `HeliosClient` from `aegis/clients/helios.py`
- Provides higher-level functions used by `meeting_detector`: `get_transcript_for_meeting`, `get_ocr_for_meeting`, `health_check`
- `HeliosTranscript` dataclass that wraps the raw API response

Delete `aegis/ingestion/screenpipe.py`.

**Task 3F.4: Simplify `aegis/ingestion/meeting_detector.py`.**

Per HELIOS.md §16.3:
- **Delete `_stitch_transcript()`** — no longer needed.
- Replace calls to old `ScreenpipeClient` with new `HeliosClient` calls.
- Update status determination to use `HeliosTranscript.coverage_pct` directly.
- Keep back-to-back detection (calendar logic, not capture-related).
- Keep buffer padding and overage detection.

Update Aegis's existing tests to reflect the simplification. Tests for the deleted `_stitch_transcript()` are removed; tests for the rewritten `build_transcript()` flow exercise the new path.

**Task 3F.5: Update Aegis config.**

Modify `aegis/config.py`:
- Add `helios_url`, `helios_token_path`, `helios_heartbeat_seconds`, `helios_heartbeat_timeout_seconds`
- Remove `screenpipe_url` and `polling_screenpipe_seconds` (or mark deprecated)

Update existing config tests.

**Task 3F.6: Add Helios heartbeat loop to Aegis poller.**

Per HELIOS.md §16.8: add a heartbeat loop that pings `/v1/health` every 60s and writes to Aegis's existing `system_health` table with component=`helios`.

On transition from `ok → down`, fire a macOS notification via Aegis's existing `notifications/macos.py`.

**Task 3F.7: Wire HeliosClient in app startup.**

Update `aegis/main.py` (or wherever Aegis's lifespan is configured) to instantiate `HeliosClient` and store in app state, per HELIOS.md §16.9.

**Task 3F.8: Voice notes Alembic migration.**

Generate Alembic migration `alembic/versions/XXXXXX_add_voice_notes.py` creating both `voice_notes` and `voice_note_attachments` tables per HELIOS.md §16.12.

**Task 3F.9: VoiceNote and VoiceNoteAttachment models.**

Add `VoiceNote` and `VoiceNoteAttachment` SQLAlchemy models to `aegis/db/models.py` per HELIOS.md §16.12.

Tests:
- Models load correctly
- Cascade delete works (deleting a voice note removes its attachments)
- Unique constraint on `(voice_note_id, target_type, target_id)` enforced
- ON DELETE SET NULL works on `excerpt_of_meeting_id` when meeting deleted

**Task 3F.10: Voice notes repository.**

Create `aegis/db/voice_notes_repository.py` with CRUD operations per HELIOS.md §16.12. Methods: `create`, `get_by_id`, `get_by_helios_id`, `list`, `list_for_person`, `list_for_workstream`, `list_for_ask`, `list_in_range`, `update_transcript_edit`, `update_attachments`, `mark_processing_status`, `set_embedding`, `delete`.

Test-first. Write `tests/test_voice_notes_repository.py` covering each method with edge cases.

**Task 3F.11: Real `POST /api/voice-notes/preview-attachments`.**

Replace Phase 2 stub with real implementation per HELIOS.md §16.12. Calls existing `aegis.processing.resolver` on the transcript text, filters matches by confidence threshold (configurable, default 0.6), returns suggestions.

Test-first:
- Transcript "follow up with Sarah" + Sarah Lin in DB → suggested with confidence ≥ 0.6
- Transcript with no entity matches → empty suggestions
- Multiple entity types (person + workstream) all returned in matches
- Confidence threshold filters low-quality matches

**Task 3F.12: Real `POST /api/voice-notes`.**

Replace Phase 2 stub with real implementation per HELIOS.md §16.12:
1. Validate request via `VoiceNoteCreate` schema
2. Create VoiceNote row via repository with `processing_status='pending'`
3. Create VoiceNoteAttachment rows for confirmed attachments
4. If `is_excerpt=true`, look up matching meeting via time range and set `excerpt_of_meeting_id`
5. Enqueue to extraction pipeline
6. Return success with `voice_note_id` and `extraction_status='queued'`

Tests cover all paths including: excerpt matching to meeting, no matching meeting, validation errors, duplicate `helios_voice_note_id` rejection.

Also implement: `GET /api/voice-notes`, `GET /api/voice-notes/{id}`, `PATCH /api/voice-notes/{id}` (transcript edit + attachments), `DELETE /api/voice-notes/{id}`.

**Task 3F.13: Voice note extractor.**

Create `aegis/processing/voice_note_extractor.py` modeled on `meeting_extractor.py` but tuned for voice notes per HELIOS.md §16.12:

- Triage step: lightweight Haiku call deciding whether the voice note is useful for extraction
- Extract step: structured extraction via Haiku 4.5 with a prompt tuned for voice notes (speaker is always user, action items typically self-directed, less commitment-tracking)
- Resolve step: entity resolution against people/workstreams (additive to suggestions)
- Embedding generation via OpenAI text-embedding-3-small
- Workstream assignment via existing logic

Test-first with a fixture set of voice note transcripts and expected extractions:
- "Note to self, follow up with Sarah about Q2 budget" → action item for user, mentions Sarah, mentions Q2 budget
- "test test one two three" → triage filters out, no extraction
- Empty/very short transcript → handled gracefully
- Transcript with no recognizable entities → still produces action items if present

**Task 3F.14: Pipeline integration.**

Update `aegis/processing/pipeline.py` LangGraph state machine to include voice notes alongside meetings/emails/chats. Voice notes flow through:
- Triage (lightweight Haiku)
- Extraction (voice_note_extractor)
- Resolver (additional matches beyond preview)
- Embedding
- Workstream assignment
- Mark `processing_status='completed'`

The pipeline scheduler picks up voice notes with `processing_status='pending'` on its normal cadence. Voice notes are processed independently of meetings (they're not gated by transcript completion since the transcript arrives at creation time).

Tests:
- Voice note created → enqueued → processed end-to-end → extraction complete
- Failed extraction → status='failed' with reason
- Pipeline picks up multiple voice notes correctly

### Phase 3 Checkpoint

Required for advancement:

- [ ] All Track 3A-3F tasks complete; tests pass
- [ ] Coverage on transcription, diarization, merge workers ≥ 80%
- [ ] Aegis's existing test suite still passes (with the simplification updates)
- [ ] Phase 3 smoke test (§12.4) signed off, including:
  - Real meeting captured end-to-end produces speaker-labeled transcript
  - WhisperX downloaded and runs on actual hardware
  - pyannote diarization works (or is correctly skipped if disabled)
  - Aegis's meeting_detector successfully populates `meetings.transcript_text`
  - Extraction pipeline runs against the real transcript and produces sensible action items
  - Voice note synchronous transcription returns within 10s for a 30s mic recording
  - Voice note Aegis row created via `POST /api/voice-notes`, processed by extraction pipeline, `processing_status` reaches `completed`
- [ ] **Human task complete:** golden transcript for at least one fixture audio created and used in tests
- [ ] `PHASE_3_CHECKPOINT.md` summary committed

This is the most consequential phase. Take extra care with the smoke test — the entire downstream pipeline (extraction, intelligence, dashboards) depends on what flows through Helios from this phase forward.

---

## Phase 4 — Menu Bar, Onboarding, Permissions

**Goal:** Complete macOS app experience. Fresh install, onboarding flow, mic + screen recording permissions, model download, menu bar with all five states, working notifications.

**Estimated time:** 1 week of Claude Code work.

### Track 4A — Menu bar HTTP client

**Task 4A.1: Test-first — menu bar HTTP client.**

Write `helios/tests/test_menubar_client.py`:
- `get_status()` returns parsed `DaemonStatus` model
- 401 → reads token from disk, retries once
- Connection refused → raises `DaemonUnreachable`
- Timeout → raises `DaemonTimeout`
- All HTTP methods (GET status, POST capture/start, etc.)

**Task 4A.2: Implement client.**

Create `helios/src/helios/menubar/client.py`. Sync interface (rumps's runloop is sync). Uses `httpx.Client` (not async) for simplicity. Caches token, refreshes on 401.

```python
class HeliosClient:
    def __init__(self, base_url: str, token_path: Path, timeout: float = 5.0):
        ...

    def get_status(self) -> DaemonStatus: ...
    def post_capture_start(self, kind: str, duration_minutes: int | None = None) -> SessionInfo: ...
    def post_capture_stop(self) -> SessionInfo | None: ...
    # ... etc
```

### Track 4B — Menu bar app

**Task 4B.1: Implement basic rumps app.**

Create `helios/src/helios/menubar/app.py` per HELIOS.md §12. Skeleton:

```python
class HeliosApp(rumps.App):
    def __init__(self):
        super().__init__("Helios", icon=..., template=True, quit_button=None)
        self._client = HeliosClient(...)
        self._poll_timer = rumps.Timer(self._poll, 3)
        self._build_menu()
        self._poll_timer.start()
```

Menu rebuilt on every poll based on current status. Five icon states.

**Task 4B.2: State-dependent menu construction.**

Implement the dynamic menu per HELIOS.md §12.3 and §21 of the spec (the §21 `_pause_submenu` example showing dynamic Mon/Tue vs Fri/Sat/Sun labels).

For each state (`armed`, `recording`, `paused`, `error`, `not_running`), construct the appropriate menu:
- Header line(s) with state context
- Primary action button (Start, Stop, Resume, Fix Permissions, Start Daemon)
- Capture Screen submenu
- Pause Capture submenu (only when armed/recording)
- Open Dashboard
- Preferences (opens dashboard settings page)
- Quit Menu Bar
- Stop Helios Daemon (with confirmation dialog)

**Task 4B.3: Test-first — pause submenu logic.**

Write tests for `_pause_submenu`:
- Mon-Thu: only "1 hour" and "Until tomorrow morning" shown
- Fri-Sun: also includes "Until Monday morning"
- 2 AM Tuesday → "Until morning" label (today's morning hour)
- 10 AM Tuesday → "Until tomorrow morning" label (Wednesday)
- Pause hour is timezone-aware (uses local time)

**Task 4B.4: Header click-through during recording.**

When in `recording` state with a calendar-linked session, clicking the header opens the linked Aegis meeting page. When recording continuous mode, header is inert.

**Task 4B.5: Optimistic UI updates.**

When user clicks "Start Continuous Capture", icon flips to recording immediately. Background thread issues the API call. On failure, revert to previous state and show error.

**Task 4B.6: Voice note menu items.**

Update menu construction:
- In `armed` and `recording` states, add "Record Voice Note" with hotkey hint if enabled
- During an active voice note, replace with "Stop Voice Note"
- During calendar/continuous recording, label is "Record Voice Note (excerpt)" to convey the excerpt semantics

Click handlers:
- "Record Voice Note" → `POST /v1/voice-note/start` with `triggered_by="menu_bar"`
- "Stop Voice Note" → `POST /v1/voice-note/stop`, then open save window with returned transcript

Tests:
- Menu structure correct in each state
- Click handler dispatches correct API call
- Optimistic UI flips icon immediately, reverts on API failure

**Task 4B.7: Sixth icon state for voice note recording.**

Add `recording_voice_note` to icon state enum. Use the new template PNGs from Track 0G addition. Transition to this icon when voice note is active (overrides the regular `recording` icon during concurrent calendar/continuous capture).

**Task 4B.8: Voice note state polling.**

Add `/v1/voice-note/active` to the menu bar poll cycle. State includes whether a voice note is active and elapsed time. The polling result drives:
- Icon state (use `recording_voice_note` when active)
- Menu items (show "Stop Voice Note" when active)
- Floating indicator window visibility (Track 4G)

**Acceptance:** Menu bar runs alongside daemon, polls every 3s, shows correct state. Manual test: trigger continuous capture, verify icon changes; stop, verify reverts. Trigger voice note from menu, verify icon transitions to voice-note variant.

### Track 4C — Notifications

**Task 4C.1: Daemon-side notifications.**

Create `helios/src/helios/notifications/notify.py` for notifications fired from the daemon process (fallback when menu bar isn't running). Uses `UNUserNotificationCenter` directly via PyObjC.

**Task 4C.2: Menu-bar-side notifications.**

Create `helios/src/helios/menubar/notifications.py` for notifications fired from the menu bar process. Same `UNUserNotificationCenter` approach but registers notification categories for action buttons (4-hour prompt's Continue/Stop, permission revoked's Open Settings).

**Task 4C.3: Test-first — 4-hour prompt action buttons.**

This is the highest-risk notification feature. Write tests as soon as scaffolding exists, then validate manually in smoke test.

Test cases:
- Notification category "FOUR_HOUR_PROMPT" registered with Continue and Stop actions
- User clicks Continue → callback POSTs to `/v1/capture/prompt-response` with `{"continue": true}`
- User clicks Stop → callback POSTs with `{"continue": false}`
- No response within 300s → daemon-side timeout fires, session stops with reason `4hr_prompt_stop`

Note: action button callbacks can't easily be unit tested (they go through macOS notification center delegate). Test the underlying response handler (the API endpoint), then verify end-to-end manually in smoke test.

**Task 4C.4: Wire all notification triggers.**

Per HELIOS.md §14.1, hook up:
- 4-hour prompt (scheduler triggers it)
- Continuous auto-stop at 5:30 PM (scheduler)
- Manual screen capture ended (scheduler)
- Capture interrupted mid-meeting (stream manager error during calendar session)
- Permission revoked (permission checker)
- Missed meeting (scheduler when session start fails due to permissions)

**Task 4C.5: Voice note notifications.**

Implement notification triggers per HELIOS.md §14.1:
- Soft cap warning (30s before max_duration): banner with "Stop" / "Continue" actions
- Auto-stopped at cap: banner with "Open save window" action
- Save failed: banner with "Retry" action (closes the save window in a recoverable error state; tapping Retry re-opens it)
- Hotkey registration failed: banner with "Open Accessibility settings" action

Categories registered in menu bar app's notification setup at startup. Each action button POSTs to the appropriate endpoint or invokes the local callback (e.g., re-opening the save window).

**Acceptance:** All triggers fire correctly. Manual: temporarily set 4-hour prompt to 1 minute (`continuous_prompt_hours = 1/60`), start continuous capture, verify prompt arrives with action buttons, click each, verify correct behavior. Set voice note `max_duration_seconds = 30` temporarily, record voice note, let it run to cap warning, verify notification.

### Track 4D — Onboarding window

**Task 4D.1: PyObjC window scaffold.**

Create `helios/src/helios/menubar/onboarding.py`. NSWindow-based for visual quality; rumps's built-in window is too ugly per HELIOS.md §15.

```python
class OnboardingWindow:
    def __init__(self, on_complete: Callable[[], None]):
        self._window = self._build_window()
        self._step_views = self._build_step_views()
        self._current_step = 0
        self._state = self._load_state()
```

Step views as separate NSView instances; navigate by hiding/showing views in the same window.

**Task 4D.2: Welcome step.**

Brief intro text, "Continue" button. Click → advance.

**Task 4D.3: Microphone permission step.**

Status indicator: ✓ granted / ✗ not granted / ⚠ unknown. "Grant Access" button triggers `AVCaptureDevice.requestAccessForMediaType_`. After grant, polls status until granted or user clicks "Open System Settings" deep-link.

Deep-link URL: `x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone`

**Task 4D.4: Screen recording permission step.**

Similar pattern. "Grant Access" button calls `SCShareableContent.current` which triggers the macOS prompt. Status check via `CGPreflightScreenCaptureAccess`.

Deep-link: `x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture`

After grant, detect "needs restart" state and show conditional "Restart Helios" step.

**Task 4D.5: Restart step (conditional).**

Only appears if screen recording was just granted. Single button "Restart Now" → `os.execv(sys.executable, [sys.executable, "-m", "helios"])`.

State persisted before restart so onboarding resumes at the next step on relaunch.

**Task 4D.6: Model download step.**

Subprocess `helios.scripts.download_whisper`. Parse JSON progress lines, update progress bar. ~1.5 GB download, takes 2-10 minutes depending on network. Verify model loads with synthetic audio after download.

Block advancement until download completes successfully. "Cancel setup" closes onboarding (user can resume on next launch).

**Task 4D.7: Login items step.**

Instructional text: "Add Helios to Login Items so it starts automatically." Button "Open System Settings" deep-links to:
`x-apple.systempreferences:com.apple.LoginItems-Settings.extension`

User adds manually. Click "I added it" to mark complete.

**Task 4D.8: Complete step.**

"Helios is ready." Brief note about optional speaker identification setup in dashboard. Buttons: "Open Helios Dashboard" (opens browser to `http://localhost:8000/helios`), "Done" (closes window, starts the menu bar polling).

**Task 4D.9: State persistence.**

Persist progress to `~/.aegis/capture/onboarding_state.json` per HELIOS.md §15.4. On launch, if state shows incomplete, jump to first incomplete step.

**Acceptance:** Manual test on a fresh user account or freshly-reset macOS:
- Launch Helios.app → onboarding window appears
- Walk through each step
- Permissions granted correctly
- Model downloads (use cached model if you've already downloaded; otherwise verify the real flow)
- Login items step opens correct settings pane
- Complete → menu bar starts polling, icon shows correct state

### Track 4E — Daemon lifecycle from menu bar

**Task 4E.1: LaunchAgent commands.**

In `helios/src/helios/menubar/app.py`, implement helpers for:
- `launchctl load ~/Library/LaunchAgents/com.aegis.helios.plist`
- `launchctl unload ~/Library/LaunchAgents/com.aegis.helios.plist`
- `launchctl kickstart` (for restart)

Used by "Start Capture Daemon", "Stop Helios Daemon", "Restart Daemon" menu actions.

**Task 4E.2: "Stop Helios Daemon" with confirmation.**

When clicked, show NSAlert with:
- Title: "Stop Helios Daemon?"
- Body: "This will stop the capture daemon and disable automatic meeting recording. The daemon won't restart until you start it again from this menu."
- Buttons: Cancel, Stop Daemon

On confirm: POST to `/v1/capture/stop` (in case of active session), then `launchctl unload`.

**Task 4E.3: "Start Capture Daemon" from not_running state.**

When daemon isn't responding, menu shows "Start Capture Daemon" as primary action. Click → `launchctl load`, wait up to 10s for daemon to respond on `/v1/health`, refresh UI.

**Acceptance:** Manual: stop daemon via menu, verify icon changes to `not_running`, click Start, verify daemon starts and icon updates.

### Track 4F — Voice note hotkey listener

**Task 4F.1: Test-first — hotkey detection.**

Tests with mocked Carbon `RegisterEventHotKey`:
- Hotkey registered when permission granted and config enabled
- Registration silently fails (logs warning, returns false) when permission denied
- Toggle behavior: first press starts, second press stops
- Hotkey can be changed in config; old combo unregistered, new registered
- Registration is re-attempted every 5 minutes if previously failed
- Hotkey reading current state checks `/v1/voice-note/active` before deciding start vs stop

**Task 4F.2: Implement hotkey listener.**

Create `helios/src/helios/menubar/hotkey.py` per HELIOS.md §12.9:

```python
class VoiceNoteHotkey:
    def __init__(self, combo: str, on_trigger: Callable, clock: Clock):
        self._combo = combo
        self._on_trigger = on_trigger
        self._clock = clock
        self._registered = False

    async def start(self):
        if not self._is_accessibility_granted():
            log.warn("hotkey_accessibility_missing")
            asyncio.create_task(self._retry_loop())
            return
        await self._register()

    def _is_accessibility_granted(self) -> bool:
        return AXIsProcessTrusted()

    async def _register(self):
        # Use Carbon RegisterEventHotKey via PyObjC
        ...

    async def _retry_loop(self):
        while not self._registered:
            await self._clock.sleep(300)  # 5 minutes
            if self._is_accessibility_granted():
                await self._register()
```

Use `pyobjc-framework-Carbon` for `RegisterEventHotKey`. Add `pyobjc-framework-Carbon` to Helios's pyproject dependencies.

**Task 4F.3: Wire to menu bar app.**

In menu bar app startup:
1. If `voice_note.hotkey_enabled` is true, create and start hotkey listener
2. On trigger callback: check current voice note state via `/v1/voice-note/active`; if active, send stop; if not, send start
3. Update menu bar UI optimistically
4. On stop response, open save window (Track 4H)

**Acceptance:** Manual test — enable hotkey in dashboard settings, grant Accessibility, press ⌥⌘V to start, indicator appears, press again to stop, save window opens.

### Track 4G — Floating recording indicator

**Task 4G.1: Implement floating indicator.**

Create `helios/src/helios/menubar/voice_note_indicator.py` per HELIOS.md §12.10. PyObjC NSWindow at status-window level, borderless, always-on-top.

Contents:
- Pulsing red dot (animation via NSAnimation or simple timer-based redraw)
- Elapsed time MM:SS
- Audio level bar (read mic samples from daemon's `/v1/voice-note/active` endpoint, which exposes a current-RMS field)
- Stop button (NSButton)

Polls `/v1/voice-note/active` every 250ms for elapsed time and `approaching_cap` state. Color shifts amber when `approaching_cap=true`.

Position: configurable via `voice_note.indicator.floating_pill_position`. Default top-right of primary display. User-draggable; final position persisted to `voice_note.indicator.last_position` in config.

Tests:
- Window appears on voice note start
- Window position respects config
- Dragging updates persisted position
- Window dismisses on voice note stop or cancel
- Color transitions correctly when approaching_cap=true

**Task 4G.2: Show/hide lifecycle.**

The indicator window is created when:
- Hotkey triggers a start AND start succeeds
- Menu bar item triggers a start AND start succeeds
- Dashboard triggers a start AND menu bar app is running (menu bar polls and notices voice_note active)

The indicator is dismissed when:
- Voice note stops successfully (transitions to save window)
- Voice note is cancelled
- Daemon becomes unreachable (graceful degradation — indicator just disappears)

**Acceptance:** Manual test — voice note from any trigger surface displays the indicator at the correct position, indicator updates elapsed time, dismisses correctly on stop.

### Track 4H — Floating save window

**Task 4H.1: Implement save window.**

Create `helios/src/helios/menubar/voice_note_save_window.py` per HELIOS.md §12.11. PyObjC NSWindow, focused, ~480×360, positioned near the indicator's last position.

Layout per spec:
- Header with duration and close button
- Read-only transcript display (NSTextView, scrollable)
- Suggested attachments list (NSStackView with checkboxes)
- "Add attachment" button (opens dropdown to manually pick person/workstream/ask)
- Discard / Save buttons; Save shows countdown when auto-save is active

Workflow:
1. Window opens with transcript displayed and "Loading suggestions..." in attachments area
2. Async call to Aegis `/api/voice-notes/preview-attachments` with the transcript
3. Suggestions populate; user can check/uncheck or manually add
4. Auto-save countdown starts at `auto_save_timeout_seconds` (10 default)
5. Any user interaction (checkbox click, "Add attachment") cancels countdown
6. On Save: assemble VoiceNoteCreate payload, POST to Aegis `/api/voice-notes`
7. On Discard: POST to Helios `/v1/voice-note/cancel` if not already saved; close window
8. On window close (× or click-away): equivalent to Save

Tests:
- Suggested attachments load and display
- Checkbox interactions cancel countdown
- Save sends correct payload
- Discard sends cancel request
- Save failure shows error and Retry button
- Window dismissal triggers auto-save
- Click-away from window also auto-saves

**Task 4H.2: Manual attachment picker.**

The "Add attachment" UI is a small dropdown/search field that queries Aegis for matching people, workstreams, asks via `GET /api/search?q=...&types=person,workstream,ask`. Reuse existing search patterns in Aegis if possible; otherwise simple list.

If Aegis doesn't have a matching search endpoint, add one as part of this task (small endpoint, ~30 minutes).

**Acceptance:** Manual test — record voice note, save window opens with transcript and suggestions, manually add an unattached entity via picker, save, verify Aegis row has all attachments.

### Phase 4 Checkpoint

Required for advancement:

- [ ] All Track 4A-4H tasks complete; tests pass
- [ ] Phase 4 smoke test (§12.5) signed off, including:
  - Fresh install + complete onboarding works end-to-end
  - All six menu bar states reachable and correctly displayed (now includes recording_voice_note)
  - 4-hour prompt notification works with action buttons (this is the highest-risk feature; validate carefully)
  - Permission revocation detected → icon flips → notification fires
  - Re-grant + retry restores capture
  - Quit Menu Bar leaves daemon running
  - Stop Helios Daemon → confirmation → daemon stops
  - Voice note from menu bar: click "Record Voice Note", indicator appears, click stop, save window opens
  - Voice note from hotkey (after enabling in settings + granting Accessibility): ⌥⌘V starts, ⌥⌘V again stops
  - Voice note during a meeting: while a calendar capture is active, trigger voice note, verify it's marked as excerpt, voice note transcript correctly extracted from the time range, meeting transcript still includes the time range
  - Voice note duration cap: with `max_duration_seconds=30` set temporarily, trigger voice note, let it run to 0:00 remaining, verify cap warning notification fires at 30s warning, force_stop fires at cap
  - Voice note save window auto-save: record a quick note, do nothing for 10s, verify auto-save fires and creates Aegis row
  - Voice note save window with manual attachment: add a person manually via picker, save, verify Aegis row has the attachment
- [ ] `PHASE_4_CHECKPOINT.md` summary committed

---

## Phase 5 — Screen OCR

**Goal:** Screen OCR with frontmost-app gating, manual override, and retention cleanup.

**Estimated time:** 5 days of Claude Code work.

### Track 5A — OCR worker

**Task 5A.1: Test-first — gating logic.**

Write `helios/tests/test_ocr_gating.py`:
- Frontmost = Teams (allowlisted) → should_capture=true
- Frontmost = Slack (not allowlisted) → should_capture=false
- Override active until ts → should_capture=true regardless of frontmost
- Override expired → reverts to frontmost check

**Task 5A.2: Test-first — frame processing.**

Write `helios/tests/test_ocr_worker.py` with mocked Vision OCR:
- Duplicate frame (same pHash) skipped
- Frame within Hamming distance 4 of recent frame skipped
- Sliding window of 10 frames for dedup
- Text < 20 chars → frame skipped
- Avg confidence < 0.7 → thumbnail saved
- Avg confidence ≥ 0.7 → no thumbnail
- Per-observation confidence < 0.5 → text excluded

**Task 5A.3: Implement OCR worker.**

Create `helios/src/helios/workers/ocr.py` matching HELIOS.md §10.

Two coroutines: `_gating_loop` (1s interval, decides should_capture) and `_frame_loop` (consumes from video queue, processes frames).

Vision OCR via PyObjC. JPEG decode for pHash uses PIL (fast on Apple Silicon).

**Task 5A.4: Display detection.**

Implement `_get_display_for_frontmost()`:
1. Get frontmost window via `CGWindowListCopyWindowInfo`
2. Get window bounds
3. Iterate displays via `CGDisplayBounds`, find which contains the window center
4. Return the display ID

Send `SET_DISPLAY <id>` to Swift helper.

**Task 5A.5: Wire video queue.**

Stream manager already has video queue (Phase 1, Task 1B.5). Wire OCR worker as the consumer.

**Acceptance:** All OCR tests pass. Real OCR on a fixture screenshot returns expected text.

### Track 5B — Manual screen capture override

**Task 5B.1: Test-first — manual override flow.**

Write tests:
- `POST /v1/capture/enable-screen-override` with active session → updates `screen_capture_override_until`
- Same call without active session → starts new `manual_screen` session, sets override
- After override expires, gating reverts; if session was started by override, it ends too
- Hard 5:30 PM stop applies if override extends past it

**Task 5B.2: Implement endpoint and orchestrator hooks.**

Update `helios/src/helios/api/routes/capture.py` for the endpoint.

In capture orchestrator:

```python
async def enable_screen_override(self, duration_minutes: int) -> int:
    until_ts = self._clock.time() + duration_minutes * 60
    active = await queries.get_active_session(self._db.writer)
    if active:
        await queries.update_session_override(self._db.writer, active.id, until_ts)
        return active.id
    else:
        session_id = await self.start_session(kind="manual_screen", screen_override_until=until_ts)
        # Schedule auto-stop
        delay = duration_minutes * 60
        asyncio.get_event_loop().call_later(
            delay, lambda: asyncio.create_task(
                self.stop_session(session_id, reason="user_stop")
            ),
        )
        return session_id
```

**Task 5B.3: Menu bar wiring.**

Update menu bar's "Capture Screen" submenu (Phase 4 already added the menu structure; now wire the click handlers to actually call the API).

Submenu has 15/30/60/90 min options. Click → `POST /v1/capture/enable-screen-override`. While active, menu label changes to "● Screen capture: 23 min left".

**Acceptance:** Manual: click Capture Screen → 30 min, verify session starts/extends, OCR captures frames regardless of frontmost app for 30 min.

### Track 5C — OCR API endpoint

**Task 5C.1: Test-first — `/v1/ocr` endpoint.**

Write tests:
- Returns frames in time window
- `min_confidence` filter works
- `app_bundle` filter works
- Thumbnail URLs return correct path
- Empty result for empty range

**Task 5C.2: Implement endpoint.**

Update `helios/src/helios/api/routes/ocr.py` (replace placeholder from Phase 2). Implementation per HELIOS.md §10.8.

Add `GET /v1/ocr/thumbnail/{frame_id}` to serve JPEG bytes from disk.

### Track 5D — Retention and cleanup

**Task 5D.1: Test-first — cleanup worker.**

Write `helios/tests/test_cleanup_worker.py`:
- WAVs older than retention window → soft-deleted to trash
- Trash files older than `trash_hold_hours` → permanently deleted
- Untranscribed chunk's WAV NOT deleted
- Diarization-pending session's WAVs NOT deleted
- Per-meeting deletion removes WAVs, transcripts, OCR, thumbnails for that session

**Task 5D.2: Implement cleanup.**

Create `helios/src/helios/workers/cleanup.py` matching HELIOS.md §17.

Schedule: nightly at 3 AM local + startup catch-up. Use APScheduler-like pattern via clock.

**Task 5D.3: Per-meeting deletion endpoint.**

Implement `DELETE /v1/sessions/{id}` per HELIOS.md §7.3. Soft-delete approach: move WAVs to trash, mark session row with `deleted_at`, but keep transcripts in DB (filter via `deleted_at IS NULL` in queries).

Actually, on reflection: deleting a session also deletes its transcripts (cascade via foreign key), since the user explicitly asked. Soft-delete is for the WAVs. The DB rows are hard-deleted.

This is a behavior call worth making explicit: when a user clicks "Delete this session" in the dashboard, transcripts are gone immediately. Disk files have a 24-hour grace period in trash.

**Task 5D.4: Disk space monitoring.**

Cleanup worker also checks free disk space; if below `disk_space_warning_gb`, fires a notification and logs warning. Surfaces in dashboard.

### Phase 5 Checkpoint

Required for advancement:

- [ ] All Track 5A-5D tasks complete; tests pass
- [ ] Phase 5 smoke test (§12.6) signed off, including:
  - OCR fires only when Teams/Zoom is frontmost during a real meeting capture
  - Manual override captures regardless of frontmost app
  - pHash dedup correctly suppresses near-duplicate frames
  - Low-confidence frame produces thumbnail
  - Cleanup runs successfully (test by setting retention to 0 days temporarily, verify safety rules apply)
  - Per-meeting deletion removes everything
- [ ] `PHASE_5_CHECKPOINT.md` summary committed

---

## Phase 6 — Helios Dashboard

**Goal:** Six-page dashboard under Aegis at `/helios`. Composes Helios API data with Aegis DB data. Includes settings UI and the guided HF wizard for speaker identification.

**Estimated time:** 1 week of Claude Code work.

### Track 6A — Aegis additions

**Task 6A.1: Add `helios_exclude` column.**

Generate Alembic migration for the new column on `meetings` table. Default false. Update Aegis's `Meeting` model.

**Task 6A.2: Update `/api/meetings/upcoming`.**

The endpoint from Phase 2 should already check `helios_exclude` OR `is_excluded`. Verify and add a test for the case where a meeting has `helios_exclude=true` but `is_excluded=false`.

### Track 6B — Helios dashboard routes

**Task 6B.1: Test-first — overview route.**

Write tests in Aegis's test suite:
- `GET /helios` returns 200 HTML
- Status pill reflects daemon state
- Today's sessions enriched with meeting titles from Aegis DB
- Excluded meetings show with their reason
- Aegis unreachable for Helios → graceful degraded UI

**Task 6B.2: Implement dashboard routes.**

Create `aegis/web/routes/helios.py` per HELIOS.md §13.2 with all six pages plus the action endpoints (POST handlers for diagnostics actions, settings updates, etc.).

Each handler:
1. Calls Helios via injected `HeliosClient`
2. Queries Aegis DB for enrichment
3. Composes template context
4. Returns Jinja2 response

For the speaker identification wizard, implement the 5-step state machine per the spec (Q10d revised). Step state stored in `~/.aegis/capture.toml` under `[diarization.setup_progress]`.

**Voice notes are NOT added under `/helios/*`.** Per HELIOS.md §13.4 and §16.12, voice note pages live in Aegis at `/voice-notes/*` (Track 6E). The Helios dashboard's overview page just adds a "Today's voice notes" section that link-throughs to `/voice-notes/{id}`. This avoids template duplication and surfaces voice notes alongside other Aegis entities.

**Task 6B.3: Templates.**

Create all templates per HELIOS.md §13.3:
- `aegis/web/templates/helios/base.html` — extends Aegis's existing base layout
- One template per page
- Partials for status pill, session row, transcript segment, coverage bar, wizard step

Templates follow Aegis's existing Jinja2 + HTMX + Tailwind conventions. HTMX `hx-trigger="every 5s"` on the overview status pill for live updates.

**Task 6B.4: Speaker name resolution.**

Implement the heuristic per HELIOS.md §16.4 in `aegis/clients/helios.py`. When session has linked meeting and N system-channel speakers match N-1 attendees, map in order of first appearance.

Display in session detail page: speaker name with small indicator (e.g., italic) if inferred, raw `SPEAKER_00` if unresolvable.

**Acceptance:** All six pages render with real data. Manual: open dashboard, verify each page loads and displays meaningful information. HTMX live updates work.

### Track 6C — Settings UI

**Task 6C.1: Settings form.**

Implement settings page per HELIOS.md §13.7. Each TOML section becomes a form section. Fields are typed (number inputs, dropdowns, checkboxes, text inputs).

Each field indicator: "Applies immediately" vs "Requires restart" based on the hot-reload classification (HELIOS.md §5.4).

Settings page sections include the **Voice notes** section per HELIOS.md §13.7:
- Enable toggle
- Max duration (number input, 10-1800s range)
- Auto-save delay (number input, 2-60s range)
- Default save action (radio: save_with_suggestions / save_unattached / discard)
- Hotkey enable + combo configuration
- Indicator preferences (position, show elapsed time, show audio level)

The hotkey enable toggle includes the Accessibility permission flow:
- When toggled on, check `AXIsProcessTrusted` via async call (UI shows checking state)
- If not granted, show System Settings deep-link with "Click here when granted" prompt
- After granted, send config update to enable hotkey
- Daemon hot-reload picks up the change; menu bar registers the hotkey on next poll

If user tries to enable hotkey without Accessibility, the toggle stays in a "pending" state until permission is granted, then auto-enables.

**Task 6C.2: Settings POST handler.**

`POST /helios/settings` writes the TOML file via the Helios API (or directly, since dashboard runs in Aegis which has token access). On save:
1. Validate against Pydantic model (Helios's config validator)
2. Write TOML
3. Helios's hot-reload watcher picks up changes automatically
4. For restart-required changes, show "Restart Daemon" prompt

**Task 6C.3: Speaker identification wizard.**

Per HELIOS.md §15.5 / Q10d revised. 5-step wizard with HTMX state transitions:
- Step 1: HF account creation link
- Step 2: Token generation instructions
- Step 3: License acceptance for 3 models (checkboxes user ticks after visiting)
- Step 4: Token paste + validation via `huggingface_hub.whoami`
- Step 5: Model download

Steps collapse with ✓ when complete. Resumable via `setup_progress` state. Each "Continue" button is an `hx-post` to a step-specific endpoint that updates progress and returns the next step's HTML.

Token validation endpoint: `POST /helios/settings/diarization/validate-token` returns success/error inline.

Model download endpoint: `POST /helios/settings/diarization/download-models` triggers download via Helios API (Helios runs the actual `huggingface_hub.snapshot_download`), polls status, returns result.

### Track 6D — Diagnostic actions

**Task 6D.1: Implement dashboard diagnostic actions.**

Per HELIOS.md §13.6:
- "Restart daemon" → `POST /v1/diagnostics/restart`
- "Flush queues" → `POST /v1/diagnostics/flush-queues`
- "Trigger test capture" → `POST /v1/diagnostics/test-capture`, poll for results
- "Reload component" → `POST /v1/diagnostics/reload-component` with component name
- "Copy diagnostics" → fetches `/v1/diagnostics`, formats text block, returns as plaintext for clipboard
- "Download diagnostic bundle" → triggers bundle creation in Helios, returns download URL

**Task 6D.2: Implement test capture self-test.**

In Helios: `POST /v1/diagnostics/test-capture` runs:
1. Start session with `kind=self_test`
2. Capture 60 seconds (mic + system)
3. Verify chunks created, audio non-silent
4. Run transcription on the chunks
5. Verify segments produced
6. Run diarization (if enabled)
7. Delete the session and all artifacts
8. Return structured result with each step's pass/fail

Dashboard polls for completion and displays results.

**Task 6D.3: Diagnostic bundle.**

`POST /v1/diagnostics/bundle` creates a tar.gz at a temp path:
- diagnostics.txt (the Copy Diagnostics output)
- logs/helios.log.gz (last 24h)
- events.json (last 100 daemon_events)
- config.toml.redacted (token redacted)
- system.txt (sw_vers, hardware model, audio devices, displays)

Cleanup task deletes bundles older than 1 hour. Returns path; dashboard serves as download.

**Acceptance:** All diagnostic actions work from dashboard. Test capture self-test produces clear pass/fail per step.

### Track 6E — Calendar page and Aegis voice notes UI

**Task 6E.1: Implement calendar page.**

`GET /helios/calendar`:
1. Query Aegis DB for upcoming meetings (next 7 days)
2. For each, determine capture status: scheduled / excluded / overridden
3. Render with per-meeting toggle (excluded → override capture; not excluded → toggle off)
4. POST handler updates `meetings.helios_exclude` in Aegis

**Task 6E.2: Per-meeting override.**

When user clicks "Override exclusion" on an excluded meeting, set `helios_exclude = false` regardless of keyword match. (Note: this is the inverse — `helios_exclude` overrides toward exclusion. The semantics need clarification.)

**Decision needed at implementation time:** How do per-meeting overrides interact with keyword exclusions?

- **Option A:** `helios_exclude` is an explicit override that supersedes keyword match. If true, exclude regardless. If false, fall back to keyword check.
- **Option B:** Two separate fields — `helios_exclude_explicit` (user override toward exclusion) and `helios_include_explicit` (user override toward inclusion).
- **Option C:** `helios_exclude` is tri-state (null = use keywords, true = exclude, false = include).

**Recommendation:** Option C, tri-state nullable column. Most flexible, simplest semantics. Update Phase 2 task 2A.1 retroactively if needed (but the column was added as a boolean by Phase 6A.1; if changing now, do so before this phase ships).

Note: this is exactly the kind of "decide and document" case where Claude Code should pick the answer (recommend Option C), document in §13 Running Decisions Log, and proceed.

**Task 6E.3: Voice notes list page in Aegis.**

Create `aegis/web/routes/voice_notes.py` with:
- `GET /voice-notes` (list page) — reverse-chronological list, filterable by date range, attachment, source
- `GET /voice-notes/{id}` (detail page) — full transcript editing, attachments editing, audio player, action buttons
- `PATCH /voice-notes/{id}/transcript` — update edited transcript (sets `transcript_text_edited`, preserves original)
- `PATCH /voice-notes/{id}/attachments` — update attachments
- `POST /voice-notes/{id}/re-extract` — manually trigger re-extraction (e.g., after transcript edit)
- `DELETE /voice-notes/{id}`

Add "Voice Notes" as a top-level nav item in Aegis's main navigation (alongside Meetings, People, Workstreams).

**Task 6E.4: Voice note row partial.**

Create reusable `aegis/web/templates/voice_notes/_partials/voice_note_row.html` and `_partials/voice_note_card.html`. Used in:
- Voice notes list page
- Person profile pages (under "Voice notes" section)
- Workstream profile pages
- Ask detail pages
- Helios dashboard overview ("Today's voice notes")
- Aegis main daily timeline

Templates follow Aegis's existing Jinja2 + HTMX + Tailwind conventions.

**Task 6E.5: Person/workstream profile additions.**

Modify existing person, workstream, and ask profile templates to add a "Voice notes" section showing attached voice notes. Small templated additions; use the partial from 6E.4.

Tests verify:
- Voice notes attached to a person appear on their profile
- Same for workstreams and asks
- Empty state displays gracefully when no voice notes attached

**Task 6E.6: Daily timeline integration.**

Modify Aegis's main dashboard "today" view (`aegis/web/templates/dashboard/today.html` or equivalent) to mix voice notes into the chronological timeline alongside meetings.

Voice notes appear between meetings at their `started_at` timestamp. Use the row partial from 6E.4.

**Task 6E.7: Briefings integration.**

Update `aegis/intelligence/briefings.py` to include voice notes in morning, Friday, and Monday briefings. Voice notes from the relevant time range count as user-generated context similar to meetings the user actively participated in.

Briefing prompt templates need updates to accept voice notes as input. Modify `aegis/intelligence/prompts/` (or wherever briefing templates live) to include a voice notes section in the input context.

Tests verify briefings include voice note content when relevant voice notes exist in the time range.

**Task 6E.8: RAG search integration.**

Update `aegis/chat/rag.py` to include voice notes:
- Add `voice_notes` to the searchable corpus
- Embed-based retrieval against `voice_notes.embedding`
- Filter by attachments when search context includes a person or workstream
- Surface voice note results in chat answers alongside meeting and email results

Tests verify:
- Voice notes appear in search results for matching queries
- Filter by person works (only voice notes attached to that person)
- Voice note context surfaces in chat answers

**Task 6E.9: Voice note inline editing with re-extraction.**

In the voice note detail page (`/voice-notes/{id}`), the transcript is editable inline.

Saving the edit:
1. Updates `transcript_text_edited` (preserves original `transcript_text`)
2. Triggers `POST /voice-notes/{id}/re-extract`
3. Re-extraction uses the edited text
4. Old extraction artifacts (action items, mentions) are replaced by new ones
5. New embedding generated from edited text

UI shows a "Re-extracting..." indicator while re-extraction runs (typically 5-15 seconds).

Tests verify:
- Inline edit triggers re-extraction
- Old artifacts replaced
- New embedding generated and persisted
- Failed re-extraction surfaces error to UI

### Phase 6 Checkpoint

Required for advancement:

- [ ] All Track 6A-6E tasks complete; tests pass
- [ ] Phase 6 smoke test (§12.7) signed off, including:
  - All 6 dashboard pages render with real data
  - Overview HTMX updates happen smoothly
  - "Today's voice notes" section on overview page shows recent voice notes
  - Session detail page shows speaker names mapped from attendees
  - Speaker identification wizard works end-to-end (with a real HF account)
  - Settings page edits write TOML and hot-reload applies
  - Voice notes settings section editable; hotkey enable triggers Accessibility flow correctly
  - Test capture self-test completes
  - Diagnostic bundle downloads with correct redaction
  - Aegis `/voice-notes` page lists all voice notes
  - Click into a voice note → detail page renders with transcript, attachments, audio player
  - Edit transcript inline → re-extraction runs, action items update
  - Add attachment → reflected in person profile under "Voice notes"
  - Voice note appears in tomorrow's morning briefing if relevant
  - Search for a person → voice notes mentioning them appear in results
- [ ] `PHASE_6_CHECKPOINT.md` summary committed

---

## Phase 7 — Hardening

**Goal:** Final integration, real-world stress testing, polish.

**Estimated time:** 3-5 days of Claude Code work plus ~half a day of human smoke testing.

### Track 7A — End-to-end stress

**Task 7A.1: Long-duration capture test.**

Manual smoke: run an 8-hour continuous capture session during a normal workday.

Verify:
- No memory leaks (check daemon RSS at start vs. end, should grow by less than 100 MB)
- No DB bloat (check size growth, projected to be <1 GB/day)
- No crashes
- All transcripts produced
- Disk usage as expected

**Task 7A.2: Calendar-day stress.**

Manual: a normal day's worth of calendar meetings (5-10 events). At end of day:
- Every meeting that should have been captured was
- Every transcript was produced
- Aegis's extraction pipeline ran on each
- Action items, decisions populated correctly
- No gaps in coverage except for known causes (laptop closed, etc.)

**Task 7A.3: Crash recovery scenarios.**

Each tested manually:
- `kill -9` daemon during active capture → LaunchAgent restarts, session marked `crash_recovery`, partial chunks present
- `kill -9` daemon during transcription queue work → on restart, pending chunks resume
- `kill -9` daemon during diarization → on restart, pending diarizations resume
- Force-quit menu bar during recording → daemon keeps running, capture continues
- Force-restart Mac during recording → daemon resumes via LaunchAgent on login

**Task 7A.4: Permission revocation during active capture.**

- Calendar session in progress → revoke mic in System Settings → notification fires, session marked with `unavailable` chunks, ends with reason `permission_revoked`
- Re-grant mic → next scheduled session works normally

**Task 7A.5: Sleep/wake during capture.**

- Active session → close laptop → reopen 5 minutes later → session has `unavailable` range for the gap, capture resumes
- Verify the gap is correctly represented in the transcript and dashboard coverage display

**Task 7A.6: Voice note stress test.**

During the 8-hour stress test, record at least 10 voice notes throughout the day with various:
- Triggers (menu bar, hotkey, dashboard)
- Durations (5s to 5min)
- Concurrent contexts (during meetings as excerpts, between meetings as standalone, during continuous capture)
- Attachments (with auto-suggestions, manually added, unattached)

Verify:
- All voice notes saved to Aegis with `processing_status='completed'`
- All extractions completed successfully
- All attachments correctly stored
- No memory growth attributable to voice note features
- Hotkey remains functional throughout the day
- Floating indicator and save window render correctly each time

### Track 7B — Polish

**Task 7B.1: Error message review.**

Review every error message in the codebase. Each should be:
- Specific (not "An error occurred")
- Actionable when possible (suggests what user can do)
- Free of jargon and stack-trace exposure to users

**Task 7B.2: Onboarding copy review.**

Re-read every string shown in the onboarding flow. Adjust for clarity, brevity, helpfulness.

**Task 7B.3: Real menu bar icons (deferred).**

If real icons are ready (separate design task), replace placeholders. Otherwise, leave placeholders and capture a follow-up TODO.

**Task 7B.4: Documentation.**

Write a brief `docs/helios_user_guide.md` for the human user (not Claude Code) covering:
- How to install
- How to use the menu bar
- How to use the dashboard
- Common troubleshooting
- How to enable speaker identification

**Task 7B.5: Gitignored fixture cleanup.**

Verify `.gitignore` excludes all the right things (audio fixtures, build artifacts, etc.) and that `helios/tests/fixtures/audio/README.md` correctly documents the fetch process.

### Phase 7 Checkpoint — Final

Required for v1 release:

- [ ] All Track 7A-7B tasks complete
- [ ] All previous phase checkpoints still pass (re-run quick smoke tests)
- [ ] No known crash scenarios
- [ ] No critical TODOs in the codebase
- [ ] Documentation present
- [ ] Final smoke test (§12.8) signed off
- [ ] Tag release: `git tag helios-v0.1.0`
- [ ] `PHASE_7_CHECKPOINT.md` summary committed

Helios is ready for daily use.

---

## §12 — Smoke Test Procedures

These are the manual verification procedures the human runs before approving each phase checkpoint. Each procedure is a checklist; all items must pass for sign-off.

Smoke tests use a harness script per phase that automates the scripted parts and prints a checklist of manual verification items.

### §12.1 — Phase 0 Smoke Test

**Estimated time:** 5 minutes.

Goal: verify the scaffolding is correct, build process works, daemon serves health endpoint.

```bash
# Run from repo root
bash scripts/smoke_phase_0.sh
```

The harness script:
1. Confirms `helios/pyproject.toml` exists and `uv sync` succeeds
2. Confirms `helios/bin/ScreenCaptureHelper` exists and `--version` works
3. Runs `bash scripts/build_helios.sh` and confirms `helios/dist/Helios.app` exists
4. Runs `bash scripts/install_helios.sh`
5. Waits 5 seconds, then `curl http://127.0.0.1:3031/v1/health`
6. Confirms response includes `"status": "ok"` and a non-zero `uptime_seconds`
7. Confirms `~/.aegis/capture.toml` exists with correct permissions
8. Confirms `~/.aegis/capture/logs/helios.log` exists and has at least one entry

Manual checklist (after harness):
- [ ] Menu bar shows the placeholder icon (any icon visible counts)
- [ ] Clicking icon shows a menu (contents may be minimal at this phase)
- [ ] No error dialogs appeared during installation

If the harness fails or any manual item fails: report findings, fix, re-run from clean state.

### §12.2 — Phase 1 Smoke Test

**Estimated time:** 10 minutes.

Goal: verify real audio capture works on actual hardware with the Swift helper.

```bash
bash scripts/smoke_phase_1.sh
```

The harness:
1. Confirms daemon is running (Phase 0 smoke prerequisite)
2. Confirms permissions are granted (mic and screen recording — if not, prompts the human to grant before proceeding)
3. Sends `POST /v1/capture/start` with `kind=continuous`
4. Records for 60 seconds (prompts the human to speak and play audio during this time)
5. Sends `POST /v1/capture/stop`
6. Queries `/v1/sessions/{id}` to confirm session was recorded
7. Inspects `~/.aegis/capture/<date>/{mic,system}/` for WAV files
8. Inspects SQLite for `audio_chunks` rows

Manual checklist:
- [ ] You spoke audibly during the 60 seconds
- [ ] You played audible audio (e.g. YouTube video, music) during the 60 seconds
- [ ] At least 2 mic WAV files exist (60s = 2 chunks at 30s each)
- [ ] At least 2 system WAV files exist
- [ ] Open one mic WAV in QuickTime — your speech is recognizable
- [ ] Open one system WAV in QuickTime — system audio is recognizable
- [ ] SQLite shows correct rows (script prints them)
- [ ] No `unavailable` rows appeared
- [ ] Battery didn't drain noticeably during the 60s

Edge case to test once during this phase:
- [ ] Close laptop lid for 30s mid-capture, reopen → resumed capture has an `unavailable` range marked `system_sleep`

### §12.3 — Phase 2 Smoke Test

**Estimated time:** 15 minutes.

Goal: verify scheduler-driven capture from real calendar events.

Prerequisite: at least one calendar event in your real calendar starting 3-5 minutes from when you start the test.

```bash
bash scripts/smoke_phase_2.sh
```

The harness:
1. Confirms Aegis is running and `GET /api/meetings/upcoming` returns the test event
2. Confirms Helios scheduler picked it up (queries `/v1/status`)
3. Polls until 60 seconds before the event, confirms session start
4. Polls until 60 seconds after the event ends, confirms session end with reason `scheduled`
5. Confirms session linked to the calendar event via junction table

Manual checklist:
- [ ] Test calendar event existed and was visible to Aegis
- [ ] Session started 60 seconds before scheduled start
- [ ] Session ended ~5 minutes after scheduled end (post-end buffer)
- [ ] Session row has `kind=calendar` and the correct `calendar_event_id` linked
- [ ] Adjacent meeting test: create two meetings 4 minutes apart on calendar. Verify one continuous session covers both.
- [ ] Pause-until test: `POST /v1/capture/pause-until` with timestamp 5 min in future. Schedule a meeting 2 min in the future. Verify it does NOT capture.
- [ ] Hard stop test: temporarily set `continuous_hard_stop_local = "<current time + 2 min>"`. Start continuous capture. Verify it stops at the configured time.

### §12.4 — Phase 3 Smoke Test

**Estimated time:** 30 minutes.

Goal: verify end-to-end transcription from real audio, including Aegis integration.

Prerequisite: WhisperX model downloaded. pyannote optional.

```bash
bash scripts/smoke_phase_3.sh
```

The harness:
1. Confirms transcription component status is `ok`
2. If pyannote enabled, confirms diarization component status is `ok`
3. Triggers a 5-minute continuous capture (prompts human to record a sample meeting — read a script with a friend or alone)
4. Stops capture
5. Polls until transcription completes (typically 1-3 minutes for 5 min of audio)
6. Polls until diarization completes (if enabled)
7. Fetches `/v1/sessions/{id}/transcript`
8. Triggers Aegis's `meeting_detector` on a fake meeting matching the time window
9. Confirms `meetings.transcript_text` populated in Aegis DB

Manual checklist:
- [ ] Transcript contains recognizable phrases from your recording
- [ ] Speaker labels exist (`user` for mic-channel; `SPEAKER_00`, `SPEAKER_01` for system if multi-speaker)
- [ ] Word-level timestamps look reasonable (within ±500ms of actual)
- [ ] If diarization enabled: speaker count matches actual number of speakers in recording
- [ ] Aegis's meeting row has the transcript text
- [ ] Aegis's extraction runs and produces sensible action items / decisions

This is the most consequential smoke test. Take time to verify the transcripts are good quality before approving.

### §12.5 — Phase 4 Smoke Test

**Estimated time:** 30 minutes.

Goal: verify the full menu bar and onboarding flow.

Prerequisite: a way to test on a clean macOS state. Options:
- A second user account on your Mac
- A VM
- Reset Helios's state: `rm -rf ~/.aegis/capture/onboarding_state.json`, revoke permissions, uninstall app

```bash
bash scripts/smoke_phase_4.sh
```

This phase is mostly manual; harness mostly checks file states.

Manual checklist:
- [ ] Fresh install: launch Helios.app, onboarding window appears
- [ ] Welcome step displays
- [ ] Mic permission step: click Grant, OS prompt appears, grant, status updates to ✓
- [ ] Screen recording step: click Grant, OS prompt appears, grant, restart prompt appears
- [ ] Restart step: click Restart, app re-launches, resumes at next step
- [ ] Model download step: progress bar updates, completes, verification passes
- [ ] Login items step: button opens correct System Settings pane
- [ ] Complete step: dashboard link works, "Done" closes window
- [ ] Menu bar icon transitions through all 6 states (force each via API or config):
  - [ ] not_running (after Stop Helios Daemon)
  - [ ] armed (after Start Daemon, no active session)
  - [ ] recording (during active capture)
  - [ ] recording_voice_note (during active voice note)
  - [ ] paused (after pause-until)
  - [ ] error (after revoking a permission)
- [ ] **4-hour prompt critical test:** temporarily set `continuous_prompt_hours = 1/60` (about 1 minute). Start continuous capture. Wait ~70 seconds. Notification appears with Continue / Stop buttons. Click Continue, verify session continues and next prompt scheduled. Repeat, click Stop, verify session ends.
- [ ] Permission revocation test: revoke mic in System Settings during active session. Within 30 seconds, notification fires, icon flips to error, capture ends with chunks marked `permission_revoked`.
- [ ] Quit Menu Bar leaves daemon running (verify with curl on /v1/status from another terminal)
- [ ] Stop Helios Daemon shows confirmation, then unloads LaunchAgent, daemon stops responding to /v1/health
- [ ] **Voice note from menu bar:** click "Record Voice Note", floating indicator appears with timer + audio level, click stop, save window appears with transcript and suggested attachments
- [ ] **Voice note from hotkey:** enable hotkey in dashboard settings (with Accessibility permission), press ⌥⌘V to start, press again to stop. Save window appears.
- [ ] **Voice note during a meeting:** while a calendar capture is active, trigger voice note. Verify it's marked as excerpt (`is_excerpt=true`), voice note transcript correctly extracted from the time range, meeting transcript still includes the time range (no carve-out).
- [ ] **Voice note duration cap:** temporarily set `voice_note.max_duration_seconds = 30`. Trigger voice note. At 0:00 remaining, cap warning notification fires; at hard stop, voice note auto-stops with reason `voice_note_cap_reached`.
- [ ] **Voice note save window auto-save:** record a quick note. Do nothing for 10 seconds. Verify auto-save fires and creates Aegis row.
- [ ] **Voice note save window with manual attachment:** add an entity manually via picker, save, verify Aegis row has the attachment.
- [ ] **Voice note save window cancellation:** trigger voice note, stop, click Discard, verify no Aegis row created.

### §12.6 — Phase 5 Smoke Test

**Estimated time:** 20 minutes.

Goal: verify OCR captures shared screen content during meetings.

Prerequisite: a Teams or Zoom meeting (real or contrived) where you can share a screen with text content.

```bash
bash scripts/smoke_phase_5.sh
```

Manual checklist:
- [ ] Start a calendar-driven capture or continuous capture
- [ ] Open Teams/Zoom, share a slide or document with readable text
- [ ] Wait 30 seconds
- [ ] Switch to a non-allowlisted app (e.g., Slack), wait 30 seconds
- [ ] Switch back to Teams/Zoom for 30 more seconds
- [ ] Stop capture
- [ ] Query `/v1/ocr?start=&end=` for the session
- [ ] OCR frames captured during Teams/Zoom periods (text matches what was on screen)
- [ ] No OCR frames captured during Slack period (gating worked)
- [ ] Manual override test: trigger Capture Screen → 30 min from menu bar. Switch to Slack. Wait 30 seconds. Verify OCR still fires.
- [ ] Cleanup test: temporarily set `raw_audio_days = 0`. Run cleanup manually via dashboard. Verify yesterday's WAVs go to trash; transcripts remain. Verify untranscribed WAVs do NOT go to trash. Reset `raw_audio_days = 7`.
- [ ] Per-meeting deletion: from dashboard, delete a session. Verify WAVs gone (or in trash), DB rows gone.

### §12.7 — Phase 6 Smoke Test

**Estimated time:** 30 minutes.

Goal: verify dashboard works end-to-end with real data.

```bash
bash scripts/smoke_phase_6.sh
```

Manual checklist:
- [ ] Open `http://127.0.0.1:8000/helios` in browser
- [ ] Overview page renders, shows current daemon state, today's sessions
- [ ] HTMX live updates: status pill refreshes every 5s without page reload
- [ ] "Today's voice notes" section on overview displays voice notes from today (record one or two beforehand)
- [ ] Sessions page lists sessions with correct filters (today, kind, status)
- [ ] Session detail page: transcript renders with speaker names, OCR tab shows frames, audio tab plays WAVs
- [ ] Calendar page shows next 7 days of meetings with capture plans
- [ ] Per-meeting override: toggle a non-excluded meeting to excluded. Verify scheduler skips it on next poll.
- [ ] Diagnostics page renders all metrics correctly
- [ ] Test capture self-test: click button, observe progress, verify pass/fail per step
- [ ] Diagnostic bundle download: click, save tar.gz, extract, verify contents present and redacted
- [ ] Settings page: change exclusion keywords, save. Verify TOML updated and hot-reload applied (check daemon logs).
- [ ] Settings page: change a restart-required value (e.g., API port). Verify "restart required" warning shown.
- [ ] Settings page Voice notes section: change max duration, save, verify config persisted. Toggle hotkey enable; if Accessibility not granted, verify deep-link prompt appears.
- [ ] Speaker identification wizard: walk through all 5 steps with a real HF account. Verify token validation, license verification (one model at a time), download.
- [ ] After wizard: diarization works on next capture session.
- [ ] **Aegis voice notes pages:** open `http://127.0.0.1:8000/voice-notes`. List displays all voice notes recorded so far.
- [ ] Click into a voice note → detail page renders with transcript, attachments, audio player.
- [ ] Edit transcript inline → re-extraction triggers, action items update after a few seconds.
- [ ] Add attachment to a voice note → reflected in person profile under "Voice notes" section.
- [ ] Voice note included in tomorrow's morning briefing if relevant (may need to wait for briefing scheduler or trigger manually).
- [ ] Search for a person via Aegis chat → voice notes mentioning them appear in retrieved context.

### §12.8 — Phase 7 Final Smoke Test

**Estimated time:** Across a normal workday (~half a day of monitoring).

Goal: confirm Helios is ready for daily use.

Run during a real workday:
- [ ] Helios captures every scheduled meeting
- [ ] All transcripts produced within 5 minutes of meeting end
- [ ] Aegis extraction runs successfully on each
- [ ] Memory usage stable (no leaks)
- [ ] CPU usage reasonable (<10% sustained)
- [ ] Battery drain acceptable
- [ ] No unexpected crashes or restarts
- [ ] No notifications fired in error
- [ ] Dashboard remains responsive and accurate throughout the day

If anything fails: open an issue, defer release until resolved.

---

## §13 — Running Decisions Log

This section is initially empty. Claude Code populates it during the build with "decide and document" outcomes (per the rule in §1).

Format for each entry:

```
### YYYY-MM-DD — Phase N — <decision title>

**Context:** <brief description of the ambiguity>

**Decision:** <what was chosen>

**Rationale:** <why>

**Affected files:** <list>
```

Example (placeholder, will be replaced as decisions accumulate):

### 2026-04-22 — Phase 6 — `helios_exclude` column type

**Context:** The dashboard's per-meeting capture override needs a way to express "user explicitly included this meeting" or "user explicitly excluded this meeting" alongside the existing keyword-based exclusion logic.

**Decision:** Use a tri-state nullable boolean column on `meetings` (Option C from build plan §6E.2).

**Rationale:** Most flexible semantics with the simplest column. NULL means "use keyword logic"; explicit true/false overrides. Avoids needing two boolean columns.

**Affected files:** `aegis/db/models.py`, `alembic/versions/XXX_helios_exclude.py`, `aegis/web/routes/api.py`, `aegis/web/routes/helios.py`

---

## Appendix — Summary of Phases

| Phase | Goal | Estimate | Key Output |
|-------|------|----------|------------|
| 0 | Scaffolding | 1-2 days | Daemon serves /v1/health, app builds; voice_notes table in initial schema |
| 1 | Capture pipeline (replay) | 1 week | WAV chunks produced, real audio capture works |
| 2 | Scheduler + API | 1 week | Calendar-triggered sessions; voice note endpoints; scheduler exemptions |
| 3 | Transcription + Aegis | 1.5-2 weeks | Real transcripts flow into Aegis; voice notes table, repository, extractor, sync transcription |
| 4 | Menu bar + onboarding | 1.5 weeks | Polished macOS app experience; voice note hotkey, indicator, save window |
| 5 | OCR | 5 days | Screen text captured during meetings |
| 6 | Dashboard | 1.5 weeks | Six-page Helios management UI; Aegis voice notes pages, profile integration, briefings, RAG |
| 7 | Hardening | 3-5 days | Production-ready; voice note stress test |
| **Total** | | **~6-7 weeks** | |

Voice notes add roughly 7 additional days of Claude Code work distributed across phases 0, 2, 3, 4, 6, and 7. No standalone "voice notes phase" — each piece has a natural home in an existing phase.

---

**End of Build Plan.**
