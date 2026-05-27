# Phase 6 — Helios Dashboard — CHECKPOINT 2026-05-27

Phase 6 (Helios Dashboard at `/helios` + Voice Notes UI + Briefings/RAG integration + diagnostic endpoints) is complete. The §12.7 smoke walkthrough passed live across all six dashboard pages, the HF speaker-identification wizard, the voice-notes pages, and the Aegis chat (RAG) integration, after one bug-report cycle and a full-pass deferral cleanup.

Test counts at sign-off: **450 Aegis pass, 787 Helios pass**.

## Waves shipped

| Wave | Tracks | Result |
|---|---|---|
| 1 — Foundation | 6A (tri-state `helios_exclude` migration + HeliosClient extension) | done 2026-05-15 |
| 2 — Build (parallel) | 6B (`/helios/*` routes + 6 templates + partials + speaker-name resolver), 6D (real diagnostic endpoints + `/v1/sessions/{id}/audio-chunks` + audio streaming + `re-transcribe`/`re-diarize` + `bundle` + extended `/v1/sessions` filters) | done 2026-05-15 |
| 3 — Build (parallel) | 6C (settings form + atomic TOML mutation seam + HF wizard 5-step), 6E (voice notes routes/templates + briefings integration + RAG corpus + profile additions) | done 2026-05-20 |
| 4 — Review + Repair | Plan-agent adversarial review → 5 critical + 7 warning fixes + migration `7a91f44b2c10` (split `voice_note_attachments.target_type='ask'` into `email_ask`/`chat_ask`) | done 2026-05-20 |
| 5 — §12.7 smoke + deferral cleanup | User-driven walkthrough across all six pages + 7 deferral fixes shipped before checkpoint | done 2026-05-27 |

## Schema changes

- **Migration `892742dc301d_helios_exclude_tristate.py`** — converts `meetings.helios_exclude` to nullable Boolean. Tri-state semantics: `NULL` = use keyword exclusion (default), `True` = always exclude, `False` = always include / override keywords. Resets legacy `false` rows to `NULL` so the previous bool default doesn't override the new "use keywords" semantic.
- **Migration `7a91f44b2c10_split_voice_note_attachment_ask_type.py`** — splits `voice_note_attachments.target_type='ask'` into `'email_ask'`/`'chat_ask'`. `EmailAsk` and `ChatAsk` use independent PK sequences, so the unified `'ask'` target type collided across the two tables. Migration detected 388 collisions in the dev DB, classified them as `email_ask` for legacy resolver behavior, and dropped the legacy `ask` value from the check constraint.

## Pages live at `/helios`

- `/helios` — overview (status pill HTMX-polled every 5s, system health row, today timeline, today's voice notes block, upcoming events)
- `/helios/sessions` — list + filters (date/kind/status); rows enriched with linked Aegis meeting titles
- `/helios/sessions/{id}` — Transcript / OCR / Audio tabs + Actions panel (re-transcribe, re-diarize, delete, export, link to Aegis meeting)
- `/helios/calendar` — 7-day forward view + per-meeting tri-state toggle (writes `meetings.helios_exclude`)
- `/helios/diagnostics` — full system state + 6 action buttons (Copy Diagnostics, Download Bundle, Test Capture self-test with HTMX polling, Restart Daemon, Flush Queues, Reload Component)
- `/helios/settings` — TOML editor with hot-reload vs restart-required classification + HF speaker-ID wizard

Aegis-native voice notes live at `/voice-notes` (list) and `/voice-notes/{id}` (detail with inline transcript edit + re-extraction + audio player + attachments). Voice notes are also surfaced on the Aegis daily timeline, in workstream/ask profile sidebars, in morning/Monday/Friday briefings, and in the RAG corpus at `/ask`.

## Notable behavior decisions

**HOT_RELOADABLE_FIELDS classification.** Mirrors HELIOS.md §5.4 verbatim — exclusion keywords, OCR `meeting_apps` + `gate_by_allowlist` + thresholds, retention values, notification settings, logging level. `capture.*`, `voice_note.*`, and any unknown field trigger the amber "Restart Daemon" banner; the daemon has no hot-reload handler for those fields.

**Bearer-token write protection.** `aegis/web/routes/_helios_settings_helpers.PROTECTED_FIELDS` includes `("api", "bearer_token")`. The generic settings POST handler skips this key unless a future "Regenerate token" flow explicitly passes `allow_bearer_overwrite=True`. Prevents a hand-crafted POST from overwriting the live bearer.

**Hotkey enable gating.** Toggling `voice_note.hotkey_enabled` ON without Accessibility access does not persist. Server-side guard in the settings POST checks `check_accessibility_granted()` before letting the field land in TOML; client-side script in the deep-link partial force-unchecks the box and an amber banner explains why the toggle didn't stick.

**Voice-note resolver word-boundary gate.** `aegis/processing/resolver.py` no longer uses `partial_ratio` against short (≤ 5 char) first names. Short candidates require a literal word-boundary token match; longer names continue to use `token_set_ratio`/`partial_ratio`. Earlier behavior auto-attached 100-250 people per 60s voice note because "Tom" matched "tomorrow". Cleaned 1119 spurious suggested attachments during the smoke.

**RAG voice-note scoring.** `triage_weight=1.3` for voice notes in `_semantic_search` composite score. Voice notes are user-spoken so they're preferred when scores are close. Bumped from the original `1.0` after the smoke showed voice notes routinely fell outside top-15 even when relevant.

**OCR gate default.** Carried over from Phase 5: `OcrConfig.gate_by_allowlist: bool = False`. The Settings UI exposes the toggle so users can flip it back on for strict allowlist gating. See Phase 5 follow-up task #22 for the screen-share-aware gating refactor.

**Audio playback.** Browser `<audio>` can't send bearer headers, so Aegis proxies the daemon's WAV stream at `/helios/sessions/{sid}/audio/{cid}`. Mic-only chunks render on the voice-note detail page; full mic + system chunk list renders on the Helios session detail page. Archived chunks (`path=NULL` after retention cleanup) render as "—" with no player.

## §12.7 smoke results (2026-05-27)

| Checklist item | Result |
|---|---|
| Overview + HTMX live status pill | ✓ |
| Sessions list with date/kind/status filters + meeting-name enrichment | ✓ |
| Session detail (Transcript / OCR / Audio tabs) | ✓ — date format + speaker resolution shipped as part of deferral cleanup |
| Calendar tri-state toggle (Default / Always exclude / Always include) | ✓ — verified via DB query showing `helios_exclude` flipping |
| Diagnostics page actions (Copy / Bundle / Test Capture polling / Restart / Flush / Reload) | ✓ — Wave 4's per-endpoint pill rendering held up |
| Settings — hot-reloadable, restart-required, and voice notes sections | ✓ — chip add via Alpine.js; hotkey gate works; bearer-token write blocked |
| HF wizard 5 steps end-to-end + token validation + reload-component | ✓ — fixed keychain location mismatch + step5 completion state + reset button |
| Diarization on next session | ✓ — component shows `ok` in `/v1/status` (DB-backed components view) and `/v1/diagnostics` |
| Voice notes list + detail (transcript edit, audio player, re-extract) | ✓ — fixed missing date column + audio player + over-attached people |
| RAG chat surfaces voice notes | ✓ — fixed `SELECT DISTINCT ORDER BY` silent failure + tuned scoring |

Non-blocking items not exercised: briefing voice-note integration (requires waiting for next scheduled run), and per-person profile section (no person detail page exists in Aegis — logged as future scope).

## Deferral cleanup (2026-05-27)

All open `[ ]` items in `docs/updates.md` flagged during the smoke were resolved before sign-off:

- Session detail transcript times → absolute clock time via `local_dt` filter
- Speaker name resolution → time-overlap fallback in `helios_session_detail`
- Settings chip add-button → Alpine.js components (exclusion + OCR)
- Hotkey toggle without Accessibility → server-side drop + client-side uncheck + warning banner
- `/v1/status` components stale → reads from `component_status` table
- Asks filter missing "voice notes" source → wired through `get_all_asks`
- RAG voice-note composite scoring → `triage_weight=1.3`

## Bug-report cycles

1. **Wave 4 review (2026-05-20):** 5 critical + 7 warning + 2 style issues found by Plan-agent adversarial review. 12 of 14 fixed in repair cycle 1 (style items deferred per scope). No escalations.
2. **§12.7 smoke (2026-05-27):** 13 bugs surfaced during live walkthrough; all 13 resolved before checkpoint (12 logged in `docs/updates.md` plus the wizard keychain key mismatch).

## Test counts evolution

- Pre-Phase-6 baseline: 313 Aegis, 759 Helios
- Wave 1 (6A): 344 Aegis, 759 Helios
- Wave 2 (6B + 6D): 374 Aegis, 787 Helios
- Wave 3 (6C + 6E): 429 Aegis, 787 Helios
- Wave 4 repair cycle 1: 449 Aegis, 787 Helios
- Deferral cleanup: **450 Aegis, 787 Helios** (final)

## Known limitations carried into Phase 7

- **Hotkey ⌥⌘V doesn't register** (task #9 from Phase 4). Carbon hotkey API path needs investigation — Accessibility grant is wired but the actual key combo doesn't fire. The settings UI now correctly gates the toggle on AX permission so the daemon doesn't enter a misleading "hotkey enabled" state.
- **Excerpt voice note empty transcript for short notes** (task #13). Chunks close at 30s boundaries; excerpt mode doesn't force-flush.
- **Mic revocation detection via no_audio runs** (task #14). AVFoundation per-process cache means the daemon can't see mid-session revocation. Need an N-consecutive-`chunk_no_audio` heuristic.
- **OCR gating during Teams screen-share** (task #22). Current default is allowlist-off because Teams desktop minimizes during share. A screen-share-aware gate (e.g. NSWorkspace activeApplications history) would let the strict allowlist work again.
- **Per-person profile page doesn't exist in Aegis.** Voice notes attach to people but there's no `/people/{id}` detail page to surface them. New scope for Phase 7 if person profiles become a priority.

## Bundle versions at sign-off

- Helios.app build 32 (rebuilt + reinstalled 2026-05-27 after `_components_view` DB-backed switch + new audio-chunks endpoint)
- Aegis uvicorn restarted with Track 6E voice notes integration + all deferral fixes loaded

## Next phase

Phase 7 — Hardening (`HELIOS_BUILD_PLAN.md:2322`). 8-hour continuous-capture stress test, calendar-day stress test (5-10 real meetings), and final polish.
