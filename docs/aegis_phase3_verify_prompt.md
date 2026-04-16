# Claude Code Prompt: Phase 3 Automated Verification Script

## Task

Build `scripts/verify_phase3.py` — an automated verification script that runs all Phase 3 checks against the live database and prints a formatted readout. This tells the user exactly what's working, what's broken, and what needs attention before proceeding to Phase 4.

## Context

Phase 3 (Email + Teams + Workstream Intelligence) has been built. The email poller, Teams poller, triage layer, email/chat extraction, workstream auto-detection, and org inference are all implemented. This script verifies that every component is working correctly by querying the database and evaluating the results.

This is a read-only diagnostic script. It does NOT modify any data. It only runs SELECT queries and prints results.

## Requirements

### Location & Command

- File path: `scripts/verify_phase3.py`
- Invocation: `python scripts/verify_phase3.py`
- Optional flags:
  - `--verbose` — show sample rows for each check (default: summary only)
  - `--fix-suggestions` — include suggested SQL or code fixes for failures

### Output Format

Use `rich` for formatted terminal output. The readout should look like:

```
╔══════════════════════════════════════════════════════════╗
║           AEGIS — Phase 3 Verification Report            ║
║           Generated: 2026-04-16 10:32:00 EST             ║
╚══════════════════════════════════════════════════════════╝

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SECTION 1: SERVICE HEALTH
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✅ email_poller        healthy   last success: 3 min ago   items/hr: 12
✅ teams_poller        healthy   last success: 2 min ago   items/hr: 34
✅ calendar_sync       healthy   last success: 8 min ago   items/hr: 0
✅ triage_batch        healthy   last success: 5 min ago   items/hr: 46
⚠️ workstream_detector degraded  last success: 2 hours ago items/hr: 0
❌ org_inference        down      last success: never       items/hr: 0

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SECTION 2: EMAIL INGESTION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Total emails ingested:        847
  Human:                      312  (36.8%)
  Automated:                  489  (57.7%)
  Newsletter:                  46   (5.4%)

✅ Noise filter active — classification distribution looks healthy

... (etc)
```

### Checks To Run

Implement ALL of the following checks. Each check should produce a status (✅ PASS / ⚠️ WARNING / ❌ FAIL), a metric, and in verbose mode, sample rows.

---

#### SECTION 1: SERVICE HEALTH

Query `system_health` table for all services. For each:
- Status indicator based on `status` column
- Time since `last_success` (human-readable: "3 min ago", "2 hours ago", "never")
- `items_processed_last_hour`
- If `last_error` is recent (within 1 hour), show `last_error_message`

**PASS**: all services healthy
**WARNING**: any service degraded
**FAIL**: any service down

---

#### SECTION 2: EMAIL INGESTION

```sql
-- Total count and classification breakdown
SELECT email_class, COUNT(*) FROM emails GROUP BY email_class;
```

**PASS**: at least 3 distinct email_class values present, human emails > 0
**WARNING**: one class has 0 count, or human < 10% of total
**FAIL**: emails table is empty

**Spot-check accuracy** (verbose mode): show 5 automated-classified and 5 human-classified with subject lines so the user can eyeball correctness.

---

#### SECTION 3: EMAIL TRIAGE

```sql
-- Triage breakdown for human emails only
SELECT triage_class, COUNT(*), ROUND(AVG(triage_score)::numeric, 2)
FROM emails WHERE email_class = 'human' GROUP BY triage_class;
```

**PASS**: all 3 triage classes present, substantive is 20-50% of human emails
**WARNING**: substantive is >60% (threshold too low, processing too much) or <15% (threshold too high, missing real content)
**FAIL**: triage_class is NULL for all human emails (triage not running)

**Spot-check** (verbose): show 3 substantive and 3 contextual emails with subjects and triage_scores.

---

#### SECTION 4: EMAIL EXTRACTION & ASK DIRECTIONALITY

```sql
-- Total email asks extracted
SELECT COUNT(*) FROM email_asks;

-- Asks with requester and target populated
SELECT COUNT(*) FILTER (WHERE requester_id IS NOT NULL) as has_requester,
       COUNT(*) FILTER (WHERE target_id IS NOT NULL) as has_target,
       COUNT(*) FILTER (WHERE requester_id IS NOT NULL AND target_id IS NOT NULL) as has_both
FROM email_asks;
```

**PASS**: email_asks count > 0, at least 50% have both requester and target
**WARNING**: asks exist but <50% have both requester and target
**FAIL**: email_asks table is empty, OR 0% have requester/target (same bug as Phase 2)

**Directionality check**: identify the user's own person record (query people table for the email used in OAuth / .env config). Then:

```sql
-- Asks directed at the user
SELECT COUNT(*) FROM email_asks WHERE target_id = {user_person_id};

-- Asks the user made
SELECT COUNT(*) FROM email_asks WHERE requester_id = {user_person_id};
```

**PASS**: both counts > 0
**WARNING**: one direction has 0 (user is never the target or never the requester — unlikely)
**FAIL**: user's person record not found

**Spot-check** (verbose): show 5 asks with requester name, target name, description, urgency.

---

#### SECTION 5: EMAIL THREAD RESOLUTION

```sql
-- Threads with multiple emails
SELECT COUNT(DISTINCT thread_id) as total_threads,
       COUNT(DISTINCT thread_id) FILTER (
           WHERE thread_id IN (SELECT thread_id FROM emails GROUP BY thread_id HAVING COUNT(*) > 1)
       ) as multi_email_threads
FROM emails WHERE thread_id IS NOT NULL;

-- Asks that were resolved by a later email in the thread
SELECT COUNT(*) FILTER (WHERE status = 'completed' AND resolved_by_email_id IS NOT NULL) as resolved,
       COUNT(*) FILTER (WHERE status = 'open') as still_open,
       COUNT(*) as total
FROM email_asks;
```

**PASS**: at least some asks have `status = 'completed'` with `resolved_by_email_id` set
**WARNING**: zero resolved asks but multi-email threads exist (thread analysis not resolving)
**FAIL**: no multi-email threads found (thread_id not being set on emails)

**Spot-check** (verbose): show one complete thread — all emails in chronological order with any asks and their resolution status.

---

#### SECTION 6: TEAMS INGESTION

```sql
-- Total messages by source type
SELECT source_type, COUNT(*) as total,
       COUNT(*) FILTER (WHERE noise_filtered = true) as filtered,
       COUNT(*) FILTER (WHERE noise_filtered = false) as kept
FROM chat_messages GROUP BY source_type;
```

**PASS**: both teams_chat and teams_channel rows exist with messages, some noise_filtered
**WARNING**: one source type has 0 messages, or 0 noise_filtered (filter not working)
**FAIL**: chat_messages table is empty

**Noise filter accuracy** (verbose): show 5 filtered messages (should be short, reactions, system msgs) and 5 kept messages (should be substantive conversation).

---

#### SECTION 7: TEAMS TRIAGE

```sql
-- Triage on non-filtered messages
SELECT triage_class, COUNT(*)
FROM chat_messages WHERE noise_filtered = false GROUP BY triage_class;
```

**PASS**: triage classes present on non-filtered messages
**WARNING**: all non-filtered messages have NULL triage_class
**FAIL**: no non-filtered messages exist

---

#### SECTION 8: CHAT ASKS EXTRACTION

```sql
-- Chat asks extracted
SELECT COUNT(*) as total,
       COUNT(*) FILTER (WHERE requester_id IS NOT NULL) as has_requester,
       COUNT(*) FILTER (WHERE target_id IS NOT NULL) as has_target
FROM chat_asks;
```

**PASS**: chat_asks count > 0 with requester/target populated
**WARNING**: chat_asks exist but directionality missing
**FAIL**: chat_asks table is empty (may be OK if no actionable Teams messages exist — downgrade to WARNING if chat_messages exist but all are contextual/noise)

---

#### SECTION 9: TEAMS MEMBERSHIP & ORG STRUCTURE

```sql
-- Teams and channels discovered
SELECT COUNT(*) as teams FROM teams;
SELECT COUNT(*) as channels FROM team_channels;
SELECT COUNT(*) as memberships FROM team_memberships;

-- Department inference from Teams
SELECT name, source, confidence, 
       (SELECT COUNT(*) FROM people p WHERE p.department_id = d.id) as member_count
FROM departments d ORDER BY member_count DESC;
```

**PASS**: teams > 0, channels > 0, memberships > 0, at least 1 department with source='teams'
**WARNING**: teams exist but no departments inferred (org inference hasn't run yet — might be expected if weekly batch hasn't triggered)
**FAIL**: teams table is empty (Teams polling not working)

---

#### SECTION 10: PEOPLE TABLE HEALTH

```sql
-- People summary
SELECT source, COUNT(*), 
       COUNT(*) FILTER (WHERE needs_review = true) as needs_review,
       COUNT(*) FILTER (WHERE is_external = true) as external
FROM people GROUP BY source;

-- People with department assigned
SELECT COUNT(*) FILTER (WHERE department_id IS NOT NULL) as has_dept,
       COUNT(*) FILTER (WHERE department_id IS NULL) as no_dept,
       COUNT(*) as total
FROM people;

-- Duplicate detection (same person, multiple records)
SELECT name, COUNT(*) as record_count, array_agg(email) as emails
FROM people GROUP BY name HAVING COUNT(*) > 1;
```

**PASS**: people from multiple sources (calendar, email, teams, meeting), some with departments, few duplicates
**WARNING**: many duplicates (>10), or 0 people from email/teams sources
**FAIL**: people table empty or only has calendar-seeded records (extraction/resolution not creating people)

---

#### SECTION 11: WORKSTREAM AUTO-DETECTION

```sql
-- Workstreams by creation method
SELECT created_by, status, COUNT(*) 
FROM workstreams GROUP BY created_by, status;

-- Auto-detected workstream details
SELECT name, confidence, status,
       (SELECT COUNT(*) FROM workstream_items wi WHERE wi.workstream_id = w.id) as items,
       (SELECT COUNT(DISTINCT person_id) FROM workstream_stakeholders ws WHERE ws.workstream_id = w.id) as stakeholders
FROM workstreams w WHERE created_by = 'auto'
ORDER BY items DESC;

-- Items assigned to multiple workstreams
SELECT COUNT(*) FROM (
    SELECT item_type, item_id FROM workstream_items 
    GROUP BY item_type, item_id HAVING COUNT(*) > 1
) multi;

-- Unassigned items
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

**PASS**: at least 1 auto-detected workstream with 3+ items, some multi-workstream items exist
**WARNING**: auto-detected workstreams exist but all have <3 items (detection too aggressive, creating tiny workstreams), or zero multi-workstream items
**FAIL**: zero auto-detected workstreams (detector not running or confidence thresholds too high)

**Spot-check** (verbose): for each auto-detected workstream, show the name and 5 sample items with their type and content preview. Ask the user: "Do these items belong together?"

---

#### SECTION 12: EMBEDDINGS & VECTOR SEARCH

```sql
-- Embedding coverage
SELECT 'meetings' as type, COUNT(*) FILTER (WHERE embedding IS NOT NULL) as has_embedding, COUNT(*) as total FROM meetings
UNION ALL
SELECT 'emails', COUNT(*) FILTER (WHERE embedding IS NOT NULL), COUNT(*) FROM emails WHERE triage_class IN ('substantive','contextual')
UNION ALL
SELECT 'chat_messages', COUNT(*) FILTER (WHERE embedding IS NOT NULL), COUNT(*) FROM chat_messages WHERE triage_class IN ('substantive','contextual');
```

**PASS**: >90% of substantive+contextual items have embeddings
**WARNING**: 50-90% have embeddings (some failed)
**FAIL**: <50% have embeddings (embedding generation broken)

**Vector search test**: pick a random email with an embedding, run a similarity query to verify pgvector is working:

```sql
SELECT e.subject, 1 - (e.embedding <=> (SELECT embedding FROM emails WHERE embedding IS NOT NULL LIMIT 1)) as similarity
FROM emails e WHERE embedding IS NOT NULL
ORDER BY e.embedding <=> (SELECT embedding FROM emails WHERE embedding IS NOT NULL LIMIT 1)
LIMIT 5;
```

**PASS**: returns results with similarity scores between 0 and 1
**FAIL**: query errors or returns no results

---

#### SECTION 13: LLM COST TRACKING

```sql
-- LLM usage in the last 7 days
SELECT model, task, SUM(input_tokens) as input_tok, SUM(output_tokens) as output_tok, SUM(calls) as total_calls
FROM llm_usage WHERE date >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY model, task ORDER BY total_calls DESC;
```

Compute estimated cost using:
- Haiku 4.5: $0.25/M input, $1.25/M output
- Sonnet 4.6: $3/M input, $15/M output
- Embeddings: $0.02/M tokens

**PASS**: usage exists, estimated weekly cost < $15
**WARNING**: weekly cost $15-30 (higher than expected, check if noise filter/triage is working)
**FAIL**: llm_usage table is empty (tracking not implemented), or weekly cost > $30

---

#### SECTION 14: CROSS-SYSTEM INTEGRATION

These checks verify that different Phase 3 systems connect properly.

```sql
-- Email → People: are email senders resolved to people records?
SELECT COUNT(*) FILTER (WHERE sender_id IS NOT NULL) as resolved,
       COUNT(*) FILTER (WHERE sender_id IS NULL) as unresolved,
       COUNT(*) as total
FROM emails WHERE email_class = 'human';
```

**PASS**: >80% of human emails have sender_id resolved
**FAIL**: <50% resolved (entity resolution not running on emails)

```sql
-- Chat → People: are chat senders resolved?
SELECT COUNT(*) FILTER (WHERE sender_id IS NOT NULL) as resolved,
       COUNT(*) FILTER (WHERE sender_id IS NULL) as unresolved,
       COUNT(*) as total
FROM chat_messages WHERE noise_filtered = false;
```

**PASS**: >80% resolved
**FAIL**: <50% resolved

```sql
-- Email asks linked to action items (cross-referencing)
SELECT COUNT(*) FROM email_asks WHERE linked_action_item_id IS NOT NULL;
```

**PASS**: at least some asks linked to action items (if relevant ones exist)
**WARNING**: zero links (may be expected if no asks match existing action items)

```sql
-- Meeting chat correlation (Teams meeting chats linked to meetings)
SELECT COUNT(*) FROM chat_messages WHERE linked_meeting_id IS NOT NULL;
```

**PASS**: some meeting chats linked
**WARNING**: zero links (may be expected if no Teams meetings occurred since Phase 3 went live)

---

### Summary Section

At the end, print a summary scorecard:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PHASE 3 VERIFICATION SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✅ PASSED:   10 / 14 checks
⚠️ WARNINGS:  3 / 14 checks
❌ FAILED:    1 / 14 checks

FAILURES (must fix before Phase 4):
  ❌ Section 11: Zero auto-detected workstreams — detector not running

WARNINGS (should investigate):
  ⚠️ Section 5: Zero resolved asks in multi-email threads
  ⚠️ Section 9: No departments inferred from Teams (weekly batch may not have run)
  ⚠️ Section 13: LLM cost tracking table empty

RECOMMENDATION: Fix the 1 failure. Warnings 2 and 3 may resolve after the
weekly batch runs. Re-run this script after fixes: python scripts/verify_phase3.py
```

---

### Implementation Notes

- Use the existing `aegis/config.py` to get DATABASE_URL and other config values
- Use SQLAlchemy async engine to run queries (reuse existing `aegis/db/engine.py`)
- Use `rich` for all output formatting (Console, Table, Panel, columns, colors)
- Each section should be a separate async function for clean organization
- Handle the case where tables are empty or don't exist (Phase 3 might be partially built)
- The script should complete in <10 seconds (it's all SELECT queries)
- Do NOT import or trigger any processing logic — this is purely diagnostic
- Identify the "user" person record by matching against AZURE_CLIENT_ID or by finding the person whose email matches the Graph API `/me` profile. If the user can't be identified, warn but don't fail — some checks will be limited.
- If `--verbose` flag is set, show sample rows for each section as indented tables below the summary line
- If `--fix-suggestions` flag is set, include a brief suggested fix for each WARNING and FAIL

### Files To Reference

Read these to understand the existing database schema and connection patterns:
- `aegis/db/engine.py` — async engine and session factory
- `aegis/db/models.py` — all SQLAlchemy models
- `aegis/config.py` — pydantic-settings configuration
