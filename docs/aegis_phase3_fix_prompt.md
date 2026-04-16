# Claude Code Prompt: Phase 3 Critical Fix — Ingestion Services Not Running

## Situation

Phase 3 verification shows nearly total failure. The root cause is that the ingestion services (email poller, Teams poller, triage batch, workstream detector) were built but are NOT running. Evidence:

```
❌ system_health table is empty       → no service has ever reported a heartbeat
❌ emails table is empty              → email poller never ran
❌ chat_messages table is empty       → Teams poller never ran
❌ teams table is empty               → Teams membership sync never ran
❌ workstream detection: none exist   → detector never ran (expected — needs data first)
⚠️ embeddings: only 27% coverage     → embedding generation partially broken or not triggered
```

Everything downstream (triage, extraction, asks, threads, workstreams) is empty because no data is flowing in. This is a startup/lifecycle wiring issue, not a logic issue.

## Investigation — Check These In Order

### 1. Are the services registered in the FastAPI startup lifecycle?

Read `aegis/main.py`. Look for how background tasks are started. The app should start polling services when FastAPI starts up, using one of these patterns:

```python
# Pattern A: FastAPI lifespan (preferred)
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start services
    await start_pollers()
    yield
    # Shutdown services
    await stop_pollers()

app = FastAPI(lifespan=lifespan)

# Pattern B: startup event (older FastAPI)
@app.on_event("startup")
async def startup():
    await start_pollers()

# Pattern C: APScheduler integration
scheduler = AsyncIOScheduler()
scheduler.add_job(email_poller.poll, 'interval', seconds=900)
scheduler.add_job(teams_poller.poll, 'interval', seconds=600)
scheduler.add_job(calendar_sync.sync, 'interval', seconds=1800)
scheduler.add_job(triage_batch.run, 'interval', seconds=1800)
scheduler.start()
```

**If none of these patterns exist in main.py**: the services were built as modules but never wired into the app lifecycle. This is the most likely root cause.

**Fix**: Wire all polling services into the FastAPI startup. Use APScheduler (already in the spec as a dependency) with the FastAPI lifespan pattern:

```python
# aegis/main.py
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aegis.config import settings
from aegis.ingestion.email_poller import poll_emails
from aegis.ingestion.teams_poller import poll_teams
from aegis.ingestion.calendar_sync import sync_calendar
from aegis.processing.triage import run_triage_batch
from aegis.processing.workstream_detector import run_detection
from aegis.processing.pipeline import process_pending

scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schedule all polling services
    scheduler.add_job(sync_calendar, 'interval', 
                      seconds=settings.polling_calendar_seconds,
                      id='calendar_sync', replace_existing=True)
    scheduler.add_job(poll_emails, 'interval', 
                      seconds=settings.polling_email_seconds,
                      id='email_poller', replace_existing=True)
    scheduler.add_job(poll_teams, 'interval', 
                      seconds=settings.polling_teams_seconds,
                      id='teams_poller', replace_existing=True)
    scheduler.add_job(run_triage_batch, 'interval', 
                      seconds=1800,  # every 30 min
                      id='triage_batch', replace_existing=True)
    scheduler.add_job(process_pending, 'interval', 
                      seconds=1800,  # every 30 min, after triage
                      id='extraction_pipeline', replace_existing=True)
    scheduler.add_job(run_detection, 'interval', 
                      seconds=1800,  # every 30 min, after extraction
                      id='workstream_detector', replace_existing=True)
    
    # Run initial sync immediately on startup
    scheduler.add_job(sync_calendar, id='calendar_sync_init')
    scheduler.add_job(poll_emails, id='email_poller_init')
    scheduler.add_job(poll_teams, id='teams_poller_init')
    
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)
```

### 2. Are the poller functions async and properly structured?

Read each poller file and check:

**`aegis/ingestion/email_poller.py`** — should have an async function (e.g., `async def poll_emails()`) that:
1. Gets a token from GraphClient
2. Fetches new emails since last poll (using a tracked timestamp or `$filter`)
3. Classifies each as human/automated/newsletter
4. Stores in the emails table
5. Updates system_health table with success/failure status

**`aegis/ingestion/teams_poller.py`** — should have an async function that:
1. Fetches chats and channel messages from Graph API
2. Applies noise filter (skip system msgs, reactions, <15 char)
3. Stores in chat_messages table
4. On first run or periodically: syncs Teams membership (teams, channels, members)
5. Updates system_health table

**Common issues to check**:
- Functions exist but are `def` instead of `async def` (APScheduler with AsyncIOScheduler needs async functions, or use `loop.run_in_executor`)
- Functions raise unhandled exceptions on first run (e.g., no "last poll timestamp" exists yet, causing a query error)
- The GraphClient isn't initialized (token cache not loaded, or the client is instantiated but `get_token()` is never called)
- Graph API queries use wrong endpoints or parameters

### 3. Is the system_health table being updated?

Each poller should update system_health after every run cycle. If the table is empty, either:
- The pollers never run (back to issue #1)
- The pollers run but don't have system_health update logic

Search the codebase:
```bash
grep -r "system_health" aegis/ --include="*.py"
```

If no results outside of `models.py`, the health tracking was defined in the schema but never implemented in the poller code.

**Fix**: Add health tracking to every poller. Create a utility function:

```python
# aegis/db/repositories.py (or a new aegis/health.py)
async def update_health(session, service: str, success: bool, items: int = 0, error: str = None):
    from aegis.db.models import SystemHealth
    from sqlalchemy import select
    from datetime import datetime, timezone
    
    now = datetime.now(timezone.utc)
    result = await session.execute(select(SystemHealth).where(SystemHealth.service == service))
    record = result.scalar_one_or_none()
    
    if record is None:
        record = SystemHealth(service=service)
        session.add(record)
    
    if success:
        record.last_success = now
        record.items_processed_last_hour = items
        record.status = 'healthy'
    else:
        record.last_error = now
        record.last_error_message = str(error)[:500] if error else None
        record.status = 'degraded'
    
    record.updated = now
    await session.commit()
```

Then wrap every poller in a try/except that calls this:

```python
async def poll_emails():
    try:
        count = await _do_email_poll()
        await update_health(session, 'email_poller', success=True, items=count)
    except Exception as e:
        await update_health(session, 'email_poller', success=False, error=e)
        raise  # re-raise so APScheduler logs it
```

### 4. Is Teams membership sync happening?

The Teams poller should sync team/channel/membership data, not just chat messages. Check if `teams_poller.py` has logic to call:
- `GraphClient.get_my_teams()` → populate `teams` table
- `GraphClient.get_team_channels(team_id)` → populate `team_channels` table
- `GraphClient.get_team_members(team_id)` → populate `team_memberships` table + create/update people records

This should run on first startup and then periodically (daily is fine — team membership doesn't change often).

If this logic doesn't exist, add it as an `async def sync_teams_membership()` function that runs once on startup and then daily.

### 5. Why is embedding coverage only 27%?

With the Phase 2 seeded meetings, some got embeddings. But if email and Teams tables are empty, the only items with embeddings are the few seeded meetings from Phase 1. The 27% is probably: 
- Meetings with transcripts that got processed in Phase 2: have embeddings ✅
- Meetings without transcripts (no_audio): may not have embeddings ⚠️
- All emails and chat messages: don't exist yet ❌

**This will resolve itself** once the pollers are fixed and data flows in. However, also check:

```python
# Is embedding generation being called in the pipeline?
grep -r "embedding" aegis/processing/ --include="*.py"
```

Verify that:
- The pipeline calls the embedding function after extraction
- Contextual items (triage_class='contextual') also get embeddings
- The OpenAI API key is correctly loaded from .env
- The embedding function doesn't silently fail (wrap in try/except with logging)

### 6. Processing pipeline ordering

Once pollers are running, verify the 30-minute batch cycle runs in correct order:

1. Pollers fetch new data → emails and chat_messages tables populate
2. Triage batch runs → sets triage_class on new items
3. Extraction pipeline runs → processes substantive items, creates entities
4. Workstream detector runs → assigns items to workstreams

If these run in parallel instead of sequentially, you'll get items going to extraction before triage (processing everything, not just substantive), or items going to workstream detection before extraction (nothing to assign).

**Fix**: Either chain them with dependencies in APScheduler, or use a single orchestrator function:

```python
async def run_processing_cycle():
    """Runs every 30 minutes. Steps must execute in order."""
    await run_triage_batch()       # Step 1: classify new items
    await process_pending()         # Step 2: extract from substantive items
    await run_workstream_assignment()  # Step 3: assign to workstreams
```

Then schedule this single function instead of three separate jobs:

```python
scheduler.add_job(run_processing_cycle, 'interval', seconds=1800, id='processing_cycle')
```

The pollers (email, Teams, calendar) run on their OWN intervals independently — they just fetch and store data. The processing cycle is separate and sequential.

### 7. Crash recovery on startup

Per the spec, on startup the app should reset any items stuck in `processing` state:

```python
# In the lifespan startup, before starting the scheduler:
async with async_session() as session:
    await session.execute(
        update(Meeting).where(Meeting.processing_status == 'processing')
        .values(processing_status='pending')
    )
    await session.execute(
        update(Email).where(Email.processing_status == 'processing')
        .values(processing_status='pending')
    )
    await session.execute(
        update(ChatMessage).where(ChatMessage.processing_status == 'processing')
        .values(processing_status='pending')
    )
    await session.commit()
```

If this doesn't exist, add it to the lifespan startup.

## Verification After Fix

After implementing fixes, restart the app and wait 5 minutes for the first poll cycle to complete. Then:

```bash
# Quick check — are tables populating?
psql -h localhost -p 5434 -U postgres -d aegis -c "
SELECT 'emails' as tbl, COUNT(*) FROM emails
UNION ALL SELECT 'chat_messages', COUNT(*) FROM chat_messages
UNION ALL SELECT 'teams', COUNT(*) FROM teams
UNION ALL SELECT 'system_health', COUNT(*) FROM system_health;
"
```

If counts are > 0, the pollers are running. Wait for the full 30-minute processing cycle, then re-run the verification script:

```bash
python scripts/verify_phase3.py --verbose
```

All Section 1-9 failures should now be PASS or WARNING. Sections 11-12 (workstreams and embeddings) may take another cycle or two to populate fully.

## Files To Read First

1. `aegis/main.py` — check lifespan/startup hooks
2. `aegis/ingestion/email_poller.py` — check if poll function exists and is async
3. `aegis/ingestion/teams_poller.py` — check if poll function exists and includes membership sync
4. `aegis/ingestion/poller.py` — check if an orchestrator exists
5. `aegis/processing/pipeline.py` — check processing cycle ordering
6. `aegis/intelligence/scheduler.py` — check if APScheduler is configured here instead of main.py
7. `aegis/config.py` — check polling interval settings exist

Read ALL of these before making changes. Understand the current structure, then fix it. Do not rewrite modules that are working — the issue is wiring, not logic.
