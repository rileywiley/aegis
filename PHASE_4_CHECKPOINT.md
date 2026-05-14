# Phase 4 Checkpoint — Menu Bar, Onboarding, Permissions, Voice-Note UX

**Closed:** 2026-05-14
**Build plan reference:** `HELIOS_BUILD_PLAN.md` lines 1487–1891

## §12.5 smoke checklist

| Item | Status | Notes |
|---|---|---|
| Fresh install + complete onboarding end-to-end | ✅ | Verified 2026-05-13 after cycle-3 fixes. State file lands with `complete: true, mic_granted: true, screen_granted: true, model_downloaded: true, login_items_acknowledged: true`. |
| Menu bar icon: `not_running` | ✅ | After `Stop Helios Daemon` confirmation |
| Menu bar icon: `armed` | ✅ | Default with no active session |
| Menu bar icon: `recording` | ✅ | During continuous capture |
| Menu bar icon: `recording_voice_note` | ✅ | During voice-note capture |
| Menu bar icon: `paused` | ✅ | Via `POST /v1/capture/pause-until` |
| Menu bar icon: `error` | ✅ | After permission revocation event |
| 4-hour prompt notification with action buttons | ✅ | Cycle 4. Session 69, banner #1 at 09:30:00, Continue at +8 s, banner #2 at 09:31:18, Stop at +11 s. |
| Quit Menu Bar leaves daemon running | ✅ | Health endpoint kept responding |
| Stop Helios Daemon NSAlert + unload | ✅ | LaunchAgent service unregistered cleanly |
| Voice note from menu bar | ✅ | Aegis voice_notes id=3, 102-char transcript |
| Voice note during meeting (excerpt) | ✅ | Aegis voice_notes id=9, `is_excerpt=true`, auto-linked to Aegis meeting id=8467 |
| Voice note duration cap | ✅ | Force-stop fires + save window opens with buffered transcript |
| Voice note save window auto-save | ✅ | Aegis voice_notes id=4 created after 10 s of idle on save window |
| Voice note save window cancellation (Discard) | ✅ | Helios session ends, no Aegis row |

## Known limitations carried forward (3 deferred items)

These are tracked as follow-ups and do **not** block Phase 4 sign-off — they failed gracefully in cycle 2 / 4 testing and have clear remediation paths.

- **Hotkey ⌥⌘V doesn't register.** Carbon `RegisterEventHotKey` listener built in Track 4F; with Accessibility granted and `voice_note.hotkey_enabled = true`, the keystroke produces no daemon-side event. Daemon `helios/menubar/hotkey.py`'s asyncio bridge needs investigation.
- **Permission revocation via System Settings toggle isn't detected.** State-machine logic verified working (cycle-2 `tccutil reset` exercises the revoked → granted → re-armed transition with regression tests). `AVCaptureDevice.authorizationStatusForMediaType_` caches per-process and macOS's "Quit & Reopen" prompt only restarts the menu bar, not the LaunchAgent daemon. Likely fix: flag `permission_revoked` from N consecutive `chunk_no_audio` mic events.
- **Short excerpt voice notes return empty transcript.** When an excerpt voice note runs entirely inside an in-progress parent-session chunk (chunks close at 30 s boundaries; only committed chunks appear in `get_session_audio_chunks`), the chunk filter returns zero matches. Fix candidates: poll briefly for the chunk to commit, plumb a `chunker.flush_partial()` into the orchestrator, or read the live audio buffer for the VN window.

## Bug-report cycle history

| Cycle | Trigger | Critical fixed | Resolved |
|---|---|---|---|
| 1 | Wave 3 review | 5 + 4 warnings | 2026-05-07 |
| 2 | §12.5 voice-note smoke | 7 (ffmpeg, sticky-perm, lambda exc, stop timeout, NSTimer target, force-stop indicator, save-window-on-force-stop) | 2026-05-12 |
| 3 | §12.5 onboarding | 4 (subprocess PYTHONPATH, verification ImportError tolerance, dead progress widget, permission status poller) | 2026-05-13 |
| 4 | §12.5 4-hour prompt | 6 (UserNotifications dep, in-bundle gate, float prompt hours, UTF-8 config reader, `last_error` reset on respond, transition-detector banner post) | 2026-05-14 |

Total cycle 1–4: 24 critical fixes + 4 warnings. Zero remaining open-critical. Three follow-ups tracked.

## Test suite

- Helios: 686 tests pass (`pytest -m "not slow"`)
- Aegis: 313 tests pass

## Phase 4 deliverables

All Track 4A–4H tasks marked complete in PHASE.md track checklist. Final bundle is `build 19` at `/Applications/Helios.app` (ad-hoc signed). Verified end-to-end on macOS 26.2.

## Next: Phase 5 — Screen OCR

`HELIOS_BUILD_PLAN.md` line 1894. Phase 5 builds:

- OCR worker with frontmost-app gating (Teams/Zoom allowlist, Slack denylist)
- Manual screen-capture override (30-min window)
- Raw-audio retention cleanup
- Per-meeting deletion from dashboard

§12.6 smoke test prerequisite: a Teams/Zoom meeting (real or contrived) with shared screen content.
