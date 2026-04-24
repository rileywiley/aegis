# Claude Code Prompt: Phase 3+4+5 Combined Verification Script

## Task

Build `scripts/verify_phase5.py` — a comprehensive verification script that re-runs ALL Phase 3 checks, ALL Phase 4 checks, and adds Phase 5 (Polish + Hardening) checks. This is the final pre-production verification. One script validates the entire system.

This replaces the need to run `verify_phase3.py` or `verify_phase4.py` separately.

## Requirements

Same patterns as previous verification scripts: use `rich`, read-only SELECT queries only, support `--verbose` and `--fix-suggestions` flags, complete in <15 seconds.

Additional flag: `--manual-checklist` prints the manual walkthrough to the terminal.

## Output Format

Use `rich` for formatted terminal output. Organize into three parts with clear dividers:

```
╔══════════════════════════════════════════════════════════╗
║       AEGIS — Full System Verification Report            ║
║       Generated: 2026-04-22 10:32:00 EST                 ║
║       Phase 3 + Phase 4 + Phase 5                        ║
╚══════════════════════════════════════════════════════════╝
```

## Checks To Run

---

### ═══ PART A: PHASE 3 RE-CHECKS (Sections 1-14) ═══

All Phase 3 checks with production-grade thresholds. The system has been running for days/weeks at this point. No more "might not have had enough time" leniency.

---

#### SECTION 1: SERVICE HEALTH (Phase 3 services)

```sql
SELECT service, status, last_success, last_error, last_error_message,
       items_processed_last_hour
FROM system_health
WHERE service IN (
    'email_poller', 'teams_poller', 'calendar_sync',
    'triage_batch', 'workstream_detector', 'extraction_pipeline'
)
ORDER BY service;
```

**PASS**: all Phase 3 services healthy, last_success within expected interval
**WARNING**: any service degraded
**FAIL**: any service down or missing

---

#### SECTION 2: EMAIL INGESTION

```sql
SELECT email_class, COUNT(*) FROM emails GROUP BY email_class;
```

**PASS**: total emails > 100, all 3 classes present, human is 20-50%
**WARNING**: total 50-100
**FAIL**: emails table empty or total < 50

---

#### SECTION 3: EMAIL TRIAGE

```sql
SELECT triage_class, COUNT(*), ROUND(AVG(triage_score)::numeric, 2)
FROM emails WHERE email_class = 'human' GROUP BY triage_class;
```

**PASS**: all 3 classes present, substantive 20-50% of human emails
**WARNING**: distribution heavily skewed
**FAIL**: triage_class NULL on all human emails

---

#### SECTION 4: EMAIL EXTRACTION & ASK DIRECTIONALITY

```sql
SELECT COUNT(*) as total,
       COUNT(*) FILTER (WHERE requester_id IS NOT NULL AND target_id IS NOT NULL) as has_both
FROM email_asks;
```

Identify user's person record, then:

```sql
SELECT COUNT(*) FROM email_asks WHERE target_id = {user_person_id};
SELECT COUNT(*) FROM email_asks WHERE requester_id = {user_person_id};
```

**PASS**: email_asks > 10, >50% have both directions, both inbound/outbound exist
**WARNING**: asks exist but <50% directionality
**FAIL**: empty or 0% directionality

---

#### SECTION 5: EMAIL THREAD RESOLUTION

```sql
SELECT COUNT(*) FILTER (WHERE status = 'completed' AND resolved_by_email_id IS NOT NULL) as resolved,
       COUNT(*) FILTER (WHERE status = 'open') as still_open,
       COUNT(*) as total
FROM email_asks;
```

**PASS**: resolved > 0
**WARNING**: zero resolved but open asks exist
**FAIL**: no asks at all

---

#### SECTION 6: TEAMS INGESTION

```sql
SELECT source_type, COUNT(*) as total,
       COUNT(*) FILTER (WHERE noise_filtered = true) as filtered,
       COUNT(*) FILTER (WHERE noise_filtered = false) as kept
FROM chat_messages GROUP BY source_type;
```

**PASS**: both source types > 20 each, some noise filtered
**WARNING**: one source type has 0
**FAIL**: chat_messages empty

---

#### SECTION 7: TEAMS TRIAGE

```sql
SELECT triage_class, COUNT(*)
FROM chat_messages WHERE noise_filtered = false GROUP BY triage_class;
```

**PASS**: triage classes distributed
**FAIL**: all NULL triage_class

---

#### SECTION 8: CHAT ASKS

```sql
SELECT COUNT(*) as total,
       COUNT(*) FILTER (WHERE requester_id IS NOT NULL) as has_requester,
       COUNT(*) FILTER (WHERE target_id IS NOT NULL) as has_target
FROM chat_asks;
```

**PASS**: chat_asks > 0 with directionality
**WARNING**: exist but no directionality
**FAIL**: empty (WARNING if all chat messages are contextual/noise)

---

#### SECTION 9: TEAMS MEMBERSHIP & ORG

```sql
SELECT COUNT(*) as teams FROM teams;
SELECT COUNT(*) as channels FROM team_channels;
SELECT COUNT(*) as memberships FROM team_memberships;

SELECT name, source, confidence,
       (SELECT COUNT(*) FROM people p WHERE p.department_id = d.id) as member_count
FROM departments d ORDER BY member_count DESC;
```

**PASS**: teams > 0, channels > 0, memberships > 0, departments with members exist
**WARNING**: teams exist but no departments
**FAIL**: teams table empty

---

#### SECTION 10: PEOPLE TABLE HEALTH

```sql
SELECT source, COUNT(*),
       COUNT(*) FILTER (WHERE needs_review = true) as needs_review,
       COUNT(*) FILTER (WHERE is_external = true) as external
FROM people GROUP BY source;

SELECT COUNT(*) FILTER (WHERE department_id IS NOT NULL) as has_dept,
       COUNT(*) as total FROM people;

SELECT name, COUNT(*) as records FROM people GROUP BY name HAVING COUNT(*) > 1;
```

**PASS**: people from 3+ sources, >30% have departments, <5 duplicates
**WARNING**: only 1-2 sources or >10 duplicates
**FAIL**: people table empty

---

#### SECTION 11: WORKSTREAM AUTO-DETECTION

```sql
SELECT created_by, status, COUNT(*) FROM workstreams GROUP BY created_by, status;

SELECT name, confidence, status,
       (SELECT COUNT(*) FROM workstream_items wi WHERE wi.workstream_id = w.id) as items
FROM workstreams w WHERE created_by = 'auto' ORDER BY items DESC;

SELECT COUNT(*) FROM (
    SELECT item_type, item_id FROM workstream_items
    GROUP BY item_type, item_id HAVING COUNT(*) > 1
) multi;
```

**PASS**: 3+ auto-detected workstreams, at least 1 with 5+ items, multi-membership exists
**WARNING**: 1-2 auto workstreams or all <5 items
**FAIL**: zero auto-detected workstreams

---

#### SECTION 12: EMBEDDINGS

```sql
SELECT 'meetings' as type,
       COUNT(*) FILTER (WHERE embedding IS NOT NULL) as has, COUNT(*) as total
FROM meetings WHERE processing_status = 'completed'
UNION ALL
SELECT 'emails', COUNT(*) FILTER (WHERE embedding IS NOT NULL), COUNT(*)
FROM emails WHERE triage_class IN ('substantive','contextual')
UNION ALL
SELECT 'chat_messages', COUNT(*) FILTER (WHERE embedding IS NOT NULL), COUNT(*)
FROM chat_messages WHERE triage_class IN ('substantive','contextual');
```

**PASS**: >90% coverage across all types
**WARNING**: 70-90%
**FAIL**: <70%

---

#### SECTION 13: LLM COST (cumulative)

```sql
SELECT model, task, SUM(input_tokens) as input_tok, SUM(output_tokens) as output_tok,
       SUM(calls) as total_calls
FROM llm_usage WHERE date >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY model, task ORDER BY total_calls DESC;
```

Compute costs. Haiku: $0.25/$1.25 per M. Sonnet: $3/$15 per M. Embeddings: $0.02 per M.

**PASS**: tracking active, weekly cost < $20
**WARNING**: $20-35
**FAIL**: empty or > $35

---

#### SECTION 14: CROSS-SYSTEM INTEGRATION

```sql
SELECT COUNT(*) FILTER (WHERE sender_id IS NOT NULL) as resolved,
       COUNT(*) as total
FROM emails WHERE email_class = 'human';

SELECT COUNT(*) FILTER (WHERE sender_id IS NOT NULL) as resolved,
       COUNT(*) as total
FROM chat_messages WHERE noise_filtered = false;

SELECT COUNT(*) FROM email_asks WHERE linked_action_item_id IS NOT NULL;
SELECT COUNT(*) FROM chat_messages WHERE linked_meeting_id IS NOT NULL;
```

**PASS**: >80% sender resolution for both emails and chats
**WARNING**: 50-80%
**FAIL**: <50%

---

### ═══ PART B: PHASE 4 RE-CHECKS (Sections 15-28) ═══

---

#### SECTION 15: SCHEDULER HEALTH (Phase 4 services)

```sql
SELECT service, status, last_success, last_error, last_error_message,
       items_processed_last_hour
FROM system_health
WHERE service IN (
    'morning_briefing', 'monday_brief', 'friday_recap',
    'meeting_prep', 'draft_generator', 'sentiment_aggregator',
    'readiness_scorer', 'dashboard_cache'
)
ORDER BY service;
```

**PASS**: at least 4 intelligence services healthy with recent last_success
**WARNING**: services registered but some never ran
**FAIL**: zero intelligence services in system_health

---

#### SECTION 16: BRIEFINGS GENERATED

```sql
SELECT briefing_type, COUNT(*), MAX(generated_at) as most_recent,
       AVG(LENGTH(content)) as avg_length
FROM briefings GROUP BY briefing_type;
```

**PASS**: at least morning + meeting_prep types exist with avg_length > 500
**WARNING**: only 1 type or very short content
**FAIL**: briefings table empty

---

#### SECTION 17: MEETING PREP PRE-GENERATION

```sql
SELECT m.title, m.start_time, b.generated_at,
       CASE WHEN b.generated_at < m.start_time THEN 'pre-generated'
            ELSE 'late' END as timing
FROM briefings b
JOIN meetings m ON b.related_meeting_id = m.id
WHERE b.briefing_type = 'meeting_prep'
ORDER BY m.start_time DESC LIMIT 10;
```

**PASS**: >80% pre-generated before meeting start
**WARNING**: 50-80% pre-generated
**FAIL**: <50% or no prep briefs linked to meetings

---

#### SECTION 18: MORNING BRIEFING CONTENT

```python
latest = get_latest_briefing('morning')
content = latest.content.lower()
has_calendar = any(w in content for w in ['meeting', 'calendar', 'today', 'schedule'])
has_actions = any(w in content for w in ['action', 'overdue', 'pending', 'awaiting'])
has_workstreams = any(w in content for w in ['workstream', 'active', 'status'])
has_topics = any(w in content for w in ['address', 'discuss', 'raise', 'topic', 'agenda'])
```

**PASS**: all 4 sections present
**WARNING**: missing 1-2 sections
**FAIL**: missing 3+ or content is generic boilerplate

---

#### SECTION 19: VOICE PROFILE

```sql
SELECT id, LENGTH(auto_profile) as profile_length,
       LEFT(auto_profile, 300) as preview,
       array_length(custom_rules, 1) as custom_rules,
       last_learned_at
FROM voice_profile LIMIT 1;
```

**PASS**: profile exists, length > 200
**WARNING**: exists but short (<100) or last_learned_at NULL
**FAIL**: voice_profile table empty

---

#### SECTION 20: DRAFT GENERATION

```sql
SELECT draft_type, status, COUNT(*) FROM drafts GROUP BY draft_type, status;

SELECT COUNT(*) as stale_action_items FROM action_items
WHERE status = 'open' AND updated < NOW() - INTERVAL '7 days';

SELECT COUNT(*) as stale_asks FROM email_asks
WHERE status = 'open' AND created < NOW() - INTERVAL '72 hours';
```

**PASS**: nudge drafts exist, or no stale items to nudge
**WARNING**: stale items exist but no nudge drafts generated
**FAIL**: drafts table empty AND stale items exist

---

#### SECTION 21: RESPONSE WORKFLOW

```sql
SELECT COUNT(*) FILTER (WHERE status = 'sent') as sent,
       COUNT(*) FILTER (WHERE status = 'pending_review') as pending,
       COUNT(*) FILTER (WHERE conversation_id IS NOT NULL) as has_email_thread,
       COUNT(*) FILTER (WHERE chat_id IS NOT NULL) as has_chat_thread,
       COUNT(*) as total
FROM drafts;
```

**PASS**: drafts exist with threading data
**WARNING**: drafts exist but no threading data
**FAIL**: no drafts at all

---

#### SECTION 22: READINESS SCORES

```sql
SELECT key, LENGTH(data::text) as data_size, computed_at
FROM dashboard_cache WHERE key = 'readiness_scores';
```

Parse JSONB and display scores. Check score diversity (not all identical).

**PASS**: cached with 3+ people, varied scores 0-100
**WARNING**: cached but only 1-2 people or all scores identical
**FAIL**: no readiness_scores in cache

---

#### SECTION 23: SENTIMENT AGGREGATION

```sql
SELECT scope_type, COUNT(*), ROUND(AVG(avg_score)::numeric, 1) as mean,
       MIN(period_start) as earliest, MAX(period_end) as latest
FROM sentiment_aggregations GROUP BY scope_type;

SELECT scope_id, avg_score, trend, interaction_count
FROM sentiment_aggregations
WHERE scope_type = 'cross_department' AND avg_score < 65
ORDER BY avg_score ASC LIMIT 5;
```

**PASS**: entries for 2+ scope_types
**WARNING**: only 1 scope_type or all identical scores
**FAIL**: table empty

---

#### SECTION 24: RAG CHAT

```sql
SELECT COUNT(*) as sessions,
       COUNT(*) FILTER (WHERE last_active > NOW() - INTERVAL '24 hours') as recent
FROM chat_sessions;
```

Vector search functional test:
```sql
SELECT COUNT(*) FILTER (WHERE embedding IS NOT NULL) as searchable
FROM (
    SELECT embedding FROM meetings WHERE embedding IS NOT NULL
    UNION ALL SELECT embedding FROM emails WHERE embedding IS NOT NULL
    UNION ALL SELECT embedding FROM chat_messages WHERE embedding IS NOT NULL
) all_emb;
```

**PASS**: >100 searchable items
**WARNING**: 10-100
**FAIL**: <10

---

#### SECTION 25: DASHBOARD CACHE

```sql
SELECT key, computed_at,
       EXTRACT(EPOCH FROM (NOW() - computed_at)) / 60 as minutes_stale,
       LENGTH(data::text) as data_size
FROM dashboard_cache ORDER BY key;
```

Expected keys: workstream_cards, pending_decisions, awaiting_response, stale_items, todays_meetings, drafts_pending, readiness_scores, department_health.

**PASS**: 6+ of 8 keys present, all refreshed within 20 min
**WARNING**: 3-5 keys or some >30 min stale
**FAIL**: 0-2 keys present

---

#### SECTION 26: NOTIFICATION CHANNELS

```sql
SELECT key, value FROM admin_settings
WHERE key IN ('notify_macos', 'notify_email_self', 'notify_teams_self');
```

**PASS**: at least macOS enabled
**WARNING**: all disabled
**FAIL**: settings don't exist

---

#### SECTION 27: LLM COST (Phase 4 tasks)

```sql
SELECT model, task, SUM(calls) as calls, SUM(input_tokens) as input, SUM(output_tokens) as output
FROM llm_usage
WHERE task IN ('briefing', 'meeting_prep', 'monday_brief', 'friday_recap',
               'draft_generation', 'response_draft', 'voice_profile',
               'rag_chat', 'sentiment', 'readiness')
  AND date >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY model, task ORDER BY calls DESC;
```

**PASS**: Phase 4 tasks in usage, weekly Sonnet cost < $20
**WARNING**: no Phase 4 tasks or cost > $20
**FAIL**: llm_usage empty

---

#### SECTION 28: END-TO-END FLOW

```sql
SELECT m.title, m.start_time, m.transcript_status, m.processing_status,
       (SELECT COUNT(*) FROM action_items WHERE source_meeting_id = m.id) as actions,
       (SELECT COUNT(*) FROM decisions WHERE source_meeting_id = m.id) as decisions,
       (SELECT COUNT(*) FROM workstream_items WHERE item_type = 'meeting' AND item_id = m.id) as workstreams,
       (SELECT COUNT(*) FROM briefings WHERE related_meeting_id = m.id AND briefing_type = 'meeting_prep') as preps,
       m.embedding IS NOT NULL as has_embedding
FROM meetings m WHERE m.processing_status = 'completed'
ORDER BY m.start_time DESC LIMIT 5;
```

**PASS**: at least 1 meeting has all pipeline stages complete
**WARNING**: some but not all stages
**FAIL**: no meetings through full pipeline

---

### ═══ PART C: PHASE 5 CHECKS (Sections 29-42) ═══

Phase 5 is about reliability, security, admin controls, and production readiness.

---

#### SECTION 29: ERROR HANDLING & RETRY LOGIC

Check if services recover from transient failures. Look at error history:

```sql
-- Services that have had errors but recovered
SELECT service, 
       COUNT(*) FILTER (WHERE last_error IS NOT NULL) as has_had_errors,
       status as current_status,
       last_success,
       last_error
FROM system_health
GROUP BY service, status, last_success, last_error;

-- Any services currently in error state
SELECT service, status, last_error, last_error_message
FROM system_health
WHERE status IN ('degraded', 'down');
```

**PASS**: all services currently healthy (even if they've had errors in the past — they recovered)
**WARNING**: any service degraded (transient, may self-recover)
**FAIL**: any service down for >1 hour

---

#### SECTION 30: GRAPH API RATE LIMIT HANDLING

Check if rate limit responses have been encountered and handled:

```sql
-- Look for rate-limit related errors in system health
SELECT service, last_error_message
FROM system_health
WHERE last_error_message LIKE '%429%' 
   OR last_error_message LIKE '%rate%' 
   OR last_error_message LIKE '%throttl%'
   OR last_error_message LIKE '%Retry-After%';
```

Also check if the GraphClient has retry logic by searching the codebase (suggest in verbose mode):

**PASS**: no rate limit errors, OR rate limit errors occurred but service is healthy (handled correctly)
**WARNING**: rate limit errors with service degraded (handling exists but imperfect)
**FAIL**: rate limit errors with service down (no retry logic)

If no rate limit errors have ever occurred, mark as **INFO**: "No rate limit events detected — cannot confirm retry logic works. Consider a load test."

---

#### SECTION 31: TOKEN SECURITY

Check token cache file permissions:

```python
import os
import stat

token_path = os.path.expanduser("~/.aegis/msal_token_cache.json")
if os.path.exists(token_path):
    mode = os.stat(token_path).st_mode
    owner_only = (mode & 0o077) == 0  # no group/other permissions
    permissions = oct(mode)[-3:]
```

**PASS**: token cache exists with permissions 600 (owner read/write only)
**WARNING**: token cache exists but permissions are too open (e.g., 644)
**FAIL**: token cache doesn't exist (auth is broken)

---

#### SECTION 32: PII-SAFE LOGGING

Check recent log files for PII leaks:

```python
import glob

log_files = glob.glob("logs/*.log") + glob.glob("*.log")
# Check last 500 lines of each log file for PII indicators
pii_patterns = [
    r'body["\s]*[:=]',       # email/chat body content
    r'transcript_text',       # raw transcript
    r'body_text',             # raw email body
    r'"content"\s*:.*@',      # email addresses in content
    r'password|secret|token', # credentials (except in config references)
]
```

**PASS**: no PII patterns found in log files
**WARNING**: possible PII detected (show the line numbers, NOT the content)
**FAIL**: clear PII in logs (email bodies, transcript text logged)

If no log files exist: **WARNING**: "No log files found — logging may not be configured"

---

#### SECTION 33: DATABASE BACKUP

```python
import os
import glob
from datetime import datetime

backup_dir = os.path.expanduser("~/.aegis/backups/")
backups = sorted(glob.glob(os.path.join(backup_dir, "*.sql*")))

if backups:
    latest = backups[-1]
    latest_mtime = datetime.fromtimestamp(os.path.getmtime(latest))
    age_hours = (datetime.now() - latest_mtime).total_seconds() / 3600
    size_mb = os.path.getsize(latest) / (1024 * 1024)
    backup_count = len(backups)
```

**PASS**: backup exists, latest is <28 hours old (daily), size > 0.1 MB, rotation working (count <= 30)
**WARNING**: backup exists but >48 hours old (missed a day) or size suspiciously small (<0.01 MB)
**FAIL**: no backups exist or backup directory doesn't exist

Verbose: list all backup files with dates and sizes.

---

#### SECTION 34: DATA RETENTION

```sql
-- Check for data age distribution
SELECT 
    COUNT(*) FILTER (WHERE datetime > NOW() - INTERVAL '90 days') as hot,
    COUNT(*) FILTER (WHERE datetime BETWEEN NOW() - INTERVAL '365 days' AND NOW() - INTERVAL '90 days') as warm,
    COUNT(*) FILTER (WHERE datetime < NOW() - INTERVAL '365 days') as cold
FROM emails;
```

This check is informational since the system may not have been running long enough for retention tiers to apply.

**PASS**: retention logic exists (check if retention job is registered in scheduler)
**INFO**: not enough data age to verify tier transitions (system < 90 days old)
**FAIL**: no retention job registered AND data is > 90 days old

---

#### SECTION 35: ADMIN SETTINGS

```sql
-- Admin settings populated
SELECT COUNT(*) as total_settings,
       COUNT(DISTINCT key) as unique_keys
FROM admin_settings;

-- Sample of settings categories
SELECT key, LEFT(value::text, 50) as value_preview, updated
FROM admin_settings
ORDER BY key LIMIT 20;
```

**PASS**: 30+ settings stored (out of ~70 total), covering multiple categories
**WARNING**: 10-30 settings (partial)
**FAIL**: admin_settings table empty (all config still in .env only, admin page can't override)

**Sub-checks** — verify critical categories have at least one setting:

```sql
SELECT 
    COUNT(*) FILTER (WHERE key LIKE 'polling_%') as polling,
    COUNT(*) FILTER (WHERE key LIKE 'triage_%') as triage,
    COUNT(*) FILTER (WHERE key LIKE 'workstream_%') as workstream,
    COUNT(*) FILTER (WHERE key LIKE 'stale_%') as stale,
    COUNT(*) FILTER (WHERE key LIKE 'notify_%') as notifications,
    COUNT(*) FILTER (WHERE key LIKE 'retention_%') as retention,
    COUNT(*) FILTER (WHERE key LIKE 'sentiment_%') as sentiment,
    COUNT(*) FILTER (WHERE key LIKE 'meeting_%') as meeting
FROM admin_settings;
```

**PASS**: 6+ categories represented
**WARNING**: 3-5 categories
**FAIL**: 0-2 categories

---

#### SECTION 36: ADMIN SETTINGS OVERRIDE

Verify that admin_settings values actually override .env defaults at runtime:

```python
from aegis.config import settings

# Check if the config system reads from admin_settings
# This is a code inspection check — verify config.py has logic like:
# 1. Load from .env as defaults
# 2. Query admin_settings table
# 3. Override .env values with admin_settings values
```

**PASS**: config.py has admin_settings override logic
**WARNING**: admin_settings table exists but config.py doesn't read from it
**FAIL**: no override mechanism exists (admin page writes to DB but nothing reads it)

---

#### SECTION 37: CRASH RECOVERY

Check if startup recovery logic exists:

```sql
-- Are there any items stuck in 'processing' state?
SELECT 'meetings' as type, COUNT(*) FROM meetings WHERE processing_status = 'processing'
UNION ALL
SELECT 'emails', COUNT(*) FROM emails WHERE processing_status = 'processing'
UNION ALL
SELECT 'chat_messages', COUNT(*) FROM chat_messages WHERE processing_status = 'processing';
```

**PASS**: zero items in 'processing' state (either recovery runs on startup, or no crashes have occurred)
**WARNING**: 1-3 items stuck in processing (recovery may not be running)
**FAIL**: >3 items stuck in processing (recovery is definitely not implemented)

---

#### SECTION 38: STARTUP SCRIPT

```python
import os
import subprocess

# Check for startup script
aegis_start = os.path.exists("scripts/aegis") or os.path.exists("aegis")
# Check for LaunchAgent plist
plist_path = os.path.expanduser("~/Library/LaunchAgents/com.aegis.app.plist")
plist_exists = os.path.exists(plist_path)
```

**PASS**: startup script exists AND LaunchAgent plist exists
**WARNING**: startup script exists but no LaunchAgent (manual start required on reboot)
**FAIL**: neither exists

---

#### SECTION 39: LOGGING

```python
import glob
import os

log_files = glob.glob("logs/*.log*")
log_dir_exists = os.path.isdir("logs")

if log_files:
    total_size = sum(os.path.getsize(f) for f in log_files)
    rotated = [f for f in log_files if '.log.' in f or f.endswith('.gz')]
```

**PASS**: log directory exists, log files present, rotation configured (rotated files exist or logrotate configured)
**WARNING**: log files exist but no rotation (will grow unbounded)
**FAIL**: no logging directory or no log files

---

#### SECTION 40: SEARCH FUNCTIONALITY

```sql
-- Verify search page has data to search across
SELECT 'meetings' as type, COUNT(*) FILTER (WHERE embedding IS NOT NULL) as searchable FROM meetings
UNION ALL
SELECT 'emails', COUNT(*) FILTER (WHERE embedding IS NOT NULL) FROM emails
UNION ALL
SELECT 'chat_messages', COUNT(*) FILTER (WHERE embedding IS NOT NULL) FROM chat_messages
UNION ALL
SELECT 'action_items', COUNT(*) FILTER (WHERE embedding IS NOT NULL) FROM action_items;
```

**PASS**: >200 total searchable items across types
**WARNING**: 50-200
**FAIL**: <50

---

#### SECTION 41: MOBILE RESPONSIVENESS

This is a code inspection check — verify templates use Tailwind responsive classes:

```python
import glob

templates = glob.glob("aegis/web/templates/**/*.html", recursive=True)
responsive_count = 0
for t in templates:
    content = open(t).read()
    if any(prefix in content for prefix in ['sm:', 'md:', 'lg:', 'xl:']):
        responsive_count += 1
```

**PASS**: >70% of templates use responsive breakpoints
**WARNING**: 30-70% use responsive breakpoints
**FAIL**: <30% use responsive breakpoints (mobile layout will be broken)

---

#### SECTION 42: DOCKER & DATABASE HEALTH

```python
import subprocess

# Check Docker container
result = subprocess.run(['docker', 'ps', '--filter', 'name=aegis-db', '--format', '{{.Status}}'],
                       capture_output=True, text=True)
container_status = result.stdout.strip()

# Check database connectivity
# Run a test query
```

```sql
-- Database stats
SELECT pg_database_size('aegis') as db_size_bytes,
       (SELECT COUNT(*) FROM pg_stat_activity WHERE datname = 'aegis') as active_connections;

-- pgvector health
SELECT extversion FROM pg_extension WHERE extname = 'vector';

-- Table sizes
SELECT relname as table_name, 
       pg_size_pretty(pg_total_relation_size(relid)) as total_size,
       n_live_tup as row_count
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC LIMIT 15;
```

**PASS**: container running, database accessible, pgvector installed, total DB size < 5 GB
**WARNING**: DB size 5-10 GB (getting large, retention may need adjustment)
**FAIL**: container not running or database inaccessible

---

### Summary Section

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FULL SYSTEM VERIFICATION SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PART A — Phase 3 (Sections 1-14):
✅ PASSED:  __ / 14      ⚠️ WARNINGS: __      ❌ FAILED: __

PART B — Phase 4 (Sections 15-28):
✅ PASSED:  __ / 14      ⚠️ WARNINGS: __      ❌ FAILED: __

PART C — Phase 5 (Sections 29-42):
✅ PASSED:  __ / 14      ⚠️ WARNINGS: __      ❌ FAILED: __

COMBINED:
✅ PASSED:  __ / 42      ⚠️ WARNINGS: __      ❌ FAILED: __

PHASE 3 FAILURES:
  ...

PHASE 4 FAILURES:
  ...

PHASE 5 FAILURES:
  ...

SYSTEM STATUS: PRODUCTION READY / NEEDS FIXES / CRITICAL ISSUES
```

**PRODUCTION READY**: 0 failures, ≤ 5 warnings
**NEEDS FIXES**: 1-3 failures
**CRITICAL ISSUES**: 4+ failures

---

### Implementation Notes

- Reuse patterns from verify_phase3.py and verify_phase4.py
- Use existing db engine, config, rich formatting
- Phase 5 checks include filesystem inspections (backups, logs, token permissions, Docker) — use `os`, `subprocess`, `glob`
- For code inspection checks (admin override, responsive templates), read files directly — don't import the modules
- Handle missing files/dirs gracefully
- The script must work even if some Phase 5 features haven't been built yet (don't crash on missing log directories)
- Print total execution time at the end

### Files To Reference

- `aegis/db/engine.py`, `aegis/db/models.py`, `aegis/config.py`
- `scripts/verify_phase3.py`, `scripts/verify_phase4.py` — follow same patterns
- `aegis/web/templates/` — for responsive check
- `aegis/main.py` — for startup/recovery logic check
