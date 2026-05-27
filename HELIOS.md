# HELIOS — Local Capture Subsystem for Aegis
## Complete Project Specification

> **This file should be saved as `HELIOS.md` in the project root alongside Aegis's existing `CLAUDE.md`.**
> Claude Code should read both files on startup. Aegis's `CLAUDE.md` describes the existing system; this file describes the new Helios capture subsystem and the modifications to Aegis required to integrate it.
> Do not modify this file during build. Design rationale lives in Appendix A.
>
> Key sections by task:
> - Repository layout → §4
> - Config schema → §5
> - Database schema → §6
> - HTTP API → §7
> - Aegis integration (files to change) → §16
> - Testing → §18
> - Per-phase file manifests → Appendix C

---

## 1. Project Overview

Helios is the local capture subsystem for Aegis. It runs on macOS as a standalone daemon with a companion menu bar app, captures meeting audio and screen content, produces transcripts with speaker attribution, and serves them to Aegis via HTTP. Helios replaces the Screenpipe integration originally envisioned in Aegis's spec; Aegis has never had a working capture layer, and Helios is its first.

**Platform**: macOS 13+ (Apple Silicon required for performance; Intel supported with degraded throughput)
**Language**: Python 3.13+ (matches Aegis)
**Package manager**: `uv` preferred, `pip` supported
**Capture technologies**: ScreenCaptureKit (system audio + screen), CoreAudio via sounddevice (mic), Vision (OCR)
**Transcription**: WhisperX with `distil-large-v3`, pyannote 3.1 for diarization (optional)
**Target**: Single user, single machine; Helios daemon on `127.0.0.1:3031`; Aegis on `127.0.0.1:8000`

Helios's responsibilities end at producing speaker-labeled transcripts and OCR text for specific time windows. It has no understanding of meetings, people, extractions, or workstreams — those are Aegis's domain. The two systems communicate over HTTP; Helios is a capture service, Aegis is a consumer.

### 1.1 Scope

Helios provides:

- **Calendar-triggered capture**: on-demand audio capture for Outlook calendar meetings, pre-starting 60 seconds before and stopping 5 minutes after
- **Continuous capture mode**: user-controlled manual mode for off-calendar conversations (Teams calls, hallway chats) with a 5:30 PM hard stop and 4-hour continuation prompts
- **Voice notes**: ad-hoc, user-triggered short captures (≤5 min) with synchronous transcription, smart-default attachments to Aegis entities, and a dedicated dashboard surface
- **Screen OCR**: gated capture of frontmost meeting-app windows during active sessions, plus a manual "capture screen for N minutes" override
- **Transcription**: local WhisperX with word-level timestamps
- **Diarization**: optional pyannote speaker attribution, with speaker embeddings stored for future voice enrollment
- **HTTP API**: clean REST interface on port 3031, consumed by Aegis and the Helios menu bar
- **Menu bar app**: status indicator, manual controls, diagnostics, voice note recording with optional global hotkey
- **Dashboard**: six-page management interface hosted under Aegis's web UI at `/helios`

Helios does not provide:

- Meeting extraction, action items, decisions (Aegis)
- People, workstreams, org inference (Aegis)
- Email or Teams message ingestion (Aegis)
- Briefings, readiness scoring, drafts (Aegis)
- Cross-meeting voice identity resolution (deferred to future voice enrollment module)

### 1.2 Naming and branding

**Helios** refers to the capture subsystem as a whole: daemon, menu bar app, dashboard pages, SQLite database, Swift helper. When this document refers to "the app" or "Helios," it means this full subsystem.

**Aegis** refers to everything else — the FastAPI web app, PostgreSQL database, extraction pipeline, intelligence layer. When the spec says "Aegis does X," it means the existing Aegis codebase documented in `CLAUDE.md`.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                       USER-FACING SURFACES                           │
├───────────────────────┬─────────────────────────────────────────────┤
│  Helios Menu Bar      │  Aegis Web UI (existing)                    │
│  (rumps + PyObjC)     │  /helios/* pages (new)                      │
│  port N/A             │  port 8000                                  │
│  Process: user GUI    │  Process: Aegis FastAPI                     │
└───────────┬───────────┴───────────────┬─────────────────────────────┘
            │                           │
            │ HTTP                      │ HTTP
            │ /v1/status                │ /v1/audio, /v1/sessions,
            │ /v1/capture/*             │ /v1/status, /v1/diagnostics
            │ /v1/diagnostics           │ /v1/ocr
            ▼                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   HELIOS DAEMON (LaunchAgent)                        │
│                                                                      │
│  FastAPI on 127.0.0.1:3031 (bearer token auth)                      │
│                                                                      │
│  ┌───────────────┐  ┌────────────────┐  ┌────────────────────────┐ │
│  │ Scheduler     │  │ Stream Manager │  │ Workers                │ │
│  │               │  │                │  │                        │ │
│  │ polls Aegis   │  │ mic stream     │  │ Transcription (async) │ │
│  │ /api/meetings │  │ (sounddevice)  │  │ Diarization (async)    │ │
│  │  /upcoming    │  │                │  │ Merge (async)          │ │
│  │ every 60s     │  │ system audio + │  │ OCR loop               │ │
│  │               │  │ screen frames  │  │ Cleanup (nightly)      │ │
│  │ manages       │  │ (Swift helper) │  │ Permission checks      │ │
│  │ session       │  │                │  │                        │ │
│  │ lifecycle     │  │ → Chunker →    │  │                        │ │
│  │               │  │   WAV + index  │  │                        │ │
│  └───────┬───────┘  └────────┬───────┘  └────────┬───────────────┘ │
│          │                   │                    │                  │
│          └───────────────────┼────────────────────┘                  │
│                              ▼                                       │
│            ┌──────────────────────────────────────┐                 │
│            │ SQLite at ~/.aegis/capture/index.db  │                 │
│            │ + WAV files at ~/.aegis/capture/     │                 │
│            │   <date>/<channel>/<ts>.wav          │                 │
│            │ + JPEG thumbnails (low-confidence    │                 │
│            │   OCR frames only)                   │                 │
│            └──────────────────────────────────────┘                 │
└─────────────────────────────────────────────────────────────────────┘
            │                                         ▲
            │ HTTP                                    │ HTTP
            │ GET /api/meetings/upcoming              │ /v1/audio,
            │                                         │ /v1/ocr
            ▼                                         │
┌─────────────────────────────────────────────────────────────────────┐
│                    AEGIS (existing, with additions)                  │
│                                                                      │
│  New/changed files:                                                  │
│  - aegis/ingestion/helios.py      (was screenpipe.py)               │
│  - aegis/ingestion/meeting_detector.py  (simplified)                │
│  - aegis/clients/helios.py        (shared HTTP client)              │
│  - aegis/web/routes/helios.py     (dashboard pages)                 │
│  - aegis/web/routes/api.py        (new /api/meetings/upcoming)      │
│  - aegis/web/templates/helios/*   (dashboard templates)             │
│                                                                      │
│  Existing tables updated:                                            │
│  - meetings.transcript_text, .transcript_status populated by        │
│    HeliosClient via meeting_detector                                 │
│  - meetings.screen_context populated with OCR text per meeting      │
│  - system_health gets 'helios' component heartbeats                 │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.1 Process layout

Helios runs as two OS-level processes:

1. **Daemon** (`launchd` LaunchAgent) — the capture engine. Starts on user login via `~/Library/LaunchAgents/com.aegis.helios.plist`. `KeepAlive=true` ensures auto-restart on crash. Owns the SQLite database, WAV files, and the HTTP API. Persists across UI sessions.

2. **Menu bar app** (user-launched) — the UI controller. rumps-based, runs in the user's login session. Communicates with the daemon exclusively via HTTP. Quitting the menu bar does not stop the daemon.

A single Swift helper subprocess (`ScreenCaptureHelper`) runs as a child of the daemon when capture is active. It handles ScreenCaptureKit for system audio and screen frame capture; the daemon controls it via stdin commands.

### 2.2 Data flow

**Calendar-triggered capture:**
1. Aegis syncs calendar from Microsoft Graph every 30 minutes (existing behavior)
2. Helios scheduler polls Aegis's new `/api/meetings/upcoming` endpoint every 60 seconds
3. 60 seconds before a meeting starts, scheduler spawns the Swift helper, starts mic and system audio streams
4. Audio flows through the chunker: 30-second WAV files per channel, metadata in SQLite
5. Transcription worker picks up chunks, runs WhisperX, writes transcript segments
6. 5 minutes after meeting end (or when extended by adjacency), scheduler stops streams
7. Diarization worker runs on the full session's system-channel audio, produces speaker turns
8. Merge worker joins speaker labels to transcript segments
9. Aegis's meeting detector pulls the completed transcript via `GET /v1/sessions/{id}/transcript`
10. Aegis populates `meetings.transcript_text` and `meetings.transcript_status`, triggers extraction pipeline

**Continuous capture:**
1. User clicks "Start Continuous Capture" in menu bar
2. Menu bar sends `POST /v1/capture/start` with `kind=continuous`
3. Daemon creates session, starts streams (no calendar linkage)
4. Every 4 hours, menu bar receives notification prompt with Continue/Stop buttons
5. 5:30 PM (user's local timezone) triggers automatic stop
6. Transcripts are produced the same way; Aegis can query by time range but no meeting is attached

**OCR:**
1. During an active session, OCR loop polls `NSWorkspace.frontmostApplication` every second
2. If frontmost bundle is in the configured meeting-apps allowlist, request a frame from Swift helper
3. Dedupe by pHash against the last 10 frames
4. Run Vision OCR, filter by confidence, store text (+ thumbnail if low-confidence)
5. OCR frames are queryable by time window via `/v1/ocr`

**Voice note (outside meeting):**
1. User triggers voice note (menu bar item, global hotkey, or dashboard button)
2. Menu bar sends `POST /v1/voice-note/start` to the daemon
3. Daemon creates a session with `kind=voice_note`, mic-only stream
4. Mic samples flow through chunker as normal (30s WAVs in `~/.aegis/capture/<date>/mic/`)
5. Visual indicator: floating "● Recording voice note" pill in upper-right of screen, plus menu bar icon in `recording_voice_note` state
6. User triggers stop (same hotkey, click on pill, click stop in menu bar)
7. Menu bar sends `POST /v1/voice-note/stop`
8. Daemon stops session, partial-flushes chunks, runs synchronous transcription on collected audio (typical voice note transcribes in 2-10 seconds)
9. Daemon returns transcript inline in the stop response
10. Menu bar opens the floating save window with transcript displayed
11. Aegis runs entity resolution on the transcript via `POST /api/voice-notes/preview-attachments`, returns suggested attachments
12. User confirms/modifies/discards within the save window, OR auto-save fires after 10 seconds
13. Save action sends `POST /api/voice-notes` to Aegis with transcript and attachments
14. Aegis creates `voice_notes` row, kicks off extraction pipeline

**Voice note during a meeting:**

Same flow except the voice note is recorded as a labeled excerpt rather than an independent recording. The mic stream is already running for the meeting session, so the voice note creates a metadata-only `voice_note` session with `excerpt_of_session_id` pointing to the meeting and `excerpt_start_ts` / `excerpt_end_ts` marking the range. The voice note's transcript is generated by transcribing the mic chunks within that time range; those same chunks remain part of the meeting's transcript (no carve-out — the meeting transcript is unchanged). Voice notes during meetings get the same save UX and Aegis integration as standalone voice notes.

### 2.3 Crash recovery

Each table has a `status` or equivalent lifecycle field. On daemon startup:
- Chunks with `status='recorded'` but `transcribed_at IS NULL` are re-queued for the transcription worker
- Sessions with `ended_at IS NOT NULL` and `diarization_status='pending'` are re-queued for the diarization worker
- Sessions with `ended_at IS NULL` (i.e., were active when daemon died) are marked `ended_at = last_chunk_end_ts`, `end_reason='crash_recovery'`

Combined with transcription idempotency (temp=0 WhisperX, stable word-level timestamps), re-processing is safe.

---

## 3. System Requirements and Prerequisites

### 3.1 Hardware

- Mac with Apple Silicon strongly preferred (M1 or later). Intel Macs work but transcription runs at ~1x real-time rather than 5x.
- 16 GB RAM minimum. WhisperX model + pyannote + FastAPI + SQLite fits comfortably; 8 GB is tight.
- 30 GB free disk space for models, audio files, transcripts, thumbnails, and logs. Most is consumed by raw WAVs during the 7-day retention window.

### 3.2 Software

Installed before Helios build begins:

| Software | Version | Purpose |
|----------|---------|---------|
| macOS | 13+ | ScreenCaptureKit minimum |
| Python | 3.13+ | Runtime (matches Aegis) |
| Xcode Command Line Tools | latest | Swift compilation (one-time for helper binary) |
| ffmpeg | 6+ | WhisperX runtime dependency (`brew install ffmpeg`) |
| PostgreSQL + pgvector | 16+ | Existing Aegis infrastructure |
| Screenpipe | — | **Do not install** (would conflict on port 3030) |

### 3.3 External accounts

- Anthropic, OpenAI, Microsoft Azure: already configured for Aegis
- **HuggingFace** (optional, for pyannote): free account, read-scope access token, accepted licenses for `pyannote/speaker-diarization-3.1`, `pyannote/segmentation-3.0`, `pyannote/embedding`

### 3.4 macOS permissions

Granted via the onboarding flow on first launch:

- **Microphone access** — required, for mic capture
- **Screen Recording** — required, for ScreenCaptureKit (both system audio and screen frames)
- **Accessibility** — not used

Helios requests permissions through normal macOS APIs; users grant via System Settings when prompted. The onboarding flow walks through each with deep-links to the right System Settings pane.

---

## 4. Repository Layout

Helios lives as a sibling directory to Aegis's Python package at the repo root:

```
aegis/                          # existing git repo root
├── aegis/                      # existing Aegis Python package (modified in §16)
├── helios/                     # NEW — Helios package
│   ├── pyproject.toml          # independent Helios dependencies
│   ├── src/
│   │   └── helios/
│   │       ├── __init__.py
│   │       ├── __main__.py         # entrypoints: menu bar, --daemon
│   │       ├── config.py           # Pydantic settings
│   │       ├── clock.py            # Clock protocol (Real + Virtual)
│   │       ├── db/
│   │       │   ├── __init__.py
│   │       │   ├── connection.py   # aiosqlite, pragmas
│   │       │   ├── migrations.py   # migration runner
│   │       │   ├── queries.py      # typed query functions
│   │       │   └── rows.py         # Pydantic row models
│   │       ├── migrations/
│   │       │   ├── 001_initial.sql
│   │       │   ├── 002_session_calendar_links.sql
│   │       │   └── ... (future)
│   │       ├── sources/
│   │       │   ├── __init__.py
│   │       │   ├── interface.py    # AudioSource, SystemAudioSource protocols
│   │       │   ├── real.py         # sounddevice + Swift helper subprocess
│   │       │   └── replay.py       # file-based fixture reader
│   │       ├── capture/
│   │       │   ├── stream_manager.py
│   │       │   ├── chunker.py
│   │       │   └── helper_protocol.py  # Swift helper stdin/stdout protocol
│   │       ├── scheduler/
│   │       │   ├── scheduler.py    # main scheduler
│   │       │   ├── calendar.py     # polls Aegis API
│   │       │   └── timezone.py     # hard stop, pause helpers
│   │       ├── workers/
│   │       │   ├── transcription.py
│   │       │   ├── diarization.py
│   │       │   ├── merge.py
│   │       │   ├── ocr.py
│   │       │   ├── cleanup.py
│   │       │   └── permissions.py
│   │       ├── api/
│   │       │   ├── __init__.py     # FastAPI app factory
│   │       │   ├── auth.py         # bearer token middleware
│   │       │   ├── routes/
│   │       │   │   ├── health.py
│   │       │   │   ├── status.py
│   │       │   │   ├── capture.py
│   │       │   │   ├── sessions.py
│   │       │   │   ├── audio.py
│   │       │   │   ├── ocr.py
│   │       │   │   ├── permissions.py
│   │       │   │   └── diagnostics.py
│   │       │   └── schemas.py      # request/response Pydantic models
│   │       ├── menubar/
│   │       │   ├── app.py          # rumps App
│   │       │   ├── onboarding.py   # PyObjC onboarding window
│   │       │   ├── client.py       # HTTP client for daemon
│   │       │   └── notifications.py
│   │       ├── notifications/
│   │       │   └── notify.py       # UNUserNotificationCenter wrapper
│   │       ├── state.py            # daemon state machine
│   │       ├── logging.py          # structured JSON logging
│   │       └── keychain.py         # macOS Keychain access for HF token
│   ├── swift/
│   │   └── ScreenCaptureHelper.swift   # human-maintained source
│   ├── bin/
│   │   └── ScreenCaptureHelper         # committed universal binary
│   ├── icons/
│   │   ├── helios_not_running_template.png
│   │   ├── helios_not_running_template@2x.png
│   │   ├── helios_armed_template.png
│   │   ├── helios_armed_template@2x.png
│   │   ├── helios_recording_template.png
│   │   ├── helios_recording_template@2x.png
│   │   ├── helios_paused_template.png
│   │   ├── helios_paused_template@2x.png
│   │   ├── helios_error_template.png
│   │   ├── helios_error_template@2x.png
│   │   └── Helios.icns
│   ├── tests/
│   │   ├── conftest.py
│   │   ├── fixtures/
│   │   │   ├── audio/              # gitignored; fetched separately
│   │   │   ├── calendar/
│   │   │   ├── ocr/
│   │   │   └── transcripts/        # golden outputs
│   │   ├── test_chunker.py
│   │   ├── test_scheduler.py
│   │   ├── test_workers.py
│   │   ├── test_api.py
│   │   └── ...
│   └── setup.py                    # py2app config
├── shared/                         # NEW — contract schemas
│   ├── pyproject.toml
│   ├── src/shared/
│   │   ├── __init__.py
│   │   ├── meetings.py             # Aegis → Helios /api/meetings/upcoming schema
│   │   └── audio.py                # Helios → Aegis /v1/audio schema
│   └── README.md
├── scripts/                        # existing + new
│   ├── build_swift_helper.sh       # NEW
│   ├── build_helios.sh             # NEW — py2app + ad-hoc sign
│   ├── install_helios.sh           # NEW — deploy to /Applications
│   ├── smoke_phase_0.sh            # NEW — Phase 0 smoke test harness
│   ├── smoke_phase_1.sh            # ... per-phase harnesses
│   └── ... (existing Aegis scripts unchanged)
├── docs/
│   ├── helios_smoke_tests.md       # NEW — manual smoke test procedures
│   └── ... (existing)
├── CLAUDE.md                       # existing Aegis spec (add pointer to HELIOS.md)
├── HELIOS.md                       # THIS FILE
└── pyproject.toml                  # Aegis's existing pyproject (unchanged)
```

### 4.1 Python environments

Helios uses its own virtualenv, separate from Aegis. Two reasons:

1. Helios's dependencies (WhisperX, pyannote, PyObjC, rumps, sounddevice) would bloat the Aegis venv significantly and would be bundled into the `.app` by py2app.
2. Independent dependency evolution — upgrading WhisperX shouldn't affect Aegis's LangGraph version.

Setup:
```bash
cd helios
uv venv
uv sync
```

Aegis takes a local-path dependency on `shared/` in its `pyproject.toml`:
```toml
[project]
dependencies = [
    # ... existing ...
    "shared @ file://../shared",
]
```

Helios does the same. PyCharm is configured with both venvs as project interpreters (Settings → Project → Python Interpreter → Add).

### 4.2 Human-maintained vs. Claude Code-maintained files

Claude Code **does not modify** the following:

- `helios/swift/ScreenCaptureHelper.swift` — Swift source, maintained by humans
- `helios/bin/ScreenCaptureHelper` — pre-built binary, committed
- `helios/icons/*.png` — placeholder icons for v1; real icons are a design task
- `helios/tests/fixtures/audio/*.wav` — recorded test meetings (see §18.4)

Claude Code **maintains** everything else, including:

- All Python source under `helios/src/`
- SQL migrations in `helios/migrations/`
- Build scripts under `scripts/`
- Aegis modifications (§16)
- Tests under `helios/tests/` except the audio fixtures

Marker comment at the top of non-modifiable Python files (where applicable):
```python
# HUMAN-MAINTAINED — do not modify via Claude Code
```

For Swift and binary files, the repo-level `.clauderc` or equivalent should block edits to `helios/swift/` and `helios/bin/`.

---

## 5. Configuration

### 5.1 File location

Primary config: `~/.aegis/capture.toml`
- Permissions: `chmod 600` (user read/write only)
- Auto-generated with defaults on first daemon startup
- Bearer token generated fresh on first creation
- Environment variable overrides supported (prefix `HELIOS_`, e.g. `HELIOS_API_PORT=3032`)

### 5.2 Full schema with defaults

```toml
[api]
# HTTP API server
port = 3031
bearer_token = "<auto-generated-32-byte-hex>"
bind_address = "127.0.0.1"

[capture]
# Capture lifecycle
calendar_triggered = true
continuous_enabled = true                   # whether manual continuous mode is allowed
calendar_pre_start_seconds = 60
calendar_post_end_seconds = 300
continuous_prompt_hours = 4
continuous_prompt_timeout_seconds = 300     # default action after timeout
continuous_hard_stop_local = "17:30"        # HH:MM in user's local timezone
continuous_hard_stop_timezone = "follow_user"  # or an IANA name like "America/New_York"
pause_morning_hour = 8                      # for "until tomorrow morning" resume

[exclusion]
# Meeting keyword exclusion (case-insensitive substring match on title)
keywords = ["confidential", "HR", "legal", "personal"]

[audio]
# Audio stream configuration
sample_rate = 16000
chunk_seconds = 30
silence_threshold_db = -50
system_source = "sck"                       # "sck" | "blackhole" (blackhole is v2)

[transcription]
runtime = "whisperx"
model = "distil-large-v3"
language = "en"
compute_type = "float16"                    # "float16" | "int8"
word_timestamps = true
vad_filter = true
vad_min_silence_ms = 1000
worker_nice = 10
transcription_max_attempts = 3
model_load_timeout_seconds = 30

[diarization]
enabled = false                             # user enables after HF setup
min_speakers = 1
max_speakers = 8
store_embeddings = true
# HF token is stored in macOS Keychain, not here; this just tracks the location
hf_token_location = "keychain"

[ocr]
enabled = true
fps = 1
dedup_hamming_threshold = 4
dedup_window_frames = 10
min_text_chars = 20
confidence_threshold = 0.5
thumbnail_confidence_threshold = 0.7
meeting_apps = [
    "com.microsoft.teams2",
    "com.microsoft.teams",
    "us.zoom.xos",
    "com.webex.meetingmanager",
    "com.cisco.webexmeetingsapp",
]

[retention]
raw_audio_days = 7
trash_hold_hours = 24
cleanup_hour_local = 3
disk_space_warning_gb = 5                   # warn if free disk below this

[storage]
root = "~/.aegis/capture"
# Subdirectories created as needed:
#   <root>/index.db         SQLite database
#   <root>/<date>/mic/*.wav    Mic audio
#   <root>/<date>/system/*.wav System audio
#   <root>/ocr_thumbs/<date>/*.jpg  Thumbnails
#   <root>/logs/helios.log   Rotating log
#   <root>/trash/...         Soft-deleted files

[scheduler]
# Calendar poller
calendar_source_url = "http://127.0.0.1:8000/api/meetings/upcoming"
calendar_poll_seconds = 60
permission_check_minutes = 5
aegis_connect_timeout_seconds = 5

[notifications]
time_sensitive_allowed = true               # bypass Focus for actionable alerts
enable_sound_on_prompts = true

[voice_note]
enabled = true
max_duration_seconds = 300                  # 5 minutes
soft_cap_notification = true                # notify at the cap rather than hard-stop
auto_save_timeout_seconds = 10              # save window auto-save delay
default_save_action = "save_with_suggestions"  # or "save_unattached", "discard"
hotkey_enabled = false                      # requires Accessibility permission
hotkey_combo = "cmd+option+v"               # parsed by menu bar app

[voice_note.indicator]
floating_pill_position = "top_right"        # "top_right", "top_left", "menu_bar"
show_elapsed_time = true
show_audio_level = true

[logging]
level = "info"                              # "debug" | "info" | "warn" | "error"
file_rotate_days = 14
file_max_size_mb = 50

[launchagent]
# Populated on first install by the install script, stable afterwards
plist_path = "~/Library/LaunchAgents/com.aegis.helios.plist"
auto_install = true
```

### 5.3 Pydantic models

All config sections have corresponding Pydantic models in `helios/src/helios/config.py`. Models use `pydantic-settings` for environment variable support. Validation runs on load; invalid config causes daemon startup failure with a clear error message pointing to the bad field.

```python
# helios/src/helios/config.py (excerpt)
class CaptureConfig(BaseSettings):
    calendar_triggered: bool = True
    continuous_enabled: bool = True
    calendar_pre_start_seconds: int = Field(60, ge=0, le=600)
    calendar_post_end_seconds: int = Field(300, ge=0, le=1800)
    # ...

class HeliosConfig(BaseSettings):
    api: ApiConfig
    capture: CaptureConfig
    # ...
    model_config = SettingsConfigDict(
        env_prefix="HELIOS_",
        env_nested_delimiter="__",
        toml_file="~/.aegis/capture.toml",
    )
```

### 5.4 Hot reload

The daemon uses `watchfiles` to monitor `~/.aegis/capture.toml` for changes. On change:

1. Re-parse the file with Pydantic validation
2. Diff against current in-memory config
3. Classify each changed field:
   - **Hot-reloadable**: exclusion keywords, OCR meeting_apps, OCR thresholds, retention days, notification settings, logging level
   - **Restart-required**: API port, bearer token, audio sample rate, chunk seconds, storage root, transcription model, diarization enabled/disabled
4. Apply hot-reloadable changes immediately; log "restart required for: ..." for others
5. Dashboard Settings page shows which changes need restart

### 5.5 First-run config generation

On first daemon launch (TOML missing):

1. Create `~/.aegis/` and `~/.aegis/capture/` directories if missing
2. Generate 32-byte hex bearer token via `secrets.token_hex(32)`
3. Write TOML with all defaults, bearer token substituted
4. `chmod 600` the file
5. Log `config_generated` event with the path (but NOT the token)

Aegis reads the token on startup for its `HeliosClient` (see §16.3); no separate config exchange needed.

---

## 6. Database Schema

Helios owns a SQLite database at `~/.aegis/capture/index.db`. All timestamps are UTC epoch seconds stored as `REAL`.

### 6.1 Pragmas

Set on every connection:

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;
```

### 6.2 Full schema

```sql
-- Schema version tracking
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);

-- Capture sessions: one per contiguous capture window
CREATE TABLE capture_sessions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('calendar', 'continuous', 'manual_screen', 'voice_note')),
    started_at REAL NOT NULL,
    ended_at REAL,                   -- NULL while active
    end_reason TEXT,
    -- 'scheduled', 'user_stop', 'hard_stop_530', '4hr_prompt_stop',
    -- 'permission_revoked', 'error', 'sleep_gap', 'crash_recovery',
    -- 'voice_note_user_stop', 'voice_note_cap_reached', 'voice_note_cancelled'
    diarization_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (diarization_status IN ('pending', 'running', 'complete', 'failed', 'not_applicable')),
    diarization_attempts INTEGER NOT NULL DEFAULT 0,
    screen_capture_override_until REAL  -- for manual screen capture windows
);
CREATE INDEX idx_sessions_time ON capture_sessions(started_at, ended_at);
CREATE INDEX idx_sessions_active ON capture_sessions(ended_at) WHERE ended_at IS NULL;

-- Link sessions to calendar events (many-to-many)
CREATE TABLE session_calendar_links (
    session_id INTEGER NOT NULL REFERENCES capture_sessions(id) ON DELETE CASCADE,
    calendar_event_id TEXT NOT NULL,    -- Microsoft Graph event ID
    overlap_start REAL NOT NULL,
    overlap_end REAL NOT NULL,
    PRIMARY KEY (session_id, calendar_event_id)
);
CREATE INDEX idx_links_calendar ON session_calendar_links(calendar_event_id);

-- Audio chunks: one WAV file per row, per channel
CREATE TABLE audio_chunks (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES capture_sessions(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('mic', 'system')),
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    path TEXT,                       -- NULL for no_audio chunks
    samples INTEGER NOT NULL,
    partial INTEGER NOT NULL DEFAULT 0,  -- 1 if flushed short on stop
    status TEXT NOT NULL DEFAULT 'recorded'
        CHECK (status IN ('recorded', 'no_audio', 'unavailable', 'transcribed',
                          'transcription_failed')),
    unavailable_reason TEXT,
    -- 'screen_locked', 'system_sleep', 'stream_error', 'permission_revoked'
    transcribed_at REAL,
    transcription_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_chunks_session ON audio_chunks(session_id);
CREATE INDEX idx_chunks_time ON audio_chunks(start_ts, end_ts);
CREATE INDEX idx_chunks_pending ON audio_chunks(status, transcribed_at)
    WHERE status = 'recorded' AND transcribed_at IS NULL;

-- Per-chunk transcripts: segments with word-level timestamps
CREATE TABLE transcript_segments (
    id INTEGER PRIMARY KEY,
    chunk_id INTEGER NOT NULL REFERENCES audio_chunks(id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    text TEXT NOT NULL,
    speaker TEXT,
    -- 'user' for mic channel after merge
    -- 'SPEAKER_00', 'SPEAKER_01', ... for system channel after diarization
    -- NULL before diarization merge
    words TEXT                       -- JSON: [{"word":"hello","start":...,"end":...}]
);
CREATE INDEX idx_segments_chunk ON transcript_segments(chunk_id);
CREATE INDEX idx_segments_time ON transcript_segments(start_ts, end_ts);

-- Diarization results: per-session speaker turns
CREATE TABLE diarization_turns (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES capture_sessions(id) ON DELETE CASCADE,
    speaker_label TEXT NOT NULL,     -- 'SPEAKER_00', 'SPEAKER_01', ...
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    embedding BLOB                   -- pyannote speaker embedding (float32 vector)
);
CREATE INDEX idx_diar_session ON diarization_turns(session_id);
CREATE INDEX idx_diar_time ON diarization_turns(start_ts, end_ts);

-- OCR frames: screen text capture
CREATE TABLE ocr_frames (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES capture_sessions(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    app_bundle TEXT NOT NULL,
    display_id INTEGER,
    phash BLOB NOT NULL,             -- 8 bytes
    text TEXT NOT NULL,
    avg_confidence REAL NOT NULL,
    thumbnail_path TEXT              -- NULL unless avg_confidence < 0.7
);
CREATE INDEX idx_ocr_session ON ocr_frames(session_id);
CREATE INDEX idx_ocr_time ON ocr_frames(ts);

-- Proactive permission check history
CREATE TABLE permission_checks (
    id INTEGER PRIMARY KEY,
    checked_at REAL NOT NULL,
    mic_granted INTEGER NOT NULL,
    screen_recording_granted INTEGER NOT NULL
);
CREATE INDEX idx_perm_time ON permission_checks(checked_at);

-- Component status history (transcription/diarization/ocr availability)
CREATE TABLE component_status (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    component TEXT NOT NULL,
    -- 'audio_capture', 'transcription', 'diarization', 'ocr'
    status TEXT NOT NULL,
    -- 'ok', 'loading', 'unavailable', 'degraded'
    reason TEXT,
    detail TEXT,
    action TEXT
);
CREATE INDEX idx_component_time ON component_status(component, ts);

-- Daemon events log (sparse, for dashboard display)
CREATE TABLE daemon_events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('info', 'warn', 'error')),
    component TEXT NOT NULL,
    event TEXT NOT NULL,
    details TEXT                     -- JSON
);
CREATE INDEX idx_events_time ON daemon_events(ts);
CREATE INDEX idx_events_component ON daemon_events(component, ts);

-- Voice note metadata. Audio chunks live in transcript_segments via the session
-- (or via excerpt_of_session_id when the voice note was triggered during another session).
CREATE TABLE voice_notes (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES capture_sessions(id) ON DELETE CASCADE,
    started_at REAL NOT NULL,
    ended_at REAL,
    -- excerpt_of_session_id: when voice note was triggered during another active
    -- session (e.g., a meeting), this points to that session. The voice note's
    -- audio chunks live in that session, not in this voice_note's own session.
    excerpt_of_session_id INTEGER REFERENCES capture_sessions(id),
    excerpt_start_ts REAL,                  -- only set if excerpt_of_session_id NOT NULL
    excerpt_end_ts REAL,
    triggered_by TEXT NOT NULL CHECK (triggered_by IN ('menu_bar', 'hotkey', 'dashboard'))
);
CREATE INDEX idx_vn_session ON voice_notes(session_id);
CREATE INDEX idx_vn_excerpt ON voice_notes(excerpt_of_session_id) WHERE excerpt_of_session_id IS NOT NULL;
CREATE INDEX idx_vn_time ON voice_notes(started_at);
```

### 6.3 Migration runner

`helios/src/helios/db/migrations.py`:

```python
async def run_migrations(db: Connection, migrations_dir: Path) -> int:
    """Apply all pending migrations. Returns the new schema version."""
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
    )
    current = await _get_current_version(db)
    files = sorted(migrations_dir.glob("*.sql"))
    for path in files:
        version = int(path.stem.split("_")[0])
        if version <= current:
            continue
        sql = path.read_text()
        async with db.execute("BEGIN"):
            await db.executescript(sql)
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, time.time()),
            )
            await db.commit()
        log.info("migration_applied", version=version, file=path.name)
        current = version
    return current
```

Migrations run on every daemon startup. Idempotent via `schema_version` check.

### 6.4 Connection management

One writer connection and a pool of 4 reader connections, all opened at daemon startup:

```python
# helios/src/helios/db/connection.py
class DatabasePool:
    def __init__(self, path: Path):
        self.path = path
        self._writer: aiosqlite.Connection | None = None
        self._readers: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(maxsize=4)

    async def open(self):
        self._writer = await _open_connection(self.path)
        for _ in range(4):
            await self._readers.put(await _open_connection(self.path))

    async def acquire_writer(self) -> aiosqlite.Connection:
        # Writer is a shared resource; callers serialize via async lock internally
        return self._writer

    @asynccontextmanager
    async def reader(self):
        conn = await self._readers.get()
        try:
            yield conn
        finally:
            await self._readers.put(conn)
```

WAL mode means readers don't block the writer and vice versa.

### 6.5 Row models and query functions

Example from `helios/src/helios/db/rows.py`:

```python
from pydantic import BaseModel
from typing import Literal

class AudioChunkRow(BaseModel):
    id: int
    session_id: int
    channel: Literal["mic", "system"]
    start_ts: float
    end_ts: float
    path: str | None
    samples: int
    partial: bool
    status: Literal["recorded", "no_audio", "unavailable", "transcribed", "transcription_failed"]
    unavailable_reason: str | None
    transcribed_at: float | None
    transcription_attempts: int

class CaptureSessionRow(BaseModel):
    id: int
    kind: Literal["calendar", "continuous", "manual_screen", "voice_note"]
    started_at: float
    ended_at: float | None
    end_reason: str | None
    diarization_status: Literal["pending", "running", "complete", "failed", "not_applicable"]
    diarization_attempts: int
    screen_capture_override_until: float | None

class VoiceNoteRow(BaseModel):
    id: int
    session_id: int
    started_at: float
    ended_at: float | None
    excerpt_of_session_id: int | None
    excerpt_start_ts: float | None
    excerpt_end_ts: float | None
    triggered_by: Literal["menu_bar", "hotkey", "dashboard"]

# ... one per table ...
```

Queries in `helios/src/helios/db/queries.py`:

```python
async def get_pending_chunks(db, limit: int = 20) -> list[AudioChunkRow]:
    async with db.execute(
        """
        SELECT id, session_id, channel, start_ts, end_ts, path, samples,
               partial, status, unavailable_reason, transcribed_at,
               transcription_attempts
        FROM audio_chunks
        WHERE status = 'recorded' AND transcribed_at IS NULL
        ORDER BY start_ts
        LIMIT ?
        """,
        (limit,),
    ) as cursor:
        cursor.row_factory = aiosqlite.Row
        return [AudioChunkRow(**dict(row)) for row in await cursor.fetchall()]

async def insert_audio_chunk(
    db,
    session_id: int,
    channel: str,
    start_ts: float,
    end_ts: float,
    path: str | None,
    samples: int,
    partial: bool,
    status: str = "recorded",
) -> int:
    async with db.execute(
        """
        INSERT INTO audio_chunks (session_id, channel, start_ts, end_ts, path,
                                   samples, partial, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, channel, start_ts, end_ts, path, samples, int(partial), status),
    ) as cursor:
        await db.commit()
        return cursor.lastrowid
```

Code outside `queries.py` does not write raw SQL.

---

## 7. HTTP API Contract

FastAPI on `127.0.0.1:3031`. All endpoints under `/v1/` prefix. Authentication via `Authorization: Bearer <token>` header.

### 7.1 Authentication

Bearer token from `[api].bearer_token` in config. Clients (Aegis `HeliosClient`, menu bar) read the token from `~/.aegis/capture.toml` on startup. On 401 response, re-read the file (token may have been regenerated) and retry once.

Auth middleware (`helios/src/helios/api/auth.py`):

```python
async def require_bearer_token(
    request: Request,
    authorization: str = Header(...),
) -> None:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, {"error": "unauthorized", "detail": "Bearer token required"})
    token = authorization.removeprefix("Bearer ")
    if not secrets.compare_digest(token, settings.api.bearer_token):
        raise HTTPException(401, {"error": "unauthorized", "detail": "Invalid token"})
```

`/v1/health` is the one unauthenticated endpoint (for external health checks).

### 7.2 Error response format

All errors return JSON with a stable shape:

```json
{
    "error": "<code>",
    "detail": "<human-readable>",
    "code": 403
}
```

Stable error code strings:
- `unauthorized` — auth failure
- `permission_denied` — macOS permission missing
- `session_not_found`
- `chunk_not_found`
- `daemon_shutting_down`
- `queue_full`
- `component_unavailable` — transcription/diarization not working
- `validation_error` — bad request parameters
- `internal_error` — unexpected server-side failure

### 7.3 Endpoints

#### `GET /v1/health`

Unauthenticated. Returns 200 if daemon is responsive.

```json
{
    "status": "ok",
    "version": "0.1.0",
    "uptime_seconds": 12345
}
```

#### `GET /v1/status`

Full daemon state. Polled by menu bar (every 3s idle, 1s when menu open) and dashboard overview.

```json
{
    "daemon": "running",
    "mode": "armed",
    "components": {
        "audio_capture": "ok",
        "transcription": "ok",
        "diarization": "unavailable",
        "ocr": "ok"
    },
    "component_errors": {
        "diarization": {
            "reason": "token_missing",
            "detail": "HuggingFace token not configured",
            "action": "Add token in Settings → Speaker identification"
        }
    },
    "active_session": {
        "id": 142,
        "kind": "calendar",
        "calendar_event_ids": ["AAMkADExample=="],
        "started_at": 1712345000.0,
        "screen_capture_override_until": null
    },
    "next_calendar_event": {
        "calendar_event_id": "AAMkADExample2==",
        "title": "Platform review",
        "starts_at": 1712348400.0,
        "pre_start_at": 1712348340.0
    },
    "paused_until": null,
    "queue": {
        "transcription_pending": 0,
        "transcription_failed_24h": 0,
        "diarization_pending": 0,
        "diarization_failed_24h": 0
    },
    "permissions": {
        "mic_granted": true,
        "screen_recording_granted": true,
        "last_checked_at": 1712345670.0
    },
    "last_error": null
}
```

`mode` values: `armed` | `recording` | `paused` | `error` | `not_running`.

#### `POST /v1/capture/start`

Start a manual capture session. Body:

```json
{
    "kind": "continuous",
    "duration_minutes": null
}
```

- `kind`: `"continuous"` or `"manual_screen"`
- `duration_minutes`: required for `manual_screen`, ignored otherwise

Response:
```json
{
    "session_id": 143,
    "kind": "continuous",
    "started_at": 1712345678.0
}
```

Errors: `component_unavailable` if audio_capture is down, `permission_denied` if mic/screen recording revoked.

#### `POST /v1/capture/stop`

Stop the active session. Idempotent (no error if nothing active).

```json
{
    "session_id": 143,
    "ended_at": 1712349278.0,
    "end_reason": "user_stop"
}
```

Returns `null` for `session_id` if nothing was active.

#### `POST /v1/capture/pause-until`

```json
{
    "until_ts": 1712400000.0
}
```

Pauses the calendar scheduler until the timestamp. Active captures continue to completion; no new sessions start until after the pause. Response echoes the target.

#### `POST /v1/capture/resume`

Clears pause state immediately.

#### `POST /v1/capture/enable-screen-override`

For the "Capture Screen for N minutes" menu option. If a session is active, adds OCR override to it; if not, starts a new `manual_screen` session.

```json
{
    "duration_minutes": 30
}
```

#### `POST /v1/voice-note/start`

Trigger a voice note. Body:

```json
{
    "triggered_by": "hotkey"
}
```

`triggered_by`: `"menu_bar"`, `"hotkey"`, or `"dashboard"`.

Response:

```json
{
    "voice_note_id": 42,
    "session_id": 143,
    "started_at": 1712345000.0,
    "is_excerpt": false,
    "max_duration_seconds": 300
}
```

If a calendar or continuous capture is currently active:
- `is_excerpt: true`
- `session_id`: the parent session's ID
- The voice_note row stores `excerpt_of_session_id` and `excerpt_start_ts = now`

If no other capture is active:
- `is_excerpt: false`
- `session_id`: a new session created with `kind='voice_note'`
- Standard mic-only capture starts

Errors:
- `409 voice_note_already_active`: another voice note is currently recording
- `503 component_unavailable`: transcription is not available; reject the start to avoid creating a note that can't be transcribed
- `403 permission_denied`: mic permission not granted

#### `POST /v1/voice-note/stop`

Stop the active voice note. Body: empty.

Response (synchronous; blocks until transcription completes, typically 2-10 seconds):

```json
{
    "voice_note_id": 42,
    "session_id": 143,
    "started_at": 1712345000.0,
    "ended_at": 1712345034.5,
    "duration_seconds": 34.5,
    "transcript": {
        "text": "Note to self, follow up with Sarah about the Q2 budget proposal by Friday.",
        "segments": [
            {
                "start": 1712345000.5,
                "end": 1712345034.0,
                "text": "Note to self, follow up with Sarah about the Q2 budget proposal by Friday.",
                "words": [...]
            }
        ]
    },
    "is_excerpt": false
}
```

Errors:
- `404 voice_note_not_active`: no voice note currently recording
- `408 transcription_timeout`: transcription took longer than 60 seconds (rare; client polls the session's transcript endpoint)

In the rare timeout case, response includes `transcript: null` and the client polls `GET /v1/sessions/{session_id}/transcript`. Clients should set their HTTP timeout to at least 60s for this endpoint.

#### `POST /v1/voice-note/cancel`

Discard the active voice note. Body: empty. Session row marked deleted, audio chunks flagged for cleanup, no transcription runs.

```json
{
    "cancelled_voice_note_id": 42
}
```

#### `GET /v1/voice-note/active`

Returns the currently-active voice note session, if any. Used by menu bar to recover state after a crash or restart.

```json
{
    "active": {
        "voice_note_id": 42,
        "started_at": 1712345000.0,
        "elapsed_seconds": 12.3,
        "is_excerpt": false,
        "approaching_cap": false
    }
}
```

`approaching_cap` is true when within 30 seconds of `max_duration_seconds`.

#### `GET /v1/sessions`

Filterable list. Query params:
- `kind`: filter by kind
- `start_ts_gte`, `start_ts_lte`: time range
- `status`: `captured` | `partial` | `no_audio` | `failed`
- `limit`, `offset`: pagination (default 50)

```json
{
    "sessions": [...],
    "total": 142,
    "offset": 0,
    "limit": 50
}
```

Each session includes kind, started_at, ended_at, end_reason, linked_calendar_event_ids, duration_seconds, chunk_count, coverage_pct.

#### `GET /v1/sessions/{id}`

Full session detail including linked calendar events, coverage, queue status.

#### `GET /v1/sessions/{id}/transcript`

Merged, speaker-labeled transcript for an entire session. This is what Aegis's `HeliosClient.get_transcript_for_meeting()` calls.

Query params:
- `include_words`: bool, default false (word timestamps inflate response)

```json
{
    "session_id": 142,
    "started_at": 1712345000.0,
    "ended_at": 1712348600.0,
    "segments": [
        {
            "start": 1712345012.34,
            "end": 1712345018.91,
            "speaker": "user",
            "text": "Okay so let's walk through the roadmap",
            "words": null
        },
        {
            "start": 1712345019.10,
            "end": 1712345024.55,
            "speaker": "SPEAKER_00",
            "text": "Sure, starting with Q2 milestones...",
            "words": null
        }
    ],
    "coverage": {
        "captured_seconds": 3580,
        "unavailable_ranges": [
            {"start": 1712346100, "end": 1712346140, "reason": "screen_locked"}
        ],
        "transcription_pending_seconds": 0
    },
    "diarization_status": "complete"
}
```

#### `GET /v1/audio`

Time-window query. Used by Aegis for retroactive transcript building, e.g. when a meeting extends past its scheduled end.

Query params: `start`, `end` (UTC epoch seconds), `include_words` (bool).

Response is the same shape as `/v1/sessions/{id}/transcript` but `session_id` may be `null` or an array if the window spans multiple sessions.

#### `GET /v1/ocr`

Time-window query for OCR frames.

Query params: `start`, `end`, `min_confidence` (default 0.5), `app_bundle` (optional filter).

```json
{
    "frames": [
        {
            "ts": 1712345234.5,
            "app_bundle": "com.microsoft.teams2",
            "display_id": 1,
            "text": "Q2 Roadmap\n- Ship auth by May\n- Beta in June",
            "confidence": 0.94,
            "thumbnail_url": null
        }
    ]
}
```

#### `GET /v1/permissions`

```json
{
    "mic_granted": true,
    "screen_recording_granted": true,
    "last_checked_at": 1712345670.0
}
```

#### `GET /v1/diagnostics`

Full diagnostic dump. Used by menu bar ⌥-click view, "Copy Diagnostics" button, and dashboard Diagnostics page.

```json
{
    "version": "0.1.0",
    "pid": 4821,
    "uptime_seconds": 238320,
    "memory_mb": 342,
    "cpu_5min_avg_pct": 8,
    "permissions": {...},
    "active_session": {...},
    "next_event": {...},
    "last_chunks": {
        "mic": {"ts": 1712345674.0, "age_seconds": 4},
        "system": {"ts": 1712345674.2, "age_seconds": 4}
    },
    "transcription_throughput_realtime_multiple": 4.1,
    "queues": {
        "transcription_pending": 1,
        "transcription_failed_24h": 0,
        "diarization_pending": 0,
        "diarization_failed_24h": 0
    },
    "storage": {
        "audio_bytes": 4509715456,
        "audio_oldest_days": 6,
        "thumbnails_bytes": 18874368,
        "database_bytes": 356515840
    },
    "component_status": {...},
    "recent_events": [
        {"ts": ..., "level": "info", "component": "scheduler",
         "event": "session_started", "details": "..."}
    ]
}
```

#### `POST /v1/diagnostics/restart`

Restarts the daemon. Used by "Restart daemon" dashboard button. Returns 202 immediately; daemon shuts down and LaunchAgent restarts it.

#### `POST /v1/diagnostics/flush-queues`

Force-runs the transcription and diarization workers outside their normal loops. Response lists what was processed.

#### `POST /v1/diagnostics/test-capture`

Runs the 60-second self-test (§13.5). Returns a job ID; poll via `GET /v1/diagnostics/test-capture/{job_id}` for results.

#### `POST /v1/diagnostics/reload-component`

```json
{
    "component": "transcription"
}
```

Retries loading a failed component (e.g., after user installs ffmpeg or adds HF token).

#### Session-level actions

- `POST /v1/sessions/{id}/re-transcribe`: re-runs transcription on all chunks
- `POST /v1/sessions/{id}/re-diarize`: re-runs diarization
- `DELETE /v1/sessions/{id}`: deletes session + chunks + transcripts + OCR + thumbnails (soft-delete to trash per retention)

### 7.4 Request/response schemas

All bodies are validated via Pydantic models in `helios/src/helios/api/schemas.py`. Examples:

```python
class CaptureStartRequest(BaseModel):
    kind: Literal["continuous", "manual_screen"]
    duration_minutes: int | None = None

    @model_validator(mode="after")
    def duration_required_for_manual_screen(self):
        if self.kind == "manual_screen" and self.duration_minutes is None:
            raise ValueError("duration_minutes required for manual_screen")
        return self

class TranscriptSegmentResponse(BaseModel):
    start: float
    end: float
    speaker: str | None
    text: str
    words: list[Word] | None = None

class VoiceNoteStartRequest(BaseModel):
    triggered_by: Literal["menu_bar", "hotkey", "dashboard"]

class VoiceNoteStartResponse(BaseModel):
    voice_note_id: int
    session_id: int
    started_at: float
    is_excerpt: bool
    max_duration_seconds: int

class VoiceNoteStopResponse(BaseModel):
    voice_note_id: int
    session_id: int
    started_at: float
    ended_at: float
    duration_seconds: float
    transcript: TranscriptResponse | None
    is_excerpt: bool

class ActiveVoiceNote(BaseModel):
    voice_note_id: int
    started_at: float
    elapsed_seconds: float
    is_excerpt: bool
    approaching_cap: bool

class VoiceNoteActiveResponse(BaseModel):
    active: ActiveVoiceNote | None

# ... etc
```

### 7.5 Graceful shutdown

On SIGTERM (from LaunchAgent or user action):

1. Stop accepting new API requests
2. Finish in-flight requests with 10s timeout
3. Stop scheduler (cancel pending session timers)
4. Stop active capture session if any (partial flush, save state)
5. Close Swift helper subprocess
6. Flush logs
7. Close database connections
8. Release port

SIGKILL cannot be handled; LaunchAgent will restart.

---

## 8. Capture Pipeline

This section describes how audio samples become WAV files on disk. The pipeline runs during every active capture session (calendar, continuous, or manual screen).

### 8.1 Source abstraction

Two protocols in `helios/src/helios/sources/interface.py`:

```python
from typing import Protocol, AsyncIterator

class AudioSample(NamedTuple):
    channel: Literal["mic", "system"]
    ts: float                    # UTC epoch seconds of first sample
    samples: np.ndarray          # int16, 1-D

class AudioSource(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def samples(self) -> AsyncIterator[AudioSample]: ...
    @property
    def is_running(self) -> bool: ...

class SystemAudioSource(AudioSource):
    """Marker protocol for system-audio sources.
    Implementations: SCKSystemAudioSource (v1), BlackHoleSystemAudioSource (v2)."""
```

Two implementations at build time:

- `helios/src/helios/sources/real.py` — uses sounddevice for mic, Swift helper for system
- `helios/src/helios/sources/replay.py` — reads fixture WAVs, emits samples at wall-clock pace (or faster via VirtualClock)

Source selection happens at daemon startup based on `HELIOS_REPLAY` env var. Switching is a one-line change in the source factory.

### 8.2 Mic source

```python
# helios/src/helios/sources/real.py (excerpt)
class MicSource:
    def __init__(self, queue: asyncio.Queue[AudioSample], samplerate: int = 16000):
        self._queue = queue
        self._samplerate = samplerate
        self._stream: sd.InputStream | None = None
        self._loop = asyncio.get_event_loop()

    def _callback(self, indata, frames, time_info, status):
        # sounddevice callback runs on an audio thread, not the event loop.
        # Marshal to event loop for queue put.
        ts = time.time() - (frames / self._samplerate)
        samples = indata[:, 0].copy()  # take first channel, copy out of buffer
        asyncio.run_coroutine_threadsafe(
            self._queue.put(AudioSample("mic", ts, samples)),
            self._loop,
        )

    async def start(self) -> None:
        self._stream = sd.InputStream(
            samplerate=self._samplerate,
            channels=1,
            dtype="int16",
            blocksize=1600,   # 100 ms
            callback=self._callback,
        )
        self._stream.start()

    async def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
```

**Device changes.** If the user plugs in AirPods mid-capture, sounddevice emits a callback with `status` containing `PaStatusFlag.InputOverflow` or similar. Handler: log `device_change_detected`, stop stream, restart with default device. Gap in audio reflected in chunker output.

**Permission failures.** `sd.InputStream` raises `PortAudioError` if mic permission is denied. Caught at stream manager level; transitions daemon to error state.

### 8.3 System audio source

System audio goes through the Swift helper (see §20 for the Swift contract). The Python side is a subprocess wrapper:

```python
# helios/src/helios/sources/real.py (excerpt)
class SCKSystemAudioSource:
    def __init__(
        self,
        queue: asyncio.Queue[AudioSample],
        helper_path: Path,
        samplerate: int = 16000,
    ):
        self._queue = queue
        self._helper_path = helper_path
        self._samplerate = samplerate
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._video_enabled = False

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            str(self._helper_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._send_command("ENABLE_AUDIO")
        self._reader_task = asyncio.create_task(self._read_loop())

    async def enable_video(self, display_id: int) -> None:
        await self._send_command(f"SET_DISPLAY {display_id}")
        await self._send_command("ENABLE_VIDEO")
        self._video_enabled = True

    async def disable_video(self) -> None:
        await self._send_command("DISABLE_VIDEO")
        self._video_enabled = False

    async def _send_command(self, cmd: str) -> None:
        self._proc.stdin.write((cmd + "\n").encode())
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        """Parse framed protocol from helper stdout.
        Frame: [1B type][8B double ts][4B uint32 length][payload]
          type = 0x01 audio (int16 PCM)
          type = 0x02 video (JPEG)
        """
        r = self._proc.stdout
        try:
            while True:
                header = await r.readexactly(13)
                packet_type = header[0]
                ts, length = struct.unpack("<dI", header[1:])
                payload = await r.readexactly(length)
                if packet_type == 0x01:
                    samples = np.frombuffer(payload, dtype=np.int16)
                    await self._queue.put(AudioSample("system", ts, samples))
                elif packet_type == 0x02:
                    await self._video_queue.put((ts, payload))
                # unknown packet types logged and ignored
        except asyncio.IncompleteReadError:
            # helper died; stream manager will restart
            log.warn("system_helper_stream_ended", proc=self._proc.returncode)

    async def stop(self) -> None:
        if self._proc:
            await self._send_command("QUIT")
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.terminate()
                await self._proc.wait()
            self._proc = None
        if self._reader_task:
            self._reader_task.cancel()
```

**Helper process failures.** If the subprocess exits unexpectedly, `_read_loop` catches `IncompleteReadError`, logs, returns. Stream manager's watchdog detects no-buffers-for-30s and triggers restart.

**Video queue.** Video frames are consumed by the OCR worker (§10), not the chunker. Separate `asyncio.Queue` for video bytes.

### 8.4 Stream manager

Orchestrates both streams, handles lifecycle, monitors health:

```python
# helios/src/helios/capture/stream_manager.py
class StreamManager:
    def __init__(
        self,
        clock: Clock,
        audio_queue: asyncio.Queue[AudioSample],
        video_queue: asyncio.Queue[VideoFrame],
        helper_path: Path,
    ):
        self._clock = clock
        self._audio_queue = audio_queue
        self._video_queue = video_queue
        self._mic = MicSource(audio_queue)
        self._system = SCKSystemAudioSource(audio_queue, helper_path)
        self._watchdog_task: asyncio.Task | None = None
        self._last_buffer_ts = {"mic": 0.0, "system": 0.0}

    async def start(self) -> None:
        await self._assert_power_assertion()
        try:
            await self._mic.start()
        except Exception as e:
            log.error("mic_start_failed", error=str(e))
            raise StreamStartError("mic", e) from e
        try:
            await self._system.start()
        except Exception as e:
            await self._mic.stop()
            log.error("system_start_failed", error=str(e))
            raise StreamStartError("system", e) from e
        self._watchdog_task = asyncio.create_task(self._watchdog())

    async def stop(self) -> None:
        if self._watchdog_task:
            self._watchdog_task.cancel()
        await asyncio.gather(
            self._mic.stop(),
            self._system.stop(),
            return_exceptions=True,
        )
        await self._release_power_assertion()

    async def _watchdog(self) -> None:
        """Detect stalled streams. If no buffers for 30s during active capture,
        attempt one restart per stream. If restart fails with permission error,
        propagate to daemon state machine. Otherwise exponential backoff."""
        while True:
            await asyncio.sleep(5)
            now = self._clock.time()
            for channel, last_ts in self._last_buffer_ts.items():
                if now - last_ts > 30:
                    await self._restart_stream(channel)
```

**Power assertion.** During active capture, prevent idle sleep via `IOPMAssertionCreateWithName` (PyObjC). Release on capture stop. Display sleep is allowed.

**Sleep/wake handling.** Subscribe to `NSWorkspace.didWakeNotification` via PyObjC; on wake, restart both streams regardless of watchdog state.

### 8.5 Chunker

Accumulates per-channel samples into 30-second WAV files:

```python
# helios/src/helios/capture/chunker.py
class Chunker:
    def __init__(
        self,
        storage_root: Path,
        db: DatabasePool,
        clock: Clock,
        samplerate: int = 16000,
        chunk_seconds: int = 30,
        silence_threshold_db: float = -50,
    ):
        self._root = storage_root
        self._db = db
        self._clock = clock
        self._samplerate = samplerate
        self._chunk_samples = samplerate * chunk_seconds
        self._silence_rms = 10 ** (silence_threshold_db / 20) * 32768
        self._buffers: dict[str, list[np.ndarray]] = {"mic": [], "system": []}
        self._buffer_starts: dict[str, float | None] = {"mic": None, "system": None}
        self._session_id: int | None = None

    def start_session(self, session_id: int) -> None:
        self._session_id = session_id
        self._buffers = {"mic": [], "system": []}
        self._buffer_starts = {"mic": None, "system": None}

    async def handle_sample(self, sample: AudioSample) -> None:
        if self._session_id is None:
            return  # not in an active session; drop
        channel = sample.channel
        if self._buffer_starts[channel] is None:
            self._buffer_starts[channel] = sample.ts
        self._buffers[channel].append(sample.samples)
        total_samples = sum(len(s) for s in self._buffers[channel])
        if total_samples >= self._chunk_samples:
            await self._flush(channel, partial=False)

    async def stop_session(self) -> None:
        """Called when session ends. Flush any remaining buffer as partial."""
        for channel in ("mic", "system"):
            if self._buffers[channel]:
                await self._flush(channel, partial=True)
        self._session_id = None

    async def _flush(self, channel: str, partial: bool) -> None:
        start_ts = self._buffer_starts[channel]
        data = np.concatenate(self._buffers[channel])
        duration = len(data) / self._samplerate
        end_ts = start_ts + duration
        rms = np.sqrt(np.mean(data.astype(np.float32) ** 2))

        if rms < self._silence_rms:
            # Silent chunk — skip file write, mark no_audio
            await queries.insert_audio_chunk(
                self._db.writer,
                session_id=self._session_id,
                channel=channel,
                start_ts=start_ts,
                end_ts=end_ts,
                path=None,
                samples=len(data),
                partial=partial,
                status="no_audio",
            )
        else:
            path = self._build_path(start_ts, channel)
            path.parent.mkdir(parents=True, exist_ok=True)
            await self._write_wav(path, data)
            await queries.insert_audio_chunk(
                self._db.writer,
                session_id=self._session_id,
                channel=channel,
                start_ts=start_ts,
                end_ts=end_ts,
                path=str(path),
                samples=len(data),
                partial=partial,
                status="recorded",
            )

        self._buffers[channel] = []
        self._buffer_starts[channel] = None

    def _build_path(self, start_ts: float, channel: str) -> Path:
        day = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        return self._root / day / channel / f"{int(start_ts * 1000)}.wav"

    async def _write_wav(self, path: Path, data: np.ndarray) -> None:
        # Use soundfile or wave stdlib; wrap blocking IO in executor
        await asyncio.get_event_loop().run_in_executor(
            None, self._write_wav_blocking, path, data
        )

    def _write_wav_blocking(self, path: Path, data: np.ndarray) -> None:
        with wave.open(str(path), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(self._samplerate)
            f.writeframes(data.tobytes())
```

### 8.6 Session lifecycle

The capture engine as a whole:

1. Scheduler decides a session should start (calendar-triggered, or API call)
2. Session row inserted with `started_at = now`, `ended_at = NULL`, kind set
3. `session_calendar_links` rows inserted for all linked calendar events
4. Chunker starts for this session
5. Stream manager starts mic and system streams (if not already running from a previous overlapping session)
6. Samples flow through the queue to the chunker
7. Scheduler decides to stop (scheduled end + buffer; user action; hard stop; etc.)
8. Chunker partial-flushes remaining buffers
9. Stream manager stops (if no other overlapping session needs it)
10. Session row updated: `ended_at = now`, `end_reason = <reason>`
11. Diarization worker enqueued for this session

Adjacent sessions (per §11.4) don't stop/start streams between them — one session ends, the next begins, streams continue uninterrupted.

### 8.7 Unavailable ranges

When streams are known to be failing (permission revoked, system sleep, helper died), the chunker writes `unavailable` status chunks with the reason, so coverage calculations are accurate:

```python
async def mark_unavailable(
    self, channel: str, start_ts: float, end_ts: float, reason: str
) -> None:
    await queries.insert_audio_chunk(
        self._db.writer,
        session_id=self._session_id,
        channel=channel,
        start_ts=start_ts,
        end_ts=end_ts,
        path=None,
        samples=0,
        partial=False,
        status="unavailable",
        unavailable_reason=reason,
    )
```

---

## 9. Transcription and Diarization Pipeline

### 9.1 Pipeline structure

Three workers, each a long-lived asyncio task:

1. **Transcription worker** — continuous loop, polls for un-transcribed chunks, runs WhisperX
2. **Diarization worker** — event-driven, runs on session end
3. **Merge worker** — event-driven, runs after diarization completes

All three write to the same database via the writer connection (serialized by asyncio lock).

### 9.2 Transcription worker

```python
# helios/src/helios/workers/transcription.py
class TranscriptionWorker:
    def __init__(
        self,
        db: DatabasePool,
        config: TranscriptionConfig,
        status_reporter: ComponentStatusReporter,
    ):
        self._db = db
        self._config = config
        self._status = status_reporter
        self._model: WhisperModel | None = None
        self._model_ready = asyncio.Event()

    async def start(self) -> None:
        os.nice(self._config.worker_nice)
        asyncio.create_task(self._load_model())
        asyncio.create_task(self._run_loop())

    async def _load_model(self) -> None:
        """Load WhisperX model in background. Daemon is responsive during load."""
        self._status.set("transcription", "loading")
        try:
            def load():
                return WhisperModel(
                    self._config.model,
                    device="auto",
                    compute_type=self._config.compute_type,
                )
            self._model = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, load),
                timeout=self._config.model_load_timeout_seconds,
            )
            self._model_ready.set()
            self._status.set("transcription", "ok")
        except asyncio.TimeoutError:
            self._status.set(
                "transcription", "unavailable",
                reason="load_timeout",
                detail=f"Model did not load within {self._config.model_load_timeout_seconds}s",
                action="Check system resources; restart daemon to retry.",
            )
        except ImportError as e:
            self._status.set(
                "transcription", "unavailable",
                reason="import_failed",
                detail=str(e),
                action=self._derive_action(e),
            )
        except Exception as e:
            log.error("transcription_model_load_failed", error=str(e),
                     traceback=traceback.format_exc())
            self._status.set(
                "transcription", "unavailable",
                reason="load_error",
                detail=str(e),
                action="Check logs; restart daemon to retry.",
            )

    def _derive_action(self, err: Exception) -> str:
        msg = str(err).lower()
        if "ffmpeg" in msg:
            return "Install ffmpeg: brew install ffmpeg"
        if "cuda" in msg or "torch" in msg:
            return "Reinstall torch: pip install --force-reinstall torch"
        return "Check logs for details."

    async def _run_loop(self) -> None:
        await self._model_ready.wait()
        while True:
            chunks = await queries.get_pending_chunks(
                self._db.writer, limit=10
            )
            if not chunks:
                await asyncio.sleep(1)
                continue
            for chunk in chunks:
                await self._transcribe_chunk(chunk)

    async def _transcribe_chunk(self, chunk: AudioChunkRow) -> None:
        try:
            await queries.increment_transcription_attempts(self._db.writer, chunk.id)
            def do_transcribe():
                return self._model.transcribe(
                    chunk.path,
                    language=self._config.language,
                    word_timestamps=self._config.word_timestamps,
                    vad_filter=self._config.vad_filter,
                    vad_parameters={
                        "min_silence_duration_ms": self._config.vad_min_silence_ms
                    },
                )
            segments, info = await asyncio.get_event_loop().run_in_executor(
                None, do_transcribe
            )
            await self._store_segments(chunk, segments)
            await queries.mark_chunk_transcribed(self._db.writer, chunk.id)
        except Exception as e:
            log.error("transcription_failed", chunk_id=chunk.id, error=str(e))
            if chunk.transcription_attempts >= self._config.transcription_max_attempts:
                await queries.mark_chunk_transcription_failed(self._db.writer, chunk.id)

    async def _store_segments(
        self, chunk: AudioChunkRow, segments: list[Any]
    ) -> None:
        for idx, seg in enumerate(segments):
            # Convert relative timestamps within chunk to absolute epoch
            start_abs = chunk.start_ts + seg.start
            end_abs = chunk.start_ts + seg.end
            words_json = None
            if seg.words:
                words_json = json.dumps([
                    {"word": w.word, "start": chunk.start_ts + w.start,
                     "end": chunk.start_ts + w.end, "probability": w.probability}
                    for w in seg.words
                ])
            await queries.insert_transcript_segment(
                self._db.writer,
                chunk_id=chunk.id,
                segment_index=idx,
                start_ts=start_abs,
                end_ts=end_abs,
                text=seg.text.strip(),
                speaker="user" if chunk.channel == "mic" else None,
                words=words_json,
            )
```

**Speaker assignment at transcription time.** Mic-channel segments get `speaker='user'` immediately. System-channel segments get `speaker=NULL`; the merge worker fills these after diarization.

### 9.3 Diarization worker

Triggered when a capture session ends. Runs on the full concatenated system-channel audio for that session.

```python
# helios/src/helios/workers/diarization.py
class DiarizationWorker:
    def __init__(
        self,
        db: DatabasePool,
        config: DiarizationConfig,
        hf_token: str | None,
        status_reporter: ComponentStatusReporter,
    ):
        self._db = db
        self._config = config
        self._hf_token = hf_token
        self._status = status_reporter
        self._pipeline: Pipeline | None = None
        self._queue: asyncio.Queue[int] = asyncio.Queue()  # session IDs

    async def start(self) -> None:
        if not self._config.enabled:
            self._status.set("diarization", "unavailable",
                           reason="disabled",
                           detail="Diarization disabled in config",
                           action="Enable in Settings → Speaker identification")
            return
        if not self._hf_token:
            self._status.set("diarization", "unavailable",
                           reason="token_missing",
                           detail="HuggingFace token not configured",
                           action="Complete setup in Settings → Speaker identification")
            return
        asyncio.create_task(self._load_pipeline())
        asyncio.create_task(self._run_loop())

    async def _load_pipeline(self) -> None:
        self._status.set("diarization", "loading")
        try:
            def load():
                return Pipeline.from_pretrained(
                    "pyannote/speaker-diarization-3.1",
                    use_auth_token=self._hf_token,
                )
            self._pipeline = await asyncio.get_event_loop().run_in_executor(None, load)
            self._status.set("diarization", "ok")
        except Exception as e:
            action = self._derive_action(e)
            self._status.set("diarization", "unavailable",
                           reason="load_failed", detail=str(e), action=action)

    def enqueue(self, session_id: int) -> None:
        self._queue.put_nowait(session_id)

    async def _run_loop(self) -> None:
        while True:
            session_id = await self._queue.get()
            await self._diarize_session(session_id)

    async def _diarize_session(self, session_id: int) -> None:
        if not self._pipeline:
            await queries.mark_session_diarization_failed(
                self._db.writer, session_id, "pipeline_not_loaded"
            )
            return
        try:
            await queries.set_session_diarization_status(
                self._db.writer, session_id, "running"
            )
            chunks = await queries.get_session_audio_chunks(
                self._db.writer, session_id, channel="system"
            )
            audio_file = await self._concatenate_audio(chunks)
            try:
                def run():
                    return self._pipeline(
                        audio_file,
                        min_speakers=self._config.min_speakers,
                        max_speakers=self._config.max_speakers,
                    )
                diarization = await asyncio.get_event_loop().run_in_executor(None, run)
                await self._store_turns(session_id, chunks, diarization)
                await queries.set_session_diarization_status(
                    self._db.writer, session_id, "complete"
                )
                # Trigger merge worker
                await self._merge_worker.enqueue(session_id)
            finally:
                audio_file.unlink(missing_ok=True)
        except Exception as e:
            log.error("diarization_failed", session_id=session_id, error=str(e))
            await queries.mark_session_diarization_failed(
                self._db.writer, session_id, str(e)
            )

    async def _concatenate_audio(self, chunks: list[AudioChunkRow]) -> Path:
        """Concatenate system-channel WAVs into a single temp file for pyannote."""
        tmp = Path(tempfile.mktemp(suffix=".wav"))
        # Use soundfile or pydub; must preserve 16kHz mono int16
        # ... implementation detail ...
        return tmp

    async def _store_turns(
        self, session_id: int, chunks: list[AudioChunkRow], diarization: Any
    ) -> None:
        session = await queries.get_session(self._db.writer, session_id)
        session_start = session.started_at
        for turn, _, speaker_label in diarization.itertracks(yield_label=True):
            # pyannote returns times relative to start of concatenated audio.
            # Map back to absolute epoch seconds.
            abs_start = session_start + turn.start
            abs_end = session_start + turn.end
            embedding = self._extract_embedding(diarization, turn, speaker_label)
            await queries.insert_diarization_turn(
                self._db.writer,
                session_id=session_id,
                speaker_label=speaker_label,
                start_ts=abs_start,
                end_ts=abs_end,
                embedding=embedding.tobytes() if embedding is not None else None,
            )
```

**Embedding extraction.** pyannote 3.1 can emit per-speaker embeddings. Store them as `BLOB` (float32 vectors, ~1-2 KB each). Used by the future voice enrollment module for cross-meeting identity.

**Gap handling.** If the session has `unavailable` chunks, the concatenation skips them. Diarization turns are emitted only for captured audio. The merge worker doesn't receive speaker info for unavailable ranges, which is correct.

### 9.4 Merge worker

Joins speaker labels from diarization turns onto transcript segments:

```python
# helios/src/helios/workers/merge.py
class MergeWorker:
    def __init__(self, db: DatabasePool):
        self._db = db
        self._queue: asyncio.Queue[int] = asyncio.Queue()

    def enqueue(self, session_id: int) -> None:
        self._queue.put_nowait(session_id)

    async def start(self) -> None:
        asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while True:
            session_id = await self._queue.get()
            await self._merge_session(session_id)

    async def _merge_session(self, session_id: int) -> None:
        turns = await queries.get_diarization_turns(self._db.writer, session_id)
        segments = await queries.get_session_transcript_segments(
            self._db.writer, session_id, channel="system"
        )
        for seg in segments:
            speaker = self._find_overlapping_speaker(seg, turns)
            if speaker:
                await queries.update_segment_speaker(
                    self._db.writer, seg.id, speaker
                )

    def _find_overlapping_speaker(
        self, segment: TranscriptSegmentRow, turns: list[DiarizationTurnRow]
    ) -> str | None:
        """For a transcript segment, find the diarization turn with maximum overlap.
        Return the speaker label of that turn."""
        best_turn, best_overlap = None, 0.0
        for turn in turns:
            overlap_start = max(segment.start_ts, turn.start_ts)
            overlap_end = min(segment.end_ts, turn.end_ts)
            overlap = max(0, overlap_end - overlap_start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_turn = turn
        return best_turn.speaker_label if best_turn else None
```

Merge is best-effort — if no turn overlaps a segment (rare edge case), speaker stays NULL. UI displays "(unknown)" in such cases.

### 9.5 Query-time transcript assembly

`GET /v1/sessions/{id}/transcript` assembles the final output:

```python
# helios/src/helios/api/routes/sessions.py
async def get_session_transcript(session_id: int) -> TranscriptResponse:
    session = await queries.get_session(db, session_id)
    segments = await queries.get_transcript_segments_for_session(
        db, session_id, ordered_by="start_ts"
    )
    coverage = await compute_coverage(db, session)
    return TranscriptResponse(
        session_id=session_id,
        started_at=session.started_at,
        ended_at=session.ended_at,
        segments=[s.to_api() for s in segments],
        coverage=coverage,
        diarization_status=session.diarization_status,
    )

async def compute_coverage(db, session: CaptureSessionRow) -> Coverage:
    chunks = await queries.get_session_chunks(db, session.id)
    captured = sum(c.end_ts - c.start_ts for c in chunks if c.status == "recorded")
    unavailable_ranges = [
        UnavailableRange(start=c.start_ts, end=c.end_ts, reason=c.unavailable_reason)
        for c in chunks if c.status == "unavailable"
    ]
    pending = sum(
        c.end_ts - c.start_ts for c in chunks
        if c.status == "recorded" and c.transcribed_at is None
    )
    return Coverage(
        captured_seconds=captured,
        unavailable_ranges=unavailable_ranges,
        transcription_pending_seconds=pending,
    )
```

Response segments are already in start-time order from the query (index `idx_segments_time`).

### 9.6 Time-window queries

`GET /v1/audio?start=X&end=Y` works the same way but across session boundaries:

```python
async def get_audio_for_window(start: float, end: float) -> TranscriptResponse:
    segments = await queries.get_transcript_segments_in_range(db, start, end)
    # Coverage spans whatever sessions overlap the window
    sessions = await queries.get_sessions_overlapping(db, start, end)
    coverage = aggregate_coverage(sessions, start, end)
    return TranscriptResponse(
        session_id=None,  # may span multiple
        started_at=start,
        ended_at=end,
        segments=[s.to_api() for s in segments],
        coverage=coverage,
    )
```

### 9.7 Re-transcription and re-diarization

`POST /v1/sessions/{id}/re-transcribe`:
1. Delete all `transcript_segments` for the session's chunks
2. Reset `audio_chunks.transcribed_at = NULL`, `transcription_attempts = 0`
3. Transcription worker picks them up on next loop iteration

`POST /v1/sessions/{id}/re-diarize`:
1. Delete all `diarization_turns` for the session
2. Set `session.diarization_status = 'pending'`
3. Enqueue to diarization worker
4. Merge worker runs after diarization completes

Both operations are idempotent and can be safely retried.

### 9.8 Voice note synchronous transcription

Voice notes need synchronous transcription so the floating save window can show the transcript immediately on stop. The transcription worker normally polls for pending chunks and processes them in order; voice notes need to skip the queue and run immediately on the voice note's audio.

Implementation: when `/v1/voice-note/stop` is called:

1. Stop the mic stream (or, if excerpt mode, just record the end timestamp; mic continues for the parent session)
2. Partial-flush any in-progress chunks
3. Wait for all of the voice note's chunks to be written to disk (synchronization point)
4. Concatenate the chunks into a single in-memory audio buffer (5 minutes at 16 kHz mono int16 = 9.6 MB, fits comfortably)
5. Call `WhisperModel.transcribe()` directly on the buffer (not via the worker queue)
6. Write transcript_segments rows in the database
7. Mark all chunks as transcribed
8. Return the response with transcript

```python
async def transcribe_synchronously(
    self, chunks: list[AudioChunkRow]
) -> TranscriptResult:
    """Run transcription on a specific list of chunks, bypassing the queue.
    Used for voice notes which need immediate transcript return."""
    await self._model_ready.wait()
    self._pause_normal_loop = True  # avoid GPU/ANE contention
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

The synchronous path bypasses the worker queue but uses the same loaded model. The transcription worker is paused for the duration of the voice note transcription (a few seconds) to avoid GPU/ANE contention. After the voice note returns, the worker resumes.

**Excerpt mode** (voice note during a meeting): chunks are owned by the parent meeting session, not deleted at voice note stop. Transcription pulls the mic-channel chunks within `[excerpt_start_ts, excerpt_end_ts]` and runs WhisperX on them. The resulting segments are stored in `transcript_segments` linked to those chunks (the meeting will see them too). The voice note row references the time range; the voice note's transcript is queried later by selecting transcript_segments in that time range.

If the parent session's normal transcription worker has already produced segments for some of the chunks in the excerpt range, those segments are reused — no duplicate transcription. Only not-yet-transcribed chunks go through the sync path.

---

## 10. OCR Pipeline

### 10.1 Overview

Screen OCR extracts text from shared content during meetings. It's gated to conserve CPU and respect privacy: during an active session, OCR is active only when a known meeting app is frontmost, unless the user has activated a manual screen-capture override.

### 10.2 Frame source

Video frames come from the same Swift helper as system audio (§8.3). The OCR worker consumes from the video queue when OCR is enabled.

When OCR should be enabled, the worker sends `ENABLE_VIDEO` and `SET_DISPLAY <id>` commands to the helper. When OCR should be disabled, `DISABLE_VIDEO`. The helper's SCK stream continues running for audio regardless.

### 10.3 Gating logic

```python
# helios/src/helios/workers/ocr.py
class OCRGating:
    def __init__(self, config: OCRConfig):
        self._meeting_bundles = set(config.meeting_apps)
        self._override_until: float | None = None

    def should_capture(self, frontmost_bundle: str, now: float) -> bool:
        if self._override_until and now < self._override_until:
            return True
        return frontmost_bundle in self._meeting_bundles

    def set_override(self, until_ts: float) -> None:
        self._override_until = until_ts
```

Gating decision is made every second based on `NSWorkspace.frontmostApplication()`:

```python
class OCRWorker:
    async def _gating_loop(self) -> None:
        while self._session_active:
            frontmost = self._get_frontmost_bundle()
            should = self._gating.should_capture(frontmost, self._clock.time())
            if should and not self._helper_video_enabled:
                display_id = self._get_display_for_frontmost()
                await self._system_source.enable_video(display_id)
                self._helper_video_enabled = True
                log.info("ocr_enabled", bundle=frontmost, display=display_id)
            elif not should and self._helper_video_enabled:
                await self._system_source.disable_video()
                self._helper_video_enabled = False
                log.info("ocr_disabled", bundle=frontmost)
            await asyncio.sleep(1)

    def _get_frontmost_bundle(self) -> str:
        ws = NSWorkspace.sharedWorkspace()
        app = ws.frontmostApplication()
        return app.bundleIdentifier() if app else ""

    def _get_display_for_frontmost(self) -> int:
        """Return the display ID containing the frontmost window's focused window."""
        # Use CGWindowListCopyWindowInfo to find frontmost window,
        # then CGDisplayBounds to determine which display contains it.
        # Returns the CGDirectDisplayID as int.
        ...
```

### 10.4 Frame processing

When video frames arrive, the worker processes them at 1 fps (Swift helper's `minimumFrameInterval` handles throttling):

```python
async def _frame_loop(self) -> None:
    while self._session_active:
        ts, jpeg_bytes = await self._video_queue.get()
        frontmost = self._last_frontmost_bundle
        phash = self._compute_phash(jpeg_bytes)
        if self._is_duplicate(phash):
            continue
        self._phash_window.append(phash)
        if len(self._phash_window) > self._config.dedup_window_frames:
            self._phash_window.popleft()
        text, avg_conf = await self._run_vision_ocr(jpeg_bytes)
        if len(text) < self._config.min_text_chars:
            continue
        thumb_path = None
        if avg_conf < self._config.thumbnail_confidence_threshold:
            thumb_path = await self._save_thumbnail(jpeg_bytes, ts)
        await queries.insert_ocr_frame(
            self._db.writer,
            session_id=self._session_id,
            ts=ts,
            app_bundle=frontmost,
            display_id=self._current_display_id,
            phash=phash,
            text=text,
            avg_confidence=avg_conf,
            thumbnail_path=str(thumb_path) if thumb_path else None,
        )

def _compute_phash(self, jpeg_bytes: bytes) -> bytes:
    img = PIL.Image.open(io.BytesIO(jpeg_bytes))
    return imagehash.phash(img).hash.tobytes()  # 8 bytes

def _is_duplicate(self, phash: bytes) -> bool:
    for existing in self._phash_window:
        if hamming_distance(phash, existing) <= self._config.dedup_hamming_threshold:
            return True
    return False
```

### 10.5 Vision OCR

Apple's Vision framework via PyObjC:

```python
from Vision import VNImageRequestHandler, VNRecognizeTextRequest
from CoreImage import CIImage

async def _run_vision_ocr(self, jpeg_bytes: bytes) -> tuple[str, float]:
    def do_ocr():
        ci_image = CIImage.imageWithData_(NSData.dataWithBytes_length_(
            jpeg_bytes, len(jpeg_bytes)))
        handler = VNImageRequestHandler.alloc().initWithCIImage_options_(ci_image, None)
        request = VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(VNRequestTextRecognitionLevelAccurate)
        handler.performRequests_error_([request], None)
        observations = request.results()
        texts, confidences = [], []
        for obs in observations:
            top = obs.topCandidates_(1)[0]
            if top.confidence() >= self._config.confidence_threshold:
                texts.append(top.string())
                confidences.append(top.confidence())
        text = "\n".join(texts)
        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
        return text, avg_conf
    return await asyncio.get_event_loop().run_in_executor(None, do_ocr)
```

### 10.6 Thumbnails

Low-confidence frames (avg_conf < 0.7) get a thumbnail saved:

```python
async def _save_thumbnail(self, jpeg_bytes: bytes, ts: float) -> Path:
    day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    path = self._root / "ocr_thumbs" / day / f"{int(ts * 1000)}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Helper already emits JPEG q85; save as-is
    await asyncio.get_event_loop().run_in_executor(
        None, path.write_bytes, jpeg_bytes
    )
    return path
```

Thumbnails follow raw-audio retention (7 days). Path stored in `ocr_frames.thumbnail_path`.

### 10.7 Manual screen capture override

`POST /v1/capture/enable-screen-override`:

1. If an active session exists: update `capture_sessions.screen_capture_override_until = now + duration_minutes * 60`
2. If no active session: create a new `manual_screen` session, start audio capture (same pipeline), set override
3. OCR gating now reports `should_capture = True` until the override expires
4. When override expires, gating reverts to frontmost-app check
5. If the manual session was created in step 2 and the override was its only reason to exist, end the session too

Timer management via the scheduler (§11.2) — scheduled stop fires when `screen_capture_override_until` is reached.

### 10.8 OCR serving

`GET /v1/ocr?start=X&end=Y&min_confidence=0.5` returns frames in the range:

```python
async def get_ocr_frames(
    start: float, end: float, min_confidence: float = 0.5, app_bundle: str | None = None
) -> OCRResponse:
    frames = await queries.get_ocr_frames_in_range(
        db, start=start, end=end, min_confidence=min_confidence, app_bundle=app_bundle
    )
    return OCRResponse(frames=[
        OCRFrame(
            ts=f.ts,
            app_bundle=f.app_bundle,
            display_id=f.display_id,
            text=f.text,
            confidence=f.avg_confidence,
            thumbnail_url=f"/v1/ocr/thumbnail/{f.id}" if f.thumbnail_path else None,
        )
        for f in frames
    ])
```

Thumbnails served via `GET /v1/ocr/thumbnail/{frame_id}` — streams the JPEG from disk with 401 if the frame doesn't exist or has no thumbnail.

---

## 11. Scheduler

### 11.1 Responsibilities

The scheduler is the brain of calendar-driven capture:

1. Poll Aegis's `/api/meetings/upcoming` endpoint every 60 seconds
2. Maintain a local view of near-future events with their exclusion status
3. Reconcile scheduled timers against the event list on each poll
4. Fire session-start timers 60 seconds before each non-excluded event
5. Handle adjacency: if a session end is within 5 minutes of the next session start, extend the current session rather than stop-and-start
6. Handle the 5:30 PM hard stop for continuous mode (timezone-aware)
7. Fire the 4-hour prompt for continuous mode
8. Handle pause-until logic
9. Gracefully degrade when Aegis is unreachable

The scheduler does not directly control streams; it emits session-start and session-end events that the capture orchestrator consumes.

### 11.2 Implementation sketch

```python
# helios/src/helios/scheduler/scheduler.py
class Scheduler:
    def __init__(
        self,
        clock: Clock,
        calendar: CalendarClient,
        orchestrator: CaptureOrchestrator,
        db: DatabasePool,
        config: SchedulerConfig,
        capture_config: CaptureConfig,
    ):
        self._clock = clock
        self._calendar = calendar
        self._orchestrator = orchestrator
        self._db = db
        self._config = config
        self._capture_config = capture_config
        self._pause_until: float | None = None
        self._pending_timers: dict[str, asyncio.TimerHandle] = {}
        self._backoff_seconds = 10

    async def start(self) -> None:
        asyncio.create_task(self._poll_loop())
        asyncio.create_task(self._hard_stop_loop())
        asyncio.create_task(self._four_hour_prompt_loop())

    async def _poll_loop(self) -> None:
        while True:
            try:
                events = await self._calendar.get_upcoming(horizon_minutes=60)
                await self._reconcile(events)
                self._backoff_seconds = 10
            except Exception as e:
                log.warn("calendar_poll_failed", error=str(e),
                        backoff=self._backoff_seconds)
                self._set_aegis_unreachable(True)
                await asyncio.sleep(self._backoff_seconds)
                self._backoff_seconds = min(self._backoff_seconds * 2, 300)
                continue
            self._set_aegis_unreachable(False)
            await asyncio.sleep(self._config.calendar_poll_seconds)

    async def _reconcile(self, events: list[CalendarEvent]) -> None:
        """Compare current events against pending timers, add/remove/update."""
        capturable = [e for e in events if not e.is_excluded]
        groups = self._group_adjacent(capturable)
        # Cancel timers for groups no longer present
        current_ids = set(self._pending_timers.keys())
        target_ids = {self._group_key(g) for g in groups}
        for stale in current_ids - target_ids:
            self._pending_timers[stale].cancel()
            del self._pending_timers[stale]
        # Schedule new groups
        for group in groups:
            key = self._group_key(group)
            if key in self._pending_timers:
                continue  # already scheduled
            if self._pause_until and group[0].starts_at < self._pause_until:
                continue  # paused through this event
            pre_start = group[0].starts_at - self._capture_config.calendar_pre_start_seconds
            delay = max(0, pre_start - self._clock.time())
            handle = asyncio.get_event_loop().call_later(
                delay, lambda g=group: asyncio.create_task(self._start_group(g))
            )
            self._pending_timers[key] = handle

    def _group_adjacent(self, events: list[CalendarEvent]) -> list[list[CalendarEvent]]:
        """Group events whose edges are within capture.calendar_post_end_seconds.
        Each group becomes one continuous capture session."""
        sorted_events = sorted(events, key=lambda e: e.starts_at)
        groups: list[list[CalendarEvent]] = []
        buffer = self._capture_config.calendar_post_end_seconds
        pre = self._capture_config.calendar_pre_start_seconds
        for event in sorted_events:
            if groups and event.starts_at - groups[-1][-1].ends_at <= (buffer + pre):
                groups[-1].append(event)
            else:
                groups.append([event])
        return groups

    async def _start_group(self, group: list[CalendarEvent]) -> None:
        session_id = await self._orchestrator.start_session(
            kind="calendar",
            calendar_events=group,
        )
        # Schedule stop for end of last event in group + buffer
        end_ts = group[-1].ends_at + self._capture_config.calendar_post_end_seconds
        delay = max(0, end_ts - self._clock.time())
        asyncio.get_event_loop().call_later(
            delay, lambda: asyncio.create_task(
                self._orchestrator.stop_session(session_id, reason="scheduled")
            )
        )
```

### 11.3 Adjacency grouping — example

Given events:
- A: 14:00 – 14:30
- B: 14:30 – 15:00
- C: 16:00 – 16:30

With `calendar_pre_start_seconds = 60` and `calendar_post_end_seconds = 300`:

- A's end_buffer = 14:35, B's pre_start = 14:29 → gap of -6 min (they overlap with the adjacency window). Group together.
- B's end_buffer = 15:05, C's pre_start = 15:59 → gap of 54 min. C is a separate group.

Result: one session covers 13:59 – 15:05 (linked to both A and B via `session_calendar_links`). A second session covers 15:59 – 16:35.

### 11.4 Reconciliation on calendar changes

On each poll, the event list may differ from the previous poll:

- **New event added** — create a new timer
- **Event cancelled** — cancel its timer; if session already active for that event's group, keep capturing (user may still be in the meeting)
- **Event rescheduled** — cancel old timer, schedule new one; if session already active, check if the event is still part of the current group or needs a new session
- **Event exclusion added** — if excluded after initially being capturable, cancel timer; if capture is active, let it finish (don't interrupt)
- **Event details changed** (title, attendees) — no scheduler action; Aegis handles display

Reconciliation is idempotent — the same event list produces the same timer set.

### 11.5 Hard stop and 4-hour prompt

```python
async def _hard_stop_loop(self) -> None:
    """Check once per minute if the 5:30 PM hard stop should fire for
    continuous mode sessions."""
    while True:
        await asyncio.sleep(60)
        now = self._clock.now_local()
        stop_time_today = now.replace(
            hour=self._capture_config.continuous_hard_stop_hour,
            minute=self._capture_config.continuous_hard_stop_minute,
            second=0, microsecond=0,
        )
        if now >= stop_time_today:
            active = await queries.get_active_session(self._db.writer)
            if active and active.kind == "continuous":
                await self._orchestrator.stop_session(
                    active.id, reason="hard_stop_530"
                )

async def _four_hour_prompt_loop(self) -> None:
    """Every 10 seconds, check if a continuous session has been running for
    a multiple of 4 hours without a user response."""
    while True:
        await asyncio.sleep(10)
        active = await queries.get_active_session(self._db.writer)
        if not active or active.kind != "continuous":
            continue
        elapsed = self._clock.time() - active.started_at
        # Fire at 4h, 8h, 12h ...
        if self._should_prompt_now(active, elapsed):
            await self._notifier.send_four_hour_prompt(active.id)
            # Start timeout timer
            asyncio.get_event_loop().call_later(
                self._capture_config.continuous_prompt_timeout_seconds,
                lambda sid=active.id: asyncio.create_task(
                    self._prompt_timeout_stop(sid)
                ),
            )
```

The notification is sent from the menu bar process (if running) or the daemon (fallback). Response comes back via `POST /v1/capture/prompt-response` with `continue: true | false`. On `false` or timeout, session stops with reason `4hr_prompt_stop`.

### 11.6 Pause-until

```python
async def set_pause(self, until_ts: float) -> None:
    self._pause_until = until_ts
    # Cancel timers for events during the pause window
    for key, handle in list(self._pending_timers.items()):
        event_start = self._timer_start_ts(key)
        if event_start < until_ts:
            handle.cancel()
            del self._pending_timers[key]
    # Schedule a resume timer
    delay = max(0, until_ts - self._clock.time())
    asyncio.get_event_loop().call_later(
        delay, lambda: asyncio.create_task(self._resume_from_pause())
    )

async def _resume_from_pause(self) -> None:
    self._pause_until = None
    # Next poll will reconcile and schedule events naturally
```

Active sessions continue through pause-until (per Q8k); only future scheduled sessions are suppressed.

### 11.7 Graceful degradation when Aegis is down

When `_poll_loop` fails, `_set_aegis_unreachable(True)` flips a flag in the daemon state. Menu bar status surfaces "⚠ Aegis unreachable — calendar capture paused." Continuous and manual modes still work (they don't depend on the calendar poll).

When Aegis comes back, the next successful poll clears the flag and reconciles against the current event list. Events that would have started during the outage are no longer in the 60-minute horizon (they're in the past), so nothing is retroactively captured — acceptable, per our earlier decisions.

### 11.8 Voice note exemptions

Voice notes are exempt from several scheduler concerns:

- **5:30 PM hard stop**: only applies to `kind=continuous`. Voice notes can be recorded at any hour.
- **4-hour prompt**: only applies to `kind=continuous`. Voice notes have their own duration cap (default 5 minutes).
- **Pause-until logic**: voice notes are explicit user action; the user can record voice notes even when the scheduler is paused.
- **Calendar adjacency grouping**: voice note sessions never extend or merge with calendar sessions. A voice note triggered during an active calendar session becomes an excerpt (per §2.2) rather than starting a new adjacent session.

Voice notes have their own duration cap enforced by a per-session timer: when started, schedule a `cap_warning` callback for `started_at + max_duration_seconds - 30` and a `force_stop` callback for `started_at + max_duration_seconds`. The first fires the soft cap notification (per `voice_note.soft_cap_notification`). If still active when the second fires, the voice note stops with reason `voice_note_cap_reached`. If the voice note is stopped or cancelled before either fires, both callbacks are cancelled.

---

## 12. Menu Bar App

### 12.1 Implementation overview

rumps-based status bar app. Runs in the user's login session, separate process from the daemon. Communicates exclusively via HTTP to `127.0.0.1:3031`.

Entry point: `python -m helios` (no `--daemon` flag → menu bar mode).

### 12.2 State polling

```python
# helios/src/helios/menubar/app.py
class HeliosApp(rumps.App):
    def __init__(self):
        super().__init__(
            "Helios",
            icon=str(ICONS_DIR / "helios_not_running_template.png"),
            template=True,
            quit_button=None,
        )
        self._client = HeliosClient(
            base_url="http://127.0.0.1:3031",
            token_path=Path.home() / ".aegis" / "capture.toml",
        )
        self._poll_timer = rumps.Timer(self._poll, 3)
        self._menu_open_poll_timer = rumps.Timer(self._poll, 1)
        self._current_state: DaemonStatus | None = None
        self._build_menu()
        self._poll_timer.start()

    def _poll(self, _timer):
        try:
            status = self._client.get_status_sync()
            self._update_ui(status)
        except ConnectionError:
            self._show_not_running()
        except AuthenticationError:
            self._show_auth_error()

    def _update_ui(self, status: DaemonStatus):
        icon_name = self._icon_for_mode(status.mode)
        self.icon = str(ICONS_DIR / icon_name)
        self._rebuild_menu(status)
```

### 12.3 Menu construction

Menu is rebuilt on each poll to reflect current state. Per §2 of the decision log, state-dependent layout:

```python
def _rebuild_menu(self, status: DaemonStatus):
    self.menu.clear()
    header = self._state_header(status)
    self.menu.add(header)
    self.menu.add(rumps.separator)
    primary = self._primary_action(status)
    for item in primary:
        self.menu.add(item)
    self.menu.add(rumps.separator)
    # Voice note (always available when daemon running and not already
    # recording a voice note). Label adapts:
    #   - "Record Voice Note" when not in any recording state
    #   - "Record Voice Note (excerpt)" when calendar/continuous recording
    #   - "Stop Voice Note" when a voice note is currently active
    if status.mode != "not_running":
        self.menu.add(self._voice_note_item(status))
    # Capture Screen submenu (always available when daemon running)
    if status.mode != "not_running":
        screen_submenu = self._screen_capture_submenu(status)
        self.menu.add(screen_submenu)
    # Pause submenu (only when armed or recording calendar/continuous)
    if status.mode in ("armed", "recording"):
        self.menu.add(self._pause_submenu())
    self.menu.add(rumps.separator)
    self.menu.add(rumps.MenuItem("Open Dashboard", callback=self._open_dashboard))
    self.menu.add(rumps.MenuItem("Preferences…", callback=self._open_preferences))
    self.menu.add(rumps.separator)
    self.menu.add(rumps.MenuItem("Quit Menu Bar", callback=rumps.quit_application))
    self.menu.add(rumps.MenuItem("Stop Helios Daemon…", callback=self._stop_daemon))
```

A sixth icon state, `recording_voice_note`, is used while a voice note is active. It overrides the `recording` icon of any concurrent calendar/continuous capture so the user sees voice-note-specific feedback. Same template-PNG approach as other states; microphone-glyph variant of the recording icon.

### 12.4 Pause submenu — dynamic labels

```python
def _pause_submenu(self) -> rumps.MenuItem:
    submenu = rumps.MenuItem("Pause Capture")
    now = datetime.now().astimezone()
    morning = self._capture_config.pause_morning_hour
    # 1 hour
    one_hr = now + timedelta(hours=1)
    submenu.add(rumps.MenuItem(
        "1 hour",
        callback=lambda _: self._pause_until(one_hr.timestamp()),
    ))
    # Morning
    target = self._next_morning(now, morning)
    label = "Until morning" if now.hour < morning else "Until tomorrow morning"
    submenu.add(rumps.MenuItem(
        label,
        callback=lambda _: self._pause_until(target.timestamp()),
    ))
    # Monday (Fri/Sat/Sun only)
    if now.weekday() >= 4:
        monday = self._next_monday(now, morning)
        submenu.add(rumps.MenuItem(
            "Until Monday morning",
            callback=lambda _: self._pause_until(monday.timestamp()),
        ))
    return submenu
```

### 12.5 Hidden diagnostic view

Option-click reveals extended information. rumps doesn't expose the Option modifier directly; implemented via a menu item that expands on click to show an alert with the diagnostic text:

```python
# Simpler alternative to NSEvent modifier tracking:
# Add a hidden "Diagnostics (⌥-click)" menu item that opens an NSAlert
# with scrollable text containing the /v1/diagnostics response.
```

Or, using native AppKit to check modifier flags at the time of menu open (preferred but more complex). For v1, implement as a visible menu item "Copy Diagnostics to Clipboard" and an "Open Diagnostics" item that opens the dashboard diagnostics page. Defer the "real" Option-click UI to a later iteration.

### 12.6 Notifications

Delegates to `helios/src/helios/menubar/notifications.py` which wraps `UNUserNotificationCenter`:

```python
async def send_four_hour_prompt(session_id: int) -> bool:
    """Send the 4-hour prompt notification with action buttons.
    Returns True if user clicked Continue, False on Stop or timeout."""
    content = UNMutableNotificationContent.alloc().init()
    content.setTitle_("Helios — Still recording?")
    content.setBody_("Continuous capture has been running for 4 hours.")
    content.setSound_(UNNotificationSound.defaultSound())
    content.setInterruptionLevel_(UNNotificationInterruptionLevelTimeSensitive)
    # ... attach "Continue" and "Stop" actions via UNNotificationCategory
    # ... await response via delegate
```

This is the most complex notification. Others (permission revoked, capture interrupted, etc.) are simpler — title + body + optional action.

**Daemon fallback.** If the menu bar process isn't running when the daemon needs to send a notification, the daemon sends it directly via `UNUserNotificationCenter` from its own process context. The 4-hour prompt action buttons still work (notifications are OS-level, not process-scoped), but attribution shows the daemon binary rather than Helios.app.

### 12.7 Onboarding window

PyObjC-based for better visual quality than rumps' built-in window. Implemented in `helios/src/helios/menubar/onboarding.py`:

```python
# Top-level class structure:
class OnboardingWindow(NSWindow):
    steps = ["welcome", "mic", "screen_recording",
             "model_download", "login_items", "complete"]
```

Each step is an `NSView` with its own content. Navigation between steps via "Continue" button. Step state persisted to `~/.aegis/capture/onboarding_state.json` for resumability.

See §15 for the detailed onboarding flow.

### 12.8 Startup behavior

On menu bar app launch:

1. Read config file, extract bearer token
2. Check onboarding state
3. If onboarding incomplete, show onboarding window (don't start polling)
4. If onboarding complete, start polling
5. If daemon not running, show "Start Capture Daemon" in menu (triggers `launchctl load`)
6. If `voice_note.hotkey_enabled = true` and Accessibility permission granted, register the global hotkey

### 12.9 Voice note hotkey listener

When `voice_note.hotkey_enabled = true` and Accessibility permission is granted, the menu bar registers a global hotkey via `pyobjc-framework-Carbon` (`RegisterEventHotKey`).

```python
class VoiceNoteHotkey:
    def __init__(self, combo: str, on_trigger: Callable):
        self._combo = self._parse_combo(combo)
        self._on_trigger = on_trigger
        self._registered = False

    def register(self) -> bool:
        if not self._is_accessibility_granted():
            return False
        # Use Carbon RegisterEventHotKey
        ...

    def _is_accessibility_granted(self) -> bool:
        return AXIsProcessTrusted()
```

The hotkey is **toggle behavior**: first press starts recording, second press stops. Matches user expectations and avoids needing a separate stop hotkey.

If Accessibility permission is not granted at startup, registration is silently deferred. A periodic check (every 5 minutes) re-attempts registration if permission becomes available later. If permission is revoked at runtime, the hotkey silently stops working until the user re-grants and the next periodic check succeeds.

The trigger callback checks current voice note state via `/v1/voice-note/active`; if active, sends stop; if not, sends start.

### 12.10 Voice note floating indicator

When a voice note is recording, the menu bar app displays a small floating window showing recording state.

PyObjC NSWindow:
- `level: NSStatusWindowLevel` (above normal windows but below alerts)
- `styleMask: [.borderless]`
- `backgroundColor: NSColor.clear` with custom NSView for content
- `ignoresMouseEvents: false` for the stop button
- Draggable; final position persisted to `voice_note.indicator.last_position`

Contents:
- Pulsing red dot (simple timer-based redraw, CPU-cheap)
- Elapsed time (MM:SS)
- Audio level meter (small bar, updated 10× per second from mic samples — daemon exposes a current-RMS field on `/v1/voice-note/active`)
- Stop button

Position: configurable via `voice_note.indicator.floating_pill_position`. Default `top_right` (10px from top-right corner of primary display).

Color shifts amber when `approaching_cap=true` from the active endpoint.

The indicator is created when a voice note start succeeds (regardless of trigger source) and dismissed when the voice note stops or is cancelled. Polls `/v1/voice-note/active` every 250ms.

### 12.11 Voice note floating save window

After `/v1/voice-note/stop` returns successfully, the menu bar app opens a floating save window for transcript review and attachment confirmation.

PyObjC NSWindow, ~480×360, focused (accepts keyboard input), positioned near the indicator's last position. Modal-like but not technically modal — clicking elsewhere on screen treats as auto-save.

Layout:

```
┌──────────────────────────────────────────────────────┐
│  Voice Note — 0:34                              [×]  │
├──────────────────────────────────────────────────────┤
│                                                      │
│  Note to self, follow up with Sarah about the Q2     │
│  budget proposal by Friday.                          │
│                                                      │
│  (transcript is read-only; edit later in dashboard)  │
│                                                      │
├──────────────────────────────────────────────────────┤
│  Suggested attachments:                              │
│                                                      │
│  ☑ Sarah Lin (person)                                │
│  ☐ Q2 Budget (workstream)                            │
│                                                      │
│  + Add attachment...                                 │
│                                                      │
├──────────────────────────────────────────────────────┤
│  [ Discard ]            [ Save (auto-save in 8s) ]   │
└──────────────────────────────────────────────────────┘
```

Behavior:

- Transcript is read-only. Inline edits happen later via the Aegis voice note detail page.
- Suggested attachments come from Aegis's entity resolver. The save window calls `POST /api/voice-notes/preview-attachments` with the transcript text immediately on open; suggestions populate when ready.
- Auto-save countdown starts at `voice_note.auto_save_timeout_seconds` (default 10) and counts down on the Save button label.
- Any user interaction (checking/unchecking a suggestion, clicking "Add attachment") cancels the auto-save countdown — engaged users get unbounded time.
- "Save" sends `POST /api/voice-notes` to Aegis with the confirmed attachments.
- "Discard" sends `POST /v1/voice-note/cancel` if the voice note hasn't been saved yet (which it hasn't — voice notes only persist to Aegis on Save), then closes the window.
- "× close button" or click-away both treated as auto-save (saves with currently-checked attachments).

If the save flow fails (Aegis unreachable, validation error), the window stays open with an error message and a Retry button. The voice note's transcript is still in Helios SQLite; nothing is lost.

The "Add attachment" picker queries Aegis for matching people, workstreams, asks via Aegis's existing search endpoint (or a small additional endpoint if needed).

---

## 13. Dashboard

### 13.1 Location

The Helios dashboard lives inside Aegis's existing web app, at routes under `/helios`. All six pages are server-rendered Jinja2 templates consistent with Aegis's existing patterns.

### 13.2 Route structure

```python
# aegis/web/routes/helios.py (new file)
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/helios", tags=["helios"])

@router.get("", response_class=HTMLResponse)
async def helios_overview(request: Request): ...

@router.get("/sessions", response_class=HTMLResponse)
async def helios_sessions(request: Request): ...

@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def helios_session_detail(request: Request, session_id: int): ...

@router.get("/calendar", response_class=HTMLResponse)
async def helios_calendar(request: Request): ...

@router.get("/diagnostics", response_class=HTMLResponse)
async def helios_diagnostics(request: Request): ...

@router.get("/settings", response_class=HTMLResponse)
async def helios_settings(request: Request): ...

# HTMX partial endpoints for live updates
@router.get("/overview/status-partial", response_class=HTMLResponse)
async def overview_status_partial(request: Request): ...

@router.post("/diagnostics/actions/restart")
async def diagnostics_restart(): ...

@router.post("/diagnostics/actions/flush-queues")
async def diagnostics_flush_queues(): ...

@router.post("/diagnostics/actions/test-capture")
async def diagnostics_test_capture(): ...

@router.post("/settings/diarization/validate-token")
async def settings_validate_hf_token(request: Request): ...

# ... etc
```

Routes compose Helios API data with Aegis DB data (§16.2).

### 13.3 Template organization

```
aegis/web/templates/helios/
├── base.html              # extends aegis base, adds Helios sidebar section
├── overview.html
├── sessions_list.html
├── session_detail.html
├── calendar.html
├── diagnostics.html
├── settings.html
├── _partials/
│   ├── status_pill.html
│   ├── session_row.html
│   ├── transcript_segment.html
│   ├── coverage_bar.html
│   └── speaker_setup_wizard_step.html
```

HTMX patterns consistent with Aegis:
- `hx-get` for partial fragment loads
- `hx-trigger="every 5s"` for live polling on the overview page
- `hx-swap="outerHTML"` for replacing updated fragments

### 13.4 Overview page

Primary landing page. Shows:

- **Status pill** at top: mode + active session (if any) or next event
- **System health row**: daemon status, permissions, component statuses
- **Today timeline**: chronological list of today's meetings with capture status
- **Today's voice notes**: list of voice notes recorded today, with link-throughs to Aegis at `/voice-notes/{id}`. Voice note pages live in Aegis (alongside meetings, people, workstreams), not duplicated in Helios.
- **Upcoming section**: next 3–5 events with capture plan

HTMX polling on `_partials/status_pill.html` every 5 seconds keeps the top of the page live without reloading the full page.

### 13.5 Session detail page

Three tabs:

1. **Transcript** — time-ordered segments with speaker labels. Speaker `SPEAKER_00` resolved to a person's name via the heuristic in §16.4. Click a segment to see word-level timestamps.
2. **OCR** — frames in the session's time window, grouped by app. Click a frame to see thumbnail (if available).
3. **Audio** — list of WAV files with in-browser `<audio>` player. Each row: timestamp, channel, duration, link.

Actions panel:
- "Re-transcribe this session" → `POST /v1/sessions/{id}/re-transcribe`
- "Re-run diarization" → `POST /v1/sessions/{id}/re-diarize`
- "Delete this session" → `DELETE /v1/sessions/{id}` (with confirmation)
- "Export transcript" → downloads as markdown, txt, or json
- "Open linked meeting in Aegis →" (only if linked to a calendar event)

### 13.6 Diagnostics page

Full state view using `GET /v1/diagnostics`:

- **System state**: version, PID, uptime, memory, CPU
- **Permissions**: mic + screen recording + last check time
- **Component status**: table of transcription/diarization/OCR with actions
- **Queues**: pending and failed counts with drill-down
- **Recent events**: last 100 from `daemon_events`
- **Error aggregation**: last 7 days grouped by event type with counts
- **Storage**: disk usage broken down by category

Action buttons:
- **Copy Diagnostics** — generates the clipboard text block (§13.10)
- **Download Diagnostic Bundle** — tar.gz with logs + events + config (redacted)
- **Trigger Test Capture** — runs the 60s self-test
- **Restart Daemon** — confirm, then `POST /v1/diagnostics/restart`
- **Flush Queues** — `POST /v1/diagnostics/flush-queues`
- **Reload Component** — dropdown to pick component, triggers reload

### 13.7 Settings page

Form UI over the TOML config. Sections mirror the TOML structure:

- Capture mode
- Meeting exclusion keywords (add/remove)
- Meeting apps for OCR (add/remove)
- Retention policy
- Transcription settings
- **Voice notes** (enable, max duration, auto-save delay, default save action, hotkey enable + combo, indicator preferences)
- **Speaker identification (the guided HF walkthrough, §15.5)**
- Advanced (chunk duration, silence threshold, API port, token regenerate)

The voice notes section's "Enable global hotkey" toggle includes the Accessibility permission flow:
- When toggled on, check `AXIsProcessTrusted` via async API
- If not granted, show System Settings deep-link with "Click here when granted" prompt
- After granted, send config update to enable hotkey
- Daemon hot-reload picks up the change; menu bar registers the hotkey on next poll

Each field indicates whether changes apply immediately or require daemon restart. On save, POST the changes to `/helios/settings`, which writes the TOML and invokes the hot-reload mechanism if applicable.

### 13.8 Calendar page

7-day forward view of meetings with capture plans:

- Events fetched from Aegis DB (`meetings` table, filtered to upcoming)
- Each row: time, title, capture status (✓ scheduled, ⊘ excluded, override available)
- Per-meeting exclusion toggle: adds a `helios_exclude` flag to the meeting (new field in Aegis DB — see §16.6)
- Clicking a row opens Aegis's meeting detail page

### 13.9 Authentication flow

Aegis backend reads the Helios bearer token from `~/.aegis/capture.toml` on startup:

```python
# aegis/clients/helios.py (new file)
class HeliosClient:
    def __init__(self, base_url: str, token_path: Path):
        self._base_url = base_url
        self._token_path = token_path
        self._token = self._load_token()
        self._http = httpx.AsyncClient(timeout=30)

    def _load_token(self) -> str:
        with open(self._token_path, "rb") as f:
            data = tomllib.load(f)
        return data["api"]["bearer_token"]

    async def _request(self, method: str, path: str, **kwargs):
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._token}"
        response = await self._http.request(
            method, self._base_url + path, headers=headers, **kwargs
        )
        if response.status_code == 401:
            # Token may have been regenerated
            self._token = self._load_token()
            headers["Authorization"] = f"Bearer {self._token}"
            response = await self._http.request(
                method, self._base_url + path, headers=headers, **kwargs
            )
        response.raise_for_status()
        return response
```

Dashboard routes use this client for all Helios API calls. User never sees the token.

### 13.10 Copy Diagnostics output format

```
Helios Diagnostics — 2026-04-22 14:32:04 EDT
Version: 0.1.0
Daemon PID: 4821 (uptime 2d 14h 12m)
Memory: 342 MB
CPU (5min avg): 8%

Permissions:
  Microphone: granted (last checked 30s ago)
  Screen Recording: granted (last checked 30s ago)

Components:
  audio_capture: ok
  transcription: ok
  diarization: unavailable (token_missing)
  ocr: ok

Active Session: 142 (calendar, started 14m ago)
  Linked events: Platform review
Next Event: Architecture sync at 15:30

Last Chunks:
  Mic:    4s ago, 30s duration, chunk_id=4821
  System: 4s ago, 30s duration, chunk_id=4822

Queues:
  Transcription: 2 pending, 0 failed (24h)
  Diarization:   0 pending, 0 failed (24h)

Disk:
  Audio: 4.2 GB (oldest: 6 days)
  Thumbnails: 18 MB
  Database: 340 MB

Recent Events (last 20):
  14:32:04  info   scheduler     session_started  id=142
  14:31:42  info   chunker       chunk_flushed    session=142 channel=mic
  ...

Configuration (non-sensitive):
  API port: 3031
  Model: distil-large-v3
  Chunk seconds: 30
  Retention days: 7
  Meeting apps: 5 configured
```

Sensitive values (bearer token, HF token, file paths with usernames) redacted.

### 13.11 Diagnostic bundle

`POST /v1/diagnostics/bundle` generates a tar.gz at a temporary path, returns the path. Dashboard serves it as a download.

Contents:
- `diagnostics.txt` — the Copy Diagnostics output
- `logs/helios.log.gz` — last 24 hours compressed
- `events.json` — last 100 daemon_events rows
- `config.toml.redacted` — config with token redacted
- `system.txt` — macOS version, hardware model, audio devices, display configuration

Bundle is deleted from disk 1 hour after creation (cleanup job).

---

## 14. Notifications

### 14.1 Which notifications fire

| Event | Notify? | Style | Action Buttons | Sound |
|-------|---------|-------|----------------|-------|
| Capture started (calendar) | No | — | — | — |
| Capture ended (calendar) | No | — | — | — |
| Continuous mode started | No | — | — | — |
| **4-hour prompt** | **Yes** | Alert (persistent) | Continue / Stop | Yes |
| Continuous auto-stop 5:30 PM | Yes | Banner | — | No |
| Manual screen capture started | No | — | — | — |
| Manual screen capture ended | Yes | Banner | — | No |
| Capture interrupted mid-meeting | Yes | Banner | Open Helios | Yes |
| Capture resumed after interruption | No | — | — | — |
| **Permission revoked** | **Yes** | Alert | Open Settings | Yes |
| Permission restored | No | — | — | — |
| Missed meeting (permission error) | Yes | Banner | — | No |
| Transcription queue backlog high | No (dashboard only) | — | — | — |
| Daemon crashed / restarted | No | — | — | — |
| Cleanup completed | No | — | — | — |
| Onboarding complete | No | — | — | — |
| Voice note approaching duration cap (30s warning) | Yes | Banner | Stop, Continue | No |
| Voice note auto-stopped at cap | Yes | Banner | Open save window | No |
| Voice note save failed | Yes | Banner | Retry | Yes |
| Voice note hotkey registration failed | Yes | Banner | Open Accessibility settings | No |

### 14.2 Time-sensitive flag

The three actionable notifications (4-hour prompt, capture interrupted, permission revoked) are marked as time-sensitive via `UNNotificationInterruptionLevelTimeSensitive`. This allows them to bypass Focus modes — important when capture issues arise during a meeting itself.

### 14.3 Implementation

`helios/src/helios/menubar/notifications.py` wraps `UNUserNotificationCenter`. Separate daemon-side implementation in `helios/src/helios/notifications/notify.py` provides the fallback path when the menu bar isn't running.

The menu bar app registers notification categories at startup:

```python
def register_notification_categories():
    continue_action = UNNotificationAction.actionWithIdentifier_title_options_(
        "CONTINUE", "Continue recording", UNNotificationActionOptionForeground
    )
    stop_action = UNNotificationAction.actionWithIdentifier_title_options_(
        "STOP", "Stop", UNNotificationActionOptionDestructive
    )
    four_hour_category = UNNotificationCategory.categoryWithIdentifier_actions_intentIdentifiers_options_(
        "FOUR_HOUR_PROMPT", [continue_action, stop_action], [], 0
    )
    # ... more categories
    center = UNUserNotificationCenter.currentNotificationCenter()
    center.setNotificationCategories_({four_hour_category, ...})
```

Action buttons invoke callbacks that `POST` to the daemon's API. For the 4-hour prompt specifically:

- Continue → `POST /v1/capture/prompt-response` with `{"continue": true}` → scheduler reschedules next prompt for +4h
- Stop → `POST /v1/capture/prompt-response` with `{"continue": false}` → scheduler stops session with reason `4hr_prompt_stop`
- No response within timeout → scheduler's timeout timer fires, stops session

### 14.4 OS notification settings

Users control notifications via System Settings → Notifications → Helios. Helios does not duplicate this in its own settings. Exception: the "enable sound on prompts" toggle in Helios settings directly influences notification construction (whether `setSound_` is called).

---

## 15. Onboarding Flow

### 15.1 Trigger

Onboarding launches automatically on first menu bar app launch when `~/.aegis/capture/onboarding_state.json` doesn't exist or indicates incomplete. Can be re-opened from Settings → "Re-run Onboarding" for troubleshooting.

### 15.2 Steps

1. **Welcome** — brief intro: what Helios does, what permissions it needs, how long setup takes (~5 minutes).
2. **Microphone permission** — button triggers `AVCaptureDevice.requestAccessForMediaType_` for audio. Status indicator. If denied, deep-link to System Settings.
3. **Screen Recording permission** — button triggers `SCShareableContent.current` (which prompts). Status indicator uses `CGPreflightScreenCaptureAccess`. Detects "granted, needs restart" state.
4. **Restart Helios** (conditional) — appears only if screen recording was just granted. "Click to restart — Helios will re-open automatically." Runs `os.execv`.
5. **Transcription model download** — WhisperX download with progress bar (see §15.3). Blocking.
6. **Add Helios to Login Items** — button opens System Settings → General → Login Items. Instructional text. User clicks "Done" when ready.
7. **Complete** — "Helios is ready." Shows dashboard link and a note about optional speaker identification setup (Issue #10, Q10c).

Each step is an `NSView` within a single `NSWindow`. State machine in Python drives transitions.

### 15.3 Model download step

```python
async def download_whisper_model():
    """Run the download helper script as subprocess, parse progress lines."""
    proc = await asyncio.create_subprocess_exec(
        "python", "-m", "helios.scripts.download_whisper",
        "--model", config.transcription.model,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    async for line in proc.stdout:
        event = json.loads(line)
        if event["type"] == "progress":
            update_progress_bar(event["pct"], event["eta_seconds"])
        elif event["type"] == "done":
            verify_model_loads()
            return
        elif event["type"] == "error":
            show_error(event["detail"])
            return
```

`helios/src/helios/scripts/download_whisper.py`:

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
    # Verify with a 1s synthetic test
    verify(snapshot_path)
    print(json.dumps({"type": "done", "path": snapshot_path}))
```

Verification runs a 1-second synthetic sine wave through WhisperX to confirm the model loads and runs.

### 15.4 Onboarding state persistence

```json
// ~/.aegis/capture/onboarding_state.json
{
    "started_at": 1712345000.0,
    "completed_at": null,
    "steps": {
        "welcome": {"completed": true, "ts": 1712345010.0},
        "mic": {"completed": true, "ts": 1712345040.0},
        "screen_recording": {"completed": true, "ts": 1712345070.0},
        "restart": {"completed": true, "ts": 1712345080.0},
        "model_download": {"completed": false, "ts": null, "progress_pct": 42},
        "login_items": {"completed": false, "ts": null}
    }
}
```

On re-launch during an incomplete flow, jumps to the first incomplete step.

### 15.5 Speaker identification setup (Dashboard)

Not part of required onboarding — lives in dashboard Settings → "Speaker identification" as a guided 5-step wizard. Full spec in Appendix A under Issue #10.

Key points:

- HF account creation (external link)
- Token generation instructions with link to HF token page
- License acceptance for three models (checkboxes the user ticks after visiting each link)
- Token paste + validation (`huggingface_hub.whoami`)
- Model download
- Resumable via `diarization.setup_progress` in config

### 15.6 Voice notes onboarding

Voice notes are not added to the required onboarding flow. The feature is enabled by default but its global hotkey is opt-in via dashboard settings (because it requires Accessibility permission).

The first time the user opens the dashboard after onboarding completes, the overview page shows a small dismissible callout: "💡 New: voice notes — record quick thoughts between meetings. [Try it →]" The button takes them to `/voice-notes` with an inline "Record your first voice note" CTA.

Users who never visit the dashboard still discover voice notes via the menu bar's "Record Voice Note" item, available as soon as the daemon is running.

---

## 16. Aegis Integration Points

This section lists every change to the existing Aegis codebase required for Helios integration.

### 16.1 File: `aegis/ingestion/screenpipe.py` → `aegis/ingestion/helios.py`

**Action:** Rename and rewrite.

The old `ScreenpipeClient` with `/health` and `/search` endpoints becomes `HeliosClient` using the `/v1/*` API.

New file `aegis/ingestion/helios.py`:

```python
class HeliosClient:
    """Client for the Helios capture daemon's HTTP API.
    Used by meeting_detector for transcript fetching."""

    def __init__(self, base_url: str, token_path: Path, http: httpx.AsyncClient):
        self._base_url = base_url.rstrip("/")
        self._token_path = token_path
        self._token = self._load_token()
        self._http = http

    async def health_check(self) -> bool:
        try:
            r = await self._http.get(f"{self._base_url}/v1/health", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    async def get_transcript_for_meeting(
        self, meeting: Meeting
    ) -> HeliosTranscript | None:
        """Fetch the transcript for a meeting's time window.
        Returns None if no capture was made (transcript_status will be no_audio)."""
        start = (meeting.start_time - timedelta(seconds=300)).timestamp()
        end = (meeting.end_time + timedelta(seconds=300)).timestamp()
        try:
            r = await self._authed_request(
                "GET", "/v1/audio", params={"start": start, "end": end}
            )
            data = r.json()
            if not data["segments"]:
                return None
            return HeliosTranscript.from_api(data)
        except HeliosUnavailable:
            return None

    async def get_ocr_for_meeting(self, meeting: Meeting) -> list[OCRFrame]:
        start = meeting.start_time.timestamp()
        end = meeting.end_time.timestamp()
        r = await self._authed_request(
            "GET", "/v1/ocr", params={"start": start, "end": end}
        )
        return [OCRFrame.from_api(f) for f in r.json()["frames"]]

    async def _authed_request(self, method: str, path: str, **kwargs):
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._token}"
        r = await self._http.request(
            method, self._base_url + path, headers=headers, **kwargs
        )
        if r.status_code == 401:
            self._token = self._load_token()
            headers["Authorization"] = f"Bearer {self._token}"
            r = await self._http.request(
                method, self._base_url + path, headers=headers, **kwargs
            )
        r.raise_for_status()
        return r
```

`HeliosTranscript` is a data class that encapsulates what the existing Aegis code expects. It contains:
- `segments`: list of `(ts, speaker, text)` tuples
- `coverage_pct`: computed from the coverage field
- `unavailable_ranges`: for gap reporting
- `formatted_text`: the stitched, speaker-labeled string Aegis stores in `meetings.transcript_text`

### 16.2 File: `aegis/clients/helios.py` (new)

This is the shared client used by **both** the ingestion layer and the dashboard routes. The `HeliosClient` shown in §16.1 is actually this shared client. Move the class to `aegis/clients/helios.py` and have `aegis/ingestion/helios.py` import it. The ingestion module is just the place where transcript-fetching logic lives; the client itself belongs to a shared location.

Final layout:
- `aegis/clients/helios.py` — `HeliosClient` class
- `aegis/ingestion/helios.py` — transcript fetching logic, uses `HeliosClient`
- `aegis/web/routes/helios.py` — dashboard routes, use `HeliosClient`

### 16.3 File: `aegis/ingestion/meeting_detector.py`

**Action:** Simplify. Remove transcript stitching logic. Keep back-to-back detection, buffer padding, overage detection, and status determination.

Changes:

1. **Remove `_stitch_transcript()` method.** Helios serves pre-stitched transcripts, so local stitching is dead code.
2. **Replace calls to `ScreenpipeClient.get_audio()`** with `HeliosClient.get_transcript_for_meeting()`.
3. **Update status determination:** use `HeliosTranscript.coverage_pct` directly instead of computing from audio chunks.
4. **Keep overage detection:** if Helios says coverage extends past scheduled end, extend Aegis's window.
5. **Keep back-to-back detection:** Aegis still needs to know which meeting a given transcript segment belongs to.
6. **Update `build_transcript()`:**

```python
# aegis/ingestion/meeting_detector.py (new implementation)
async def build_transcript(self, meeting: Meeting) -> None:
    transcript = await self._helios.get_transcript_for_meeting(meeting)
    if transcript is None:
        meeting.transcript_status = "no_audio"
        meeting.transcript_text = None
    else:
        meeting.transcript_status = self._status_from_coverage(transcript.coverage_pct)
        meeting.transcript_text = transcript.formatted_text
        meeting.screen_context = await self._fetch_ocr_context(meeting)
    await self._repo.save(meeting)

def _status_from_coverage(self, pct: float) -> str:
    if pct >= 50:
        return "captured"
    if pct > 0:
        return "partial"
    return "no_audio"

async def _fetch_ocr_context(self, meeting: Meeting) -> dict:
    frames = await self._helios.get_ocr_for_meeting(meeting)
    # Group by app, concatenate text, store as JSONB
    grouped = defaultdict(list)
    for frame in frames:
        grouped[frame.app_bundle].append({"ts": frame.ts, "text": frame.text})
    return dict(grouped)
```

### 16.4 Speaker-name resolution

When rendering a transcript in the dashboard session detail page, `SPEAKER_00` labels need to become human names. Logic lives in `aegis/clients/helios.py`:

```python
async def resolve_speaker_names(
    transcript: HeliosTranscript, meeting: Meeting | None,
    people_repo: PeopleRepository,
) -> HeliosTranscript:
    """Attach human names to SPEAKER_XX labels based on heuristics."""
    if meeting is None:
        return transcript  # no attendee context

    attendees = await people_repo.get_meeting_attendees(meeting.id)
    system_speakers = {
        seg.speaker for seg in transcript.segments
        if seg.speaker and seg.speaker.startswith("SPEAKER_")
    }

    # Heuristic: if N unique speakers and N-1 non-user attendees, map in order
    # of first appearance.
    non_user_attendees = [a for a in attendees if a.is_external or a.email != meeting.organizer_email]
    if len(system_speakers) == len(non_user_attendees):
        first_appearance = {}
        for seg in transcript.segments:
            if seg.speaker and seg.speaker.startswith("SPEAKER_"):
                first_appearance.setdefault(seg.speaker, seg.start)
        speaker_order = sorted(first_appearance.keys(), key=first_appearance.get)
        mapping = dict(zip(speaker_order, non_user_attendees))
        for seg in transcript.segments:
            if seg.speaker in mapping:
                seg.speaker_display_name = mapping[seg.speaker].display_name
    return transcript
```

### 16.5 File: `aegis/web/routes/api.py` (or equivalent, new)

**Action:** Add `GET /api/meetings/upcoming` endpoint. Locate in whichever existing route file Aegis uses for JSON endpoints (the summary indicates there are a few under `aegis/web/routes/`; Claude Code picks the right one at implementation time).

```python
@router.get("/api/meetings/upcoming")
async def meetings_upcoming(
    horizon_minutes: int = Query(60, ge=1, le=1440),
    repo: MeetingsRepository = Depends(get_meetings_repo),
) -> UpcomingMeetingsResponse:
    now = datetime.utcnow()
    until = now + timedelta(minutes=horizon_minutes)
    meetings = await repo.get_in_range(now, until)
    return UpcomingMeetingsResponse(events=[
        UpcomingMeetingEvent(
            calendar_event_id=m.calendar_event_id,
            title=m.title if not m.is_excluded else "(excluded)",
            starts_at=m.start_time.timestamp(),
            ends_at=m.end_time.timestamp(),
            is_online_meeting=m.is_online_meeting,
            is_excluded=m.is_excluded,
            exclusion_reason=m.exclusion_reason,
            series_master_id=m.recurring_series_id,
            attendee_count=len(m.attendees),
        )
        for m in meetings
    ])
```

Schemas imported from `shared/meetings.py` so Helios and Aegis share the types.

No authentication required — consistent with Aegis's other read endpoints.

### 16.6 File: `aegis/db/models.py`

**Action:** Add `helios_exclude` boolean to the `Meeting` model. Used by the per-meeting override from the dashboard Calendar page. Default False.

Alembic migration:

```python
# alembic/versions/XXXXXX_add_helios_exclude_to_meetings.py
def upgrade():
    op.add_column(
        "meetings",
        sa.Column("helios_exclude", sa.Boolean, nullable=False, server_default="false"),
    )

def downgrade():
    op.drop_column("meetings", "helios_exclude")
```

The `/api/meetings/upcoming` endpoint honors `helios_exclude` OR `is_excluded` in the `is_excluded` field of the response.

### 16.7 File: `aegis/config.py`

**Action:** Add Helios config values. Remove or deprecate Screenpipe values.

```python
class Settings(BaseSettings):
    # ... existing fields ...

    # Helios (replaces screenpipe_url and polling_screenpipe_seconds)
    helios_url: str = "http://127.0.0.1:3031"
    helios_token_path: Path = Path("~/.aegis/capture.toml").expanduser()
    helios_heartbeat_seconds: int = 60
    helios_heartbeat_timeout_seconds: int = 5

    # Legacy (mark deprecated, unused after migration):
    # screenpipe_url: str = "http://localhost:3030"  # REMOVED
    # polling_screenpipe_seconds: int = 300          # REMOVED
```

### 16.8 File: `aegis/ingestion/poller.py`

**Action:** Add Helios heartbeat polling loop.

```python
async def helios_heartbeat_loop(
    client: HeliosClient,
    repo: SystemHealthRepository,
    interval_seconds: int,
) -> None:
    while True:
        ok = await client.health_check()
        await repo.record_heartbeat(
            component="helios",
            status="ok" if ok else "down",
            timestamp=datetime.utcnow(),
        )
        if not ok:
            # Fire macOS notification on transition to down
            await _notify_if_transition("helios", "down")
        await asyncio.sleep(interval_seconds)
```

Invoked from Aegis's existing polling orchestrator alongside the email, Teams, and calendar loops.

### 16.9 File: `aegis/main.py` or equivalent startup file

**Action:** Register the new Helios router, initialize the `HeliosClient`.

```python
# In the FastAPI app factory:
from aegis.web.routes import helios as helios_routes

app.include_router(helios_routes.router)

# In the lifespan context manager:
helios_client = HeliosClient(
    base_url=settings.helios_url,
    token_path=settings.helios_token_path,
    http=shared_http_client,
)
app.state.helios_client = helios_client
```

### 16.10 File: `CLAUDE.md` (Aegis's existing spec)

**Action:** Prepend a short pointer at the top:

```markdown
> **Note:** This document describes Aegis. A companion document `HELIOS.md`
> describes the Helios capture subsystem, which replaces the Screenpipe
> integration described in §2 and §6 of this document. Where this spec
> references Screenpipe, consult HELIOS.md for the actual implementation.
> See HELIOS.md §16 for the specific Aegis changes required.
```

Existing Screenpipe references in the spec body are not modified; they're superseded by HELIOS.md.

### 16.11 Summary of Aegis changes

| Change | File(s) | Complexity |
|--------|---------|-----------|
| Rename Screenpipe client to Helios | `aegis/ingestion/screenpipe.py` → `aegis/ingestion/helios.py`, `aegis/clients/helios.py` (new) | Low |
| Simplify meeting_detector | `aegis/ingestion/meeting_detector.py` | Medium (delete stitching, rewire) |
| Add upcoming meetings API | `aegis/web/routes/api.py` (add endpoint) | Low |
| Add Helios dashboard routes | `aegis/web/routes/helios.py` (new), `aegis/web/templates/helios/` (new) | Medium |
| Add `helios_exclude` column | `aegis/db/models.py`, `alembic/versions/` | Low |
| Update config | `aegis/config.py` | Low |
| Add heartbeat loop | `aegis/ingestion/poller.py` | Low |
| Wire up client in app startup | `aegis/main.py` | Low |
| Update CLAUDE.md pointer | `CLAUDE.md` | Trivial |
| **Voice notes integration** | See §16.12 | Medium-High |

Estimated total Aegis modification: 4-5 days of Claude Code work, with voice notes integration adding ~2 days on top of the core meeting capture integration. Most of the effort is in new templates and the voice note extractor.

### 16.12 Voice notes integration

Voice notes are first-class Aegis entities — alongside meetings, people, workstreams, asks. They get their own database tables, model file, repository, web routes, templates, and extraction pipeline path. Existing Aegis behavior is unchanged; voice notes integrate alongside.

#### Database changes

Two new tables, no modifications to existing tables.

**`voice_notes`:**

```sql
CREATE TABLE voice_notes (
    id SERIAL PRIMARY KEY,
    helios_voice_note_id INT UNIQUE NOT NULL,
    helios_session_id INT NOT NULL,
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP NOT NULL,
    duration_seconds REAL NOT NULL,
    transcript_text TEXT NOT NULL,
    transcript_text_edited TEXT,
    triggered_by VARCHAR(20) NOT NULL,         -- 'menu_bar', 'hotkey', 'dashboard'
    source_device VARCHAR(20) NOT NULL DEFAULT 'mac',
    is_excerpt BOOLEAN NOT NULL DEFAULT FALSE,
    excerpt_of_meeting_id INT REFERENCES meetings(id) ON DELETE SET NULL,
    processing_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    -- 'pending', 'processing', 'completed', 'failed'
    embedding VECTOR(1536),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_voice_notes_started_at ON voice_notes(started_at);
CREATE INDEX idx_voice_notes_processing ON voice_notes(processing_status)
    WHERE processing_status IN ('pending', 'processing');
CREATE INDEX idx_voice_notes_excerpt ON voice_notes(excerpt_of_meeting_id)
    WHERE excerpt_of_meeting_id IS NOT NULL;
CREATE INDEX idx_voice_notes_embedding ON voice_notes
    USING ivfflat (embedding vector_cosine_ops);
```

**`voice_note_attachments`** (polymorphic many-to-many):

```sql
CREATE TABLE voice_note_attachments (
    id SERIAL PRIMARY KEY,
    voice_note_id INT NOT NULL REFERENCES voice_notes(id) ON DELETE CASCADE,
    target_type VARCHAR(20) NOT NULL,          -- 'person', 'workstream', 'ask'
    target_id INT NOT NULL,
    is_suggested BOOLEAN NOT NULL,             -- true if auto-suggested, false if user-added
    confirmed_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(voice_note_id, target_type, target_id)
);
CREATE INDEX idx_vna_target ON voice_note_attachments(target_type, target_id);
```

Polymorphic foreign keys to `people`, `workstreams`, `asks` are intentionally not enforced at the DB level — application code is responsible for cleaning up attachments when target entities are deleted. Pragmatic call; if SQL purity matters more, separate `*_attachments` tables per target type can be substituted, but the unified pattern is simpler.

Single Alembic migration `alembic/versions/XXXXXX_add_voice_notes.py` creates both tables.

#### File: `aegis/db/models.py` — additions

```python
class VoiceNote(Base):
    __tablename__ = "voice_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    helios_voice_note_id: Mapped[int] = mapped_column(unique=True)
    helios_session_id: Mapped[int]
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime]
    duration_seconds: Mapped[float]
    transcript_text: Mapped[str]
    transcript_text_edited: Mapped[str | None] = mapped_column(default=None)
    triggered_by: Mapped[str]
    source_device: Mapped[str] = mapped_column(default="mac")
    is_excerpt: Mapped[bool] = mapped_column(default=False)
    excerpt_of_meeting_id: Mapped[int | None] = mapped_column(
        ForeignKey("meetings.id", ondelete="SET NULL"), default=None,
    )
    processing_status: Mapped[str] = mapped_column(default="pending")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), default=None)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    attachments: Mapped[list["VoiceNoteAttachment"]] = relationship(
        back_populates="voice_note", cascade="all, delete-orphan",
    )
    excerpt_of_meeting: Mapped[Optional["Meeting"]] = relationship(
        foreign_keys=[excerpt_of_meeting_id],
    )

class VoiceNoteAttachment(Base):
    __tablename__ = "voice_note_attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    voice_note_id: Mapped[int] = mapped_column(
        ForeignKey("voice_notes.id", ondelete="CASCADE")
    )
    target_type: Mapped[str]
    target_id: Mapped[int]
    is_suggested: Mapped[bool]
    confirmed_at: Mapped[datetime] = mapped_column(default=func.now())

    voice_note: Mapped["VoiceNote"] = relationship(back_populates="attachments")

    __table_args__ = (
        UniqueConstraint("voice_note_id", "target_type", "target_id"),
    )
```

#### File: `aegis/db/voice_notes_repository.py` (new)

CRUD operations:

- `create(voice_note_create: VoiceNoteCreate) -> VoiceNote`
- `get_by_id(id: int) -> VoiceNote | None`
- `get_by_helios_id(helios_voice_note_id: int) -> VoiceNote | None`
- `list(filters: VoiceNoteFilters) -> list[VoiceNote]`
- `list_for_person(person_id: int) -> list[VoiceNote]`
- `list_for_workstream(workstream_id: int) -> list[VoiceNote]`
- `list_for_ask(ask_id: int) -> list[VoiceNote]`
- `list_in_range(start: datetime, end: datetime) -> list[VoiceNote]`
- `update_transcript_edit(id: int, edited_text: str) -> VoiceNote`
- `update_attachments(id: int, attachments: list[AttachmentInput]) -> VoiceNote`
- `mark_processing_status(id: int, status: str, reason: str | None = None)`
- `set_embedding(id: int, embedding: list[float])`
- `delete(id: int)`

#### File: `aegis/web/routes/api.py` (or new `voice_notes_api.py`) — additions

JSON endpoints called by the menu bar app's save window:

**`POST /api/voice-notes/preview-attachments`**

Takes a transcript, returns suggested attachments. Used by the floating save window before final save.

```json
Request:
{
    "transcript_text": "Note to self, follow up with Sarah about Q2 budget."
}

Response:
{
    "suggested_attachments": {
        "person_ids": [42],
        "workstream_ids": [11],
        "ask_ids": []
    },
    "matches": [
        {"type": "person", "id": 42, "display_name": "Sarah Lin",
         "match_text": "Sarah", "confidence": 0.85},
        {"type": "workstream", "id": 11, "display_name": "Q2 Budget Review",
         "match_text": "Q2 budget", "confidence": 0.72}
    ]
}
```

Implementation: runs the existing `aegis.processing.resolver` on the transcript, returns matches with confidence above threshold (default 0.6). Match preview text helps the save window UI show what was matched.

**`POST /api/voice-notes`**

Receives a completed voice note from the menu bar app, creates DB row, kicks off extraction.

Request body uses the `VoiceNoteCreate` schema from `shared/audio.py`:

```python
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

Response:

```json
{
    "voice_note_id": 42,
    "extraction_status": "queued"
}
```

Implementation:
1. Validate the request
2. Insert `voice_notes` row with `processing_status='pending'`
3. Insert `voice_note_attachments` rows for each confirmed attachment
4. If `is_excerpt=true`, look up matching meeting via time range and set `excerpt_of_meeting_id`
5. Enqueue to extraction pipeline
6. Return success

**`GET /api/voice-notes`**

Lists voice notes with optional filters: `start_date`, `end_date`, `person_id`, `workstream_id`, `ask_id`, `has_text` (for search). Used by Helios dashboard's link-throughs and any future API clients.

**`GET /api/voice-notes/{id}`**

JSON detail with attachments.

**`PATCH /api/voice-notes/{id}`**

Update transcript (sets `transcript_text_edited`, triggers re-extraction) or attachments.

**`DELETE /api/voice-notes/{id}`**

Removes the voice note. Audio in Helios remains under normal retention; the DB row is deleted.

**`GET /api/search?q=...&types=person,workstream,ask`**

Manual attachment search for the save window's "Add attachment" picker. May already exist in some form in Aegis (the codebase has RAG search); use or extend it.

#### File: `aegis/processing/voice_note_extractor.py` (new)

Extraction pipeline tuned for voice notes. Modeled on `meeting_extractor.py`:

- **Triage step**: is this voice note useful for extraction? Filters out things like accidental triggers or "test, test, one two three" — quick Haiku call.
- **Extract step**: structured extraction via Haiku 4.5 with a prompt tuned for voice notes:
  - Speaker is always the user (no "who said this" disambiguation)
  - Action items typically self-directed ("I need to...", "Follow up on...")
  - Less commitment-tracking than meetings (you don't make commitments to others in voice notes)
  - Decisions, mentioned people, mentioned topics, sentiment about mentioned people/topics
- **Resolve step**: entity resolution against people/workstreams (additive to suggestions already provided by `preview-attachments`)
- **Embedding**: OpenAI text-embedding-3-small on transcript
- **Workstream assignment**: same logic as meetings

Failure cases mark `processing_status='failed'` with a reason field.

#### File: `aegis/processing/pipeline.py`

Update LangGraph state machine to include voice notes alongside meetings, emails, chats:

```python
def build_pipeline():
    pipeline = StateGraph(PipelineState)
    pipeline.add_node("triage_meetings", triage_meetings)
    pipeline.add_node("triage_voice_notes", triage_voice_notes)        # NEW
    pipeline.add_node("extract_meetings", extract_meetings)
    pipeline.add_node("extract_voice_notes", extract_voice_notes)      # NEW
    # ... etc
```

Voice notes flow through the same general pattern as meetings but with the voice-note-specific extractor. The pipeline scheduler picks up voice notes with `processing_status='pending'` on its normal cadence. Voice notes are processed independently of meetings (they're not gated by transcript completion since the transcript arrives at creation time).

#### File: `aegis/web/routes/voice_notes.py` (new)

Server-rendered routes for voice note pages:

- `GET /voice-notes` — list page
- `GET /voice-notes/{id}` — detail page with editable transcript and attachments
- `PATCH /voice-notes/{id}/transcript` — update edited transcript, trigger re-extraction
- `PATCH /voice-notes/{id}/attachments` — update attachments
- `POST /voice-notes/{id}/re-extract` — manually trigger re-extraction
- `DELETE /voice-notes/{id}`

Voice notes pages live in Aegis's main UI alongside meetings, people, workstreams. They are NOT duplicated under `/helios/voice-notes/*` — the Helios dashboard's overview page links through to `/voice-notes/{id}`.

#### File: `aegis/web/templates/voice_notes/` (new)

- `list.html` — voice notes list page
- `detail.html` — voice note detail with transcript editing
- `_partials/voice_note_row.html` — reusable row for lists
- `_partials/voice_note_card.html` — reusable card for embedding in person/workstream/ask pages

#### Modified existing templates

Small additions to surface voice notes alongside other entities:

- `aegis/web/templates/people/detail.html` — add "Voice notes" section using `_partials/voice_note_card.html`
- `aegis/web/templates/workstreams/detail.html` — same
- `aegis/web/templates/asks/detail.html` — same
- `aegis/web/templates/dashboard/today.html` (or main timeline template) — mix voice notes into the chronological daily timeline alongside meetings

#### File: `aegis/intelligence/briefings.py`

Update morning, Friday, and Monday briefing generators to include voice notes from relevant time ranges. Voice notes count as user-generated context similar to meetings the user actively participated in. The morning briefing's "yesterday's notes" section pulls voice notes from yesterday alongside meeting summaries.

Briefing prompt templates (in `aegis/intelligence/prompts/` or similar) need to accept voice notes as input — minor templating change.

#### File: `aegis/chat/rag.py`

Update RAG search to include voice notes:
- Add `voice_notes` to the searchable corpus
- Embed-based retrieval against `voice_notes.embedding`
- Filter by attachments when search context includes a person or workstream
- Surface voice note results in chat answers alongside meeting and email results

#### File: `aegis/main.py` (or wherever routes are mounted)

Mount the new `voice_notes` router and `voice_notes_api` router.

#### Tests

New test modules:
- `tests/test_voice_notes_repository.py` — CRUD and relationships
- `tests/test_voice_note_extractor.py` — extraction quality on fixture transcripts
- `tests/test_voice_notes_api.py` — JSON endpoints
- `tests/test_voice_notes_routes.py` — server-rendered pages

Updated existing tests:
- `tests/test_pipeline.py` — verify voice notes flow through correctly
- `tests/test_briefings.py` — verify voice notes included in briefings
- `tests/test_rag.py` — verify voice notes searchable

#### What this does NOT change in Aegis

The following remain unchanged. Voice notes integrate alongside, not in place of, existing functionality:

- Calendar sync, meetings table, meeting extraction
- Email and Teams ingestion
- People, workstreams, asks tables and their existing flows
- Org inference, sentiment aggregation, readiness scoring
- Existing chat/RAG behavior (just gains voice notes as additional source)
- Existing dashboard pages (just gain voice note sections)
- Existing config, deployment, infrastructure

---

## 17. Retention and Cleanup

Helios retains data according to these rules:

**Raw WAV audio** is retained for 7 days (configurable via `retention.raw_audio_days`). After that, files are soft-deleted to `~/.aegis/capture/trash/` and kept for 24 hours before permanent deletion. Soft-delete provides a grace period for accidental deletion.

**Transcripts and OCR text** are kept forever. They're tiny (~100 KB/hour of meeting) and are the primary value Aegis consumes. Per-meeting deletion is available as an explicit user action from the session detail page, but nothing ages them out automatically.

**Thumbnails** (low-confidence OCR frames) follow raw-audio retention — 7 days. They're debugging artifacts, not primary data.

**Database rows** remain even when referenced WAVs are deleted. An `audio_chunks` row with a deleted WAV has its `path` column set to `NULL` during cleanup and status flipped to `archived`. This way, timestamps and transcripts remain queryable even after raw audio is gone.

Cleanup runs nightly at 3:00 AM local time (configurable). If the Mac was asleep at 3 AM, a startup catch-up pass runs cleanup on daemon launch. Safety rules prevent data loss:

1. Never delete a WAV whose transcript hasn't succeeded (check `transcribed_at` is not NULL)
2. Never delete a WAV from a session whose diarization hasn't completed
3. Before deleting, confirm the transcript row exists and is non-empty
4. Two-stage delete (trash hold → purge) for recoverability

```python
# helios/src/helios/workers/cleanup.py (sketch)
class CleanupWorker:
    async def run_cleanup(self) -> CleanupReport:
        cutoff = self._clock.time() - self._config.raw_audio_days * 86400
        candidates = await queries.get_audio_older_than(
            self._db.writer, cutoff,
            require_transcribed=True,
            require_diarization_complete=True,
        )
        trash_dir = self._storage_root / "trash"
        trash_dir.mkdir(exist_ok=True)
        report = CleanupReport()
        for chunk in candidates:
            if chunk.path and Path(chunk.path).exists():
                dest = trash_dir / f"{chunk.id}_{Path(chunk.path).name}"
                Path(chunk.path).rename(dest)
                await queries.mark_chunk_archived(self._db.writer, chunk.id)
                report.archived += 1
        # Purge trash older than trash_hold_hours
        purge_cutoff = self._clock.time() - self._config.trash_hold_hours * 3600
        for trash_file in trash_dir.iterdir():
            if trash_file.stat().st_mtime < purge_cutoff:
                trash_file.unlink()
                report.purged += 1
        return report
```

Per-meeting deletion is a user action from the dashboard session detail page. It removes WAVs, transcripts, OCR frames, and thumbnails for the session in a single transaction. Soft-delete applies here too — the session's files go to trash, transcripts remain in the database but are marked deleted, and the row is hidden from queries (filtered via `deleted_at IS NULL` in standard reads).

**Voice note retention.** Voice note audio follows the same 7-day retention as meeting audio. Voice note transcripts and metadata are kept forever (in both Helios SQLite and Aegis Postgres). When a voice note is deleted via the Aegis dashboard, the cascade matches meeting deletion: WAVs go to trash (24h hold), Helios DB rows deleted, Aegis row deleted, embedding deleted. When a voice note is an excerpt of a meeting (`is_excerpt=true`), deleting the voice note does NOT delete the underlying meeting audio — only the voice note's metadata row is removed; the parent meeting's chunks and transcripts remain.

Disk space is monitored. When free disk on the volume containing `~/.aegis/capture` falls below `retention.disk_space_warning_gb`, a notification fires and the dashboard shows a warning. Helios does not auto-delete more aggressively under pressure — that's the user's call.

---

## 18. Testing Strategy

### 18.1 Test pyramid

Three layers, each with distinct purpose:

**Unit tests** live next to each module and test individual functions and classes with all external dependencies mocked. Fast (whole suite runs in under 30 seconds), run on every change, guarantee module-level correctness.

**Integration tests** exercise multiple components together using replay-mode sources and real SQLite. They test behaviors like "scheduler starts a session at the right time given fixture calendar events," or "chunker + transcription worker produces correct segments given fixture audio." Real WhisperX and pyannote run against small fixtures; tests complete in 2-5 minutes.

**Smoke tests** are manual procedures run on real hardware before advancing each build phase. Documented in `docs/helios_smoke_tests.md`, driven by `scripts/smoke_phase_N.sh` harnesses that automate the scripted parts while leaving human verification items as checklist outputs. See §7 of the Build Plan document for the full smoke test procedures.

### 18.2 Replay mode

`HELIOS_REPLAY=1` environment variable activates replay sources. The daemon substitutes file-based fixture readers for:

- Mic audio stream → WAV file playback (real-time or accelerated via VirtualClock)
- System audio stream → same
- Video frames → sequence of JPEG files with timestamps
- Calendar → static JSON file describing events
- Wall clock → `VirtualClock` that tests advance on demand
- Permission checks → always return granted
- LaunchAgent context → not used; daemon runs as a normal subprocess

Source selection happens at daemon startup:

```python
# helios/src/helios/__main__.py
def make_source_factory(config):
    if os.environ.get("HELIOS_REPLAY") == "1":
        return ReplaySourceFactory(config.replay)
    return RealSourceFactory(config)
```

Replay config in the TOML (only used in tests):

```toml
[replay]
mic_wav = "tests/fixtures/audio/meeting_2speaker_10min_mic.wav"
system_wav = "tests/fixtures/audio/meeting_2speaker_10min_system.wav"
calendar_json = "tests/fixtures/calendar/single_meeting.json"
start_ts = 1712345000.0    # synthetic epoch for the fixture
speed_multiplier = 10      # 10x real-time for fast tests
```

### 18.3 Virtual clock

All time-dependent code takes a `Clock` instance via dependency injection. No calls to `time.time()` or `asyncio.sleep()` outside `helios/src/helios/clock.py`.

```python
# helios/src/helios/clock.py
class Clock(Protocol):
    def time(self) -> float: ...
    def now_local(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...
    def call_later(self, delay: float, callback) -> TimerHandle: ...

class RealClock:
    def time(self) -> float:
        return time.time()
    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
    # ...

class VirtualClock:
    def __init__(self, initial_ts: float):
        self._now = initial_ts
        self._timers: list[tuple[float, asyncio.Event]] = []

    def time(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        event = asyncio.Event()
        self._timers.append((self._now + seconds, event))
        await event.wait()

    async def advance(self, seconds: float) -> None:
        target = self._now + seconds
        while True:
            due = [e for ts, e in self._timers if ts <= target]
            if not due:
                break
            self._now = min(ts for ts, e in self._timers if ts <= target)
            self._timers = [(ts, e) for ts, e in self._timers if ts > self._now]
            for e in due:
                e.set()
            await asyncio.sleep(0)  # yield to awakened coroutines
        self._now = target
```

Tests look like:

```python
async def test_scheduler_starts_session_60s_before_event(virtual_clock, scheduler):
    event_time = virtual_clock.time() + 3600  # 1 hour from now
    scheduler.schedule_event(event_time)
    await virtual_clock.advance(3540)  # advance to 60s before event
    assert scheduler.active_session is not None
```

### 18.4 Fixture management

Fixtures live under `helios/tests/fixtures/`:

```
helios/tests/fixtures/
├── audio/                       # gitignored; fetched separately
│   ├── meeting_2speaker_10min_mic.wav
│   ├── meeting_2speaker_10min_system.wav
│   ├── meeting_3speaker_30min_system.wav
│   ├── silence_5min.wav
│   └── crosstalk_example.wav
├── calendar/
│   ├── single_meeting.json
│   ├── adjacent_meetings.json
│   ├── overlapping_meetings.json
│   ├── all_excluded.json
│   └── recurring_series.json
├── ocr/
│   ├── slide_screenshots/       # real screen captures for Vision tests
│   └── no_meeting_app/
└── transcripts/
    ├── expected_meeting_2speaker_10min.json   # golden outputs
    └── expected_diarization_3speaker.json
```

**Audio fixtures are human-recorded.** Setting up the fixture library is a one-time task before Phase 1 can complete:

1. Record five test meetings of varied types: clean 2-speaker conversation, messy 3-speaker discussion, silence-heavy meeting, crosstalk-heavy meeting, poor-audio meeting. Target 5-30 minutes each.
2. Transcribe each manually (or via a cloud ASR as a starting point, then correct). Store goldens as JSON with segments and expected speaker counts.
3. Upload the ~500 MB of audio to a shared location (S3, Dropbox, a team drive). Store the fetch URL in `helios/tests/fixtures/audio/README.md`.
4. Add a `helios/tests/fixtures/fetch.sh` script that downloads fixtures on first test run.

When WhisperX or pyannote versions bump and the golden outputs need updating, treat it as an intentional code review event — regenerate goldens, diff, confirm the changes are acceptable.

### 18.5 Mocking strategy

| Component | Mock at | Rationale |
|-----------|---------|-----------|
| WhisperX | Function level (`WhisperModel.transcribe`) | Library-level, deterministic Pydantic-return-typed mocks in unit tests; real model in integration tests on small fixtures |
| pyannote | Function level (`Pipeline.__call__`) | Same pattern |
| Apple Vision OCR | Function level (`VNRecognizeTextRequest`) | Real Vision is fast and deterministic on fixture images; use real in integration tests |
| ScreenCaptureKit | Swift subprocess substituted entirely in replay mode | Can't meaningfully mock SCK |
| sounddevice | Substituted in replay mode | Same |
| Microsoft Graph | Not mocked — Aegis handles Graph; Helios talks to Aegis | N/A |
| Aegis API | `httpx.MockTransport` for unit tests; real local Aegis for integration | Realistic enough |
| SQLite | Real in temp directory, NOT mocked | Real SQL catches real bugs; same strategy Aegis uses |
| UNUserNotificationCenter | Mocked at PyObjC boundary | Headless test environments can't observe notifications |
| LaunchAgent | Not tested in CI | OS-level behavior; manual verification during setup |

### 18.6 Test fixtures (Python)

Shared pytest fixtures in `helios/tests/conftest.py`:

```python
@pytest.fixture
async def test_db(tmp_path):
    """Real SQLite in a temp directory, migrations applied."""
    db_path = tmp_path / "test.db"
    pool = DatabasePool(db_path)
    await pool.open()
    await run_migrations(pool.writer, MIGRATIONS_DIR)
    yield pool
    await pool.close()

@pytest.fixture
def virtual_clock():
    return VirtualClock(initial_ts=1712345000.0)

@pytest.fixture
def mock_whisper_model():
    """Returns a WhisperModel that produces canned segments."""
    m = MagicMock(spec=WhisperModel)
    def transcribe(path, **kwargs):
        return _canned_segments_for_fixture(path), TranscriptionInfo(...)
    m.transcribe = transcribe
    return m

@pytest.fixture
async def replay_daemon(test_db, virtual_clock, tmp_path):
    """A fully-initialized daemon in replay mode for integration tests."""
    config = HeliosConfig.for_testing(...)
    daemon = await Daemon.create(
        config=config, clock=virtual_clock, db=test_db,
        source_factory=ReplaySourceFactory(...),
    )
    await daemon.start()
    yield daemon
    await daemon.stop()
```

### 18.7 Per-module test coverage targets

- `chunker.py`: 90%+ line coverage, tests for silence detection, partial flushes, WAV correctness
- `scheduler/scheduler.py`: 85%+, tests for all reconciliation scenarios (adjacent, gap, overlapping, cancelled, rescheduled)
- `workers/transcription.py`: 80%+, tests for success, retry, failure, model-load handling
- `workers/diarization.py`: 80%+, tests for success, failure, disabled-by-config
- `workers/merge.py`: 90%+, tests for speaker assignment under various turn overlaps
- `workers/ocr.py`: 75%+, tests for gating logic, dedup, confidence filtering
- `api/*`: 85%+, tests for every endpoint including error paths
- `db/queries.py`: 90%+, tests for every query function against real SQLite

Integration tests aim for behavioral coverage rather than line coverage — each named user story has at least one integration test.

### 18.8 CI and local dev

Helios's `pyproject.toml` declares test commands:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.coverage.run]
branch = true
source = ["src/helios"]
```

Local commands:

```bash
cd helios
uv sync --extra dev
pytest                           # all tests
pytest tests/test_chunker.py     # single module
pytest -m "not slow"             # skip slow integration tests
pytest --cov=helios              # with coverage
```

CI configuration is out of scope for this spec; when CI gets set up, it runs `pytest` on every push and `pytest -m slow` plus smoke harness automation on main.

---

## 19. Logging and Observability

Structured JSON logging throughout. Each log event is one line of JSON:

```json
{
    "ts": 1712345678.123,
    "level": "info",
    "component": "scheduler",
    "event": "session_started",
    "session_id": 142,
    "calendar_event_id": "AAMk...",
    "details": {"kind": "calendar", "started_at": 1712345678.0}
}
```

**Destinations.** Every event goes to the rotating file log at `~/.aegis/capture/logs/helios.log` (daily rotation, 14-day retention, gzip-compressed older files, 50 MB per-file size cap). Important events (level ≥ info for state changes, errors, warnings) also land in the `daemon_events` SQLite table for dashboard display. Stdout/stderr are used only in `--replay` or `--debug` mode.

**Components** are stable strings: `scheduler`, `mic_stream`, `system_stream`, `chunker`, `transcriber`, `diarizer`, `merger`, `ocr`, `api`, `cleanup`, `permission_check`, `startup`.

**Events** are stable snake_case identifiers. The full taxonomy is in Appendix B. Using stable strings enables log grepping and future analytics.

**Sensitive data filter.** The following must never appear in logs: transcript text content, OCR text content, bearer tokens, HuggingFace tokens, meeting titles for excluded meetings (log `"(excluded)"` instead), file paths containing usernames (log `<user>` substituted). When logging exception messages, sanitize paths before emitting.

**API calls** are logged at debug level only, never info. With the menu bar polling `/v1/status` every 3 seconds, info-level logging of every API call would produce ~30k entries per day. Debug-level keeps them available for troubleshooting but keeps steady-state log volume manageable (~5 MB/day).

**Correlation.** Every log event relating to a session includes `session_id`. Events relating to a chunk include `chunk_id` too. The JSON format makes filtering trivial:

```bash
jq 'select(.session_id == 142)' ~/.aegis/capture/logs/helios.log
```

**Crash handling.** The daemon installs `sys.excepthook` that writes the full traceback to the log with `level=error, event=unhandled_exception`. Swift helper crashes produce macOS CrashReporter dumps at `~/Library/Logs/DiagnosticReports/`; these are findable but not surfaced by Helios itself.

**Startup summary.** On every daemon startup, a single `daemon_started` event logs version, config path, API port, model name, permission states, pending work counts, and schema version. This single event is gold for reconstructing "what was the world like when this broke."

---

## 20. Swift Helper Contract

The Swift helper is a human-maintained artifact. This section documents the contract it implements so Python code can rely on its behavior; Claude Code does not modify the Swift source.

**Binary:** `helios/bin/ScreenCaptureHelper` (universal binary: arm64 + x86_64). Pre-built via `scripts/build_swift_helper.sh`, committed to the repo. Built for macOS 13+ target.

**Invocation:** spawned as a subprocess by the daemon's system audio source. Accepts no command-line arguments for normal operation. `--version` prints a version string and exits 0.

**Protocol over stdout:** framed binary packets.

```
Packet layout:
  [1 byte  packet_type]
  [8 bytes presentation_timestamp, float64, little-endian]
  [4 bytes payload_length, uint32, little-endian]
  [N bytes payload]

Packet types:
  0x01  audio  — payload is int16 PCM at 16 kHz mono, little-endian
  0x02  video  — payload is JPEG q85 bytes
```

Presentation timestamps are derived from `CMSampleBufferGetPresentationTimeStamp` converted to UTC epoch seconds. All packets use the same time base, allowing Python to align audio and video accurately.

**Commands over stdin:** newline-terminated ASCII text:

```
ENABLE_AUDIO             Start emitting audio packets
DISABLE_AUDIO            Stop emitting audio packets
ENABLE_VIDEO             Start emitting video packets (after SET_DISPLAY)
DISABLE_VIDEO            Stop emitting video packets
SET_DISPLAY <id>         Set target display by CGDirectDisplayID
QUIT                     Graceful shutdown
```

**Acknowledgments over stderr:** line-oriented text confirming or rejecting commands:

```
OK ENABLE_VIDEO
ERR display_not_found 12345
```

Errors are never fatal to the subprocess unless `QUIT` was sent or an unrecoverable SCK error occurs; the helper reports the error on stderr and continues.

**Behavior:**

- Audio starts at 16 kHz mono int16 after `ENABLE_AUDIO`. The helper uses `AVAudioConverter` to defensively resample if SCK delivers at a different rate.
- Video emits at 1 fps when enabled, throttled via `SCStreamConfiguration.minimumFrameInterval`.
- Video frames are JPEG-encoded at quality 85, approximately 800×600 resolution.
- Audio and video can be independently enabled — the single SCK stream supports both.
- On SCK stream errors (permission revoked, display disconnected), the helper logs to stderr and exits with code 2. The Python side interprets exit-2 as "restart the helper."

**Building:** human-maintained source at `helios/swift/ScreenCaptureHelper.swift`. Build script:

```bash
#!/bin/bash
# scripts/build_swift_helper.sh
set -euo pipefail
swiftc -O \
  -target arm64-apple-macos13 \
  -target x86_64-apple-macos13 \
  helios/swift/ScreenCaptureHelper.swift \
  -o helios/bin/ScreenCaptureHelper
codesign --force --sign - helios/bin/ScreenCaptureHelper
echo "Built universal binary for ScreenCaptureHelper"
```

The binary is committed to the repo. Rebuild happens only when the Swift source changes (rare — 1-2 times per year in normal operation). Claude Code never modifies `helios/swift/` or `helios/bin/` contents; any Swift change requires a human to run the build script and commit the updated binary.

---

## 21. Packaging, Signing, Installation

**Packaging** uses py2app. `helios/setup.py` defines the bundle configuration:

```python
from setuptools import setup

APP = ["src/helios/__main__.py"]
DATA_FILES = [
    ("icons", [
        "icons/helios_not_running_template.png",
        "icons/helios_not_running_template@2x.png",
        "icons/helios_armed_template.png",
        "icons/helios_armed_template@2x.png",
        "icons/helios_recording_template.png",
        "icons/helios_recording_template@2x.png",
        "icons/helios_paused_template.png",
        "icons/helios_paused_template@2x.png",
        "icons/helios_error_template.png",
        "icons/helios_error_template@2x.png",
    ]),
    ("bin", ["bin/ScreenCaptureHelper"]),
]

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "icons/Helios.icns",
    "packages": [
        "rumps", "sounddevice", "numpy", "aiosqlite",
        "fastapi", "uvicorn", "httpx", "pydantic",
        "whisperx", "pyannote", "torch", "torchaudio",
        "Vision", "AppKit", "CoreAudio", "ScreenCaptureKit",
        "imagehash", "PIL",
    ],
    "plist": {
        "LSUIElement": True,
        "CFBundleName": "Helios",
        "CFBundleIdentifier": "com.aegis.helios",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "LSMinimumSystemVersion": "13.0",
        "NSMicrophoneUsageDescription":
            "Helios records meeting audio to generate transcripts for Aegis.",
        # No NSScreenCaptureUsageDescription — macOS prompts from SCK call itself
    },
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
```

**Build script** `scripts/build_helios.sh`:

```bash
#!/bin/bash
set -euo pipefail
cd helios
uv sync --extra build
rm -rf build dist
uv run python setup.py py2app
codesign --force --deep --sign - dist/Helios.app
echo "Helios.app built and ad-hoc signed at dist/Helios.app"
```

Ad-hoc signing (`--sign -`) stabilizes TCC permissions across rebuilds and satisfies macOS's "signed" requirement for the bundle structure. Gatekeeper will still prompt on first launch (right-click → Open bypasses); this is acceptable for personal use.

**Install script** `scripts/install_helios.sh` handles the deployment dance:

```bash
#!/bin/bash
set -euo pipefail

APP_SOURCE="helios/dist/Helios.app"
APP_DEST="/Applications/Helios.app"
PLIST_PATH="$HOME/Library/LaunchAgents/com.aegis.helios.plist"

# 1. Quit menu bar app if running
osascript -e 'tell application "Helios" to quit' 2>/dev/null || true

# 2. Unload daemon if running
launchctl unload "$PLIST_PATH" 2>/dev/null || true

# 3. Copy bundle
rm -rf "$APP_DEST"
cp -R "$APP_SOURCE" "$APP_DEST"

# 4. Write LaunchAgent plist
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.aegis.helios</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Applications/Helios.app/Contents/MacOS/Helios</string>
        <string>--daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/.aegis/capture/logs/launchagent.out</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.aegis/capture/logs/launchagent.err</string>
</dict>
</plist>
EOF

# 5. Reload daemon
launchctl load "$PLIST_PATH"

# 6. Launch menu bar
open "$APP_DEST"

echo "Helios installed. Check menu bar for the icon."
```

**Update flow.** On subsequent installs, the script detects `~/.aegis/capture.toml` exists (not first install), skips onboarding prompts, and just replaces the bundle and reloads.

**Launch at login.** The LaunchAgent's `RunAtLoad=true` ensures the daemon starts on user login. The menu bar app requires separate handling — on first run, the onboarding flow's final step prompts the user to add Helios to Login Items via System Settings deep-link. Programmatic `SMAppService` registration is a future enhancement.

**Signing for distribution.** When Helios needs to be shared beyond one user, upgrade to Apple Developer ID signing and notarization. The build script's `codesign` line changes to use a real identity (`--sign "Developer ID Application: Your Name (TEAMID)"`), and a notarization step (`xcrun notarytool submit ... --wait` followed by `xcrun stapler staple`) is added. One-time cert setup plus ~30 seconds of build-time upload. Skip until actually needed.

---

## Appendix A — Design Decisions

This appendix records the rationale for key decisions made during design. Referenced throughout the spec as "(Q-N)" where N is the question number in the decision process. Not required reading for implementation; useful when asking "why does it work this way?"

### A.1 Capture lifecycle (hybrid model)

Calendar-triggered capture by default with manual continuous mode for off-calendar conversations. Rejected 24/7 continuous capture (battery, privacy, disk) and rejected calendar-only (would miss unscheduled Teams calls that were a real user need). The manual toggle handles the gap without the downsides of always-on capture. The 5:30 PM hard stop and 4-hour prompts are safety rails against forgotten toggles.

### A.2 LaunchAgent + menu bar as separate processes

Early draft assumed a single process. Reconsidered when robustness became primary: LaunchAgent auto-restart survives daemon crashes, menu bar can crash without interrupting capture, and updates don't require capture to stop. Single app from user's perspective (one bundle in /Applications, one icon), two processes internally — the LaunchAgent plist is installation detail the user never sees.

### A.3 Ad-hoc signing

Apple Developer ID ($99/yr + notarization workflow) is deferred until distribution beyond one user becomes necessary. Ad-hoc signing stabilizes TCC grants across rebuilds (the practical pain point), costs nothing, and the one-time Gatekeeper bypass on first launch is acceptable friction for personal use.

### A.4 16 kHz mono int16 audio

Whisper's native input format. Higher sample rates waste disk and CPU without accuracy benefit. Resampling at capture time (in the Swift helper for system audio, in CoreAudio for mic) means the chunker has zero format logic.

### A.5 Separate mic and system streams, separate transcripts

Free coarse diarization (mic=user, system=others). Enables independent re-transcription. Enables crosstalk preservation. Doubles Whisper CPU but that's acceptable at 5x real-time on Apple Silicon. Merging at query time is trivial (interleave by timestamp).

### A.6 WhisperX + pyannote over plain Whisper

Original plan used faster-whisper. Revised after recognizing that speaker identification was a stated priority. WhisperX wraps faster-whisper with better alignment and integrated pyannote diarization. Accuracy improvements on speaker-attributed transcripts are meaningful. Speaker embeddings stored from day one enable future cross-meeting voice enrollment.

### A.7 Hybrid transcription pipeline (streaming + batch)

Per-chunk transcription on flush (streaming) so per-chunk latency is bounded. Full-session diarization on session end (batch) because pyannote needs the whole meeting to cluster speakers correctly. Merge worker joins them. Clean separation: Whisper streams, pyannote batches.

### A.8 Transcripts in Helios SQLite, stitched transcripts in Aegis Postgres

Helios owns the full transcript detail (per-chunk segments, word timestamps, diarization turns, embeddings). Aegis's `meetings.transcript_text` gets the final stitched version as a text blob. If Helios SQLite is lost, historical meetings still have their stitched transcripts. Helios is a cache layer over real audio; Aegis is the source of truth for what was extracted.

### A.9 OCR only when meeting app is frontmost

Gating by `NSWorkspace.frontmostApplication()` reduces OCR volume by 70-90% and maintains the privacy property that Helios doesn't OCR your email during a meeting. The manual "capture screen for N minutes" override handles the case where the user knows they're about to screen-share from a non-allowlisted app (browser-based Meet, etc.).

### A.10 Raw SQL via aiosqlite (no ORM)

Helios's queries are simple and performance-sensitive (transcription worker polls constantly). SQLAlchemy adds query compilation overhead without real benefit. Typed Pydantic row models and a `queries.py` module keep the code discoverable without the ORM layer.

### A.11 Port 3031 (not 3030)

Port 3030 is Screenpipe's. Even though Aegis never installed Screenpipe, choosing 3031 avoids future conflict if the user tries to run Screenpipe alongside Helios for comparison.

### A.12 Path B: clean API, not Screenpipe-compatible

Serving Screenpipe's `/search` endpoint would force throwing away useful metadata (coverage, unavailable ranges, session IDs). Writing a new `HeliosClient` in Aegis is a day of work; getting richer data downstream is worth it.

### A.13 Delete `meeting_detector._stitch_transcript()`

Since Helios serves pre-stitched transcripts via `/v1/sessions/{id}/transcript`, local stitching is dead code. Removing it simplifies Aegis and forces Helios to own transcript quality.

### A.14 Sibling package in existing Aegis repo

Not a monorepo restructure. Helios lives at the repo root next to the `aegis/` Python package with its own pyproject.toml and venv. `shared/` directory holds contract schemas. py2app bundles only Helios's venv, keeping the .app size reasonable.

### A.15 Defer pyannote setup to dashboard

Onboarding is already carrying five steps. Adding HuggingFace signup, license acceptance across three models, and token generation overloads it. The dashboard Settings page has a guided 5-step wizard with resumable state — users who want speaker identification opt in when they're ready.

### A.16 Speaker embeddings stored from day one

Adding them later is impractical (would require re-running diarization on old sessions with their original audio, which ages out after 7 days). Storing them preemptively costs ~1-2 KB per turn; negligible even over years of use. Enables the future voice enrollment module without infrastructure debt.

### A.17 Keychain for HuggingFace token

macOS Keychain is the conventionally correct place for secrets. Slightly more code than file-with-chmod but the pattern is well-understood and future-proofs against config-file leak scenarios.

### A.18 No formal rollback mechanism

Git checkout of previous SHA + rebuild covers the rare case of a broken update. SQLite migrations should be additive when possible (new columns, new tables rather than renames) to preserve read-forward compat. Formal rollback infrastructure doesn't earn its keep for a single-user tool.

### A.19 Manual retry for failed components

Auto-retry of failed imports is noise during unfixed problems. Single retry on daemon startup covers the "I fixed it and relaunched" case. Explicit retry button in dashboard for "I fixed it without restarting." Simple, predictable, debuggable.

### A.20 Dashboard composition in Aegis backend

Server-side Jinja2 composition fits Aegis's existing pattern (mostly HTML, few JSON endpoints). Client-side composition would require building a bunch of new Aegis JSON endpoints just for the dashboard. One request per page, no race conditions.

### A.21 Voice notes have three trigger surfaces

Menu bar, global hotkey, dashboard button. Hotkey is opt-in to keep Accessibility permission optional. The hotkey is the critical fast path for "I just thought of X, capture before I forget." Menu bar makes the feature discoverable without permission requirements. Dashboard button is nearly free since the dashboard already exists.

### A.22 Voice note during a meeting is an excerpt, not an independent recording

The mic stream is already running for the meeting; making a voice note creates a separate recording would double-capture the same audio. Treating it as a labeled excerpt that *also* appears in the meeting transcript matches reality better — when you make a voice note during a meeting, you've still spoken aloud and your meeting attendees may have heard it.

### A.23 Voice note audio kept per normal 7-day retention

Consistency with meetings; disk impact is negligible (~1 MB per 30-second note). If you ever want to re-transcribe with a better model or hear what you actually said, the audio is there.

### A.24 Smart-default attachments via Aegis's existing entity resolver

The save window calls Aegis's `preview-attachments` endpoint with the transcript and gets back suggested people, workstreams, asks. User confirms, modifies, or skips. Auto-save fires after 10 seconds with the suggested defaults. Best of fast path (do nothing, smart defaults applied) and explicit path (review and adjust).

### A.25 Read-only transcript at save time, editable from Aegis dashboard

Voice notes are meant to be fast. If you stop to edit, you've lost the speed advantage. The Aegis dashboard's voice note detail page has full edit capability for cleanup later. Misheard names in extraction are handled by the resolver's fuzzy matching anyway.

### A.26 5-minute soft cap with 30-second grace period

Catches "I forgot to stop" without truncating legitimately longer notes. Soft cap fires a notification 30 seconds before the hard stop; user can dismiss and continue or let it auto-stop.

### A.27 Mac-only for v1, but `source_device` field anticipates future iOS

Adding the `source_device` column now (defaulted to `"mac"`) means future iOS/Shortcuts integration doesn't require a migration. Voice notes are the natural feature to capture on a phone; we want that door open.

### A.28 Voice notes are first-class Aegis entities

Voice note pages live at Aegis's `/voice-notes/*`, alongside meetings, people, workstreams. They appear in person profiles, workstream profiles, daily timeline, briefings, and RAG search. They're not buried under a "Notes" section. The Helios dashboard's overview shows recent voice notes inline with link-throughs.

### A.29 Hotkey configurable in dashboard settings, no conflict detection

Users can change the hotkey combo in dashboard settings. Conflict detection (warning when the chosen hotkey is already used by another app) is over-engineering for v1; macOS has its own tools for inspecting hotkey conflicts.

### A.30 No exclusion mechanism for voice notes in v1

Voice notes are deliberate — the user pressed record. Exclusion is mostly a meeting-capture concern (you didn't choose to be in that 1:1). A privacy flag or exclusion column can be added later if needed; trivial to retrofit.

### A.31 PyObjC NSWindow for the save window

The save UI is the moment of friction in this feature — the user just spoke a thought, and the next 5 seconds determine whether they engage with attachments or hit Save. A clean floating NSWindow with focus is what makes that fast and pleasant. rumps' built-in window is too ugly; opening a browser tab pulls focus from whatever else the user was doing.

### A.32 Voice note features distributed across existing build phases

Each piece has a natural home: API in Phase 2, transcription in Phase 3, menu bar UI in Phase 4, Aegis UI in Phase 6. No standalone "voice notes phase." Acceptable tradeoff — pieces are individually testable in their phase, end-to-end completion lands in Phase 6.

### A.33 Accessibility permission for hotkey is opt-in via dashboard settings

Adding Accessibility to the required onboarding flow would lengthen it for a feature most users will not immediately use. Burying the permission request behind "I want to enable the hotkey" makes the cost-benefit obvious. Without permission, menu bar item and dashboard button still work — the hotkey is a power-user accelerator, not a base requirement.

### A.34 Synchronous transcription for voice notes

Voice notes are short (typically 30 seconds, max 5 minutes). Transcription on Apple Silicon runs at ~5x real-time, so a 30-second note transcribes in 6 seconds. Synchronous return from `/v1/voice-note/stop` lets the save window display the transcript immediately, matching the user's expectation of "fast feedback after I stopped recording." Async with polling adds UX complexity (progress indicator, eventual transcript appears) without saving meaningful time. The 60-second timeout fallback handles the rare slow case.

---

## Appendix B — Error Code Reference

HTTP API error codes:

| Code | HTTP | Meaning |
|------|------|---------|
| `unauthorized` | 401 | Bearer token missing or invalid |
| `permission_denied` | 403 | macOS permission required but not granted |
| `validation_error` | 422 | Request body or query params failed validation |
| `session_not_found` | 404 | Session ID doesn't exist |
| `chunk_not_found` | 404 | Chunk ID doesn't exist (for thumbnail fetch) |
| `component_unavailable` | 503 | Required component (transcription, etc.) failed to load |
| `daemon_shutting_down` | 503 | Daemon is in shutdown; try again after restart |
| `queue_full` | 503 | Internal queue is at capacity; rare |
| `internal_error` | 500 | Unexpected server-side failure; check logs |
| `voice_note_already_active` | 409 | A voice note is already recording |
| `voice_note_not_active` | 404 | No voice note currently recording (for stop/cancel) |
| `transcription_timeout` | 408 | Voice note transcription exceeded 60-second sync timeout; client should poll |

Log event taxonomy (non-exhaustive, add as needed):

**Scheduler:**
- `session_started`, `session_ended`, `session_reconciled`
- `pause_set`, `pause_resumed`
- `hard_stop_fired`, `four_hour_prompt_sent`, `prompt_response_received`, `prompt_timeout`
- `calendar_poll_failed`, `aegis_reachable`, `aegis_unreachable`

**Capture:**
- `stream_started`, `stream_stopped`, `stream_error`, `stream_restarted`
- `device_change_detected`, `sleep_detected`, `wake_detected`
- `helper_spawned`, `helper_exited`, `helper_crashed`
- `chunk_flushed`, `chunk_silent_skipped`, `chunk_marked_unavailable`

**Transcription:**
- `transcription_model_loading`, `transcription_model_loaded`, `transcription_model_load_failed`
- `chunk_transcription_started`, `chunk_transcription_completed`, `chunk_transcription_failed`
- `transcription_queue_backlogged`

**Diarization:**
- `diarization_started`, `diarization_completed`, `diarization_failed`
- `diarization_pipeline_loading`, `diarization_pipeline_loaded`, `diarization_pipeline_load_failed`

**OCR:**
- `ocr_enabled`, `ocr_disabled`, `ocr_override_activated`, `ocr_override_expired`
- `frame_captured`, `frame_duplicate_skipped`, `frame_low_text_skipped`
- `ocr_failed`

**Permissions:**
- `permission_granted`, `permission_revoked`, `permission_check_performed`

**Daemon:**
- `daemon_started`, `daemon_shutting_down`, `daemon_config_reloaded`
- `unhandled_exception`, `migration_applied`

**Cleanup:**
- `cleanup_started`, `cleanup_completed`, `cleanup_archived_files`, `cleanup_purged_files`
- `disk_space_warning`

**Voice notes:**
- `voice_note_started`, `voice_note_stopped`, `voice_note_cancelled`
- `voice_note_cap_warning_fired`, `voice_note_cap_reached`
- `voice_note_sync_transcription_started`, `voice_note_sync_transcription_completed`
- `voice_note_save_failed`, `voice_note_save_succeeded`
- `voice_note_hotkey_registered`, `voice_note_hotkey_registration_failed`
- `voice_note_excerpt_created`

---

## Appendix C — File Manifests per Phase

These manifests support the separate Build Plan document. Each phase's checkpoint requires the listed files to exist and pass the relevant tests.

### Phase 0 — Scaffolding

**Created:**
- `helios/pyproject.toml`
- `helios/src/helios/__init__.py`
- `helios/src/helios/__main__.py` (stub)
- `helios/src/helios/config.py`
- `helios/src/helios/logging.py`
- `helios/src/helios/api/__init__.py` (stub with /health)
- `helios/src/helios/api/auth.py`
- `helios/migrations/001_initial.sql`
- `helios/swift/ScreenCaptureHelper.swift` (or stub)
- `helios/bin/ScreenCaptureHelper` (or placeholder)
- `helios/icons/` (placeholder PNGs)
- `helios/setup.py`
- `helios/tests/conftest.py`
- `shared/pyproject.toml`
- `shared/src/shared/__init__.py`
- `shared/src/shared/meetings.py` (schema only)
- `shared/src/shared/audio.py` (schema only)
- `scripts/build_helios.sh`
- `scripts/build_swift_helper.sh`
- `scripts/install_helios.sh`
- `HELIOS.md` (this file, at repo root)

**Modified:**
- `CLAUDE.md` (add pointer to HELIOS.md)

### Phase 1 — Capture pipeline

**Created:**
- `helios/src/helios/clock.py`
- `helios/src/helios/sources/interface.py`
- `helios/src/helios/sources/real.py`
- `helios/src/helios/sources/replay.py`
- `helios/src/helios/capture/stream_manager.py`
- `helios/src/helios/capture/chunker.py`
- `helios/src/helios/capture/helper_protocol.py`
- `helios/src/helios/db/connection.py`
- `helios/src/helios/db/migrations.py`
- `helios/src/helios/db/queries.py`
- `helios/src/helios/db/rows.py`
- `helios/tests/test_chunker.py`
- `helios/tests/test_stream_manager.py`
- `helios/tests/test_sources_replay.py`
- `helios/tests/test_db.py`

### Phase 2 — Scheduler and API

**Created:**
- `helios/src/helios/scheduler/scheduler.py`
- `helios/src/helios/scheduler/calendar.py`
- `helios/src/helios/scheduler/timezone.py`
- `helios/src/helios/state.py`
- `helios/src/helios/api/routes/status.py`
- `helios/src/helios/api/routes/capture.py`
- `helios/src/helios/api/routes/sessions.py`
- `helios/src/helios/api/routes/permissions.py`
- `helios/src/helios/api/routes/diagnostics.py`
- `helios/src/helios/api/routes/voice_note.py`
- `helios/src/helios/api/schemas.py`
- `helios/migrations/002_session_calendar_links.sql`
- `helios/tests/test_scheduler.py`
- `helios/tests/test_api.py`
- `helios/tests/test_voice_note_endpoints.py`

**Modified (Aegis):**
- `aegis/web/routes/api.py` — add `GET /api/meetings/upcoming`
- `aegis/web/routes/api.py` — add stub `POST /api/voice-notes/preview-attachments` and `POST /api/voice-notes` (real implementations in Phase 3)
- `shared/src/shared/meetings.py` — finalize response schema
- `shared/src/shared/audio.py` — add VoiceNoteCreate, SuggestedAttachments, ConfirmedAttachments, AttachmentMatch, AttachmentPreviewResponse schemas
- `aegis/config.py` — add Helios config values

### Phase 3 — Transcription and Aegis integration

**Created:**
- `helios/src/helios/workers/transcription.py`
- `helios/src/helios/workers/diarization.py`
- `helios/src/helios/workers/merge.py`
- `helios/src/helios/scripts/download_whisper.py`
- `helios/src/helios/api/routes/audio.py`
- `helios/src/helios/keychain.py`
- `helios/tests/test_transcription_worker.py`
- `helios/tests/test_diarization_worker.py`
- `helios/tests/test_merge_worker.py`
- `helios/tests/test_voice_note_sync_transcription.py`
- `aegis/clients/helios.py`
- `aegis/db/voice_notes_repository.py`
- `aegis/processing/voice_note_extractor.py`
- `alembic/versions/XXXXXX_add_voice_notes.py`
- `tests/test_voice_notes_repository.py` (in Aegis)
- `tests/test_voice_note_extractor.py` (in Aegis)

**Modified:**
- `aegis/ingestion/screenpipe.py` → renamed `aegis/ingestion/helios.py`, rewritten
- `aegis/ingestion/meeting_detector.py` — simplified, stitching removed
- `aegis/main.py` — wire up HeliosClient
- `aegis/ingestion/poller.py` — add Helios heartbeat loop
- `aegis/db/models.py` — add `VoiceNote` and `VoiceNoteAttachment` models
- `aegis/web/routes/api.py` — replace Phase 2 stubs with real `preview-attachments` and `POST /api/voice-notes` implementations; add `GET /api/voice-notes`, `GET /api/voice-notes/{id}`, `PATCH`, `DELETE`
- `aegis/processing/pipeline.py` — add voice notes to LangGraph state machine

### Phase 4 — Menu bar and onboarding

**Created:**
- `helios/src/helios/menubar/app.py`
- `helios/src/helios/menubar/client.py`
- `helios/src/helios/menubar/onboarding.py`
- `helios/src/helios/menubar/notifications.py`
- `helios/src/helios/menubar/hotkey.py`
- `helios/src/helios/menubar/voice_note_indicator.py`
- `helios/src/helios/menubar/voice_note_save_window.py`
- `helios/src/helios/notifications/notify.py`
- `helios/tests/test_menubar_client.py`
- `helios/tests/test_onboarding.py`
- `helios/tests/test_voice_note_hotkey.py`

### Phase 5 — OCR

**Created:**
- `helios/src/helios/workers/ocr.py`
- `helios/src/helios/workers/cleanup.py`
- `helios/src/helios/api/routes/ocr.py`
- `helios/tests/test_ocr_worker.py`
- `helios/tests/test_cleanup_worker.py`

### Phase 6 — Dashboard

**Created:**
- `aegis/web/routes/helios.py` — all dashboard routes
- `aegis/web/templates/helios/base.html`
- `aegis/web/templates/helios/overview.html`
- `aegis/web/templates/helios/sessions_list.html`
- `aegis/web/templates/helios/session_detail.html`
- `aegis/web/templates/helios/calendar.html`
- `aegis/web/templates/helios/diagnostics.html`
- `aegis/web/templates/helios/settings.html`
- `aegis/web/templates/helios/_partials/` (various)
- `aegis/web/routes/voice_notes.py` — Aegis voice notes pages
- `aegis/web/templates/voice_notes/list.html`
- `aegis/web/templates/voice_notes/detail.html`
- `aegis/web/templates/voice_notes/_partials/voice_note_row.html`
- `aegis/web/templates/voice_notes/_partials/voice_note_card.html`
- `tests/test_voice_notes_routes.py` (in Aegis)
- `tests/test_voice_notes_api.py` (in Aegis)

**Modified:**
- `aegis/db/models.py` — add `helios_exclude` to `Meeting`
- `alembic/versions/XXXXXX_add_helios_exclude.py` — new migration
- `aegis/web/templates/base.html` — add Helios sidebar section
- `aegis/web/templates/people/detail.html` — add Voice notes section
- `aegis/web/templates/workstreams/detail.html` — add Voice notes section
- `aegis/web/templates/asks/detail.html` — add Voice notes section
- `aegis/web/templates/dashboard/today.html` (or main timeline) — mix voice notes into timeline
- `aegis/intelligence/briefings.py` — include voice notes in briefings
- `aegis/chat/rag.py` — include voice notes in RAG search
- `aegis/main.py` — mount voice notes router

### Phase 7 — Hardening

No new files; modifications only as needed based on smoke test findings.

---

## Appendix D — Human-Provided Assets

Items Claude Code must not attempt to create. Each is an input the build depends on.

| Asset | Location | Status | When needed |
|-------|----------|--------|-------------|
| Swift helper source | `helios/swift/ScreenCaptureHelper.swift` | To be drafted from the sketch in HELIOS.md | Phase 0 |
| Swift helper binary | `helios/bin/ScreenCaptureHelper` | Built from source via `scripts/build_swift_helper.sh` | Phase 0 |
| Audio test fixtures | `helios/tests/fixtures/audio/*.wav` | Five recorded meetings; ~500 MB total | Phase 1 |
| Golden transcripts | `helios/tests/fixtures/transcripts/*.json` | Manually produced for audio fixtures | Phase 1 |
| Real menu bar icons | `helios/icons/*.png` | Placeholder icons acceptable for v1; real icons as separate design task | Can defer |
| HuggingFace account + token | Runtime, via Keychain | Required to use speaker identification | Phase 3 (optional) |
| Apple Developer ID | — | Not required for v1 | Post-v1 only |

---

**End of Spec.**
