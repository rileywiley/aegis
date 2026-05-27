# Phase 5 — Screen OCR — CHECKPOINT 2026-05-15

Phase 5 (Screen OCR + Retention + Per-meeting Delete) is complete. The §12.6 smoke walkthrough passed on the live daemon after one default-change cycle.

## Tracks shipped

| Track | Owner | Module(s) | Status |
|---|---|---|---|
| 5A — OCR worker | parallel agent | `helios/workers/ocr.py`, `helios/workers/frontmost.py` | done |
| 5B — Screen-capture override | parallel agent | `helios/api/routes/capture.py` (`enable_screen_override`), `helios/scheduler/scheduler.py` (`set_screen_capture_override`) | done |
| 5C — Retention + per-meeting delete | parallel agent | `helios/workers/cleanup.py`, `helios/api/routes/sessions.py` (`DELETE /v1/sessions/{id}`) | done |

Daemon integration: per-session OCR worker via `orchestrator.set_ocr_worker_factory(factory)` — orchestrator spawns the worker in `start_session`, stops it in `stop_session`. Cleanup worker wired in the lifespan; runs daily at `cleanup_hour_local`.

## Default behavior change (2026-05-15)

`OcrConfig.gate_by_allowlist: bool = False` is the new default. The original strict-allowlist behavior (only OCR while a `meeting_apps` bundle id is frontmost) is now opt-in.

**Why.** Teams desktop minimizes during screen-share and hands frontmost to whatever app is being shared. In the §12.6 walkthrough the live daemon drained 190 SCK video packets/min but persisted 0 OCR frames because every frame's frontmost was `com.apple.Safari` (the user's shared browser tab) — `Safari` isn't in the allowlist, so every frame fell through. Adding browsers to the list isn't a real fix; the same starvation happens whenever Teams shares a different app.

**How it works.** `gate_allows_frame(... gate_by_allowlist=False)` returns True for every frame regardless of frontmost. The override-until path still works orthogonally. Daemon writes its current config to `OcrConfig.gate_by_allowlist`; users who want the privacy-tight allowlist behavior can set `gate_by_allowlist = true` in their `[ocr]` section of capture.toml.

**Follow-up.** Task #22 tracks a smarter, screen-share-aware gate (e.g. detect that SCK is in a share-window session via NSWorkspace activeApplications history + ScreenCaptureKit metadata, and treat "Teams was frontmost in last N seconds" as still-in-meeting).

## §12.6 smoke results (2026-05-15)

Verified on the live daemon (`/Applications/Helios.app`, port 3031) after rebuild with `gate_by_allowlist=False`:

| Item | Result |
|---|---|
| OCR with allowlist bypass | session 80 (manual_screen, 60s) — 29 ocr_frames persisted, `app_bundle=com.jetbrains.pycharm`, avg_confidence=0.5 |
| Manual override (Track 5B) | implicit pass — override path is unchanged; gate is now open by default so override gives the same behavior |
| Cleanup with `raw_audio_days=0` | `scripts/run_cleanup_smoke.py` → archived=935 (transcribed→trash), skipped_untranscribed=275 (transcription_failed preserved), purged=884 (sweep_trash on previously-trashed >24h files), errors=0. Post-state: 935 transcribed rows with `path=NULL`, 0 transcribed-with-path, transcription_failed paths untouched. |
| Per-meeting deletion | `DELETE /v1/sessions/80` → 200, `chunks_trashed=0` (already archived by cleanup), session row + 4 audio_chunks + 29 ocr_frames all removed |

Driver script for the cleanup item: `scripts/run_cleanup_smoke.py` — connects to the live `index.db`, overrides retention in-memory, runs `CleanupWorker.run_cleanup` once, prints a report. The on-disk `capture.toml` was not modified (still `raw_audio_days = 7`).

## Test counts at sign-off

- Helios: 759 pass (Phase 5 added test_ocr_gating.py + test_ocr_worker.py + test_cleanup.py + test_sessions_delete.py + test_screen_override.py)
- Diagnostic `_log.debug` lines added during the §12.6 investigation were stripped from `workers/ocr.py:_handle_frame` and `sources/real.py:_read_loop` before commit.

## Known limitations carried into Phase 6

- Allowlist gating is off by default — Phase 6 dashboard should surface the `gate_by_allowlist` toggle so a user can flip it back on once Phase 6 has a UI for it. See task #22.
- `helios/scripts/smoke_phase_5.sh` was not wired up (the smoke checklist in `HELIOS_BUILD_PLAN.md:2615` references it but the file doesn't exist). Manual walkthrough used in lieu. Phase 6 dashboard will replace these CLI smoke scripts with UI flows.

## Bug-report cycles

None. Phase 5 closed in a single build → review → smoke cycle, with the gate-default flip applied mid-smoke (build 26).

## Next phase

Phase 6 — Helios Dashboard (`HELIOS_BUILD_PLAN.md:2050`). Adds the `/helios` six-page dashboard under Aegis, including the OCR frame viewer, retention/diagnostics page, and the Settings UI that should expose `ocr.gate_by_allowlist` as a top-level toggle.
