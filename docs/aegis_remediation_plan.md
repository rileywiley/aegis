# Aegis Remediation Plan — Checklist FAILs + TODO Items

## Context
User completed Phase 5 manual checklist and a separate todo list. This plan addresses all FAIL items from `docs/aegis_phase5_manual_checklist.md`, all items from `docs/todo.md`, and the search TODO note. Goal: bring Aegis to production-ready status.

---

## Priority 1: Infrastructure FAILs

### 1.1 — `aegis` CLI command not found
- **Source**: Checklist — `aegis start` / `aegis stop` FAIL
- **Root cause**: `scripts/aegis_ctl.py` exists but `pyproject.toml` has no `[project.scripts]` entry
- **Fix**: Add `[project.scripts]` to pyproject.toml OR create a shell wrapper script. User re-installs with `pip install -e .`
- **Files**: `pyproject.toml`

### 1.2 — Backup not runnable from CLI
- **Source**: Checklist — backup script FAIL
- **Root cause**: `scripts/backup.py` exists, daily 2AM schedule in `main.py:347-362`, but user didn't know about it
- **Fix**: Add `aegis backup` subcommand to `aegis_ctl.py`. Verify manual run works.
- **Files**: `scripts/aegis_ctl.py`

### 1.3 — Log files not found + LOG_LEVEL not in admin
- **Source**: Checklist — 3 logging FAILs
- **Root cause**: File logging configured in `main.py:264-290` writing to `~/.aegis/logs/aegis.log` with 10MB rotation. But log dir may not be created when running via `uvicorn` directly. LOG_LEVEL not in admin sections.
- **Fix**: Ensure `~/.aegis/logs/` is created in lifespan startup. Add `log_level` to admin `_build_sections()`.
- **Files**: `aegis/main.py`, `aegis/web/routes/admin.py`

---

## Priority 2: Feature FAILs

### 2.1 — LLM suggestions for people not populated
- **Source**: Checklist — "No LLM suggestion available yet"
- **Root cause**: `Person.llm_suggestion` field exists, template reads it, but NO code writes to it. `org_inference.py:533-604` parses signatures with regex writing directly to `title`/`org`, bypassing llm_suggestion.
- **Fix**: Add `generate_people_suggestions()` in `org_inference.py`. For each `needs_review=True` person with no suggestion: gather email/meeting/chat context, call Haiku, write to `llm_suggestion` JSONB.
- **Files**: `aegis/processing/org_inference.py`

### 2.2 — Workstream sentiment not showing on dashboard
- **Source**: Checklist — No trend arrows or sentiment dots
- **Root cause**: Code exists in `dashboard.py:120-130` and `dashboard.html:94-108`. Likely `sentiment_aggregations` table has no workstream-scoped rows, so values are None.
- **Fix**: Verify `sentiment.py` computes `scope_type='workstream'` aggregations. Add fallback display for None (gray dot). Debug why aggregation isn't populating.
- **Files**: `aegis/intelligence/sentiment.py`, `aegis/web/templates/dashboard.html`

### 2.3 — Chat messages link to /asks from workstream timeline
- **Source**: Checklist — FAIL
- **Root cause**: `workstream_detail.html:116-117` hardcodes `{% set item_href = '/asks' %}` for chat_message type
- **Fix**: Create a chat message detail route or link to source chat with context. Simplest: `/asks?source=chat&q={preview}` or new `/chat-messages/{id}` detail view.
- **Files**: `aegis/web/templates/workstream_detail.html`, possibly new route

### 2.4 — No unassigned items queue
- **Source**: Checklist — FAIL
- **Root cause**: No route or template exists
- **Fix**: Add `/workstreams/unassigned` route. Query items with `processing_status='completed'` that have no `workstream_items` entry. Show with manual assign dropdown.
- **Files**: `aegis/web/routes/workstreams.py`, new template

### 2.5 — No internal vs external ask filtering
- **Source**: Checklist — FAIL
- **Fix**: Join asks with `people.is_external` on requester/target. Add "All / Internal / External" dropdown to asks page.
- **Files**: `aegis/web/routes/asks.py`, `aegis/web/templates/asks.html`

---

## Priority 3: TODO Items (from docs/todo.md)

### 3.1 — Meeting briefs formatting and content (#1)
- **Fix**: Review and improve prompts in `aegis/intelligence/briefings.py`. Better HTML structure for web display.
- **Files**: `aegis/intelligence/briefings.py`

### 3.2 — Purge non-org people (#2)
- **Fix**: Add configurable `org_email_domains` setting. Cleanup script to mark non-@hawthorneheath.com as `is_external=True`. Improve resolver to auto-flag external on ingestion.
- **Files**: `aegis/config.py`, `aegis/processing/resolver.py`, cleanup script

### 3.3 — Close/complete actions/asks + auto-close from replies (#3)
- **Root cause**: Inline click-to-cycle status exists. Missing: explicit Complete/Cancel buttons, auto-close from email replies.
- **Fix**: Add clear Complete/Cancel buttons. In `thread_analyzer.py`: when reply detected to email with open asks, auto-mark resolved.
- **Files**: `aegis/web/templates/actions.html`, `aegis/web/templates/asks.html`, `aegis/processing/thread_analyzer.py`

### 3.4 — Admin setting descriptions (#4)
- **Root cause**: Descriptions defined in `admin.py _build_sections()` but may not render in template.
- **Fix**: Verify `admin.html` renders `field.description`. Add if missing.
- **Files**: `aegis/web/templates/admin.html`

### 3.5 — RAG not using recent data (#5)
- **Fix**: Check `rag.py` for date cutoffs. Verify recent items have embeddings. Boost recency weight in ranking formula.
- **Files**: `aegis/chat/rag.py`

### 3.6 — Ask source view with chat context (#6)
- **Fix**: Create `/asks/{ask_id}` detail view. For chat asks: query surrounding 5 messages before/after in same chat_id.
- **Files**: `aegis/web/routes/asks.py` (new detail route), new template `ask_detail.html`

### 3.7 — Department: trash icon + free text search (#7)
- **Fix**: Add trash icon per person row (POST to remove from dept). Replace `<select>` with HTMX-powered text search input.
- **Files**: `aegis/web/templates/department_detail.html`, `aegis/web/routes/departments.py`

### 3.8 — Manual workstream detection trigger (#8)
- **Fix**: Add "Detect Workstreams" button on `/workstreams` that POSTs to trigger `workstream_detector.run_detection()`.
- **Files**: `aegis/web/routes/workstreams.py`, `aegis/web/templates/workstreams.html`

### 3.9 — Manual workstream item scan (#9)
- **Fix**: Add "Find Related Items" button on workstream detail that triggers assignment scan for that workstream.
- **Files**: `aegis/web/routes/workstreams.py`, `aegis/web/templates/workstream_detail.html`

### 3.10 — Ask/action size thresholds (#10)
- **Fix**: Add admin settings: `extraction_min_ask_confidence`, `extraction_min_action_confidence`. Document in admin descriptions.
- **Files**: `aegis/config.py`, `aegis/web/routes/admin.py`, extraction files

### 3.11 — Nudge box on command center (#11)
- **Fix**: Add dedicated "Today's Nudges" section. Filter drafts where `draft_type='nudge'`. Add postpone (snooze 24h) action alongside send/delete.
- **Files**: `aegis/web/templates/dashboard.html`, `aegis/web/routes/dashboard.py`, `aegis/db/models.py` (add snoozed_until), migration

### 3.12 — Chat search shows sender name (#12)
- **Root cause**: Search doesn't join Person table for chat sender_id. Falls back to summary (often NULL).
- **Fix**: Join Person on sender_id. Set title to `"{sender}: {body_preview[:50]}"`.
- **Files**: `aegis/web/routes/search.py`

---

## Priority 4: Search TODO

### 4.1 — Email detail collapsible body
- **Source**: Phase 5 checklist search observation
- **Fix**: Add `<details><summary>Full Email Body</summary>` section to `email_detail.html` showing `email.body_text`.
- **Files**: `aegis/web/templates/email_detail.html`

---

## Implementation Order

**Batch 1 — Quick wins** (< 30 min each):
1.1, 1.3, 3.4, 3.12, 4.1

**Batch 2 — Medium effort** (30-60 min each):
1.2, 2.3, 2.5, 3.2, 3.7, 3.8, 3.9

**Batch 3 — Larger features** (1-2 hours each):
2.1, 2.2, 2.4, 3.3, 3.5, 3.6, 3.11

**Batch 4 — Iterative** (needs user feedback):
3.1, 3.10

## Verification
- Re-run Phase 5 manual checklist — all FAILs should be PASS
- Verify each todo.md item addressed
- Run `pytest` for regressions
- Start via `aegis start`, verify logs at `~/.aegis/logs/aegis.log`
