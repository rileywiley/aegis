# Claude Code Prompt: Phase 3+4 Combined Verification Script

## Task

Build `scripts/verify_phase4.py` — a comprehensive verification script that re-runs ALL Phase 3 checks (some of which needed more runtime to fully populate) AND all Phase 4 (Intelligence + Response Workflow) checks. This is a single script that validates the entire system end-to-end.

This replaces the need to run `verify_phase3.py` separately. If Phase 3 items that were previously WARNING (workstream auto-detection, org inference, embedding coverage) haven't resolved after additional runtime, they should now show as FAIL.

This script checks the mechanical/structural aspects. A manual testing checklist is included at the bottom for things that require human judgment (briefing quality, voice accuracy, RAG answer correctness).

## Requirements

Same as verify_phase3.py: use `rich`, read-only SELECT queries only, support `--verbose` and `--fix-suggestions` flags, complete in <10 seconds.

## Checks To Run

The script is organized into two parts:
- **PART A: Phase 3 Re-Checks** (Sections 1-14) — re-run all Phase 3 verifications with stricter thresholds since the system has had more time to run
- **PART B: Phase 4 Checks** (Sections 15-28) — new intelligence layer verifications

Print a clear divider between the two parts in the output.

---

### ═══ PART A: PHASE 3 RE-CHECKS ═══

These are the same checks from verify_phase3.py but with stricter pass/fail criteria. The system has had multiple polling cycles to populate data. Items that were WARNING before should now be PASS or FAIL — no more "might not have run yet" excuses.

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

**PASS**: all Phase 3 services show `healthy` with `last_success` within expected interval
**WARNING**: any service `degraded`
**FAIL**: any service `down` or missing from the table entirely

---

#### SECTION 2: EMAIL INGESTION

```sql
SELECT email_class, COUNT(*) FROM emails GROUP BY email_class;
```

**PASS**: total emails > 50, all 3 classes present, human is 20-50% of total
**WARNING**: total emails 10-50 (system hasn't been running long)
**FAIL**: emails table still empty after multiple polling cycles — poller is broken

Verbose: show 5 automated and 5 human emails with subjects for spot-check.

---

#### SECTION 3: EMAIL TRIAGE

```sql
SELECT triage_class, COUNT(*), ROUND(AVG(triage_score)::numeric, 2)
FROM emails WHERE email_class = 'human' GROUP BY triage_class;
```

**PASS**: all 3 triage classes present, substantive is 20-50% of human emails
**WARNING**: distribution heavily skewed (>70% substantive or >70% noise)
**FAIL**: triage_class is NULL on all human emails

---

#### SECTION 4: EMAIL EXTRACTION & ASK DIRECTIONALITY

```sql
SELECT COUNT(*) as total,
       COUNT(*) FILTER (WHERE requester_id IS NOT NULL) as has_requester,
       COUNT(*) FILTER (WHERE target_id IS NOT NULL) as has_target,
       COUNT(*) FILTER (WHERE requester_id IS NOT NULL AND target_id IS NOT NULL) as has_both
FROM email_asks;
```

Also identify the user's person record and check ask directionality:

```sql
-- Asks directed at user
SELECT COUNT(*) FROM email_asks WHERE target_id = {user_person_id};
-- Asks user made
SELECT COUNT(*) FROM email_asks WHERE requester_id = {user_person_id};
```

**PASS**: email_asks > 10, >50% have both requester and target, both directions exist
**WARNING**: asks exist but <50% have directionality
**FAIL**: email_asks empty or 0% have requester/target

---

#### SECTION 5: EMAIL THREAD RESOLUTION

```sql
SELECT COUNT(*) FILTER (WHERE status = 'completed' AND resolved_by_email_id IS NOT NULL) as resolved,
       COUNT(*) FILTER (WHERE status = 'open') as still_open,
       COUNT(*) as total
FROM email_asks;
```

**PASS**: at least some asks resolved (resolved > 0)
**WARNING**: zero resolved but open asks exist (thread analysis not resolving — NOW a concern since system has had time)
**FAIL**: no asks at all

---

#### SECTION 6: TEAMS INGESTION

```sql
SELECT source_type, COUNT(*) as total,
       COUNT(*) FILTER (WHERE noise_filtered = true) as filtered,
       COUNT(*) FILTER (WHERE noise_filtered = false) as kept
FROM chat_messages GROUP BY source_type;
```

**PASS**: both teams_chat and teams_channel present with messages > 20 each
**WARNING**: one source type has 0 messages
**FAIL**: chat_messages still empty

---

#### SECTION 7: TEAMS TRIAGE

```sql
SELECT triage_class, COUNT(*)
FROM chat_messages WHERE noise_filtered = false GROUP BY triage_class;
```

**PASS**: triage classes distributed on non-filtered messages
**FAIL**: all NULL triage_class or no non-filtered messages

---

#### SECTION 8: CHAT ASKS EXTRACTION

```sql
SELECT COUNT(*) as total,
       COUNT(*) FILTER (WHERE requester_id IS NOT NULL) as has_requester,
       COUNT(*) FILTER (WHERE target_id IS NOT NULL) as has_target
FROM chat_asks;
```

**PASS**: chat_asks > 0 with directionality
**WARNING**: chat_asks exist but no directionality
**FAIL**: empty (downgrade to WARNING only if all non-filtered chat messages are contextual/noise)

---

#### SECTION 9: TEAMS MEMBERSHIP & ORG STRUCTURE

```sql
SELECT COUNT(*) as teams FROM teams;
SELECT COUNT(*) as channels FROM team_channels;
SELECT COUNT(*) as memberships FROM team_memberships;

SELECT name, source, confidence,
       (SELECT COUNT(*) FROM people p WHERE p.department_id = d.id) as member_count
FROM departments d ORDER BY member_count DESC;
```

**PASS**: teams > 0, channels > 0, memberships > 0, at least 1 department with members
**WARNING**: teams exist but zero departments (org inference batch might still not have run — check if it's a weekly job and less than a week has passed)
**FAIL**: teams table still empty — Teams membership sync is broken

---

#### SECTION 10: PEOPLE TABLE HEALTH

```sql
SELECT source, COUNT(*),
       COUNT(*) FILTER (WHERE needs_review = true) as needs_review,
       COUNT(*) FILTER (WHERE is_external = true) as external
FROM people GROUP BY source;

SELECT COUNT(*) FILTER (WHERE department_id IS NOT NULL) as has_dept,
       COUNT(*) as total
FROM people;

-- Duplicates
SELECT name, COUNT(*) as records, array_agg(email) as emails
FROM people GROUP BY name HAVING COUNT(*) > 1;
```

**PASS**: people from 3+ sources (calendar, email, teams, meeting), >30% have departments, <5 duplicates
**WARNING**: only 1-2 sources, or many duplicates (>10)
**FAIL**: people table empty or only calendar-seeded records

---

#### SECTION 11: WORKSTREAM AUTO-DETECTION (stricter now)

```sql
SELECT created_by, status, COUNT(*)
FROM workstreams GROUP BY created_by, status;

SELECT name, confidence, status,
       (SELECT COUNT(*) FROM workstream_items wi WHERE wi.workstream_id = w.id) as items,
       (SELECT COUNT(DISTINCT person_id) FROM workstream_stakeholders ws WHERE ws.workstream_id = w.id) as stakeholders
FROM workstreams w WHERE created_by = 'auto'
ORDER BY items DESC;

-- Multi-workstream items
SELECT COUNT(*) FROM (
    SELECT item_type, item_id FROM workstream_items
    GROUP BY item_type, item_id HAVING COUNT(*) > 1
) multi;

-- Unassigned substantive items
SELECT 'emails' as type, COUNT(*) FROM emails e
WHERE email_class = 'human' AND triage_class IN ('substantive','contextual')
  AND NOT EXISTS (SELECT 1 FROM workstream_items wi WHERE wi.item_type = 'email' AND wi.item_id = e.id)
UNION ALL
SELECT 'chat_messages', COUNT(*) FROM chat_messages cm
WHERE noise_filtered = false AND triage_class IN ('substantive','contextual')
  AND NOT EXISTS (SELECT 1 FROM workstream_items wi WHERE wi.item_type = 'chat_message' AND wi.item_id = cm.id)
UNION ALL
SELECT 'meetings', COUNT(*) FROM meetings m
WHERE is_excluded = false AND processing_status = 'completed'
  AND NOT EXISTS (SELECT 1 FROM workstream_items wi WHERE wi.item_type = 'meeting' AND wi.item_id = m.id);
```

**PASS**: 3+ auto-detected workstreams, at least 1 with 5+ items, some multi-membership items exist
**WARNING**: 1-2 auto-detected workstreams, or all have <5 items
**FAIL**: zero auto-detected workstreams — this is now a FAIL since the system has had email + Teams data flowing

Verbose: for each auto-detected workstream, show name + 5 sample items with type and preview.

---

#### SECTION 12: EMBEDDINGS (stricter now)

```sql
SELECT 'meetings' as type,
       COUNT(*) FILTER (WHERE embedding IS NOT NULL) as has_embedding,
       COUNT(*) as total
FROM meetings WHERE processing_status = 'completed'
UNION ALL
SELECT 'emails',
       COUNT(*) FILTER (WHERE embedding IS NOT NULL),
       COUNT(*)
FROM emails WHERE triage_class IN ('substantive','contextual')
UNION ALL
SELECT 'chat_messages',
       COUNT(*) FILTER (WHERE embedding IS NOT NULL),
       COUNT(*)
FROM chat_messages WHERE triage_class IN ('substantive','contextual');
```

**PASS**: >90% embedding coverage across all types
**WARNING**: 70-90% coverage
**FAIL**: <70% coverage (was 27% in Phase 3 initial check — should be much higher now)

---

#### SECTION 13: LLM COST (cumulative)

```sql
SELECT model, task, SUM(input_tokens) as input_tok, SUM(output_tokens) as output_tok, SUM(calls) as total_calls
FROM llm_usage WHERE date >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY model, task ORDER BY total_calls DESC;
```

Compute costs with: Haiku $0.25/$1.25 per M tokens, Sonnet $3/$15 per M tokens, Embeddings $0.02 per M tokens.

**PASS**: usage tracking active, estimated weekly cost < $15 for Phase 3 tasks
**WARNING**: weekly cost $15-25
**FAIL**: llm_usage empty or weekly cost > $25 (noise filter/triage not doing their job)

---

#### SECTION 14: CROSS-SYSTEM INTEGRATION

```sql
-- Email → People resolution rate
SELECT COUNT(*) FILTER (WHERE sender_id IS NOT NULL) as resolved,
       COUNT(*) as total
FROM emails WHERE email_class = 'human';

-- Chat → People resolution rate
SELECT COUNT(*) FILTER (WHERE sender_id IS NOT NULL) as resolved,
       COUNT(*) as total
FROM chat_messages WHERE noise_filtered = false;

-- Email asks linked to action items
SELECT COUNT(*) FROM email_asks WHERE linked_action_item_id IS NOT NULL;

-- Meeting chat correlation
SELECT COUNT(*) FROM chat_messages WHERE linked_meeting_id IS NOT NULL;
```

**PASS**: >80% email sender resolution, >80% chat sender resolution
**WARNING**: 50-80% resolution
**FAIL**: <50% resolution

---

### ═══ PART B: PHASE 4 CHECKS ═══

These verify the intelligence layer, response workflow, and dashboard.

---

#### SECTION 15: SCHEDULER HEALTH (Phase 4 services)

Verify APScheduler jobs are registered and running.

```sql
-- System health for intelligence services
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

Also check if the scheduler is registering jobs. Query system_health for ANY intelligence service that has ever reported a heartbeat.

**PASS**: at least 3 intelligence services show `last_success` within the last 24 hours
**WARNING**: services registered but `last_success` is NULL (never ran — might be expected if the app was just started or the scheduled time hasn't arrived yet)
**FAIL**: zero intelligence services in system_health (scheduler not wired up — same bug pattern as Phase 3 pollers)

---

#### SECTION 16: BRIEFINGS GENERATED

```sql
-- Briefings by type
SELECT briefing_type, COUNT(*), 
       MAX(generated_at) as most_recent,
       MIN(generated_at) as earliest
FROM briefings 
GROUP BY briefing_type;

-- Most recent of each type
SELECT briefing_type, generated_at, 
       LENGTH(content) as content_length,
       LEFT(content, 200) as preview
FROM briefings b1
WHERE generated_at = (
    SELECT MAX(generated_at) FROM briefings b2 
    WHERE b2.briefing_type = b1.briefing_type
)
ORDER BY briefing_type;
```

**PASS**: at least `morning` type exists with content_length > 500
**WARNING**: briefings exist but content is very short (<200 chars) or only one type exists
**FAIL**: briefings table is empty

**Sub-checks**:
- Morning briefing exists: ✅/❌
- Monday brief exists: ✅/❌ (only expected if a Monday has passed since Phase 4 was deployed)
- Friday recap exists: ✅/❌ (only expected if a Friday has passed)
- Meeting prep briefs exist: ✅/❌

For meeting prep specifically:
```sql
-- Meeting prep briefs linked to meetings
SELECT b.generated_at, m.title, m.start_time,
       LENGTH(b.content) as content_length
FROM briefings b
JOIN meetings m ON b.related_meeting_id = m.id
WHERE b.briefing_type = 'meeting_prep'
ORDER BY m.start_time DESC LIMIT 10;
```

**PASS**: prep briefs exist and are linked to actual meetings
**WARNING**: prep briefs exist but `related_meeting_id` is NULL (not linked)
**FAIL**: zero meeting_prep briefings

---

#### SECTION 17: MEETING PREP PRE-GENERATION

Verify that prep briefs are pre-generated (created BEFORE the meeting starts, not on-demand).

```sql
-- For each prep brief, check: was it generated before the meeting started?
SELECT m.title, m.start_time, b.generated_at,
       CASE WHEN b.generated_at < m.start_time THEN 'pre-generated ✅'
            ELSE 'generated late ⚠️' END as timing
FROM briefings b
JOIN meetings m ON b.related_meeting_id = m.id
WHERE b.briefing_type = 'meeting_prep'
ORDER BY m.start_time DESC LIMIT 10;
```

**PASS**: >80% of prep briefs were generated before the meeting start_time
**WARNING**: 50-80% pre-generated (some created late, possibly on-demand)
**FAIL**: <50% pre-generated, or all generated after meeting started

---

#### SECTION 18: MORNING BRIEFING CONTENT VALIDATION

Check that the morning briefing contains the required sections. Parse the content text for expected structural elements.

```python
# Check latest morning briefing content for expected sections
latest_morning = get_latest_briefing('morning')
content = latest_morning.content.lower()

has_calendar = any(word in content for word in ['meeting', 'calendar', 'today', 'schedule'])
has_action_items = any(word in content for word in ['action', 'overdue', 'pending', 'awaiting'])
has_workstreams = any(word in content for word in ['workstream', 'active', 'status'])
has_topics = any(word in content for word in ['address', 'discuss', 'raise', 'topic', 'agenda'])
```

**PASS**: briefing contains calendar section + action items + workstream health + meeting topics
**WARNING**: missing 1-2 sections
**FAIL**: missing 3+ sections or content is generic boilerplate

---

#### SECTION 19: VOICE PROFILE

```sql
-- Voice profile exists
SELECT id, 
       LENGTH(auto_profile) as profile_length,
       LEFT(auto_profile, 300) as profile_preview,
       array_length(custom_rules, 1) as custom_rule_count,
       last_learned_at,
       updated
FROM voice_profile LIMIT 1;
```

**PASS**: voice_profile record exists with auto_profile length > 200 characters
**WARNING**: profile exists but very short (<100 chars) or last_learned_at is NULL
**FAIL**: voice_profile table is empty (Phase 0 backfill didn't generate it, or table wasn't created)

**Verbose mode**: print the full auto_profile text so the user can judge if it sounds like them.

---

#### SECTION 20: DRAFT GENERATION

```sql
-- Drafts created
SELECT draft_type, status, COUNT(*)
FROM drafts
GROUP BY draft_type, status
ORDER BY draft_type, status;

-- Recent drafts with details
SELECT d.draft_type, d.status, d.channel,
       p.name as recipient,
       d.subject, LEFT(d.body, 150) as body_preview,
       d.triggered_by_type, d.created
FROM drafts d
LEFT JOIN people p ON d.recipient_id = p.id
ORDER BY d.created DESC LIMIT 10;
```

**PASS**: drafts exist, at least some `draft_type = 'nudge'` (auto-generated for stale items)
**WARNING**: drafts exist but all are `draft_type = 'response'` (only user-triggered, no auto-generation)
**FAIL**: drafts table is empty

**Sub-checks**:
- Auto-nudges generated for stale items: ✅/❌
- Meeting recap drafts generated: ✅/❌
- Drafts have correct channel assignment (email vs teams_chat): ✅/❌
- Drafts have `conversation_id` set for email threading: ✅/❌

```sql
-- Stale items that SHOULD have triggered nudge drafts
SELECT 'action_items' as type, COUNT(*)
FROM action_items 
WHERE status = 'open' 
  AND updated < NOW() - INTERVAL '7 days'
UNION ALL
SELECT 'email_asks', COUNT(*)
FROM email_asks
WHERE status = 'open'
  AND created < NOW() - INTERVAL '72 hours'
UNION ALL
SELECT 'chat_asks', COUNT(*)
FROM chat_asks
WHERE status = 'open'
  AND created < NOW() - INTERVAL '72 hours';
```

If stale items exist but no nudge drafts were generated, the draft generator isn't running or isn't detecting stale items.

---

#### SECTION 21: RESPONSE WORKFLOW INFRASTRUCTURE

Verify the Graph API write endpoints are accessible and the response workflow components exist.

```sql
-- Any drafts that were actually sent
SELECT d.draft_type, d.channel, p.name as recipient,
       d.subject, d.sent_at
FROM drafts d
LEFT JOIN people p ON d.recipient_id = p.id
WHERE d.status = 'sent'
ORDER BY d.sent_at DESC LIMIT 10;

-- Drafts with conversation threading data
SELECT COUNT(*) FILTER (WHERE conversation_id IS NOT NULL) as has_email_thread,
       COUNT(*) FILTER (WHERE chat_id IS NOT NULL) as has_chat_thread,
       COUNT(*) FILTER (WHERE conversation_id IS NULL AND chat_id IS NULL) as no_threading,
       COUNT(*) as total
FROM drafts;
```

**PASS**: at least one draft has been sent OR pending_review drafts have proper threading data
**WARNING**: drafts exist with pending_review status but no threading data (sends will fail or not thread correctly)
**FAIL**: no drafts at all (nothing to test the workflow with)

Note: it's OK if zero drafts have been sent yet — the user may not have clicked Send. The check is that the infrastructure is in place.

---

#### SECTION 22: READINESS SCORES

```sql
-- Readiness scores cached
SELECT key, LENGTH(data::text) as data_size, computed_at
FROM dashboard_cache
WHERE key = 'readiness_scores';

-- If cached, parse and show top scores
-- The data is JSONB, so extract person scores
```

If the dashboard_cache has a `readiness_scores` entry, parse it and display:

```
Person                Score   Open Items   Blocking   Trend
James Park              82        14          4       ▲
Derek Wu                71         7          2       ▲
Lisa Chen               65         6          1       —
...
```

**PASS**: readiness_scores cached with data for 3+ people, scores between 0-100
**WARNING**: cached but only 1-2 people, or all scores are 0
**FAIL**: no readiness_scores in cache (readiness scorer not running)

**Sanity check**: verify scores are relative (not all the same):
```python
scores = parse_readiness_data()
unique_scores = set(s['score'] for s in scores)
if len(unique_scores) == 1:
    # WARNING: all scores identical — normalization may be broken
```

---

#### SECTION 23: SENTIMENT AGGREGATION

```sql
-- Sentiment aggregations by scope
SELECT scope_type, COUNT(*), 
       ROUND(AVG(avg_score)::numeric, 1) as mean_sentiment,
       MIN(period_start) as earliest,
       MAX(period_end) as latest
FROM sentiment_aggregations
GROUP BY scope_type;

-- Cross-department friction
SELECT scope_id, avg_score, trend, interaction_count
FROM sentiment_aggregations
WHERE scope_type = 'cross_department'
  AND avg_score < 65
ORDER BY avg_score ASC LIMIT 10;
```

**PASS**: sentiment_aggregations has entries for at least 2 scope_types (person, department, etc.)
**WARNING**: only 1 scope_type, or all scores are identical (aggregation running but not differentiating)
**FAIL**: sentiment_aggregations table is empty

**Friction check**:
**PASS**: at least one cross_department entry with avg_score < 65 exists (friction detected — may or may not be real, but the detection mechanism works)
**INFO**: no low-scoring cross_department entries (either no friction exists or detection threshold needs adjustment)

---

#### SECTION 24: RAG CHAT INFRASTRUCTURE

```sql
-- Chat sessions exist
SELECT COUNT(*) as total_sessions,
       COUNT(*) FILTER (WHERE last_active > NOW() - INTERVAL '24 hours') as recent_sessions,
       MAX(jsonb_array_length(messages)) as max_messages_in_session
FROM chat_sessions;
```

**PASS**: chat_sessions table exists and is queryable (even if 0 sessions — user may not have used chat yet)
**FAIL**: query errors (table doesn't exist or schema mismatch)

**Vector search functional test** — run a test query to verify RAG retrieval works end-to-end:

```sql
-- Verify we have enough embeddings for meaningful search
SELECT COUNT(*) FILTER (WHERE embedding IS NOT NULL) as searchable_items
FROM (
    SELECT embedding FROM meetings WHERE embedding IS NOT NULL
    UNION ALL
    SELECT embedding FROM emails WHERE embedding IS NOT NULL
    UNION ALL
    SELECT embedding FROM chat_messages WHERE embedding IS NOT NULL
) all_embeddings;
```

**PASS**: >100 searchable items with embeddings
**WARNING**: 10-100 searchable items (search will work but results may be limited)
**FAIL**: <10 searchable items (not enough for meaningful RAG search)

---

#### SECTION 25: DASHBOARD CACHE

```sql
-- All cache keys and their freshness
SELECT key, computed_at,
       EXTRACT(EPOCH FROM (NOW() - computed_at)) / 60 as minutes_stale,
       LENGTH(data::text) as data_size_bytes
FROM dashboard_cache
ORDER BY key;
```

Expected cache keys from the spec:
- `workstream_cards`
- `pending_decisions`
- `awaiting_response`
- `stale_items`
- `todays_meetings`
- `drafts_pending`
- `readiness_scores`
- `department_health`

**PASS**: 6+ of 8 expected keys present, all refreshed within last 20 minutes
**WARNING**: 3-5 keys present, or some are >30 minutes stale
**FAIL**: 0-2 keys present (cache refresh job not running)

---

#### SECTION 26: NOTIFICATION CHANNELS

Check if notification delivery infrastructure exists. This is a code/config check more than a data check.

```sql
-- Check config for notification settings
SELECT key, value FROM admin_settings 
WHERE key IN ('notify_macos', 'notify_email_self', 'notify_teams_self');
```

If admin_settings is empty, fall back to .env defaults.

**PASS**: at least macOS notifications enabled
**WARNING**: all notification channels disabled
**FAIL**: notification settings don't exist in either admin_settings or config

---

#### SECTION 27: LLM COST TRACKING (Phase 4 additions)

```sql
-- LLM usage for Phase 4 tasks specifically
SELECT model, task, SUM(calls) as total_calls,
       SUM(input_tokens) as input_tok, SUM(output_tokens) as output_tok
FROM llm_usage
WHERE task IN ('briefing', 'meeting_prep', 'monday_brief', 'friday_recap',
               'draft_generation', 'response_draft', 'voice_profile',
               'rag_chat', 'sentiment', 'readiness')
  AND date >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY model, task
ORDER BY total_calls DESC;
```

Compute estimated cost. Phase 4 uses Sonnet for briefings/drafts/chat (more expensive than Phase 3's Haiku-heavy workload).

**PASS**: Phase 4 tasks appearing in usage, weekly cost reasonable (<$20)
**WARNING**: no Phase 4 tasks in usage (intelligence services built but not making LLM calls), or weekly cost >$20
**FAIL**: llm_usage completely empty (tracking still broken from Phase 3)

---

#### SECTION 28: END-TO-END FLOW CHECK

Verify the full pipeline from ingestion through intelligence:

```sql
-- Find a recent meeting that went through the complete pipeline
SELECT m.title, m.start_time, m.transcript_status, m.processing_status,
       (SELECT COUNT(*) FROM action_items ai WHERE ai.source_meeting_id = m.id) as action_items,
       (SELECT COUNT(*) FROM decisions d WHERE d.source_meeting_id = m.id) as decisions,
       (SELECT COUNT(*) FROM workstream_items wi WHERE wi.item_type = 'meeting' AND wi.item_id = m.id) as workstreams,
       (SELECT COUNT(*) FROM briefings b WHERE b.related_meeting_id = m.id AND b.briefing_type = 'meeting_prep') as prep_briefs,
       m.embedding IS NOT NULL as has_embedding
FROM meetings m
WHERE m.processing_status = 'completed'
ORDER BY m.start_time DESC LIMIT 5;
```

For each meeting, it should have: extraction (action_items > 0 or decisions > 0), workstream assignment (workstreams > 0), prep brief (prep_briefs > 0 for future meetings), and embedding (has_embedding = true).

**PASS**: at least 1 meeting has all pipeline stages complete
**WARNING**: meetings have some but not all stages
**FAIL**: no meetings have gone through the full pipeline

Similarly for emails:
```sql
SELECT e.subject, e.email_class, e.triage_class, e.processing_status,
       (SELECT COUNT(*) FROM email_asks ea WHERE ea.email_id = e.id) as asks,
       (SELECT COUNT(*) FROM workstream_items wi WHERE wi.item_type = 'email' AND wi.item_id = e.id) as workstreams,
       e.embedding IS NOT NULL as has_embedding
FROM emails e
WHERE e.email_class = 'human' AND e.triage_class = 'substantive'
ORDER BY e.datetime DESC LIMIT 5;
```

---

### Summary Section

Same format as verify_phase3.py:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PHASE 3+4 COMBINED VERIFICATION SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PART A — Phase 3 Re-Checks (Sections 1-14):
✅ PASSED:   12 / 14 checks
⚠️ WARNINGS:  1 / 14 checks
❌ FAILED:    1 / 14 checks

PART B — Phase 4 Checks (Sections 15-28):
✅ PASSED:   11 / 14 checks
⚠️ WARNINGS:  2 / 14 checks
❌ FAILED:    1 / 14 checks

COMBINED:
✅ PASSED:   23 / 28 checks
⚠️ WARNINGS:  3 / 28 checks
❌ FAILED:    2 / 28 checks

PHASE 3 FAILURES (must fix — these have had enough runtime):
  ❌ ...

PHASE 4 FAILURES (must fix before Phase 5):
  ❌ ...

WARNINGS (investigate):
  ⚠️ ...

NEXT STEPS:
  Fix all failures. Phase 3 failures are now blockers — the system has had 
  enough time for these to resolve naturally. Then run the manual checklist
  for Phase 4 subjective quality checks.

  Run: python scripts/verify_phase4.py --manual-checklist
```

---

### Manual Checklist Flag

When `--manual-checklist` is passed, print the following checklist to the terminal (formatted with `rich`). This is NOT automated — it's a reference the user walks through manually.

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PHASE 3+4 MANUAL VERIFICATION CHECKLIST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Open http://localhost:8000 and verify each item.

─── PHASE 3 CHECKS ───

EMAIL NOISE FILTER ACCURACY
  [ ] Navigate to /emails — browse the email list
  [ ] Spot-check 5 emails classified as "automated" — are they actually automated?
      (JIRA notifications, CI/CD alerts, calendar accepts = correct)
  [ ] Spot-check 5 emails classified as "human" — are they actually from real people?
  [ ] If a real email got classified as automated, or a newsletter as human, flag it

EMAIL TRIAGE QUALITY  
  [ ] On the emails page, filter by triage class
  [ ] Read 3 "substantive" emails — do they contain decisions, asks, or deliverables?
  [ ] Read 3 "contextual" emails — are these acknowledgments or low-value replies?
  [ ] If a substantive email got triaged as noise, the triage threshold needs adjustment

ASK DIRECTIONALITY
  [ ] Navigate to /asks
  [ ] Check "Directed at you" tab — are these actually things people asked YOU to do?
  [ ] Check "You asked" tab — are these things YOU asked others to do?
  [ ] If the directions are reversed, the extraction prompt has a directionality bug

WORKSTREAM QUALITY
  [ ] Navigate to /workstreams
  [ ] Review each auto-detected workstream name — do you recognize these as real initiatives?
  [ ] Click into the largest auto-detected workstream — do the items actually belong together?
  [ ] If items from unrelated departments are grouped together, the org chart partition 
      constraint may not be working
  [ ] Check the unassigned items queue — are there items that clearly belong to a workstream
      but weren't assigned? (Threshold may be too high)

ORG CHART & PEOPLE
  [ ] Navigate to /people — check the "Needs review" queue
  [ ] For people with LLM suggestions: are the suggested titles and departments correct?
  [ ] Approve or correct 3-5 people to verify the flow works
  [ ] Navigate to /org — does the chart reflect your actual org structure?
  [ ] Are departments reasonable? Are manager assignments plausible?

TEAMS DATA
  [ ] Are Teams channels showing in the system? Do the team names match your actual Teams?
  [ ] Navigate to /emails or /asks — do you see Teams-originated asks alongside email asks?

─── PHASE 4 CHECKS ───

BRIEFINGS
  [ ] Open the Command Center — does the morning briefing display as the default view?
  [ ] Read the morning briefing — are today's meetings listed with 2-3 suggested topics each?
  [ ] Are the suggested topics relevant? (Do they reference real open items with the right attendees?)
  [ ] Does the "Requires your action" section show real pending decisions and asks?
  [ ] Does the "Overnight activity" section reflect emails/chats that arrived recently?
  [ ] Does the workstream health section show accurate status and sentiment for your workstreams?
  [ ] If today is Monday: does the Monday brief show weekly objectives? Do the objectives make sense?
      (The LLM should identify priorities from deadlines, stale items, and workstream momentum — 
       not just list what's on your calendar)

MEETING PREP
  [ ] Click a meeting on today's calendar — does the prep brief open?
  [ ] Does it list attendees with recent interaction context?
  [ ] Does it show open items involving those attendees?
  [ ] Does it reference the previous meeting in the series (if recurring)?
  [ ] Are the suggested talking points relevant to the actual meeting topic?
  [ ] Click the "Next up" floating widget (bottom-right) — does it link to the correct prep brief?
  [ ] Test back-to-back scenario: after your current meeting ends, can you instantly view 
      the next meeting's prep brief? (Should be pre-computed, no loading delay)

VOICE PROFILE
  [ ] Go to Admin → Communication/Voice section
  [ ] Read the auto-generated voice profile — does it accurately describe how you write emails?
  [ ] Does it capture your greeting style, sign-off, formality level, and typical length?
  [ ] If something is wrong, edit a custom rule (e.g., "Never use 'Hope this helps'")
      and verify it saves

RESPONSE WORKFLOW
  [ ] Go to Command Center → Requires Your Attention → Decisions tab
  [ ] Click "Respond" on a pending decision
  [ ] Type a short directive: "Approved with the condition that we cap at $280K"
  [ ] Click "Generate draft" — does a full email appear?
  [ ] Does the draft sound like YOU? (Compare against emails you've recently sent)
  [ ] Is the To field correct? Subject correctly threaded (starts with "Re:")?
  [ ] Edit the draft slightly, then click "Discard" (don't actually send unless you want to)
  [ ] Try the same workflow on a Teams-originated ask — does it generate a Teams 
      message instead of an email?

DRAFTS
  [ ] Check the Drafts section on the command center
  [ ] Are there auto-generated nudge drafts for stale items?
  [ ] Read one — does it sound professional and appropriate? (Not too aggressive)
  [ ] Does it reference the right person, the right item, and the right timeframe?
  [ ] Are there meeting recap drafts for recently completed meetings?
  [ ] Click "Send" on a draft you're comfortable with — does it actually send?
      (Check your Sent Items in Outlook to confirm)

READINESS
  [ ] Navigate to /readiness
  [ ] Does the table show people with busyness scores?
  [ ] Do the scores match your intuition? (Is the person you know is overloaded
      scoring higher than someone with a lighter load?)
  [ ] Click to expand a person's row — does it show their specific open items?
  [ ] Is the "Scores reflect workload visible through your meetings, emails, and 
      Teams activity" caveat displayed?

SENTIMENT & DEPARTMENT HEALTH
  [ ] Navigate to /departments
  [ ] Do department sentiment scores have values (not all 0 or all identical)?
  [ ] Are trend arrows showing? (up/down/flat)
  [ ] If you know of tension between two departments, is it flagged as a friction pair?
  [ ] Do workstream cards on the command center show sentiment dots and trend arrows?

RAG CHAT
  [ ] Click "Ask Aegis" on the command center (or navigate to /ask)
  [ ] Ask: "What did we decide about [something you know was decided]?"
      → Does the answer cite the correct meeting or email?
  [ ] Ask: "What are [person name]'s open action items?"
      → Does it return a correct list? (Compare against /actions page)
  [ ] Ask: "Summarize the [workstream name] this week"
      → Does it provide an accurate summary with sources?
  [ ] Ask a follow-up question referencing the previous answer
      → Does it maintain conversation context?
  [ ] Try asking from the floating widget on a different page
      → Does the widget open and function correctly?

NOTIFICATIONS
  [ ] Check that macOS notifications fired for the morning briefing
      (Look in Notification Center — the notification should have appeared at the 
       configured briefing time)
  [ ] If email-to-self is enabled: check your inbox for the briefing email
  [ ] If Teams-to-self is enabled: check your Teams chat for the briefing message
  [ ] Wait for a meeting to be 15 minutes away — does a prep notification fire?

DASHBOARD COMMAND CENTER
  [ ] All 6 zones present and populated:
      1. Workstream cards (horizontal scroll, pinned first)
      2. Requires your attention (tabbed: decisions / awaiting / stale)
      3. Today's meetings (with topics and prep brief links)
      4. Drafts ready for review
      5. "Next up" floating widget
      6. "Ask Aegis" chat panel (toggleable)
  [ ] Workstream cards show: name, status pill, sentiment dot, trend arrow,
      source breakdown (meetings/emails/chats), open item count
  [ ] "Respond" button on decisions opens the response workflow modal
  [ ] Draft send/edit/discard buttons work
  [ ] Chat panel receives questions and returns sourced answers
  [ ] Sidebar navigation reaches all pages
  [ ] Dashboard refreshes (watch for data changes after a polling cycle)
```

---

### Implementation Notes

- Same patterns as verify_phase3.py: use existing db engine, rich formatting, async queries
- For Section 4 (briefing content validation), use simple string matching — don't import the LLM
- For Section 8 (readiness), parse the JSONB from dashboard_cache
- For the manual checklist, just print it as formatted rich text — no database queries
- Handle missing tables gracefully (if a Phase 4 table doesn't exist, FAIL that section with a clear message)
- At the end, print estimated time for manual checklist: "Manual checklist: ~30 minutes (Phase 3: ~10 min, Phase 4: ~20 min)"

### Files To Reference

- `aegis/db/models.py` — table schemas
- `aegis/db/engine.py` — async session
- `aegis/config.py` — settings
- `scripts/verify_phase3.py` — follow the same patterns
