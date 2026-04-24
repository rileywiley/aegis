# Claude Code Prompt: Phase 1 End-to-End Test Seed Script

## Task

Build `scripts/seed_test_data.py` — a one-time script that populates the Aegis database with sample data so Phase 1 can be validated end-to-end without Screenpipe installed.

## Context

Phase 1 (Calendar + Screenpipe + Basic UI) is complete. The calendar sync, GraphClient, meeting detector, and dashboard all exist. However, Screenpipe is not yet installed on the user's machine, which means no real audio transcripts can be captured. This script fills that gap by injecting fake transcript content into past calendar events so the full UI flow can be validated.

This is NOT part of Phase 1's build — it's a test harness that runs AFTER Phase 1 is complete to verify everything works. Do not add this to Alembic migrations or production code paths.

## Requirements

### Location & Command

- File path: `scripts/seed_test_data.py`
- Invocation: `python scripts/seed_test_data.py`
- Must be idempotent: running twice should not create duplicate data or break anything

### Behavior

1. **Fetch real calendar events** from Graph API using the existing `GraphClient`
   - Pull events from the last 7 days AND today AND the next 2 days
   - Apply the same filtering as the normal calendar sync (skip all-day, declined, solo blocks, etc.)
   - Upsert into the `meetings` table by `calendar_event_id` (use existing repository logic if available)

2. **Inject fake transcripts** into PAST meetings only (where `end_time < now`)
   - For each past meeting, match the title against a dictionary of sample transcripts (see below)
   - If a match is found: set `transcript_text` to the sample, `transcript_status = 'captured'`, and populate a minimal `summary`
   - If no match is found: set `transcript_status = 'no_audio'` (simulates an in-person meeting or one where Screenpipe wasn't running)
   - Leave `processing_status` as `'pending'` so the extraction pipeline (when Phase 2 is built) will process them naturally

3. **Create one test-scenario meeting explicitly** (not from the real calendar — insert directly):
   - Title: `"Confidential HR discussion"` — verify `is_excluded = true` gets set by the keyword filter
   - Use a synthetic `calendar_event_id` prefixed with `"seed-test-"` so it doesn't collide with real events

4. **Print a summary report** to stdout:
   ```
   SEED COMPLETE
   ================
   Real calendar events fetched:     23
   Meetings inserted/updated:         17 (6 filtered out)
   Past meetings with fake transcript: 4
   Past meetings marked no_audio:     6
   Test meeting (HR confidential):    excluded ✓

   Open http://localhost:8000 to verify.
   ```

5. **Dry-run mode**: support a `--dry-run` flag that shows what would be inserted without writing to the database

### Sample Transcripts Dictionary

Include these inside the script as a constant. Match case-insensitively and by partial substring (so "Eng Standup" matches "Engineering standup - Wednesday"):

```python
SAMPLE_TRANSCRIPTS = {
    "standup": """James: Morning everyone. Let's do updates.
Sarah: I finished the ALB config yesterday. Staging is stable now. 502s are gone.
James: Nice work. Derek, where are we on the cost projections for the board deck?
Derek: I have a draft. Should be ready by Wednesday.
James: The Phase 2 migration timeline is still blocked on DBA availability. I'll resolve that this week.
Sarah: I can help if you need another pair of hands on the Aurora setup.
James: Thanks. Let's regroup Thursday.""",

    "series b": """You: David, thanks for sending the redline Friday.
David: Happy to. I flagged three sections where we need to push back hard.
You: The liquidation preference is the most important. Let's address that first.
David: Agreed. Their 2x participating preferred is unusual for this stage.
You: Push for 1x non-participating. If they won't move, I'll escalate to the partner.
David: I'll draft the response and send it for your review by Thursday.
You: Perfect. Also confirm the investor dinner attendee list with Tom before May 2.""",

    "budget": """Lisa: The revised Q3 marketing budget is $310K, up from the original $240K.
You: Walk me through the increase.
Lisa: Digital spend is up $45K. Events are up $25K. I've included the breakdown.
Derek: This exceeds our Q3 allocation by 29%. We need to flag this to finance.
You: I want to review the digital breakdown before approving. Send me the line items.
Lisa: I'll have it to you by end of day.
Derek: If approved, I'll need to revise our Q3 forecast.""",

    "steering": """You: Let's review Phase 1 completion.
James: Phase 1 is signed off. All services migrated, no rollbacks needed.
Derek: I'm concerned about staging cost overruns — we're $8K over projection.
Anika: What's the impact on product roadmap timelines?
James: Phase 2 DBA issue is blocking the data migration. We may slip 2 weeks.
Anika: That pushes the feature flag rollout into next quarter.
You: Let's revisit the timeline at next steering. Derek, quantify the staging overruns by Apr 14.
James: I'll have the Phase 2 mitigation plan ready for next meeting.""",

    "1:1": """You: How are you doing overall?
Lisa: Honestly a bit stretched. The Q3 campaign launch is consuming most of my time.
You: Is the team bandwidth the issue, or something else?
Lisa: Team bandwidth. We're running two campaigns in parallel and Sarah is out next week.
You: What would help most — a contractor, reprioritization, or pushing a deadline?
Lisa: Contractor would be the fastest unblock. 6 weeks at 20 hours/week.
You: Put together a proposal with cost and I'll approve this week.
Lisa: Thanks. I'll send it tomorrow."""
}

SAMPLE_SUMMARIES = {
    "standup": "Sarah confirmed ALB fix; staging stable. Derek's cost projections due Wednesday. James blocked on DBA for Phase 2; will resolve this week.",
    "series b": "Reviewed term sheet redline with David. Liquidation preference is top priority. David drafting response by Thursday. Investor dinner list needed by May 2.",
    "budget": "Lisa proposed Q3 marketing budget increase from $240K to $310K. Derek flagged 29% overage. User requested digital breakdown before approving.",
    "steering": "Phase 1 signed off. Staging cost overruns $8K above projection. Phase 2 blocked on DBA; potential 2-week slip. Derek to quantify overruns by Apr 14.",
    "1:1": "Lisa stretched by Q3 campaign launch. Sarah out next week. Lisa to send contractor proposal (6 weeks, 20 hrs/week) for user's approval this week."
}
```

### Matching Logic

```python
def match_sample(meeting_title: str) -> tuple[str | None, str | None]:
    """Returns (transcript, summary) if matched, (None, None) otherwise."""
    title_lower = meeting_title.lower()
    for keyword, transcript in SAMPLE_TRANSCRIPTS.items():
        if keyword in title_lower:
            return transcript, SAMPLE_SUMMARIES[keyword]
    return None, None
```

## Validation Steps (For The Human After Running)

After `python scripts/seed_test_data.py` completes, the user should open `http://localhost:8000` and verify:

- [ ] Today's meetings list correctly (real calendar events from Graph API)
- [ ] Past meetings with fake transcripts show the full transcript when clicked
- [ ] Past meetings marked `no_audio` display correctly (no error, shows status badge)
- [ ] The "Confidential HR discussion" test meeting is excluded (grayed out or hidden)
- [ ] All timestamps display in the user's local timezone (not UTC)
- [ ] Recurring meetings (if any in calendar) share the same `recurring_series_id` in DB
- [ ] Sidebar navigation works
- [ ] Dashboard renders correctly at 375px viewport (mobile-responsive check)
- [ ] A meeting from 11:30 PM yesterday to 12:30 AM today appears in "today's meetings" (midnight boundary check — only if such an event exists in the calendar)

## Out of Scope

- Do NOT call any LLM (extraction pipeline is Phase 2, not yet built)
- Do NOT generate embeddings (same reason)
- Do NOT seed emails, Teams messages, workstreams, or any other tables (Phase 3+)
- Do NOT mock the Graph API — use the real GraphClient with the user's real OAuth token
- Do NOT add this script to Alembic migrations or any production startup sequence

## Style

- Follow the existing codebase conventions (async, SQLAlchemy async, pydantic-settings)
- Use `rich` for the output summary (colored, formatted table)
- Log what's happening so the user can watch progress
- Handle errors gracefully (if Graph API returns 401, tell the user to re-run setup wizard)
