# Claude Code Prompt: Decisions / Asks / Actions Rework + Database Purge & Reimport

## Context

The "Requires Attention" section on the Command Center has been organizing items by entity type (decisions, asks, action items) instead of by directionality (what do I owe vs what do others owe me). Additionally, there is overlap between entity types — an `ask_type = 'decision'` on email_asks overlaps with the `decisions` table, causing inconsistent classification.

This prompt implements a clean separation between the three entity types, reworks the dashboard attention section, adds a dedicated /decisions page, and purges + reimports all data so everything gets re-extracted with the corrected prompts.

## Part 1: Schema Changes

### 1a. Update the `decisions` table

```sql
-- Add new columns to decisions table
ALTER TABLE decisions ADD COLUMN outcome TEXT;                    -- filled when resolved
ALTER TABLE decisions ADD COLUMN status TEXT CHECK (status IN ('pending','resolved')) DEFAULT 'pending';
ALTER TABLE decisions ADD COLUMN pending_owner_id INT REFERENCES people(id);
ALTER TABLE decisions ADD COLUMN source_ask_id INT REFERENCES email_asks(id);
ALTER TABLE decisions ADD COLUMN resolved_at TIMESTAMPTZ;

-- If decided_by already exists, keep it. If not, add it:
-- ALTER TABLE decisions ADD COLUMN decided_by INT REFERENCES people(id);
```

### 1b. Add `related_decision_id` to action_items

```sql
ALTER TABLE action_items ADD COLUMN related_decision_id INT REFERENCES decisions(id);
```

### 1c. Remove 'decision' from ask_type enum

Update the CHECK constraint on `email_asks.ask_type` and `chat_asks.ask_type`:

```sql
-- Old enum: 'deliverable','decision','follow_up','question','approval','review','info_request'
-- New enum: 'deliverable','follow_up','question','approval','review','info_request'
-- 'decision' is REMOVED. 'approval' covers "can you approve this?" patterns.
```

**Important:** Create a proper Alembic migration for all of these changes. Do NOT use raw SQL outside of Alembic.

### 1d. Add index for the new FK

```sql
CREATE INDEX idx_action_items_decision ON action_items(related_decision_id);
CREATE INDEX idx_decisions_pending_owner ON decisions(pending_owner_id);
CREATE INDEX idx_decisions_source_ask ON decisions(source_ask_id);
CREATE INDEX idx_decisions_status ON decisions(status);
```

---

## Part 2: Extraction Prompt Changes

### 2a. Update `aegis/processing/meeting_extractor.py`

The LLM extraction prompt for meetings must clearly distinguish between the three entity types. Replace the extraction instructions with:

```
When extracting from a meeting transcript, classify each item into exactly ONE of these categories:

DECISION: A choice, approval, judgment call, or resolution that was MADE or ANNOUNCED during the meeting.
  Examples:
    "We approved Aurora over self-managed RDS" → DECISION (resolved, decided_by = speaker)
    "We're going with vendor B" → DECISION (resolved)
    "We need to decide on the Q3 budget by Friday" → DECISION (pending, pending_owner = whoever needs to decide)
  A decision is NOT a task. It has an outcome (or pending outcome), not a deliverable.

ACTION ITEM: A specific task that someone committed to or was assigned during the meeting.
  Examples:
    "James will resolve DBA availability this week" → ACTION ITEM (assignee = James, deadline = this week)
    "Sarah to test the ALB config by tomorrow" → ACTION ITEM (assignee = Sarah)
    "Let's have Derek quantify the staging overruns" → ACTION ITEM (assignee = Derek)
  An action item IS a task with an assignee and usually a deliverable.

COMMITMENT: A promise one person makes to another during the meeting.
  Examples:
    "I'll have the cost projections to you by Friday" → COMMITMENT (committer = speaker, recipient = listener)
  Commitments are similar to action items but emphasize the interpersonal promise.

Rules:
  - If a decision creates follow-up work, extract BOTH:
    → One DECISION (the choice that was made)
    → One or more ACTION ITEMS with related_decision_id linking back to the decision
    Example: "Approved the $280K budget" → DECISION. "Derek to process the allocation" → ACTION ITEM linked to that decision.
  
  - Never create a decision AND an action item for the same thing.
    "James will resolve DBA availability" → ACTION ITEM only (this is a task, not a judgment call)
    "We approved Aurora" → DECISION only (this is a judgment call, not a task)
  
  - If someone says "we need to decide X" but no decision is made → DECISION with status='pending'
  - If someone says "I'll do X" → ACTION ITEM, not a decision

For each DECISION, return:
  - description: what was decided or what needs to be decided
  - status: 'resolved' if decided during the meeting, 'pending' if still open
  - decided_by_name: who made/announced the decision (NULL if pending)
  - pending_owner_name: who needs to make the decision (NULL if resolved)
  - outcome: the actual decision made (NULL if pending)

For each ACTION ITEM, return:
  - description: the specific task
  - assignee_name: who is responsible
  - deadline: when it's due (if mentioned)
  - related_decision_description: if this action stems from a decision made in the same meeting, include the decision description so they can be linked
```

### 2b. Update `aegis/processing/email_extractor.py`

Update the extraction prompt for emails:

```
When extracting asks from emails, classify each ask into ONE of these types:
  - deliverable: requester is asking target to produce/send something
  - follow_up: requester is checking status of something previously discussed
  - question: requester is asking for information
  - approval: requester is asking target to approve/authorize something
  - review: requester is asking target to review/provide feedback on something
  - info_request: requester is asking for specific data or documents

Do NOT use 'decision' as an ask_type. That type no longer exists.

If an email asks someone to approve something (ask_type = 'approval'), note this separately so the system can create a linked pending decision record. Return a flag:
  creates_pending_decision: true
  decision_description: "Approve the revised Q3 marketing budget ($310K)"
  decision_pending_owner_name: the person being asked to approve
```

### 2c. Update `aegis/processing/chat_extractor.py`

Same changes as email_extractor — remove 'decision' from ask_type, add the `creates_pending_decision` flag for approval-type asks.

---

## Part 3: Pipeline Changes

### 3a. Update `aegis/processing/pipeline.py` — decision+ask linking

After extraction, the store step must handle the new linking logic:

```python
# When storing an email ask with ask_type='approval' and creates_pending_decision=True:
async def store_email_ask_with_decision(session, ask_data, email_id):
    # 1. Create the email_ask record
    ask = EmailAsk(
        email_id=email_id,
        ask_type='approval',
        description=ask_data.description,
        requester_id=ask_data.requester_id,
        target_id=ask_data.target_id,
        urgency=ask_data.urgency,
        deadline=ask_data.deadline,
        status='open',
    )
    session.add(ask)
    await session.flush()  # get the ask.id
    
    # 2. Create the linked pending decision
    decision = Decision(
        description=ask_data.decision_description,
        status='pending',
        pending_owner_id=ask_data.target_id,  # the person being asked to approve
        source_email_id=email_id,
        source_ask_id=ask.id,  # link back to the ask
    )
    session.add(decision)
    await session.commit()
```

```python
# When storing meeting extraction results with related decisions and actions:
async def store_meeting_extraction(session, extraction, meeting_id):
    # Store decisions first (so we have their IDs)
    decision_map = {}  # description -> decision.id
    for d in extraction.decisions:
        decision = Decision(
            description=d.description,
            status=d.status,
            decided_by=resolve_person(d.decided_by_name),
            pending_owner_id=resolve_person(d.pending_owner_name),
            outcome=d.outcome,
            source_meeting_id=meeting_id,
        )
        session.add(decision)
        await session.flush()
        decision_map[d.description] = decision.id
    
    # Store action items, linking to related decisions
    for a in extraction.action_items:
        related_decision_id = None
        if a.related_decision_description:
            # Fuzzy match against decisions extracted from this same meeting
            related_decision_id = find_related_decision(
                decision_map, a.related_decision_description
            )
        
        action = ActionItem(
            description=a.description,
            assignee_id=resolve_person(a.assignee_name),
            source_meeting_id=meeting_id,
            deadline=a.deadline,
            related_decision_id=related_decision_id,
        )
        session.add(action)
    
    await session.commit()
```

### 3b. Update the response workflow

When the user responds to a pending decision via the response workflow:

```python
async def resolve_decision_via_response(session, decision_id, outcome_text, sent_draft_id):
    decision = await session.get(Decision, decision_id)
    decision.status = 'resolved'
    decision.outcome = outcome_text
    decision.decided_by = user_person_id
    decision.resolved_at = datetime.utcnow()
    
    # Also close the source ask if it exists
    if decision.source_ask_id:
        ask = await session.get(EmailAsk, decision.source_ask_id)
        ask.status = 'completed'
    
    await session.commit()
```

---

## Part 4: Dashboard Rework — "Requires Attention"

### 4a. Replace the three tabs with two directional tabs

**Old tabs**: Decisions | Awaiting | Stale
**New tabs**: Needs Your Action | Awaiting Others

### 4b. "Needs Your Action" tab

Shows everything where the user is the bottleneck — decisions they need to make, asks directed at them, and action items assigned to them. All in one list, sorted by urgency then age.

```sql
-- Pending decisions where user needs to decide
SELECT 'decision' as item_type, d.id as item_id,
       d.description, p.name as from_person, d.datetime as created,
       'high' as urgency,  -- pending decisions are always high priority
       d.datetime as age_start,
       w.name as workstream_name
FROM decisions d
LEFT JOIN people p ON p.id = (
    SELECT ea.requester_id FROM email_asks ea WHERE ea.id = d.source_ask_id
)
LEFT JOIN workstream_items wi ON wi.item_type = 'decision' AND wi.item_id = d.id
LEFT JOIN workstreams w ON w.id = wi.workstream_id
WHERE d.pending_owner_id = {user_person_id} AND d.status = 'pending'

UNION ALL

-- Asks directed at the user
SELECT 'ask', ea.id, ea.description, p.name, ea.created,
       ea.urgency, ea.created,
       w.name
FROM email_asks ea
LEFT JOIN people p ON p.id = ea.requester_id
LEFT JOIN workstream_items wi ON wi.item_type = 'email_ask' AND wi.item_id = ea.id
LEFT JOIN workstreams w ON w.id = wi.workstream_id
WHERE ea.target_id = {user_person_id} AND ea.status = 'open'

UNION ALL

SELECT 'ask', ca.id, ca.description, p.name, ca.created,
       ca.urgency, ca.created,
       w.name
FROM chat_asks ca
LEFT JOIN people p ON p.id = ca.requester_id
LEFT JOIN workstream_items wi ON wi.item_type = 'chat_ask' AND wi.item_id = ca.id
LEFT JOIN workstreams w ON w.id = wi.workstream_id
WHERE ca.target_id = {user_person_id} AND ca.status = 'open'

UNION ALL

-- Action items assigned to the user
SELECT 'action', ai.id, ai.description, NULL, ai.created,
       CASE WHEN ai.created < NOW() - INTERVAL '{stale_days} days' THEN 'high'
            ELSE 'medium' END,
       ai.created,
       w.name
FROM action_items ai
LEFT JOIN workstream_items wi ON wi.item_type = 'action_item' AND wi.item_id = ai.id
LEFT JOIN workstreams w ON w.id = wi.workstream_id
WHERE ai.assignee_id = {user_person_id} AND ai.status IN ('open', 'in_progress')

ORDER BY urgency DESC, age_start ASC
LIMIT 25;
```

**Visual treatment:**
- Each row shows: item_type badge (Decision/Ask/Action), description, who's waiting on you, age, urgency dot, workstream link
- Items older than `stale_action_item_days` get a red "X days overdue" badge (not a separate tab)
- Decisions get a "Respond" button (opens response workflow)
- Asks get a "Respond" button (same workflow, channel-aware)
- Action items get a "Mark complete" button

### 4c. "Awaiting Others" tab

Shows everything where someone else is the bottleneck — asks the user made to others, and action items the user assigned that haven't been completed.

```sql
-- Asks the user made to others
SELECT 'ask' as item_type, ea.id as item_id,
       ea.description, p.name as who_owes, ea.created,
       ea.urgency, ea.created as age_start,
       w.name as workstream_name
FROM email_asks ea
LEFT JOIN people p ON p.id = ea.target_id
LEFT JOIN workstream_items wi ON wi.item_type = 'email_ask' AND wi.item_id = ea.id
LEFT JOIN workstreams w ON w.id = wi.workstream_id
WHERE ea.requester_id = {user_person_id} AND ea.status = 'open'

UNION ALL

SELECT 'ask', ca.id, ca.description, p.name, ca.created,
       ca.urgency, ca.created,
       w.name
FROM chat_asks ca
LEFT JOIN people p ON p.id = ca.target_id
LEFT JOIN workstream_items wi ON wi.item_type = 'chat_ask' AND wi.item_id = ca.id
LEFT JOIN workstreams w ON w.id = wi.workstream_id
WHERE ca.requester_id = {user_person_id} AND ca.status = 'open'

UNION ALL

-- Action items assigned to others from meetings the user attended
SELECT 'action', ai.id, ai.description, p.name, ai.created,
       CASE WHEN ai.created < NOW() - INTERVAL '{stale_days} days' THEN 'high'
            ELSE 'medium' END,
       ai.created,
       w.name
FROM action_items ai
LEFT JOIN people p ON p.id = ai.assignee_id
LEFT JOIN workstream_items wi ON wi.item_type = 'action_item' AND wi.item_id = ai.id
LEFT JOIN workstreams w ON w.id = wi.workstream_id
WHERE ai.assignee_id != {user_person_id}
  AND ai.assignee_id IS NOT NULL
  AND ai.status IN ('open', 'in_progress')
  AND ai.source_meeting_id IN (
      SELECT meeting_id FROM meeting_attendees WHERE person_id = {user_person_id}
  )

ORDER BY urgency DESC, age_start ASC
LIMIT 25;
```

**Visual treatment:**
- Each row shows: item_type badge, description, who owes it, age, urgency dot, workstream link
- Overdue items get the red badge
- Each row has a "Nudge" button → opens draft generation for a follow-up message to that person
- The "Nudge" button should pre-populate: recipient = who_owes, context = the original ask/action, channel = match the original source (email ask → email nudge, chat ask → Teams nudge)

### 4d. Update dashboard cache keys

Old keys: `pending_decisions`, `awaiting_response`, `stale_items`
New keys: `needs_your_action`, `awaiting_others`

Update the cache invalidation logic accordingly.

### 4e. Update the header count

Old: "7 items need your attention"
New: "4 need your action · 6 awaiting others"

---

## Part 5: /decisions Page

### 5a. Add route and template

Create `aegis/web/routes/decisions.py` and `aegis/web/templates/decisions.html`.

**Page layout:**

**Top section: Pending Decisions** (status = 'pending', pending_owner_id = user)
- Each shows: description, who asked (from source_ask), how long pending, workstream
- "Respond" button opens response workflow
- Badge showing source: "from email" or "from meeting"

**Bottom section: Recent Decisions** (status = 'resolved', last 30 days)
- Each shows: description, outcome, who decided, when, source meeting/email
- Expandable to show: related action items (via related_decision_id), source ask
- Filterable by: workstream, date range, decided_by
- Searchable

### 5b. Update sidebar nav

Add "Decisions" to the sidebar navigation between "Org chart" and "Action items":

```
Command center
Workstreams
Readiness
Department health
People
Org chart
Decisions          ← NEW
Action items
Pending asks
Meetings
Emails
Ask Aegis
Admin
```

### 5c. Update /actions page

On the /actions page, action items with `related_decision_id IS NOT NULL` should show a "Decision" badge. Clicking the badge navigates to the decision detail (or expands inline to show the decision description and outcome).

### 5d. Update /asks page

Remove any references to `ask_type = 'decision'`. The asks page should only show: deliverable, follow_up, question, approval, review, info_request.

Asks with `ask_type = 'approval'` should show a link to the related pending decision (if one was created via `decisions.source_ask_id`).

---

## Part 6: Database Purge & Reimport

After all code changes are complete and the Alembic migration has been created, run the purge and reimport.

### 6a. Create `scripts/purge_and_reimport.py`

This script:

1. **Warns the user** with a confirmation prompt:
   ```
   ⚠️  This will DELETE all data from the following tables:
     - decisions, action_items, commitments, email_asks, chat_asks
     - emails, chat_messages, meetings (and meeting_attendees)
     - workstreams, workstream_items, workstream_stakeholders, workstream_milestones
     - topics, meeting_topics, email_topics, chat_message_topics
     - briefings, drafts, dashboard_cache, chat_sessions
     - sentiment_aggregations, attachments
     - system_health, llm_usage
   
   The following will be PRESERVED:
     - people (but needs_review will be reset to true for all)
     - departments (preserved as baseline)
     - teams, team_channels, team_memberships (will be re-synced)
     - voice_profile (preserved — learned from sent emails)
     - admin_settings (preserved — your configuration)
   
   After purge, the backfill script will reimport:
     - 90 days of email from Graph API
     - 60 days of calendar events
     - 30 days of Teams messages
   
   Type 'PURGE' to confirm:
   ```

2. **Preserves people table** but resets `needs_review = true` and clears `interaction_count` so the LLM re-evaluates everyone with the new extraction logic. Keeps names, emails, departments, and manual corrections.

3. **Preserves voice_profile** — this was learned from sent emails and isn't affected by the schema changes.

4. **Preserves admin_settings** — user's configuration stays.

5. **Truncates all other tables** using `TRUNCATE ... CASCADE`.

6. **Runs Alembic migration** to apply the schema changes (new columns on decisions, new indexes, updated check constraints).

7. **Runs the backfill** (same as Phase 0):
   - Import 90 days email
   - Import 60 days calendar → re-seed meeting_attendees
   - Import Teams membership
   - Import 30 days Teams messages
   - Re-run the seed_test_data.py script if Screenpipe isn't installed yet

8. **Triggers a full processing cycle**:
   - Triage all new items
   - Extract all substantive items (with NEW prompts)
   - Run workstream detection

9. **Prints a summary**:
   ```
   REIMPORT COMPLETE
   ═══════════════════
   Emails imported:        847
   Calendar events:        124
   Teams messages:         1,203
   Teams membership:       6 teams, 42 members
   
   Processing will begin on the next 30-minute cycle.
   Run verify_phase5.py after processing completes (~30-60 min).
   ```

### 6b. Run sequence

```bash
# 1. Apply code changes (extraction prompts, pipeline, dashboard, decisions page)
# 2. Run the Alembic migration
alembic upgrade head

# 3. Purge and reimport
python scripts/purge_and_reimport.py

# 4. Start Aegis and wait for processing
python -m aegis.main
# Wait 30-60 min for triage + extraction + workstream detection

# 5. Verify
python scripts/verify_phase5.py --verbose
```

---

## Part 7: Identifying the User

A critical prerequisite for the directional tabs: the system must know which person record in the `people` table represents the user. This is needed for every query in the "Needs Your Action" and "Awaiting Others" tabs.

**Check if this already exists.** Search the codebase for how the user's identity is determined:
```bash
grep -r "user_person_id\|current_user\|get_user_person\|my_person" aegis/ --include="*.py"
```

**If it doesn't exist**, implement it:

```python
# In aegis/config.py or a utility module
async def get_user_person_id(session) -> int:
    """
    Identify the user's person record by matching against
    the email address from the Graph API /me profile.
    Cache the result — this doesn't change.
    """
    # Option 1: Fetch from Graph API /me on startup, match email
    # Option 2: Store user_email in .env and match against people.email
    # Option 3: Store user_person_id in admin_settings after first identification
```

Add `USER_EMAIL` to `.env.example` if it doesn't exist. The purge_and_reimport script should verify this is set before proceeding.

---

## Files To Modify

1. `alembic/versions/` — new migration for schema changes
2. `aegis/db/models.py` — update Decision model, ActionItem model, EmailAsk/ChatAsk ask_type enum
3. `aegis/processing/meeting_extractor.py` — new extraction prompt
4. `aegis/processing/email_extractor.py` — remove 'decision' ask_type, add creates_pending_decision
5. `aegis/processing/chat_extractor.py` — same as email
6. `aegis/processing/pipeline.py` — decision+ask linking logic, decision+action linking
7. `aegis/web/routes/dashboard.py` — rework Requires Attention to 2 directional tabs
8. `aegis/web/templates/dashboard.html` — update tab UI
9. `aegis/web/routes/decisions.py` — NEW: /decisions page
10. `aegis/web/templates/decisions.html` — NEW: decisions template
11. `aegis/web/routes/actions.py` — add Decision badge for linked items
12. `aegis/web/routes/asks.py` — remove decision ask_type, add approval→decision link
13. `aegis/web/templates/base.html` — add Decisions to sidebar nav
14. `aegis/web/routes/respond.py` — update to resolve decisions when responding
15. `aegis/db/repositories.py` — new queries for directional tabs
16. `scripts/purge_and_reimport.py` — NEW: purge + reimport script
17. `.env.example` — add USER_EMAIL if not present
18. `aegis/config.py` — add get_user_person_id utility

## Files NOT To Modify

- `aegis/ingestion/graph_client.py` — Graph API methods are unchanged
- `aegis/ingestion/email_poller.py` — polling logic unchanged
- `aegis/ingestion/teams_poller.py` — polling logic unchanged  
- `aegis/ingestion/calendar_sync.py` — calendar sync unchanged
- `aegis/intelligence/voice_profile.py` — voice unchanged
- `aegis/intelligence/readiness.py` — readiness scoring unchanged
- `scripts/verify_phase5.py` — will need updates after this change but handle separately

## After Implementation

Run the full verification:
```bash
python scripts/verify_phase5.py --verbose
```

The Requires Attention section should now show:
- "Needs Your Action" tab: decisions pending your input + asks directed at you + your action items
- "Awaiting Others" tab: asks you made + action items you assigned to others
- Each item shows type badge, person, age, urgency, workstream, and appropriate action button (Respond/Nudge/Complete)
