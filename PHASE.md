# Helios Phase 4 — Progress

Persistent tracker for Phase 4 (Menu Bar, Onboarding, Permissions, Voice Note UX). Build plan at `/Users/rickydelemos/.claude/plans/lively-orbiting-beacon.md`. Canonical task list at `HELIOS_BUILD_PLAN.md` lines 1487-1891. Smoke test at lines 2561-2604.

## Status

- Phase: 4 (Menu Bar, Onboarding, Permissions, Voice Note UX)
- Started: 2026-05-07
- Current wave: 5 (User smoke + sign-off)
- Bug-report cycle: 1 (resolved)
- Smoke test (§12.5): pending

**Wave 1 result (2026-05-07):** 10 agents complete. 620 Helios tests pass (+208 new), 313 Aegis tests pass (+16 new).

**Wave 2 result (2026-05-07):** 4-MenuBarApp shipped. `app.py` 723 LOC + 1164-LOC test file with 43 tests. 663 Helios tests pass (+43 new), 313 Aegis tests unchanged.

**Wave 4 cycle 1 result (2026-05-07):** Repair clean in cycle 1. All 5 critical bugs fixed (UN auth-status enum off-by-one × 2, save-window payload schema mismatch with Aegis × 3). Warnings 6-9 also fixed: model-download stderr deadlock, missing indicator-position persist callback + config field, empty onboarding step views (now real labels/buttons/progress wired to existing handlers), and the save-window content view (header / transcript / suggestions stack / Discard / Add-attachment / Save). Manual attachment picker (NSPanel) deferred per scope. 665 Helios tests pass (+2 new smoke tests), 313 Aegis tests unchanged.

## Wave roster

### Wave 1 — Foundations (parallel, 10 agents)

| Agent | Owns | Tests | Status |
|---|---|---|---|
| 4-Client | `menubar/client.py` | `test_menubar_client.py` | done |
| 4-DaemonNotify | `notifications/notify.py` | `test_daemon_notifications.py` | done |
| 4-MenuBarNotify | `menubar/notifications.py` | `test_menubar_notifications.py` | done |
| 4-Onboarding | `menubar/onboarding.py` | `test_onboarding.py` | done |
| 4-ModelDownload | `menubar/model_download.py` | `test_model_download.py` | done |
| 4-Indicator | `menubar/voice_note_indicator.py` + `current_rms` daemon work | `test_voice_note_indicator.py` | done |
| 4-SaveWindow | `menubar/voice_note_save_window.py` | `test_voice_note_save_window.py` | done |
| 4-AegisSearch | `aegis/web/routes/search.py` JSON variant | `tests/test_api_search.py` | done |
| 4-Hotkey | `menubar/hotkey.py` + `pyproject.toml` Carbon dep | `test_hotkey.py` | done |
| 4-Lifecycle | `menubar/launchctl.py` | `test_launchctl.py` | done |

### Wave 2 — Menu bar integration (1 agent)

| Agent | Owns | Tests | Status |
|---|---|---|---|
| 4-MenuBarApp | `menubar/app.py` (rewrite) | `test_menubar_app.py` | done |

### Waves 3-5

- Wave 3 — Review (single agent; produces `bug_report.md`)
- Wave 4 — Repair (≤3 cycles; deletes `bug_report.md` when clean)
- Wave 5 — User §12.5 smoke + `PHASE_4_CHECKPOINT.md`

## Track checklist

References `HELIOS_BUILD_PLAN.md` lines 1487-1891 as canonical.

### Track 4A — Menu bar HTTP client
- [x] 4A.1 — Test-first menu bar HTTP client
- [x] 4A.2 — Implement client

### Track 4B — Menu bar app
- [x] 4B.1 — Basic rumps app
- [x] 4B.2 — State-dependent menu construction
- [x] 4B.3 — Test-first pause-submenu logic
- [x] 4B.4 — Header click-through during recording
- [x] 4B.5 — Optimistic UI updates
- [x] 4B.6 — Voice note menu items
- [x] 4B.7 — Sixth icon state for voice-note recording
- [x] 4B.8 — Voice-note state polling

### Track 4C — Notifications
- [x] 4C.1 — Daemon-side notifications
- [x] 4C.2 — Menu-bar-side notifications
- [x] 4C.3 — Test-first 4-hour prompt action buttons
- [x] 4C.4 — Wire all notification triggers
- [x] 4C.5 — Voice-note notifications

### Track 4D — Onboarding window
- [x] 4D.1 — PyObjC window scaffold
- [x] 4D.2 — Welcome step
- [x] 4D.3 — Microphone permission step
- [x] 4D.4 — Screen recording permission step
- [x] 4D.5 — Restart step (conditional)
- [x] 4D.6 — Model download step
- [x] 4D.7 — Login items step
- [x] 4D.8 — Complete step
- [x] 4D.9 — State persistence

### Track 4E — Daemon lifecycle from menu bar
- [x] 4E.1 — LaunchAgent commands
- [x] 4E.2 — Stop Helios Daemon with confirmation
- [x] 4E.3 — Start Capture Daemon from not_running state

### Track 4F — Voice note hotkey listener
- [x] 4F.1 — Test-first hotkey detection
- [x] 4F.2 — Implement hotkey listener
- [x] 4F.3 — Wire to menu bar app

### Track 4G — Floating recording indicator
- [x] 4G.1 — Implement floating indicator
- [x] 4G.2 — Show/hide lifecycle

### Track 4H — Floating save window
- [x] 4H.1 — Implement save window
- [~] 4H.2 — Manual attachment picker (search backend done; NSPanel UI deferred to Phase 4 polish — `handle_add_attachment_clicked` carries TODO)

## Sync invariants

**Wave 1 → Wave 2:**
- Every Wave 1 module importable + unit-tested.
- No Wave 1 agent edited `menubar/app.py`.
- `pyproject.toml` Carbon dep merged exactly once (4-Hotkey).
- `current_rms` field live in `/v1/voice-note/active` responses.
- `/api/search` JSON endpoint live on Aegis side.

**Wave 2 → Wave 3:**
- `app.py` integrates every Wave 1 module without circular imports.
- All six icon states reachable in tests.
- All notification categories registered at app start.

## Open questions / decisions log

- 2026-05-07 — Manual attachment picker NSPanel deferred to Phase 4 polish (warning, not critical). Search backend (`/api/search`) and `search_attachments()` data plumbing both shipped; only the NSPanel UI is missing. `handle_add_attachment_clicked` carries TODO referencing HELIOS_BUILD_PLAN.md §4H.2.
- 2026-05-07 — `mode: error` reported by daemon immediately after install due to mic component "degraded" — likely transient from rapid LaunchAgent kill/relaunch. Should clear after first capture cycle. Watch during smoke test.

## Smoke test (§12.5) handoff — 2026-05-07

Bundle rebuilt + reinstalled at `/Applications/Helios.app`. Daemon running (port 3031), menu bar UI auto-launched via Login Item. Manual checklist for sign-off:

**Must verify** (per HELIOS_BUILD_PLAN.md lines 2561-2604):
- [ ] Menu bar icon visible in macOS menu bar (look near system icons)
- [ ] All 6 icon states reachable: not_running, armed, recording, recording_voice_note, paused, error
- [x] Onboarding window: launch with `rm ~/.aegis/capture/onboarding_state.json` then click "Re-run Onboarding" from menu (or kill + relaunch the menu bar process) — verified end-to-end 2026-05-13 after cycle-3 fixes (NSTimer block-API permission poller, progress widget moved out of dead code, main-thread dispatch for download callbacks, ImportError-tolerant verification step, non-JSON line tolerance, subprocess PYTHONPATH inheritance). State file lands with `complete: true, mic_granted: true, screen_granted: true, model_downloaded: true, login_items_acknowledged: true`.
  - Walk through welcome → mic → screen → restart → model → login items → complete
  - Each step has real labels + buttons (built in Wave 4 cycle 1)
- [x] 4-hour prompt notification (HIGHEST RISK feature) — verified end-to-end 2026-05-14 (cycle 4). Session 69 fired first banner at 09:30:00 → Continue clicked at 09:30:08 (`prompt_response_continue` logged, daemon cleared `last_error`, next prompt rescheduled). Second banner at 09:31:18 (menu-bar transition detector picked up the fresh pending state) → Stop clicked at 09:31:29 (`prompt_response_stop` → `session_ended` reason `4hr_prompt_stop`).
  - `continuous_prompt_hours` widened from `int` to `float` so the spec's `1/60` smoke-test override (≈60 s) is expressible
  - `helios/api/routes/voice_note.py` config also reads `read_text(encoding="utf-8")` (`§` in a previous capture.toml comment crashed daemon startup with `UnicodeDecodeError`)
- [~] Permission revocation: revoke mic in System Settings during active session — DEFERRED 2026-05-13. State-machine logic verified working (tccutil-reset path triggers revocation+restoration correctly, see workers/permissions.py tests). What doesn't work: detecting the System Settings toggle in the LaunchAgent daemon, because `AVCaptureDevice.authorizationStatusForMediaType_` caches per-process and macOS's "Quit & Reopen" only restarts the menu bar, not the daemon. Tracked as a follow-up — likely fix is to flag permission_revoked from N consecutive chunk_no_audio mic events.
- [ ] Quit Menu Bar leaves daemon running (`curl http://127.0.0.1:3031/v1/health` from another terminal)
- [ ] Stop Helios Daemon shows confirmation NSAlert, then unloads — health check fails afterward
- [x] Voice note from menu bar: click "Record Voice Note" → indicator window appears with timer + audio level → click stop → save window appears with transcript (verified 2026-05-12 — Aegis voice_notes id=3, helios session 40)
  - Save window has: header, transcript display, suggestion checkboxes, Discard / Add attachment / Save buttons (Add attachment NSPanel is deferred — clicking just cancels countdown for now)
- [~] Voice note from hotkey: enable in dashboard settings (Accessibility permission required), press ⌥⌘V to start, again to stop — DEFERRED 2026-05-12. Accessibility granted to `/Applications/Helios.app` and `voice_note.hotkey_enabled = true` in capture.toml, but ⌥⌘V doesn't register. Needs investigation of `helios.menubar.hotkey.VoiceNoteHotkey` (Carbon RegisterEventHotKey) — not blocking smoke sign-off.
- [x] Voice note during meeting (excerpt): trigger voice note while calendar capture active → `is_excerpt=true` (verified 2026-05-13 — continuous capture session 53 active, voice note returned `is_excerpt=true`, Aegis voice_notes id=9 saved with `is_excerpt=true` AND auto-linked to Aegis meeting id=8467 via temporal match). Open follow-up: a short excerpt voice note that runs entirely inside an in-progress chunk (chunks close at 30s boundaries) returns an empty transcript — `get_session_audio_chunks` only returns committed rows. Standalone voice notes don't hit this because their `stop_session` force-flushes the current chunk; excerpt mode doesn't stop the parent. Fix options: (A) wait briefly in `/voice-note/stop` excerpt branch for the in-flight chunk to close + transcribe (adds up to 30s latency); (B) plumb a "flush partial chunk" call into the parent's chunker; (C) read the live audio buffer for the voice-note window. None of these block the smoke item.
- [x] Voice note duration cap (set `voice_note.max_duration_seconds = 60` for the smoke run): force-stop fires at hard cap, indicator auto-closes, save window opens with the buffered transcript, user-clicked Save persists to Aegis (verified 2026-05-12 — session 52 ran the full 59.8s cap window, Aegis voice_notes id=8 created with duration=60.19s, transcript_text=216 chars). Visual cue at 30s elapsed = cap-warning amber timer, not the stop. Open follow-up: macOS banner delivery for cap warning + force-stop deferred — daemon-side UNUserNotificationCenter post still returns `notification_skipped_unauthorized` even with system-level grant.
- [x] Save window auto-save: record quick note → wait 10s → auto-save fires + Aegis row created (verified 2026-05-12 — Aegis voice_notes id=4, helios session 44, after NSTimer block-API fix)
- [x] Save window cancellation: trigger voice note, stop, click Discard → no Aegis row created (verified 2026-05-12 — Helios session 41 ended cleanly, Aegis voice_notes count unchanged)

**Known limitations** (acceptable for sign-off):
- 4H.2 manual attachment picker NSPanel not built — only data plumbing
- Polish items in `bug_report.md` history are deferred to future polish

## Bug-report cycle history

| Cycle | Triggered by | Critical | Warnings | Resolved at |
|---|---|---|---|---|
| 1 | Wave 3 review (2026-05-07) | 5 fixed | 4 fixed (6-9), 1 deferred (manual picker NSPanel) | 2026-05-07 |
| 2 | §12.5 smoke run (2026-05-12) | 7 fixed — see below | 0 | 2026-05-12 |
| 3 | §12.5 smoke run (2026-05-13) onboarding | 4 fixed — see below | 0 | 2026-05-13 |
| 4 | §12.5 smoke run (2026-05-14) 4-hour prompt | 6 fixed — see below | 0 | 2026-05-14 |

### Cycle 2 — §12.5 smoke fixes (2026-05-12)

Triggered when the menu-bar voice-note path crashed mid-smoke. Root causes were independent but all blocked the same flow.

- **`helios/workers/transcription.py`** — WhisperX's `transcribe` / `align` subprocess-exec `ffmpeg` when given a path; the bundle doesn't ship ffmpeg. Every voice note sync transcription was failing with `[Errno 2] No such file or directory: 'ffmpeg'`. Fix: decode the chunk's 16-bit mono PCM WAV with stdlib `wave` + numpy and pass the array to WhisperX (which accepts `Union[str, np.ndarray]`). Adds `_load_wav_mono_16k()`. +3 tests.
- **`helios/workers/permissions.py`** — once the state machine entered `mode="error"` from a revoked permission, restoring the grant never cleared the `ComponentError`. Fix: detect `revoked → granted` transitions, call `clear_component_errors`, and transition back to `armed` when no other errors remain. +3 tests covering single-component, mixed-error preservation, and cold-start revoke→restore.
- **`helios/menubar/app.py`** — `_trigger_voice_note_stop` dispatched an error-alert lambda to the main thread that captured `exc` from the surrounding `except` block. By the time the NSBlockOperation ran, Python had cleared `exc` → `NameError` → ObjC exception → SIGABRT. Fix: bind `exc`'s name into the lambda's defaults at definition time.
- **`helios/menubar/client.py`** — `post_voice_note_stop` used the 5s default timeout, but the daemon's sync transcription on the final partial chunk legitimately takes 10–30s. Fix: per-call `timeout=60.0` and a `timeout` kwarg on `_request`.

Also bumped `[transcription] model_load_timeout_seconds = 90` in `~/.aegis/capture.toml` (default 30s was too tight on cold loads after a fresh bundle).

- **`helios/menubar/voice_note_save_window.py`** — auto-save countdown's NSTimer used `self` (a plain Python object) as the target, but NSTimer needs an NSObject. Same pattern as the original Save/Discard button bug — selector dispatch silently no-ops, so the countdown never ticked and the window stayed open indefinitely. Fix: use `scheduledTimerWithTimeInterval_repeats_block_` (macOS 10.12+), which PyObjC bridges Python closures into directly. Bumped `permission_check_minutes` in `~/.aegis/capture.toml` from 5 → 1 so the daemon picks up newly-granted permissions faster after each rebuild's TCC reset.
- **`helios/api/routes/voice_note.py`** — after a scheduler force-stop the orchestrator's active session was cleared but `app.state.voice_note_active` was not (the stop endpoint is what clears it, and force-stop bypasses the endpoint). `/v1/voice-note/active` kept returning the stale dict, so the floating indicator never saw `active=null` and stayed on screen. Refactor: `_get_state` returns raw state without consulting the orchestrator; `_orchestrator_owns` helper detects force-stop; `_active` is now `/voice-note/active`-only (returns None when not owned, leaves state intact); `/voice-note/stop` reads via `_get_state` and tolerates force-stopped sessions (skips the orch.stop_session call, builds response from existing chunks). +2 endpoint tests.
- **`helios/menubar/app.py`** — `_poll` detects RECORDING_VOICE_NOTE→ARMED transitions that weren't user-initiated (cap-timer force-stop, external cancel) and dispatches the same `_trigger_voice_note_stop` worker the Stop button uses. The worker hits `/v1/voice-note/stop` (which now serves post-force-stop calls) and opens the save window. Guarded by `_voice_note_stop_handled_by_user` flag so the explicit user-stop path doesn't double-fire.

### Cycle 3 — Onboarding (2026-05-13)

Onboarding wizard ran end-to-end after these fixes; previously stuck on permission "checking..." labels and an invisible model-download step.

- **`helios/menubar/model_download.py`** — bundled subprocess inherits `sys.path` via a `PYTHONPATH` env var so `python -m helios.scripts.download_whisper` resolves (py2app's bootstrap doesn't run for the child). Also: non-JSON lines on subprocess stdout (e.g. `RequestsDependencyWarning` text from `import requests`) are now skipped instead of treated as fatal `parse_error`. +1 regression test (`test_non_json_lines_are_tolerated`).
- **`helios/scripts/download_whisper.py`** — verification probe (`whisperx.load_model` + tiny transcribe) treats `ImportError` / `ModuleNotFoundError` raised from inside a successfully-imported whisperx as a warning, not a hard error. The bundled subprocess sees `typing_extensions` as missing because py2app ships only the dist-info; the daemon itself (running under py2app's bootstrap) has the package available. Files-on-disk verdict is enough.
- **`helios/menubar/onboarding.py`** — three independent fixes: (1) the `NSProgressIndicator` and progress-status label were dead code inside `_build_objc_button_target` after a `return None` — moved to `_populate_step_view` so the model step actually has a visible progress widget; (2) added an `NSTimer`-block-based 500 ms permission poll (`_start_status_poll` / `_stop_status_poll`) wired up in `_render_current_step` and torn down in `close()`, so the mic / screen "Status: checking..." labels flip to ✓ within half a second of the OS grant and auto-advance; (3) `_dispatch_to_main_thread` helper wraps the `ModelDownloader` callbacks so AppKit writes don't race the read-loop thread. +9 regression tests covering progress widget construction, formatter, mic/screen poll auto-advance, main-thread dispatch, and timer teardown on close.

### Cycle 4 — 4-hour prompt notification (2026-05-14)

The §12.5 "highest risk" item. Scheduler fired the prompt event but no banner reached the user, and even if it had, the daemon's state machine left `last_error="4hr_prompt_pending"` set after the user clicked Continue, so a second prompt within the same session would have been silent.

- **`helios/pyproject.toml` + `helios/setup.py`** — added `pyobjc-framework-UserNotifications` + `UserNotifications` to py2app's `includes`. Without it, `import UserNotifications` inside the bundle raised `ModuleNotFoundError`, `MenubarNotifier.register_categories()` early-returned with the warning, `_categories_registered` stayed False, and every `post(...)` silently returned False. Discovered via a temporary file-based tracer in the menu-bar's `_poll`; left the gate in place but stripped the tracer once the path was proven.
- **`helios/config.py`** — `continuous_prompt_hours` widened from `int` to `float` so the spec's `1/60` (≈60 s) smoke-test override is expressible. Also fixed `load_config` to read capture.toml with explicit `encoding="utf-8"` — a stray `§` in a TOML comment crashed daemon startup with `UnicodeDecodeError: 'ascii' codec can't decode byte 0xc2`.
- **`helios/scheduler/scheduler.py`** — `respond_to_prompt` now calls `state_machine.transition(last_error=None)` so the menu bar's transition detector sees `4hr_prompt_pending` → cleared → `4hr_prompt_pending` across consecutive prompts and posts a fresh banner each time. The cap-warning notify path was also moved to a fire-and-forget `_spawn_task` so a 5 s `wait_for(...)` inside `DaemonNotifier.post` can't stall the cap chain (`_safe_stop` was being masked in the virtual-clock test).
- **`helios/notifications/notify.py`** — `_in_app_bundle()` gate (Foundation's `NSBundle.mainBundle().bundleIdentifier()` must match `com.aegis.helios`) short-circuits every `UNUserNotificationCenter` call. Outside a real bundle, the API aborts with an uncatchable C-level `NSException`; this gate makes the dev venv + pytest path safe. Tests flip `_FAKE_BUNDLE_FOR_TESTS = True` to exercise the fake-module path.
- **`helios/menubar/app.py`** — `_poll` watches `status.last_error == "4hr_prompt_pending"` and posts the `FOUR_HOUR_PROMPT` notification via `MenubarNotifier.post`. The daemon-side `DaemonNotifier` can't reliably deliver action-button banners from a LaunchAgent context (UN authorization sticks at `notDetermined`); the menu bar already requested authorization in `register_categories()` so it owns the post. Dedup via `_4hr_prompt_posted_for_session` so a single fire only posts once; the daemon-side `last_error=None` reset (above) makes the dedup release between prompts.

End-to-end verification: session 69 fired first banner at 09:30:00 → Continue at 09:30:08 (`prompt_response_continue`, daemon cleared `last_error`) → second banner at 09:31:18 → Stop at 09:31:29 (`prompt_response_stop` → `session_ended` reason `4hr_prompt_stop`).
