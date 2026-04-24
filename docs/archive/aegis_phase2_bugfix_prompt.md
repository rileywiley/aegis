# Claude Code Prompt: Phase 2 Critical Bug Investigation & Repair

## Context

Phase 2 (Extraction + Org Bootstrap) has been built and the pipeline runs without crashing. However, manual end-to-end testing using seeded meeting transcripts revealed **three critical bugs** that must be fixed before proceeding to Phase 3.

The seeded transcripts are multi-speaker meeting conversations. Each line starts with the speaker's name followed by a colon, e.g.:

```
James: The Phase 2 migration timeline is still blocked on DBA availability. I'll resolve this week.
Derek: I have a draft. Should be ready by Wednesday.
Sarah: I finished the ALB config yesterday. Staging is stable now.
```

## Bug 1: Extracted entities not assigned to speakers

**Observed behavior**: Action items, commitments, and decisions are extracted from the transcript text, but `assignee_id` on action_items, `committer_id` on commitments, and `decided_by` on decisions are all NULL. The LLM is identifying WHAT was said but not WHO said it.

**Expected behavior**: When James says "I'll resolve this week," the extracted action item should have `assignee_id` pointing to the James record in the people table. When Derek says "Should be ready by Wednesday," the commitment should have `committer_id` pointing to Derek.

**Investigation steps**:
1. Read `aegis/processing/meeting_extractor.py` — examine the extraction prompt sent to the LLM
2. Check if the prompt instructs the LLM to identify the speaker for each extracted entity
3. Check the Pydantic schema for `ExtractedActionItem`, `ExtractedCommitment`, `ExtractedDecision` — do they have a field for the person (e.g., `assignee_name`, `committer_name`)?
4. Check the store/resolve step — even if the LLM returns a person name, is the pipeline actually using it to look up or create a person record and set the FK?

**Likely root causes** (check all):
- The extraction prompt doesn't ask the LLM to identify speakers or attribute statements to specific people
- The Pydantic schema has the person field but it's Optional and the LLM isn't filling it
- The LLM returns a person name string, but the resolver doesn't run on it or fails silently
- The resolver runs but can't match transcript names ("James") to people table records (which may have "James Park" from calendar attendees)

**Fix requirements**:
- The extraction prompt MUST explicitly instruct the LLM: "For each action item, identify WHO committed to it or was assigned it based on the speaker attribution in the transcript. For each decision, identify WHO made or announced it. Return the person's name as it appears in the transcript."
- The Pydantic extraction schemas MUST include a required person name field (not Optional) for:
  - `ExtractedActionItem.assignee_name: str`
  - `ExtractedCommitment.committer_name: str`  
  - `ExtractedCommitment.recipient_name: str | None`
  - `ExtractedDecision.decided_by_name: str`
- The resolver MUST attempt to match these names against the people table using fuzzy matching. "James" should match "James Park" if James Park is the only James in the people table. If no match found, create a new person record with `needs_review = True`.
- The store step MUST set the FK (`assignee_id`, `committer_id`, `decided_by`) to the resolved person's ID

**Test**: After fixing, re-run extraction on seeded meetings and verify:
```sql
-- Should return rows with non-NULL assignee names
SELECT a.description, p.name as assignee, a.deadline
FROM action_items a
LEFT JOIN people p ON a.assignee_id = p.id
WHERE a.source_meeting_id IS NOT NULL
ORDER BY a.created DESC;

-- Zero NULLs expected in the assignee column for action items from meetings
SELECT COUNT(*) FROM action_items WHERE assignee_id IS NULL AND source_meeting_id IS NOT NULL;
-- Should return 0
```

---

## Bug 2: Entity resolution not populating people table

**Observed behavior**: The `people` table is empty (or only contains records from Phase 0 calendar attendee seeding, not from extraction). No new people records were created from transcript speaker names.

**Expected behavior**: When the extraction identifies "James", "Sarah", "Derek", "Lisa", "David" from transcripts, the resolver should either match them to existing people records (seeded from calendar attendees in Phase 0/1) or create new people records. After extraction, the people table should contain all speakers from all processed transcripts.

**Investigation steps**:
1. Check what's actually in the people table right now:
   ```sql
   SELECT id, name, email, source, confidence, needs_review FROM people ORDER BY id;
   ```
2. If people exist from calendar seeding (Phase 0 backfill or Phase 1 calendar sync), check if they have names that should match transcript speakers
3. Read `aegis/processing/resolver.py` — trace the full flow:
   - Does it receive person names from the extractor?
   - Does it query the people table for matches?
   - Does it use fuzzy matching (rapidfuzz)?
   - Does it create new records when no match is found?
   - Does it return a person_id that gets set on the extracted entity?
4. Check if the resolver is even being called in the pipeline — read `aegis/processing/pipeline.py` and verify the resolve step runs after extraction
5. Check for silent errors — add logging or check existing logs for any exceptions in the resolver

**Likely root causes** (check all):
- The resolver is never called (pipeline skips the resolve step)
- The resolver is called but receives empty person names (because Bug 1 means no names are extracted)
- The resolver queries the people table but the fuzzy match threshold is too strict ("James" doesn't match "James Park" at the configured threshold)
- The resolver matches but doesn't persist the match (doesn't update the FK on the extracted entity)
- The resolver creates new people records but the transaction isn't committed
- The meeting_attendees join table wasn't populated during calendar sync, so there are no people to match against

**Fix requirements**:
- Fix Bug 1 first — the resolver can't match people if the extractor doesn't provide names
- The resolver MUST:
  1. Receive person name strings from the extraction output
  2. Query people table for matches using rapidfuzz with a threshold of 80 (not higher — "James" vs "James Park" should match)
  3. If a match is found with confidence >= 0.8: use existing person_id
  4. If no match found: create a new person record with `source='meeting'`, `confidence=0.5`, `needs_review=True`
  5. Return the person_id so the store step can set it on the entity's FK
- Calendar sync (Phase 1) MUST be populating meeting_attendees with person records from calendar event attendees — verify this is happening. These are the records the resolver should match against.

**Test**: After fixing:
```sql
-- Should show people from transcripts
SELECT name, email, source, confidence, needs_review
FROM people
WHERE source IN ('meeting', 'calendar', 'backfill')
ORDER BY name;

-- Should show matches between action items and people
SELECT a.description, p.name, p.email
FROM action_items a
JOIN people p ON a.assignee_id = p.id
LIMIT 20;
```

---

## Bug 3: Extraction is not idempotent — re-processing doubles entity count

**Observed behavior**: When the same meetings are re-processed (by resetting `processing_status = 'pending'` and re-running the pipeline), the action_items, decisions, and commitments tables get duplicate rows. The count doubles on each re-run.

**Expected behavior**: Re-processing the same meeting should produce the same entities. If the entities already exist, they should be updated (merged), not duplicated. The count should stay the same after re-processing.

**Investigation steps**:
1. Read the store step in the pipeline — how does it write extracted entities to the database?
2. Check: does it do a blind INSERT, or does it check for existing records first?
3. Check: is `last_extracted_at` being set on the meeting after extraction? Is it being checked before re-extracting?
4. Check: is there any deduplication logic comparing new entities against existing ones by source_meeting_id + description similarity?

**Likely root causes** (check all):
- The store step does `INSERT` without checking if entities from this meeting already exist
- There's no dedup check — no query like `SELECT FROM action_items WHERE source_meeting_id = X AND description SIMILAR TO Y`
- `last_extracted_at` is set but never checked (the pipeline re-extracts regardless)
- The pipeline checks `processing_status` but not whether entities already exist for this source

**Fix requirements — implement ALL of these**:

1. **Before extraction**: Check `last_extracted_at` on the meeting. If it's already set AND `processing_status = 'completed'`, skip extraction unless explicitly forced (e.g., via a `force=True` parameter). When the user manually resets `processing_status = 'pending'` for testing, the pipeline should still check for existing entities.

2. **Before storing each entity**: Query for existing entities from the same source meeting. For each extracted entity, check if an entity with the same `source_meeting_id` and a similar description already exists:
   ```python
   # Pseudocode for dedup check
   existing = await db.execute(
       select(ActionItem).where(
           ActionItem.source_meeting_id == meeting_id,
           ActionItem.description == extracted_item.description  # exact match first
       )
   )
   if existing:
       # Update the existing record if the new extraction is richer
       # Do NOT create a new record
   ```
   For fuzzy matching (if the LLM phrases things slightly differently on re-extraction), compare by embedding cosine similarity if embeddings exist, or by rapidfuzz string similarity on the description field (threshold 85).

3. **Delete-and-replace strategy** (simpler alternative): Before storing new extraction results for a meeting, delete ALL existing entities that reference this meeting as their source:
   ```python
   # Before storing new extraction results
   await db.execute(delete(ActionItem).where(ActionItem.source_meeting_id == meeting_id))
   await db.execute(delete(Decision).where(Decision.source_meeting_id == meeting_id))
   await db.execute(delete(Commitment).where(Commitment.source_meeting_id == meeting_id))
   # Then INSERT the new extraction results
   ```
   This is the simpler and more reliable approach. Since extraction uses `temperature=0`, re-extraction produces identical results, so delete-and-replace is safe. **This is the recommended approach.**

4. **Update processing tracking**: After extraction completes, set BOTH `processing_status = 'completed'` AND `last_extracted_at = NOW()` on the meeting.

5. **Also fix for email_asks and chat_asks**: The same idempotency issue likely exists for these tables too. Apply the same delete-and-replace pattern using `source_email_id` and `source_chat_message_id` respectively.

**Test**: After fixing:
```bash
# Count entities
psql -h localhost -p 5434 -U postgres -d aegis -c "SELECT COUNT(*) as action_count FROM action_items;"

# Reset and re-process
psql -h localhost -p 5434 -U postgres -d aegis -c "UPDATE meetings SET processing_status = 'pending' WHERE transcript_status = 'captured';"

# Re-run pipeline (use whatever command triggers processing)
python -c "import asyncio; from aegis.processing.pipeline import process_pending; asyncio.run(process_pending())"

# Count again — should be IDENTICAL
psql -h localhost -p 5434 -U postgres -d aegis -c "SELECT COUNT(*) as action_count FROM action_items;"
```

Run this reset-and-reprocess cycle THREE times. The count must remain identical every time.

---

## Fix Order

These bugs are interconnected. Fix them in this order:

1. **Bug 1 first** (speaker attribution in extraction prompt + schema) — without names, the resolver has nothing to work with
2. **Bug 2 second** (entity resolution) — once names are extracted, wire up the resolver to match/create people and set FKs
3. **Bug 3 third** (idempotency) — implement delete-and-replace, then verify with the triple re-process test

After all three are fixed, run the full pipeline on all seeded meetings and verify:
- Action items have non-NULL assignee_id with correct people
- People table contains all transcript speakers
- Re-processing 3 times produces identical entity counts
- `processing_status = 'completed'` and `last_extracted_at` is set on all processed meetings

## Files To Examine

Start by reading these files to understand the current implementation:

1. `aegis/processing/meeting_extractor.py` — the extraction prompt and Pydantic schemas
2. `aegis/processing/resolver.py` — entity resolution logic
3. `aegis/processing/pipeline.py` — pipeline flow (order of steps, how data passes between them)
4. `aegis/db/models.py` — SQLAlchemy models for action_items, decisions, commitments, people
5. `aegis/db/repositories.py` — data access patterns (how entities get stored)

Do NOT modify the seeded transcript data, the meeting_detector, the calendar_sync, or the GraphClient. Those are working correctly. The bugs are isolated to the extraction → resolution → storage path.
