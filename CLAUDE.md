# AEGIS — AI Chief of Staff
## Complete Project Specification for Claude Code (macOS)

> **This file should be saved as `CLAUDE.md` in the project root.**
> Claude Code reads it automatically on startup. Do not modify during build.
>
> Key sections by task:
> - Schema → Section 3
> - Pipeline flow → Section 2 + Pipeline Flow Clarification
> - Processing contracts → Section 6
> - Agent assignments → Section 9c
> - Review checklists → Section 9c (per-phase review agent checks)
> - Testing strategy → Section 9f
> - Architectural rules → Section 9e (all agents must follow)

---

## 1. Project Overview

Aegis is a single-user AI Chief of Staff for macOS. It ingests meeting transcripts (via Screenpipe device-level audio capture), Outlook emails, and Microsoft Teams messages through the Microsoft Graph API. It continuously builds an organizational knowledge graph — people, workstreams, action items, decisions, commitments, dependencies — and proactively generates briefings, tracks readiness, detects sentiment patterns, and drafts communications in the user's voice.

**Platform**: macOS only (Apple Silicon preferred, Intel supported)
**Language**: Python 3.11+
**Package manager**: `uv` if available, otherwise `pip` with `pyproject.toml`
**Capture layer**: Screenpipe (pre-installed separately by user)
**Target**: Single user, single machine, `localhost:8000`

---

## 2. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                         DATA SOURCES                              │
├────────────────────────┬─────────────────────────────────────────┤
│  Screenpipe            │  Microsoft Graph API                     │
│  REST API on :3030     │  (single OAuth for all below)            │
│  (already running)     │                                          │
│  Audio transcriptions  │  • Outlook Mail (full inbox, all folders)│
│  Screen OCR text       │  • Outlook Calendar (events + attendees) │
│  Speaker diarization   │  • Teams Chats (1:1, group, meeting)     │
│                        │  • Teams Channels (messages + replies)    │
│                        │  • Teams Membership (teams + members)     │
└──────────┬─────────────┴──────────────┬──────────────────────────┘
           │                            │
           ▼                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                    INGESTION SERVICE                               │
│  Python async daemon (runs continuously with FastAPI)             │
│                                                                    │
│  ★ Calendar Sync (PRIMARY — meeting backbone):                    │
│    • Pulls today + tomorrow events from Graph API                 │
│    • On startup + every 30 min to catch changes                   │
│    • FILTERS: skip all-day, cancelled, declined, solo blocks,     │
│      OOO, focus time. Keep 2+ attendee or isOnlineMeeting=true    │
│    • Recurring events linked via seriesMasterId                   │
│    • Keyword exclusion list (confidential, HR, legal, etc.)       │
│    • Upsert by calendar_event_id (idempotent)                     │
│                                                                    │
│  • Meeting Transcript Builder:                                     │
│    After event ends (+5 min buffer):                              │
│    1. Detect adjacent meetings, truncate padding to avoid overlap  │
│    2. Query Screenpipe for audio in adjusted time window           │
│    3. Handle overlapping events via screen OCR (active app detect) │
│    4. Detect meeting overage (audio continues past scheduled end)  │
│    5. Set transcript_status: captured/partial/no_audio             │
│    6. Pull screen OCR for shared content (slides, docs)            │
│    7. Stitch into single transcript, attach to meeting row         │
│    8. Send to processing pipeline                                  │
│    * All timestamps normalized to UTC on ingestion                 │
│                                                                    │
│  • Unattributed Audio Detector (every 15 min):                    │
│    Scan Screenpipe for multi-speaker audio outside calendar        │
│    windows. Flag for user to label or dismiss.                    │
│                                                                    │
│  • Email Poller (every 15 min):                                   │
│    Fetch new emails from Graph API. Pre-filter:                   │
│    - Rule-based noise filter (no-reply, unsubscribe, blocklist)   │
│    - Classify as human/automated/newsletter                       │
│    - Only human emails proceed to full processing                 │
│    - Automated stored with minimal metadata (still searchable)    │
│                                                                    │
│  • Teams Poller (every 10 min):                                   │
│    Fetch new messages from chats + channels. Pre-filter:          │
│    - Skip system messages, reactions, <15 char, emoji-only        │
│    - Batch channel messages into 30-min windows                   │
│    - Process 1:1 and group chats individually                     │
│                                                                    │
│  • Deduplication: track last-seen timestamps per source           │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                    TRIAGE LAYER (Haiku batch, every 30 min)       │
│                                                                    │
│  All new items since last run evaluated in one LLM call:          │
│  • Substantive (0.7-1.0): contains decisions, asks, deliverables, │
│    project updates, new information → full extraction + embedding  │
│  • Contextual (0.3-0.7): provides context but no extractable      │
│    intelligence on its own → embedding only + workstream assign    │
│  • Noise (0.0-0.3): zero intelligence value → store for search,   │
│    skip extraction, skip embedding, skip workstream assignment     │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│              PROCESSING PIPELINE (LangGraph)                      │
│                                                                    │
│  classify_node → [branch by source type] →                        │
│  extract_node → resolve_node → store_node → alert_node            │
│                                                                    │
│  classify: Meeting transcript, email, or Teams chat?              │
│                                                                    │
│  meeting_extract: Extract people, action items, decisions,        │
│    commitments, dependencies, topics, sentiment                   │
│                                                                    │
│  email_extract: Classify intent (request/fyi/decision_needed/     │
│    follow_up/question/response/scheduling). Extract specific      │
│    asks with requester→target directionality. Detect urgency      │
│    and deadlines. Determine if response required.                 │
│                                                                    │
│  chat_extract: Same as email but adapted for informal messages.   │
│    Channel batches get batch summary extraction.                  │
│                                                                    │
│  thread_analyze: For email/chat threads with multiple messages,   │
│    determine which asks are resolved vs still pending.            │
│                                                                    │
│  resolve: Entity resolution. Match extracted people against       │
│    People table. Fuzzy match with rapidfuzz + LLM fallback.      │
│    Detect external people by email domain.                        │
│    Log role/dept changes to people_history.                       │
│                                                                    │
│  store: Write to PostgreSQL + generate embeddings.                │
│    Idempotent: temp=0, track last_extracted_at, dedup by          │
│    source_id + embedding similarity. Merge, never duplicate.      │
│                                                                    │
│  alert: Check triggers — overdue items, stale asks,               │
│    unresolved decisions, new high-urgency asks                    │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│              WORKSTREAM DETECTION (runs alongside pipeline)       │
│                                                                    │
│  Layer 1 — Weekly clustering (batch job):                         │
│    Embed all unassigned items from last 7 days. Cluster by        │
│    semantic similarity with org chart partition constraint         │
│    (items from unrelated departments with no shared participants  │
│    cannot cluster). LLM reviews each cluster: confirms coherence, │
│    names it, splits over-grouped clusters. Creates new workstream │
│    only if 3+ items across 2+ source types with confidence >0.7.  │
│    Lower confidence → "Suggested workstream" for user to accept.  │
│                                                                    │
│  Layer 2 — Assignment (every 30 min):                             │
│    Pre-filter: compute embedding similarity of new items against  │
│    active workstreams. Eliminate <0.4 similarity. Send remaining  │
│    candidates + workstream list to Haiku in one batch call.       │
│    >0.8 confidence → auto-assign. 0.6-0.8 → assign with low-    │
│    confidence flag. <0.6 → leave unassigned for weekly clustering.│
│    Items can be assigned to MULTIPLE workstreams.                 │
│                                                                    │
│  Layer 3 — LLM verification (new workstream creation only):      │
│    Dedup check against existing workstreams. Coherence check.     │
│    Naming. Runs only when Layer 1 proposes a new workstream.      │
│                                                                    │
│  Re-classification trigger: runs immediately after any            │
│  workstream split, merge, or manual creation. Scans unassigned    │
│  items, parent workstream items, and adjacent workstreams.        │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│              KNOWLEDGE BASE (PostgreSQL + pgvector)                │
│                                                                    │
│  Core tables:                                                      │
│  ├── people             (see schema section)                      │
│  ├── departments        (inferred + manual org structure)         │
│  ├── people_history     (role/dept/manager change audit trail)    │
│  ├── workstreams        (unified: replaces old projects table)    │
│  ├── workstream_items   (polymorphic junction, multi-membership)  │
│  ├── workstream_stakeholders                                      │
│  ├── workstream_milestones                                        │
│  ├── meetings           (calendar-driven + transcript)            │
│  ├── emails             (with triage class + intent)              │
│  ├── email_asks         (directional: requester→target)           │
│  ├── teams / team_channels / team_memberships                     │
│  ├── chat_messages      (with noise filter + intent)              │
│  ├── chat_asks          (mirrors email_asks structure)            │
│  ├── action_items       (from meetings)                           │
│  ├── decisions          (from meetings + emails)                  │
│  ├── commitments        (who promised what to whom)               │
│  ├── dependencies       (workstream→workstream blockers)          │
│  ├── topics             (semantic tags with embeddings)           │
│  ├── drafts             (pending review: nudges, recaps, replies) │
│  ├── briefings          (stored morning/Monday/Friday briefs)     │
│  ├── voice_profile      (auto-learned + custom rules)             │
│  ├── dashboard_cache    (pre-computed aggregations, 15-min TTL)   │
│  ├── chat_sessions      (RAG chat conversation history)           │
│  ├── admin_settings     (runtime-editable config, overrides .env) │
│  ├── sentiment_aggregations (pre-computed per-scope sentiment)    │
│  ├── attachments        (email/chat attachment metadata only)     │
│  ├── system_health      (per-service heartbeat + status)          │
│  └── llm_usage          (daily token counts + cost tracking)      │
│                                                                    │
│  Junction tables:                                                  │
│  ├── meeting_attendees / meeting_topics                            │
│  ├── email_topics / chat_message_topics                            │
│                                                                    │
│  All timestamps: TIMESTAMP WITH TIME ZONE (UTC)                   │
│  All text content columns with embeddings: vector(1536)           │
│  HNSW indexes on all embedding columns                            │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                    INTELLIGENCE LAYER                              │
│                                                                    │
│  Scheduled outputs (APScheduler, all toggleable in admin):        │
│  ├── Morning Briefing (daily, configurable time)                  │
│  │   Today's meetings with suggested topics per meeting           │
│  │   Requires-your-action items (decisions + asks + stale)        │
│  │   Overnight activity summary                                   │
│  │   Workstream health overview with sentiment                    │
│  │   Drafts ready for review                                      │
│  │   Also pre-generates all meeting prep briefs for the day       │
│  │                                                                │
│  ├── Monday Brief (replaces morning brief on Mondays)             │
│  │   LLM-identified objectives for the week                      │
│  │   Calendar overview + deadlines this week                      │
│  │   Workstreams needing attention                                │
│  │   Carryover from last week                                     │
│  │                                                                │
│  ├── Meeting Prep (pre-generated, notification 15 min before)     │
│  │   Attendee profiles + recent interactions                      │
│  │   Open items involving attendees                               │
│  │   Linked workstream status                                     │
│  │   Previous meeting in recurring series                         │
│  │   Suggested talking points (LLM-generated)                     │
│  │   Always available on-demand (pre-computed, no latency)        │
│  │                                                                │
│  ├── Friday Recap (weekly, configurable time)                     │
│  │   Decisions made this week                                     │
│  │   Commitment tracker (made/completed/overdue)                  │
│  │   Ask completion rate                                          │
│  │   Workstream summary                                           │
│  │   Sentiment trends + friction alerts                           │
│  │   People to check in with                                      │
│  │                                                                │
│  ├── Draft Generation (with morning brief + on triggers)          │
│  │   Auto-nudges for stale items past threshold                   │
│  │   Meeting recaps for completed meetings                        │
│  │   All drafts use voice profile, await user approval            │
│  │                                                                │
│  ├── Org Inference (weekly batch)                                 │
│  │   CC gravity scoring from email patterns                       │
│  │   1:1 calendar pattern detection                               │
│  │   Email signature parsing (one-time per person)                │
│  │   Teams membership as direct department signal                 │
│  │   Department clustering + responsibility mapping               │
│  │   Request routing validation                                   │
│  │                                                                │
│  └── Workstream Lifecycle (daily)                                 │
│      Auto-quiet after configurable inactivity period              │
│      Auto-archive after 90 days quiet/completed                   │
│                                                                    │
│  Delivery channels (all toggleable per output in admin):          │
│  ├── Web dashboard (primary)                                      │
│  ├── macOS notifications (time-sensitive alerts)                  │
│  ├── Email to self via Mail.Send (briefings)                      │
│  └── Teams message to self via ChatMessage.Send (briefings)       │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                    RESPONSE WORKFLOW                               │
│                                                                    │
│  For any pending decision, ask, or stale item:                    │
│  1. User types directive in plain language                        │
│     "Approved, but cap at $280K and require monthly reporting"    │
│  2. Aegis generates full email/Teams message in user's voice      │
│     using voice profile + context from source item + workstream   │
│  3. Channel-aware: email ask → email reply, Teams ask → Teams     │
│  4. User reviews, edits if needed, clicks Send or Discard         │
│  5. Sent via Graph API (Mail.Send or ChatMessage.Send)            │
│  6. Threaded onto original conversation                           │
│  7. System updates ask/item status to completed                   │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                    WEB DASHBOARD (FastAPI + Jinja2 + HTMX)        │
│                    http://localhost:8000                            │
│                    Mobile-responsive (Tailwind CSS)                │
│                                                                    │
│  Sidebar navigation:                                               │
│  ├── /                  Command center (daily hub)                │
│  ├── /workstreams       All workstreams (filterable table)        │
│  ├── /workstreams/:id   Workstream detail (timeline + sidebar)    │
│  ├── /readiness         Workload balance / personnel readiness    │
│  ├── /departments       Department health (moved from dashboard)  │
│  ├── /people            People directory + needs-review queue     │
│  ├── /org               Org chart (inferred + manual)             │
│  ├── /actions           Action items                              │
│  ├── /asks              Pending asks (email + Teams, in + out)    │
│  ├── /meetings          Meeting history with search               │
│  ├── /emails            Email browser with intent badges          │
│  ├── /ask               Ask Aegis (RAG chat, also floating widget)│
│  └── /admin             Admin settings (~70 configurable values)  │
│                                                                    │
│  Command center zones:                                             │
│  1. Active workstreams (horizontal scroll cards, pinned first)    │
│  2. Requires your attention (tabbed: decisions/awaiting/stale)    │
│  3. Today's meetings (with suggested topics, prep brief links)    │
│  4. Drafts ready for review (send/edit/discard)                   │
│  5. "Next up" floating widget (bottom-right, links to prep brief) │
│  6. "Ask Aegis" chat panel (right sidebar, togglable)             │
│                                                                    │
│  Dashboard cache: pre-computed every 15 min, stored in            │
│  dashboard_cache table. Immediate refresh on meeting processing.  │
│  Cache keys: 'workstream_cards', 'pending_decisions',             │
│  'awaiting_response', 'stale_items', 'todays_meetings',          │
│  'drafts_pending', 'readiness_scores', 'department_health'        │
│                                                                    │
│  Admin settings: ~70 configurable values stored in admin_settings │
│  table (overrides .env defaults at runtime). Collapsible sections,│
│  HTMX auto-save, no page reload needed. Changes take effect       │
│  immediately except polling intervals (next cycle).               │
└──────────────────────────────────────────────────────────────────┘
```

---

### Pipeline Flow Clarification

**Ordering matters.** Every 30 minutes, the system runs this sequence:

1. **Polling** — email poller, Teams poller, and calendar sync all fetch new data
2. **Rule-based noise filter** — catches automated emails, system messages, reactions (no LLM cost)
3. **Triage** — one Haiku batch call classifies remaining items as substantive/contextual/noise
4. **Extraction** — Haiku runs full entity/ask extraction on substantive items only
5. **Workstream assignment** — one Haiku batch call assigns substantive + contextual items to workstreams

**Meeting transcripts bypass triage** — they always go directly to full extraction. A meeting you attended is always substantive. Triage only applies to emails and Teams messages.

**Email noise classification feeds triage.** The email poller classifies emails as human/automated/newsletter using rule-based heuristics (no-reply sender, unsubscribe links, blocklist). Only `human` emails enter the triage batch. Automated and newsletter emails are stored with metadata but skip triage, extraction, and workstream assignment entirely.

**Contextual items get embeddings but no extraction.** "Sounds good, thanks!" gets an embedding (cheap) so it appears in vector search, and gets assigned to a workstream (so it shows in the timeline), but it does NOT get entity/ask extraction (expensive, would produce nothing useful).

**Crash recovery.** Each item has a `processing_status` field: pending → processing → completed → failed. The pipeline sets `processing` before extraction and `completed` after committing entities. On startup, Aegis resets any items stuck in `processing` back to `pending` and re-queues them. Combined with extraction idempotency, re-processing a partially extracted item is safe.

**Graph API pagination.** All Graph API list endpoints return paginated results (50-100 items per page). Every call that fetches a list MUST follow `@odata.nextLink` until exhausted. Never assume a single page contains all results. The backfill script (which processes thousands of items) must handle pagination, respect `Retry-After` headers on 429 responses, pace requests (100ms between pages), and support resume-on-failure via progress tracking.

**Meeting chat correlation.** Teams meeting chats (`chatType = "meeting"`) are linked to their corresponding meeting record via `onlineMeetingId`. Chat messages from meeting chats get `linked_meeting_id` set on the `chat_messages` table. The meeting detail page shows transcript + meeting chat messages together. Pre-meeting chat messages enhance the meeting prep brief.

**Attachment handling.** Emails and Teams messages may have attachments. Store metadata only (filename, content_type, size_bytes) in the `attachments` table — do NOT download file content. Include attachment filenames in extraction prompts for better ask/deliverable identification. Skip inline images (`is_inline = true`) during extraction.

---

## 3. Database Schema

### Core Tables

**Note on creation order**: The SQL below is organized by domain, not creation order.
Alembic handles dependency ordering. For manual creation, use: departments → people →
people_history → workstreams → workstream_items → workstream_stakeholders →
workstream_milestones → meetings → meeting_attendees → emails → email_asks → teams →
team_channels → team_memberships → chat_messages → chat_asks → action_items →
decisions → commitments → dependencies → topics → junction tables → system tables.

```sql
-- ═══════════════════════════════════════════════════════════
-- PEOPLE & ORG STRUCTURE
-- ═══════════════════════════════════════════════════════════

CREATE TABLE people (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    aliases TEXT[] DEFAULT '{}',
    title TEXT,
    role TEXT,
    email TEXT UNIQUE,
    org TEXT,
    department_id INT REFERENCES departments(id),
    manager_id INT REFERENCES people(id),
    seniority TEXT CHECK (seniority IN ('executive','senior','mid','junior','unknown'))
        DEFAULT 'unknown',
    is_external BOOLEAN DEFAULT FALSE,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    interaction_count INT DEFAULT 0,
    cc_gravity_score FLOAT DEFAULT 0.0,
    notes TEXT,
    source TEXT CHECK (source IN ('calendar','email','teams','meeting','manual','backfill'))
        DEFAULT 'calendar',
    confidence FLOAT DEFAULT 0.5,
    needs_review BOOLEAN DEFAULT TRUE,
    llm_suggestion JSONB,  -- LLM's suggested profile for user to approve/correct
    embedding vector(1536)
);

CREATE TABLE departments (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    responsibilities TEXT,
    head_id INT REFERENCES people(id),
    parent_dept_id INT REFERENCES departments(id),
    source TEXT CHECK (source IN ('inferred','manual','teams')) DEFAULT 'inferred',
    confidence FLOAT DEFAULT 0.5
);

CREATE TABLE people_history (
    id SERIAL PRIMARY KEY,
    person_id INT NOT NULL REFERENCES people(id),
    field_changed TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    change_source TEXT CHECK (change_source IN ('inferred','manual'))
);

-- ═══════════════════════════════════════════════════════════
-- WORKSTREAMS (replaces projects table entirely)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE workstreams (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT CHECK (status IN ('active','quiet','paused','completed','archived'))
        DEFAULT 'active',
    created_by TEXT CHECK (created_by IN ('auto','manual')) DEFAULT 'manual',
    confidence FLOAT DEFAULT 1.0,  -- 1.0 for manual, lower for auto
    owner_id INT REFERENCES people(id),  -- NULL for unmanaged
    target_date DATE,
    is_managed BOOLEAN DEFAULT FALSE,  -- has owner/milestones
    pinned BOOLEAN DEFAULT FALSE,
    auto_quiet_days INT DEFAULT 14,
    split_from_id INT REFERENCES workstreams(id),
    merged_into_id INT REFERENCES workstreams(id),
    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    embedding vector(1536)
);

CREATE TABLE workstream_items (
    id SERIAL PRIMARY KEY,
    workstream_id INT NOT NULL REFERENCES workstreams(id),
    item_type TEXT NOT NULL CHECK (item_type IN (
        'meeting','email','chat_message','action_item',
        'decision','commitment','email_ask','chat_ask'
    )),
    item_id INT NOT NULL,
    relevance_score FLOAT DEFAULT 1.0,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    linked_by TEXT CHECK (linked_by IN ('auto','manual')) DEFAULT 'auto',
    UNIQUE(workstream_id, item_type, item_id)
    -- Same item can link to MULTIPLE workstreams (different workstream_ids)
);

CREATE TABLE workstream_stakeholders (
    workstream_id INT NOT NULL REFERENCES workstreams(id),
    person_id INT NOT NULL REFERENCES people(id),
    role TEXT CHECK (role IN ('owner','lead','contributor','informed'))
        DEFAULT 'contributor',
    PRIMARY KEY (workstream_id, person_id)
);

CREATE TABLE workstream_milestones (
    id SERIAL PRIMARY KEY,
    workstream_id INT NOT NULL REFERENCES workstreams(id),
    name TEXT NOT NULL,
    description TEXT,
    target_date DATE,
    status TEXT CHECK (status IN ('pending','in_progress','completed'))
        DEFAULT 'pending',
    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ═══════════════════════════════════════════════════════════
-- MEETINGS (calendar-driven)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE meetings (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,
    duration INT,  -- minutes
    status TEXT CHECK (status IN ('scheduled','in_progress','completed'))
        DEFAULT 'scheduled',
    transcript_status TEXT CHECK (transcript_status IN (
        'pending','captured','partial','no_audio','processing','failed'
    )) DEFAULT 'pending',
    meeting_type TEXT CHECK (meeting_type IN (
        'virtual','in_person','hybrid','solo_block'
    )) DEFAULT 'virtual',
    is_excluded BOOLEAN DEFAULT FALSE,
    calendar_event_id TEXT UNIQUE,
    online_meeting_url TEXT,
    recurring_series_id TEXT,
    instance_number INT,
    organizer_email TEXT,
    summary TEXT,
    transcript_text TEXT,
    screen_context JSONB,
    last_extracted_at TIMESTAMPTZ,
    processing_status TEXT CHECK (processing_status IN (
        'pending','processing','completed','failed'
    )) DEFAULT 'pending',
    processing_error TEXT,         -- error message if failed (no PII)
    sentiment TEXT CHECK (sentiment IN ('positive','neutral','tense','negative','urgent')),
    embedding vector(1536)
);

CREATE TABLE meeting_attendees (
    meeting_id INT NOT NULL REFERENCES meetings(id),
    person_id INT NOT NULL REFERENCES people(id),
    PRIMARY KEY (meeting_id, person_id)
);

-- ═══════════════════════════════════════════════════════════
-- EMAILS
-- ═══════════════════════════════════════════════════════════

CREATE TABLE emails (
    id SERIAL PRIMARY KEY,
    graph_id TEXT UNIQUE NOT NULL,
    subject TEXT,
    sender_id INT REFERENCES people(id),
    recipients JSONB,  -- [{email, name, type: to|cc}]
    datetime TIMESTAMPTZ NOT NULL,
    body_text TEXT,
    body_preview TEXT,
    thread_id TEXT,
    is_read BOOLEAN,
    importance TEXT,
    has_attachments BOOLEAN DEFAULT FALSE,
    email_class TEXT CHECK (email_class IN ('human','automated','newsletter'))
        DEFAULT 'human',
    triage_class TEXT CHECK (triage_class IN ('substantive','contextual','noise')),
    triage_score FLOAT,
    intent TEXT CHECK (intent IN (
        'request','fyi','decision_needed','follow_up',
        'question','response','scheduling'
    )),
    requires_response BOOLEAN,
    response_status TEXT CHECK (response_status IN (
        'pending','replied','no_action_needed','overdue'
    )),
    summary TEXT,
    last_extracted_at TIMESTAMPTZ,
    processing_status TEXT CHECK (processing_status IN (
        'pending','processing','completed','failed'
    )) DEFAULT 'pending',
    processing_error TEXT,
    sentiment TEXT CHECK (sentiment IN ('positive','neutral','tense','negative','urgent')),
    embedding vector(1536)
);

CREATE TABLE email_asks (
    id SERIAL PRIMARY KEY,
    email_id INT NOT NULL REFERENCES emails(id),
    thread_id TEXT,
    ask_type TEXT CHECK (ask_type IN (
        'deliverable','decision','follow_up','question',
        'approval','review','info_request'
    )) NOT NULL,
    description TEXT NOT NULL,
    requester_id INT REFERENCES people(id),
    target_id INT REFERENCES people(id),
    deadline TEXT,
    urgency TEXT CHECK (urgency IN ('high','medium','low')) DEFAULT 'medium',
    status TEXT CHECK (status IN ('open','in_progress','completed','stale'))
        DEFAULT 'open',
    resolved_by_email_id INT REFERENCES emails(id),
    linked_action_item_id INT REFERENCES action_items(id),
    linked_meeting_id INT REFERENCES meetings(id),
    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    embedding vector(1536)
);

-- ═══════════════════════════════════════════════════════════
-- TEAMS
-- ═══════════════════════════════════════════════════════════

CREATE TABLE teams (
    id SERIAL PRIMARY KEY,
    graph_team_id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT
);

CREATE TABLE team_channels (
    id SERIAL PRIMARY KEY,
    graph_channel_id TEXT UNIQUE NOT NULL,
    team_id INT NOT NULL REFERENCES teams(id),
    name TEXT NOT NULL,
    description TEXT
);

CREATE TABLE team_memberships (
    team_id INT NOT NULL REFERENCES teams(id),
    person_id INT NOT NULL REFERENCES people(id),
    role TEXT,
    PRIMARY KEY (team_id, person_id)
);

CREATE TABLE chat_messages (
    id SERIAL PRIMARY KEY,
    graph_message_id TEXT UNIQUE NOT NULL,
    source_type TEXT CHECK (source_type IN ('teams_chat','teams_channel')) NOT NULL,
    chat_id TEXT,
    channel_id INT REFERENCES team_channels(id),
    sender_id INT REFERENCES people(id),
    datetime TIMESTAMPTZ NOT NULL,
    body_text TEXT,
    body_preview TEXT,
    thread_root_id TEXT,
    linked_meeting_id INT REFERENCES meetings(id),  -- for Teams meeting chats
    noise_filtered BOOLEAN DEFAULT FALSE,
    triage_class TEXT CHECK (triage_class IN ('substantive','contextual','noise')),
    triage_score FLOAT,
    intent TEXT CHECK (intent IN (
        'request','fyi','decision_needed','follow_up','question','response'
    )),
    requires_response BOOLEAN,
    summary TEXT,
    last_extracted_at TIMESTAMPTZ,
    processing_status TEXT CHECK (processing_status IN (
        'pending','processing','completed','failed'
    )) DEFAULT 'pending',
    processing_error TEXT,
    sentiment TEXT CHECK (sentiment IN ('positive','neutral','tense','negative','urgent')),
    embedding vector(1536)
);

CREATE TABLE chat_asks (
    id SERIAL PRIMARY KEY,
    message_id INT NOT NULL REFERENCES chat_messages(id),
    ask_type TEXT CHECK (ask_type IN (
        'deliverable','decision','follow_up','question',
        'approval','review','info_request'
    )) NOT NULL,
    description TEXT NOT NULL,
    requester_id INT REFERENCES people(id),
    target_id INT REFERENCES people(id),
    deadline TEXT,
    urgency TEXT CHECK (urgency IN ('high','medium','low')) DEFAULT 'medium',
    status TEXT CHECK (status IN ('open','in_progress','completed','stale'))
        DEFAULT 'open',
    resolved_by_message_id INT REFERENCES chat_messages(id),
    linked_action_item_id INT REFERENCES action_items(id),
    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    embedding vector(1536)
);

-- ═══════════════════════════════════════════════════════════
-- EXTRACTED ENTITIES (from meetings)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE action_items (
    id SERIAL PRIMARY KEY,
    description TEXT NOT NULL,
    assignee_id INT REFERENCES people(id),
    source_meeting_id INT REFERENCES meetings(id),
    source_email_id INT REFERENCES emails(id),
    source_chat_message_id INT REFERENCES chat_messages(id),
    deadline TEXT,
    status TEXT CHECK (status IN ('open','in_progress','completed','stale'))
        DEFAULT 'open',
    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    embedding vector(1536)
);

CREATE TABLE decisions (
    id SERIAL PRIMARY KEY,
    description TEXT NOT NULL,
    decided_by INT REFERENCES people(id),
    source_meeting_id INT REFERENCES meetings(id),
    source_email_id INT REFERENCES emails(id),
    datetime TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    embedding vector(1536)
);

CREATE TABLE commitments (
    id SERIAL PRIMARY KEY,
    description TEXT NOT NULL,
    committer_id INT REFERENCES people(id),
    recipient_id INT REFERENCES people(id),
    source_meeting_id INT REFERENCES meetings(id),
    source_email_id INT REFERENCES emails(id),
    deadline TEXT,
    status TEXT CHECK (status IN ('open','completed','overdue','broken'))
        DEFAULT 'open',
    created TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE dependencies (
    id SERIAL PRIMARY KEY,
    blocker_workstream_id INT REFERENCES workstreams(id),
    blocked_workstream_id INT REFERENCES workstreams(id),
    description TEXT,
    status TEXT CHECK (status IN ('active','resolved')) DEFAULT 'active',
    identified_date TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE topics (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    embedding vector(1536)
);

-- ═══════════════════════════════════════════════════════════
-- TOPIC JUNCTION TABLES
-- ═══════════════════════════════════════════════════════════

CREATE TABLE meeting_topics (
    meeting_id INT NOT NULL REFERENCES meetings(id),
    topic_id INT NOT NULL REFERENCES topics(id),
    PRIMARY KEY (meeting_id, topic_id)
);

CREATE TABLE email_topics (
    email_id INT NOT NULL REFERENCES emails(id),
    topic_id INT NOT NULL REFERENCES topics(id),
    PRIMARY KEY (email_id, topic_id)
);

CREATE TABLE chat_message_topics (
    chat_message_id INT NOT NULL REFERENCES chat_messages(id),
    topic_id INT NOT NULL REFERENCES topics(id),
    PRIMARY KEY (chat_message_id, topic_id)
);

-- ═══════════════════════════════════════════════════════════
-- SYSTEM TABLES
-- ═══════════════════════════════════════════════════════════

CREATE TABLE drafts (
    id SERIAL PRIMARY KEY,
    draft_type TEXT CHECK (draft_type IN ('nudge','recap','response','follow_up'))
        NOT NULL,
    triggered_by_type TEXT CHECK (triggered_by_type IN (
        'action_item','email_ask','chat_ask','meeting'
    )),
    triggered_by_id INT,
    recipient_id INT REFERENCES people(id),
    channel TEXT CHECK (channel IN ('email','teams_chat')) NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    conversation_id TEXT,  -- for threading email replies
    chat_id TEXT,          -- for Teams replies
    status TEXT CHECK (status IN ('pending_review','sent','discarded','edited'))
        DEFAULT 'pending_review',
    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ
);

CREATE TABLE briefings (
    id SERIAL PRIMARY KEY,
    briefing_type TEXT CHECK (briefing_type IN (
        'morning','monday','friday','meeting_prep'
    )) NOT NULL,
    related_meeting_id INT REFERENCES meetings(id),  -- for meeting_prep only
    content TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE voice_profile (
    id SERIAL PRIMARY KEY,
    auto_profile TEXT,       -- LLM-generated from sent emails
    custom_rules TEXT[],     -- user-defined overrides
    edit_history JSONB,      -- diffs between generated drafts and user edits
    last_learned_at TIMESTAMPTZ,
    updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE dashboard_cache (
    key TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE chat_sessions (
    id SERIAL PRIMARY KEY,
    messages JSONB NOT NULL DEFAULT '[]',
    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE admin_settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    description TEXT,
    updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Runtime-editable settings. The admin page writes here.
-- On startup, values from admin_settings override var.env defaults.
-- This allows changing polling intervals, thresholds, schedules, 
-- notification toggles, etc. from the web UI without editing var.env.

CREATE TABLE sentiment_aggregations (
    id SERIAL PRIMARY KEY,
    scope_type TEXT CHECK (scope_type IN (
        'person','relationship','department','cross_department','workstream'
    )) NOT NULL,
    scope_id TEXT NOT NULL,         -- person_id, "person1_id:person2_id", dept_id, etc.
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    avg_score FLOAT NOT NULL,       -- 0-100
    interaction_count INT NOT NULL,
    trend TEXT CHECK (trend IN ('up','down','flat')),
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(scope_type, scope_id, period_start)
);
-- Pre-computed sentiment for department health, friction detection,
-- and workstream sentiment. Refreshed by daily background job.

CREATE TABLE attachments (
    id SERIAL PRIMARY KEY,
    source_type TEXT CHECK (source_type IN ('email','chat_message')) NOT NULL,
    source_id INT NOT NULL,
    graph_attachment_id TEXT,
    filename TEXT NOT NULL,
    content_type TEXT,              -- 'application/pdf', 'image/png', etc.
    size_bytes INT,
    is_inline BOOLEAN DEFAULT FALSE, -- inline images vs real attachments
    created TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Metadata only — no file content stored. Filenames used in extraction
-- prompts for better ask/deliverable identification. Inline images skipped
-- during extraction (is_inline = true).

CREATE TABLE system_health (
    service TEXT PRIMARY KEY,       -- 'screenpipe', 'email_poller', 'teams_poller',
                                    -- 'calendar_sync', 'triage_batch', 'workstream_detector'
    last_success TIMESTAMPTZ,
    last_error TIMESTAMPTZ,
    last_error_message TEXT,        -- truncated, no PII
    items_processed_last_hour INT DEFAULT 0,
    status TEXT CHECK (status IN ('healthy','degraded','down')) DEFAULT 'healthy',
    updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Status logic:
--   healthy  = last_success within expected polling interval
--   degraded = last_success stale (>2× interval) OR recent errors
--   down     = last_success >5× interval or never succeeded
-- Dashboard header reads from this table for live status indicators.

CREATE TABLE llm_usage (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    model TEXT NOT NULL,             -- 'haiku-4.5', 'sonnet-4.6', 'embedding-3-small'
    task TEXT NOT NULL,              -- 'triage', 'extraction', 'briefing', 'embedding', etc.
    input_tokens INT DEFAULT 0,
    output_tokens INT DEFAULT 0,
    calls INT DEFAULT 1,
    UNIQUE(date, model, task)
);
-- Upsert after each LLM call. Admin page shows daily/weekly cost estimate.
-- Not exact (pricing changes) but catches runaway costs.

-- ═══════════════════════════════════════════════════════════
-- INDEXES
-- ═══════════════════════════════════════════════════════════

CREATE INDEX idx_meetings_start ON meetings(start_time);
CREATE INDEX idx_meetings_calendar_event ON meetings(calendar_event_id);
CREATE INDEX idx_meetings_series ON meetings(recurring_series_id);
CREATE INDEX idx_emails_datetime ON emails(datetime);
CREATE INDEX idx_emails_thread ON emails(thread_id);
CREATE INDEX idx_emails_sender ON emails(sender_id);
CREATE INDEX idx_email_asks_status ON email_asks(status);
CREATE INDEX idx_email_asks_target ON email_asks(target_id);
CREATE INDEX idx_chat_messages_datetime ON chat_messages(datetime);
CREATE INDEX idx_chat_asks_status ON chat_asks(status);
CREATE INDEX idx_action_items_status ON action_items(status);
CREATE INDEX idx_action_items_assignee ON action_items(assignee_id);
CREATE INDEX idx_workstream_items_ws ON workstream_items(workstream_id);
CREATE INDEX idx_workstream_items_item ON workstream_items(item_type, item_id);
CREATE INDEX idx_people_email ON people(email);
CREATE INDEX idx_people_department ON people(department_id);

-- HNSW vector indexes
CREATE INDEX idx_meetings_embedding ON meetings
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_emails_embedding ON emails
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_chat_messages_embedding ON chat_messages
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_action_items_embedding ON action_items
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_email_asks_embedding ON email_asks
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_workstreams_embedding ON workstreams
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_topics_embedding ON topics
    USING hnsw (embedding vector_cosine_ops);
```

---

## 4. Project Structure

```
aegis/
├── pyproject.toml
├── docker-compose.yml              # PostgreSQL + pgvector (port 5434)
├── .env.example
├── .env                                # gitignored
├── .gitignore                          # must include: .env, ~/.aegis/, __pycache__, .venv/
├── alembic.ini
├── alembic/
│   └── versions/
├── aegis/
│   ├── __init__.py
│   ├── config.py                       # pydantic-settings, all ~70 config values
│   ├── main.py                         # FastAPI app + startup hooks + scheduler
│   ├── db/
│   │   ├── __init__.py
│   │   ├── engine.py                   # SQLAlchemy async engine + connection pool
│   │   ├── models.py                   # SQLAlchemy ORM models (all tables above)
│   │   └── repositories.py            # Data access layer (queries, aggregations)
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── screenpipe.py              # Screenpipe REST API client (async httpx)
│   │   ├── graph_client.py            # Microsoft Graph client (mail, calendar, Teams)
│   │   ├── calendar_sync.py           # Calendar sync + filtering + recurring link
│   │   ├── meeting_detector.py        # Transcript builder + overlap + overage
│   │   ├── email_poller.py            # Email polling + noise classification
│   │   ├── teams_poller.py            # Teams chat + channel polling + noise filter
│   │   ├── backfill.py                # Historical import (Phase 0, run once)
│   │   └── poller.py                  # Main polling orchestrator
│   ├── processing/
│   │   ├── __init__.py
│   │   ├── pipeline.py                # LangGraph pipeline definition
│   │   ├── triage.py                  # Triage layer (substantive/contextual/noise)
│   │   ├── meeting_extractor.py       # Meeting transcript extraction + prompts
│   │   ├── email_extractor.py         # Email intent + ask extraction + prompts
│   │   ├── chat_extractor.py          # Teams message extraction + prompts
│   │   ├── thread_analyzer.py         # Thread resolution analysis (email + chat)
│   │   ├── org_inference.py           # Org chart learning (CC gravity, 1:1s, sigs)
│   │   ├── workstream_detector.py     # 3-layer workstream detection + assignment
│   │   ├── resolver.py                # Entity resolution (fuzzy + LLM)
│   │   └── embeddings.py              # Embedding generation (OpenAI)
│   ├── intelligence/
│   │   ├── __init__.py
│   │   ├── briefings.py               # Morning + Monday + Friday brief generators
│   │   ├── meeting_prep.py            # Pre-meeting context + talking points
│   │   ├── alerts.py                  # Stale items, overdue tracking
│   │   ├── readiness.py               # Workload balance scoring
│   │   ├── sentiment.py               # Sentiment aggregation + friction detection
│   │   ├── draft_generator.py         # Auto-nudges, recaps, response drafts
│   │   ├── voice_profile.py           # Voice learning + management
│   │   └── scheduler.py              # APScheduler job definitions
│   ├── chat/
│   │   ├── __init__.py
│   │   └── rag.py                     # RAG: intent classify → hybrid search → rerank → answer
│   ├── web/
│   │   ├── __init__.py
│   │   ├── routes/
│   │   │   ├── dashboard.py           # Command center
│   │   │   ├── workstreams.py         # List + detail + merge/split
│   │   │   ├── readiness.py           # Workload balance view
│   │   │   ├── departments.py         # Department health
│   │   │   ├── people.py              # Directory + needs-review queue
│   │   │   ├── org_chart.py           # Org tree visualization
│   │   │   ├── actions.py             # Action items
│   │   │   ├── asks.py                # Pending asks (email + Teams)
│   │   │   ├── meetings.py            # Meeting history + prep briefs
│   │   │   ├── emails.py              # Email browser
│   │   │   ├── search.py              # Semantic + keyword hybrid search
│   │   │   ├── chat.py                # Ask Aegis (RAG chat)
│   │   │   ├── respond.py             # Response workflow (draft + send)
│   │   │   └── admin.py               # Admin settings
│   │   └── templates/
│   │       ├── base.html              # Layout + sidebar nav
│   │       ├── dashboard.html
│   │       ├── workstreams.html
│   │       ├── workstream_detail.html
│   │       ├── readiness.html
│   │       ├── departments.html
│   │       ├── people.html
│   │       ├── org_chart.html
│   │       ├── actions.html
│   │       ├── asks.html
│   │       ├── meetings.html
│   │       ├── meeting_prep.html
│   │       ├── emails.html
│   │       ├── search.html
│   │       ├── chat.html
│   │       ├── respond.html
│   │       ├── admin.html
│   │       └── components/            # HTMX partials
│   └── notifications/
│       ├── __init__.py
│       └── macos.py                   # macOS native notifications (osascript)
├── scripts/
│   ├── setup_graph.py                 # Interactive Azure + OAuth setup wizard
│   ├── backfill.py                    # Historical import CLI wrapper
│   └── seed_org.py                    # Optional: import CSV of people/org data
└── tests/
    ├── test_screenpipe.py
    ├── test_graph_client.py
    ├── test_triage.py
    ├── test_extractor.py
    ├── test_resolver.py
    ├── test_workstream_detector.py
    ├── test_pipeline.py
    └── test_readiness.py
```

---

### 4a. docker-compose.yml

Create this file in the project root. PostgreSQL + pgvector on port 5434
to avoid conflicts with other local PostgreSQL instances.

```yaml
# docker-compose.yml
services:
  aegis-db:
    image: pgvector/pgvector:pg16
    container_name: aegis-db
    ports:
      - "5434:5432"
    environment:
      POSTGRES_DB: aegis
      POSTGRES_HOST_AUTH_METHOD: trust
    volumes:
      - aegis_pgdata:/var/lib/postgresql/data
    restart: unless-stopped

volumes:
  aegis_pgdata:
```

### 4b. .env.example

Create this file in the project root. The user copies it to `.env` and fills
in their API keys. `.env` must be in `.gitignore`.

```env
# ═══════════════════════════════════════════════
# REQUIRED — must set before first run
# ═══════════════════════════════════════════════

# LLM API Keys
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# Microsoft Graph (from Azure app registration)
AZURE_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# Database (Docker on port 5434)
DATABASE_URL=postgresql+asyncpg://postgres@localhost:5434/aegis

# ═══════════════════════════════════════════════
# OPTIONAL — sensible defaults provided
# ═══════════════════════════════════════════════

# Screenpipe
SCREENPIPE_URL=http://localhost:3030

# Timezone (auto-detected from system if not set)
AEGIS_TIMEZONE=America/New_York

# Server
AEGIS_HOST=127.0.0.1
AEGIS_PORT=8000
LOG_LEVEL=INFO

# Polling intervals (seconds)
POLLING_CALENDAR_SECONDS=1800
POLLING_EMAIL_SECONDS=900
POLLING_TEAMS_SECONDS=600
POLLING_SCREENPIPE_SECONDS=300

# Intelligence schedule
MORNING_BRIEFING_TIME=07:30
MONDAY_BRIEF_TIME=07:30
FRIDAY_RECAP_TIME=16:00
MEETING_PREP_MINUTES_BEFORE=15

# Triage thresholds
TRIAGE_SUBSTANTIVE_THRESHOLD=0.7
TRIAGE_CONTEXTUAL_THRESHOLD=0.3

# Workstream detection
WORKSTREAM_AUTO_CREATE_CONFIDENCE=0.7
WORKSTREAM_ASSIGN_HIGH_CONFIDENCE=0.8
WORKSTREAM_ASSIGN_LOW_CONFIDENCE=0.6
WORKSTREAM_DEFAULT_QUIET_DAYS=14

# Stale item thresholds
STALE_ACTION_ITEM_DAYS=7
STALE_ASK_HOURS=72
STALE_NUDGE_THRESHOLD_DAYS=3

# Noise filtering
EMAIL_SKIP_NOREPLY=true
TEAMS_MIN_MESSAGE_LENGTH=15
TEAMS_CHANNEL_BATCH_MINUTES=30

# Data retention (days)
RETENTION_HOT_DAYS=90
RETENTION_WARM_DAYS=365

# Dashboard
DASHBOARD_CACHE_TTL_SECONDS=900
DASHBOARD_MAX_WORKSTREAM_SLOTS=8

# Notifications (true/false)
NOTIFY_MACOS=true
NOTIFY_EMAIL_SELF=false
NOTIFY_TEAMS_SELF=false

# Meeting exclusion keywords (comma-separated)
MEETING_EXCLUSION_KEYWORDS=confidential,HR,performance review,legal,board session,personnel,disciplinary,termination

# Readiness score thresholds
READINESS_LIGHT_MAX=40
READINESS_MODERATE_MAX=70
READINESS_HEAVY_MAX=85

# Sentiment
SENTIMENT_ROLLING_WINDOW_DAYS=30
SENTIMENT_TREND_WINDOW_DAYS=14
SENTIMENT_FRICTION_THRESHOLD=60
```

---

## 5. Microsoft Graph API — Permissions & Auth

**Auth model**: Delegated permissions via Device Code Flow (PublicClientApplication).
No client_secret needed. User signs in once via browser. MSAL caches refresh token
at `~/.aegis/msal_token_cache.json` (chmod 600). Silent refresh thereafter.

**Read permissions (required from day 1):**
- `Mail.Read` — read all emails (read + unread, all folders, sent items)
- `Calendars.Read` — read calendar events + attendees
- `User.Read` — basic profile info
- `Chat.Read` — read 1:1, group, and meeting chats
- `ChannelMessage.Read.All` — read channel messages in your teams
- `Team.ReadBasic.All` — list teams you belong to
- `Channel.ReadBasic.All` — list channels in your teams

**Write permissions (for response workflow + notifications):**
- `Mail.Send` — send emails (drafts, nudges, recaps, briefing digests)
- `Calendars.ReadWrite` — create calendar events (future: deadline blocks)
- `ChatMessage.Send` — send Teams chat messages (responses, notifications)

All permissions granted per-user via OAuth consent screen. No admin consent needed
unless the tenant restricts third-party apps.

---

## 6. Key Processing Contracts

### 6a. Triage Schema

```python
class TriageResult(BaseModel):
    item_id: int
    triage_class: Literal["substantive", "contextual", "noise"]
    score: float  # 0.0 - 1.0
    reason: str   # brief explanation
```

### 6b. Meeting Extraction Schema

```python
class MeetingExtraction(BaseModel):
    summary: str
    people: list[ExtractedPerson]
    action_items: list[ExtractedActionItem]
    decisions: list[ExtractedDecision]
    commitments: list[ExtractedCommitment]
    dependencies: list[ExtractedDependency]
    topics: list[str]
    sentiment: Literal["positive", "neutral", "tense", "negative", "urgent"]
```

### 6c. Email Extraction Schema

```python
class EmailExtraction(BaseModel):
    summary: str
    intent: Literal["request", "fyi", "decision_needed", "follow_up",
                     "question", "response", "scheduling"]
    requires_response: bool
    asks: list[ExtractedEmailAsk]  # with requester→target directionality
    people: list[ExtractedPerson]
    decisions_made: list[ExtractedDecision]
    commitments: list[ExtractedCommitment]
    topics: list[str]
    sentiment: Literal["positive", "neutral", "tense", "negative", "urgent"]
```

### 6d. Readiness Scoring

```python
class ReadinessScore(BaseModel):
    person_id: int
    score: int  # 0-100
    open_items: int       # action_items + email_asks + chat_asks
    blocking_count: int   # items where others wait on this person
    incoming_velocity: float  # new items last 7d vs completions
    workstream_count: int # active workstreams owned/led
    trend: Literal["up", "down", "flat"]  # 14d comparison

# Score formula (all components normalized 0-1 relative to peers):
# busyness = (open_items * 0.30 + blocking * 0.25
#           + velocity * 0.25 + workstreams * 0.20) * 100

# Scores are computed by the dashboard cache refresh job and stored 
# in dashboard_cache under key 'readiness_scores'. The readiness page
# reads from this cache, not from live aggregation queries.
# Caveat displayed in UI: "Scores reflect workload visible through
# your meetings, emails, and Teams activity."
```

### 6e. RAG Chat Retrieval

```
User question
  → Intent classification (structured query vs semantic search vs hybrid)
  → IF structured: direct PostgreSQL query, return results
  → IF semantic:
      1. Vector search (substantive + contextual items, excludes noise)
      2. Rank: similarity × 0.5 + recency × 0.2 + triage_weight × 0.3
      3. Top 10-15 → LLM reranks to 5-8
      4. Sonnet generates sourced answer with clickable citations
  → Conversation continuity via chat_sessions table
```

---

## 7. LLM Usage Strategy

| Task | Model | Frequency | Temp |
|------|-------|-----------|------|
| Triage (substantive/contextual/noise) | Haiku 4.5 | Every 30 min batch | 0 |
| Email noise classification | Haiku 4.5 | Per email batch | 0 |
| Meeting entity extraction | Haiku 4.5 | Per meeting | 0 |
| Email entity/ask extraction | Haiku 4.5 | Per substantive email | 0 |
| Teams chat extraction | Haiku 4.5 | Per chat/batch | 0 |
| Thread resolution analysis | Haiku 4.5 | Per updated thread | 0 |
| Email signature parsing | Haiku 4.5 | Once per new person | 0 |
| Entity resolution | Haiku 4.5 | Per ambiguous entity | 0 |
| Workstream assignment (Layer 2) | Haiku 4.5 | Every 30 min batch | 0 |
| Workstream clustering review (Layer 1) | Haiku 4.5 | Weekly batch | 0 |
| New person profile suggestion | Haiku 4.5 | Per new person | 0 |
| Morning/Monday/Friday briefing | Sonnet 4.6 | Per schedule | 0.3 |
| Meeting prep + talking points | Sonnet 4.6 | Per meeting | 0.3 |
| Monday objectives identification | Sonnet 4.6 | Weekly | 0.3 |
| Draft generation (nudges, recaps) | Sonnet 4.6 | On trigger | 0.3 |
| Response draft in user's voice | Sonnet 4.6 | On user request | 0.3 |
| RAG chat answers | Sonnet 4.6 | On user query | 0.3 |
| Voice profile generation | Sonnet 4.6 | Monthly / on demand | 0 |
| Embeddings | text-embedding-3-small | Per substantive+contextual item | — |

**Estimated monthly cost** (20 meetings/week, 200 emails/day, 200 Teams messages/day):
- Haiku (triage + extraction + assignment): ~$10-20/month
- Sonnet (briefings + drafts + chat): ~$8-18/month
- Embeddings: ~$3-5/month
- **Total: ~$20-45/month**

---

## 8. Build Phases

### Phase 0: Prerequisites & Setup (1 day)
- [ ] Run `scripts/setup_graph.py` (Azure app registration wizard)
- [ ] Grant all 10 delegated permissions (7 read + 3 write)
- [ ] Device code OAuth flow → token cache (chmod 600)
- [ ] Validate: fetch calendar events, emails, Teams membership
- [ ] Verify Screenpipe running: `curl http://localhost:3030/health`
- [ ] Verify PostgreSQL + pgvector: `docker ps | grep aegis-db` and `psql -h localhost -p 5434 -U postgres -d aegis -c "SELECT 1;"`
- [ ] Historical backfill (background job, runs once):
      - 90 days email (people + topics extraction only)
      - 60 days calendar → seed People table from attendees
      - Teams membership → seed teams/channels/memberships
      - 30 days Teams channel messages (lightweight extraction)
      - Voice profile: analyze 30-50 sent emails, store in voice_profile table
        (the voice_profile table must exist in the initial migration even though
        the full voice learning system isn't built until Phase 4)
- [ ] Optional: import CSV of people (name, email, dept, manager)

### Phase 1: Calendar + Screenpipe + Basic UI (2 weeks)
- [ ] Project scaffolding (pyproject.toml, config, directory structure)
- [ ] Create docker-compose.yml (Section 4a) and .env.example (Section 4b)
- [ ] Create .gitignore (.env, __pycache__, .venv/, *.pyc, bug_report.md)
- [ ] SQLAlchemy models + Alembic migrations for ALL tables
- [ ] All timestamps UTC (TIMESTAMPTZ), display in local tz
- [ ] GraphClient: calendar sync with filtering + recurring series linking
- [ ] Calendar polling loop (30 min, upsert by calendar_event_id)
- [ ] Keyword auto-exclusion for sensitive meetings
- [ ] ScreenpipeClient: async httpx wrapper
- [ ] Meeting Transcript Builder:
      - Back-to-back detection, padding truncation
      - Overlap handling via screen OCR
      - Overage detection (audio continues past scheduled end)
      - transcript_status: captured/partial/no_audio
- [ ] Unattributed conversation detector
- [ ] Screenpipe health monitor + macOS notification
- [ ] FastAPI app + Jinja2/HTMX/Tailwind templates
- [ ] Dashboard: today's meetings with status
- [ ] Meeting detail page with transcript
- [ ] Meeting exclusion toggle
- [ ] Midnight boundary handling (time-range queries, not date)

### Phase 2: Extraction + Org Bootstrap (2 weeks)
- [ ] Triage layer (Haiku batch every 30 min)
- [ ] LangGraph pipeline (classify → branch → extract → resolve → store)
- [ ] Meeting extraction with Pydantic structured output
- [ ] Extraction idempotency (temp=0, last_extracted_at, dedup)
- [ ] Entity resolution (rapidfuzz + LLM fallback)
- [ ] Seed People from calendar attendees + detect external
- [ ] New person onboarding: LLM profile suggestion → needs_review queue
- [ ] People history tracking (role/dept/manager changes)
- [ ] Embedding generation (text-embedding-3-small)
- [ ] pgvector HNSW indexes
- [ ] Org bootstrap: 1:1 calendar patterns, Teams membership → departments
- [ ] People page with needs-review queue (approve/correct/dismiss)
- [ ] Basic org chart page
- [ ] Action items page
- [ ] Workstream data model + manual creation
- [ ] Workstream detail page (timeline view)

### Phase 3: Email + Teams + Workstream Intelligence (2-3 weeks)
- [ ] Email poller + noise filter (human/automated/newsletter)
- [ ] Email preprocessing (strip signatures, quoted replies)
- [ ] Email extraction (intent + asks + directionality)
- [ ] Thread analysis (resolved vs still-pending asks)
- [ ] Teams poller + noise filter (system msgs, reactions, short msgs)
- [ ] Teams channel batch summarization (30-min windows)
- [ ] Chat extraction (same schema as email asks → chat_asks)
- [ ] Cross-reference People by email address
- [ ] Email-meeting correlation (participants + topics + temporal)
- [ ] Ask-to-action-item linking
- [ ] Workstream auto-detection (Layer 1: weekly clustering)
- [ ] Workstream assignment (Layer 2: 30-min Haiku batch)
- [ ] Workstream verification (Layer 3: new workstream creation)
- [ ] Workstream merge/split + post-split re-classification
- [ ] Workstream lifecycle (auto-quiet, auto-archive)
- [ ] Multi-workstream item membership
- [ ] Manual workstream assignment from any data point card
- [ ] Unassigned items queue
- [ ] Org inference: CC gravity, email signatures, department clustering
- [ ] Org inference: manager detection, responsibility mapping
- [ ] Request routing validation
- [ ] Email browser page with intent badges
- [ ] Pending asks page (email + Teams, inbound + outbound)
- [ ] Workstreams list page (filterable, sortable)
- [ ] Department health page
      (Phase 3: basic version with open items + overdue counts.
       Sentiment + friction detection added in Phase 4.)

### Phase 4: Intelligence + Response Workflow (2-3 weeks)
- [ ] APScheduler integration with FastAPI lifecycle
- [ ] Morning briefing (meetings + topics + action items + overnight)
- [ ] Monday brief (LLM-identified weekly objectives + carryover)
- [ ] Meeting prep (pre-generated with daily brief, notification at 15 min)
- [ ] "Next up" floating widget
- [ ] Friday recap (decisions + commitments + ask rates + sentiment)
- [ ] Briefings stored in briefings table for historical access
- [ ] Voice profile learning from sent emails + edit diffs
- [ ] Draft auto-generation (nudges for stale items, meeting recaps)
- [ ] Response workflow: type directive → generate in voice → review → send
- [ ] Channel-aware: email ask → email reply, Teams ask → Teams reply
- [ ] Drafts section on dashboard (send/edit/discard)
- [ ] Readiness page (busyness scoring, sortable table, expandable rows)
- [ ] Sentiment aggregation (per-interaction, per-relationship, per-dept)
- [ ] Friction detection (cross-department sentiment patterns)
- [ ] RAG chat: hybrid retrieval + triage-aware ranking + sourced answers
- [ ] Chat as floating widget accessible from any page
- [ ] Notification delivery: macOS + email-to-self + Teams-to-self
- [ ] All outputs toggleable per channel in admin
- [ ] Command center: all 6 zones wired up with live data
- [ ] Dashboard cache (15-min refresh, immediate on meeting processing)

### Phase 5: Polish + Hardening (1-2 weeks)
- [ ] Error handling for all external services
- [ ] Retry logic with exponential backoff + jitter
- [ ] Graph API rate limit handling (Retry-After headers)
- [ ] Token security (macOS Keychain or chmod 600)
- [ ] PII-safe logging (never log raw content)
- [ ] Database connection pooling tuning
- [ ] Daily pg_dump backup (LaunchAgent, 30-day rotation)
- [ ] Data retention tiers (hot 90d / warm 365d / cold 365d+)
- [ ] Search page (semantic + keyword hybrid)
- [ ] Admin settings page (~70 values, collapsible sections)
- [ ] Voice profile: custom rules, regenerate on demand, monthly auto-update
- [ ] Startup script (`aegis start` / `aegis stop`)
- [ ] LaunchAgent plist for auto-start on macOS boot
- [ ] Logging with rotation
- [ ] Mobile-responsive layout for all pages (prep for Tailscale)

### Phase 6: Future Enhancements (not in initial build)
- [ ] Tailscale integration for remote/mobile access
- [ ] Graph API write Tier 2: auto-create calendar blocks for deadlines
- [ ] Graph API write Tier 3: autonomous stale-item nudges
- [ ] Multi-Screenpipe instance support (multiple machines)
- [ ] LLM-assisted workstream split (Option B)
- [ ] Meeting overage pattern detection ("standup consistently runs over")
- [ ] Workstream dependency graph visualization

---

## 9. Claude Code Instructions — Multi-Agent Orchestration

### 9a. Agent Architecture

Each phase runs as a **sprint** with three agent roles operating in sequence:

```
┌─────────────────────────────────────────────────────────┐
│                    PHASE SPRINT                          │
│                                                          │
│  Step 1: BUILD (parallel agents per track)               │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │
│  │ Agent A  │ │ Agent B  │ │ Agent C  │ │ Agent D  │   │
│  │ Track 1  │ │ Track 2  │ │ Track 3  │ │ Track 4  │   │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘   │
│       └─────────────┴─────────────┴─────────────┘        │
│                         │                                │
│                         ▼                                │
│  Step 2: REVIEW (single agent, full codebase scan)       │
│  ┌──────────────────────────────────────────────┐        │
│  │ Review Agent                                  │        │
│  │ • Type checking + import validation           │        │
│  │ • Schema consistency (models ↔ migrations)    │        │
│  │ • Cross-agent integration (do modules connect)│        │
│  │ • Test execution                              │        │
│  │ • Produces: bug_report.md                     │        │
│  └────────────────────┬─────────────────────────┘        │
│                       │                                  │
│                       ▼                                  │
│  Step 3: REPAIR (targeted agents per bug)                │
│  ┌──────────────────────────────────────────────┐        │
│  │ Repair Agent                                  │        │
│  │ • Reads bug_report.md                         │        │
│  │ • Fixes each issue                            │        │
│  │ • Re-runs failed tests                        │        │
│  │ • Loops until clean                           │        │
│  └──────────────────────────────────────────────┘        │
│                       │                                  │
│                       ▼                                  │
│  Step 4: HUMAN REVIEW (checkpoint — see build plan)      │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### 9b. Agent Role Definitions

**Builder Agents** (parallel, one per track):
- Each agent owns a specific track of work within a phase
- Agents work in parallel on separate files/modules
- Each agent must write tests for its own code
- Agents should not modify files owned by another agent's track
- Shared files (config.py, models.py, base templates) must be modified by only ONE designated agent per phase — the others import from it

**Review Agent** (sequential, runs after all builders complete):
- Performs a comprehensive codebase review covering:
  1. **Import validation**: every `from aegis.x import y` resolves to a real module/class
  2. **Schema consistency**: SQLAlchemy models match Alembic migrations, Pydantic schemas match DB models
  3. **Interface contracts**: function signatures match between callers and callees across modules
  4. **Async consistency**: no sync calls inside async functions, no missing `await`
  5. **Config completeness**: every value referenced in code exists in config.py with a default
  6. **Template validation**: every route renders a template that exists, every HTMX endpoint exists
  7. **Test execution**: run `pytest` and capture all failures
  8. **Dead code**: imports that aren't used, functions that aren't called
  9. **Security**: no hardcoded secrets, no PII logging, token cache permissions
  10. **Type checking**: run `pyright` or `mypy` if available, flag type errors
- Produces `bug_report.md` in the project root with:
  - Severity (critical / warning / style)
  - File path and line number
  - Description of the issue
  - Suggested fix

**Repair Agent** (sequential, runs after review):
- Reads `bug_report.md`
- Fixes all critical issues first, then warnings
- Style issues are noted but not fixed unless trivial
- After fixing, re-runs the specific failed tests
- If new failures are introduced by fixes, repair those too
- Loops until: all critical issues resolved, all tests pass
- Updates `bug_report.md` with resolution status
- Maximum 3 repair cycles — if issues persist after 3 cycles, escalate to human review with a summary of what's stuck

### 9c. Phase-by-Phase Agent Assignments

#### Phase 0: Setup (sequential — no parallelism needed)
```
Single agent:
  → setup_graph.py wizard
  → backfill.py script  
  → seed_org.py script
  → .env.example generation

Review agent: verify scripts run without errors
Repair agent: fix any issues
Human checkpoint: run setup wizard, verify connections
```

#### Phase 1: Calendar + Screenpipe + Basic UI
```
Agent A — Data Layer (runs first, others depend on it):
  → docker-compose.yml (Section 4a)
  → .env.example (Section 4b)
  → .gitignore
  → config.py (pydantic-settings, all config values from .env.example)
  → db/engine.py (async engine + session factory)
  → db/models.py (ALL SQLAlchemy models for ALL tables)
  → Alembic initial migration
  → db/repositories.py (base query patterns)

  ⏸ SYNC POINT: Agent A must complete before B, C, D start.
     Other agents import from models.py and config.py.

Agent B — Calendar Sync (after A):
  → ingestion/graph_client.py (all Graph API methods)
  → ingestion/calendar_sync.py (filtering, recurring, exclusion)
  → ingestion/poller.py (orchestrator)
  → tests/test_graph_client.py

Agent C — Screenpipe Integration (after A):
  → ingestion/screenpipe.py (async client)
  → ingestion/meeting_detector.py (transcript builder, overlap, overage)
  → notifications/macos.py (health alerts)
  → tests/test_screenpipe.py

Agent D — Web UI Foundation (after A):
  → main.py (FastAPI app + startup hooks)
  → web/templates/base.html (sidebar nav, layout)
  → web/routes/dashboard.py (basic meeting list)
  → web/routes/meetings.py (meeting detail + transcript)
  → All template stubs for future pages

Review agent checks:
  • docker-compose.yml matches Section 4a exactly (port 5434, pgvector/pgvector:pg16)
  • .env.example contains ALL config values from Section 4b
  • config.py has a pydantic-settings field for every .env.example variable with correct defaults
  • .gitignore includes .env, __pycache__, .venv/, *.pyc
  • GraphClient methods match the Graph API permissions in the spec
  • Calendar sync filtering rules match the spec exactly
  • Meeting detector handles back-to-back (test with adjacent events)
  • SQLAlchemy models match the SQL schema in Section 3 exactly
  • Alembic migration produces the same schema as the SQL in the spec
  • DATABASE_URL in config.py defaults to postgresql+asyncpg://postgres@localhost:5434/aegis
  • FastAPI app starts without errors
  • Templates render without Jinja2 errors
  • All imports resolve across all modules

Repair agent: fix issues, re-run tests
Human checkpoint: see Phase 1 checks in build plan
```

#### Phase 2: Extraction + Org Bootstrap
```
Agent A — Triage + Pipeline (shared foundation):
  → processing/triage.py
  → processing/pipeline.py (LangGraph definition)
  → processing/embeddings.py
  → tests/test_triage.py, test_pipeline.py

  ⏸ SYNC POINT: Pipeline must exist before extractors.

Agent B — Meeting Extraction (after A):
  → processing/meeting_extractor.py (prompts + schemas)
  → processing/resolver.py (entity resolution)
  → tests/test_extractor.py, test_resolver.py

Agent C — Org + People (after A):
  → processing/org_inference.py (bootstrap only: 1:1 patterns, Teams)
  → intelligence/readiness.py (scoring formula stub — full UI in Phase 4)
  → web/routes/people.py + templates (needs-review queue)
  → web/routes/org_chart.py + template

Agent D — Workstream Foundation (after A):
  → All workstream CRUD logic in db/repositories.py
  → web/routes/workstreams.py + templates (list + detail + timeline)
  → web/routes/actions.py + template
  → Manual item assignment UI components

Review agent checks:
  • Triage correctly routes: substantive → extraction, contextual → embedding only, noise → skip
  • Extraction schemas match Section 6 Pydantic contracts exactly
  • Entity resolution correctly matches against People table
  • Workstream_items UNIQUE constraint allows multi-membership
  • Pipeline processes a meeting end-to-end: transcript → triage → extract → resolve → store
  • No extraction duplicates on re-processing same meeting (idempotency)
  • People needs-review queue shows LLM suggestions

Repair agent: fix issues, re-run tests
Human checkpoint: see Phase 2 checks in build plan
```

#### Phase 3: Email + Teams + Workstream Intelligence
```
Agent A — Email Ingestion:
  → ingestion/email_poller.py
  → processing/email_extractor.py
  → processing/thread_analyzer.py
  → web/routes/emails.py + template
  → web/routes/asks.py + template

Agent B — Teams Ingestion:
  → ingestion/teams_poller.py
  → processing/chat_extractor.py
  → (extends thread_analyzer for chat threads)

Agent C — Workstream Intelligence:
  → processing/workstream_detector.py (all 3 layers)
  → Merge/split logic + re-classification trigger
  → Lifecycle management (auto-quiet, auto-archive)
  → Workstreams list page updates
  → Unassigned items queue

Agent D — Org Inference:
  → processing/org_inference.py (full: CC gravity, signatures, clustering)
  → web/routes/departments.py + template (basic version)
  → Org chart page updates with inferred data

Review agent checks:
  • Email poller → noise filter → triage → extraction pipeline flows correctly
  • Teams poller → noise filter → triage → extraction pipeline flows correctly
  • Channel messages batch into 30-min windows (not per-message)
  • Email asks have correct requester→target directionality
  • Thread analyzer marks resolved asks
  • Workstream Layer 1 respects org chart partition constraint
  • Workstream Layer 2 assignment uses embedding pre-filter before LLM
  • Merge/split triggers re-classification
  • Multi-workstream membership works (same item, two workstreams)
  • Triage scores stored on emails and chat_messages
  • All three pollers (calendar, email, Teams) can run concurrently without conflicts

Repair agent: fix issues, re-run tests
Human checkpoint: see Phase 3 checks in build plan
```

#### Phase 4: Intelligence + Response Workflow
```
Agent A — Briefings + Scheduler:
  → intelligence/briefings.py (morning, Monday, Friday)
  → intelligence/meeting_prep.py
  → intelligence/scheduler.py (APScheduler jobs)
  → Briefings stored in briefings table
  → web/routes for briefing views

Agent B — Voice + Drafts + Response:
  → intelligence/voice_profile.py
  → intelligence/draft_generator.py
  → web/routes/respond.py (response workflow)
  → Draft section on dashboard
  → Mail.Send + ChatMessage.Send integration

Agent C — Readiness + Sentiment:
  → intelligence/readiness.py (full scoring + caching)
  → intelligence/sentiment.py (aggregation + friction)
  → web/routes/readiness.py + template
  → Department health page updates (add sentiment)
  → Sentiment on workstream cards

Agent D — RAG Chat + Dashboard:
  → chat/rag.py (intent classify → search → rerank → answer)
  → web/routes/chat.py + template + floating widget
  → Dashboard command center (all 6 zones with live data)
  → Dashboard cache logic (15-min refresh job)
  → "Next up" floating widget
  → Notification delivery (macOS + email + Teams toggles)

Review agent checks:
  • Morning briefing includes per-meeting suggested topics
  • Monday brief identifies objectives (test with real workstream data)
  • Meeting prep is pre-computed (stored in briefings table, not generated on request)
  • Voice profile produces drafts that match auto_profile style
  • Response workflow: directive → draft → send updates item status
  • Channel-aware routing (email ask → email, Teams ask → Teams)
  • Readiness scores cached in dashboard_cache, not computed on page load
  • Sentiment aggregations table populated by batch job
  • RAG chat: structured queries hit SQL directly, semantic queries use pgvector
  • RAG ranking uses triage_weight (substantive > contextual > noise)
  • Dashboard cache refreshes on meeting processing (not just 15-min timer)
  • All notification channels fire correctly when enabled
  • All admin toggles for outputs + channels work

Repair agent: fix issues, re-run tests
Human checkpoint: see Phase 4 checks in build plan
```

#### Phase 5: Polish + Hardening
```
Agent A — Reliability + Security:
  → Error handling for all external services
  → Retry logic (exponential backoff + jitter)
  → Graph API rate limit handling
  → Token security
  → PII-safe logging
  → DB connection pooling

Agent B — Admin + Operations:
  → web/routes/admin.py + template (~70 settings)
  → admin_settings table read/write
  → Startup script (aegis start/stop)
  → LaunchAgent plist
  → Backup script (daily pg_dump)
  → Data retention logic

Agent C — Search + Voice Management:
  → web/routes/search.py + template (hybrid search)
  → Voice profile management UI in admin
  → Mobile-responsive audit for all pages
  → Logging with rotation

Review agent checks:
  • Aegis survives: Screenpipe restart, OAuth token refresh, Graph API 429
  • admin_settings values override .env at runtime
  • Backup produces valid pg_dump file
  • No PII in log output (grep logs for email bodies)
  • All pages render at 375px viewport width
  • Search returns results across meetings, emails, and chat messages
  • LaunchAgent auto-starts Aegis on boot

Repair agent: fix issues, re-run tests
Human checkpoint: see Phase 5 checks in build plan
```

### 9d. Orchestration Rules

1. **Sync points are mandatory.** When a phase has a sync point (marked ⏸), NO agents proceed past it until the designated agent completes. The sync point agent builds shared infrastructure (models, config, pipeline) that other agents import from.

2. **File ownership is strict.** Each agent in a phase owns specific files. If Agent B needs a function from Agent A's file, Agent B adds a TODO comment and the Review Agent catches missing integrations. Agents never edit each other's files.

3. **Shared state files.** `db/models.py`, `config.py`, and `main.py` are single-owner per phase. The designated owner for these files is always Agent A (Data Layer / Foundation). Other agents import from these files but never modify them.

4. **Tests are not optional.** Every builder agent writes tests for its own module. The Review Agent runs the full test suite. The Repair Agent's primary signal is test failures.

5. **The Review Agent is adversarial.** It doesn't just check if the code runs — it checks if the code matches the spec. If the spec says "skip all-day events" and the code doesn't filter `isAllDay`, that's a critical bug even if tests pass.

6. **The Repair Agent is conservative.** It fixes the bug, not the architecture. If a repair requires rethinking a module's design, the Repair Agent flags it for human review instead of rewriting.

7. **Three-cycle limit.** Build → Review → Repair is one cycle. If issues persist after 3 cycles, stop and surface to human review. This prevents infinite loops.

8. **bug_report.md format:**
```markdown
## Phase X Review — [timestamp]

### Critical (must fix before proceeding)
- [ ] **[FILE:LINE]** Description. Suggested fix.

### Warning (should fix, won't break things immediately)  
- [ ] **[FILE:LINE]** Description. Suggested fix.

### Style (note for future, don't fix now)
- [ ] **[FILE:LINE]** Description.

### Test Results
- Passed: X
- Failed: Y
- Errors: Z
- Failed test details: [list]
```

### 9e. Architectural Rules (All Agents Must Follow)

1. **Calendar events are the source of truth for meetings.** Never detect meetings from audio alone.
2. **Filter calendar noise aggressively.** Skip all-day, cancelled, declined, solo, OOO, focus time.
3. **Handle back-to-back meetings.** Truncate padding, midpoints for boundaries, screen OCR for overlaps.
4. **All timestamps UTC.** Store as TIMESTAMPTZ, convert on ingestion, display in local tz.
5. **Triage before extraction.** Substantive → extract + embed. Contextual → embed only. Noise → skip.
6. **Pre-filter noise.** Rule-based first (free), then LLM triage (cheap batch call).
7. **Extraction must be idempotent.** temp=0, last_extracted_at, dedup by source_id + embedding similarity.
8. **Workstreams replace projects.** There is no projects table.
9. **Items can belong to multiple workstreams.** UNIQUE on (workstream_id, item_type, item_id).
10. **Use async everywhere.** FastAPI, httpx, asyncpg, SQLAlchemy async.
11. **Pydantic models for all data contracts.** Between ingestion, processing, and storage.
12. **Server-rendered HTML with HTMX.** FastAPI + Jinja2 + HTMX + Tailwind CDN + Alpine.js.
13. **Mobile-responsive from day one.** Tailwind responsive breakpoints.
14. **GraphClient uses delegated permissions.** PublicClientApplication, device code flow, `/me/` endpoint.
15. **Never log PII.** Metadata only. Structured logging with PII-safe formatter.
16. **Alembic for all schema changes.** Never raw SQL for migrations.
17. **Use `uv` if available**, otherwise `pip` with `pyproject.toml`.
18. **Test with real Screenpipe data in Phase 1.**
19. **Seed People from calendar attendees early.**
20. **Meeting transcripts bypass triage** — they always go to full extraction.
21. **Do not modify CLAUDE.md.** It is a reference document. If the spec needs updating, the human does that.
22. **Update system_health table** after every poller cycle and LLM call. Update llm_usage table after every LLM call. These power the health status indicators and cost tracking.
23. **Attachments are metadata only.** Store filename, content_type, size_bytes. Do NOT download file content. Include filenames in extraction prompts. Skip inline images.
24. **On startup, reset stuck items.** Any meetings/emails/chat_messages with `processing_status = 'processing'` get reset to `'pending'` and re-queued.
25. **All Graph API list calls must paginate.** Follow `@odata.nextLink` until exhausted. Never assume a single page contains all results. Pace at 100ms between pages. Respect `Retry-After` on 429s.

### 9f. Testing Strategy

**Never make real API calls in tests.** Mock all external services:

- **Screenpipe**: Fixture JSON files in `tests/fixtures/`. Patch `httpx.AsyncClient.get` to return fixture data. Include fixtures for audio responses, health checks, and empty results.
- **Graph API**: Fixture JSON files for each endpoint. Include edge cases: paginated responses with `@odata.nextLink`, 429 rate-limit responses, empty result sets, `isAllDay=true` events.
- **Anthropic (Haiku/Sonnet)**: Mock at the function level, NOT HTTP level. Use canned Pydantic model instances (TriageResult, MeetingExtraction, EmailExtraction, etc.) in `tests/conftest.py`.
- **OpenAI (embeddings)**: Mock to return a deterministic 1536-dimension vector. Don't test embedding quality — test that embeddings get stored and pgvector queries work.
- **PostgreSQL**: Use a REAL test database (`aegis_test`) on the same Docker instance (port 5434). Create it in the test session fixture, run Alembic migrations, tear down after. Do NOT mock the database — real SQL catches real bugs.

**Fixture directory:**
```
tests/
├── conftest.py                   # shared fixtures, DB setup, mock factories
├── fixtures/
│   ├── screenpipe_audio.json
│   ├── screenpipe_health.json
│   ├── graph_calendar_events.json
│   ├── graph_calendar_recurring.json
│   ├── graph_emails_page1.json
│   ├── graph_emails_page2.json   # with @odata.nextLink
│   ├── graph_emails_noise.json   # newsletters, automated
│   ├── graph_teams_chats.json
│   ├── graph_teams_channels.json
│   ├── graph_teams_messages.json
│   └── graph_rate_limit_429.json
├── test_screenpipe.py
├── test_graph_client.py
├── ...
```

Every builder agent writes tests for its own module. The Review Agent runs the full test suite (`pytest`). The Repair Agent's primary signal is test failures.

---

## 10. Risk Mitigations

| Risk | Mitigation |
|------|-----------|
| Screenpipe not running | Health check + periodic ping + macOS notification. Detect capture gaps. Mark transcript_status=partial. |
| Screenpipe crashes mid-meeting | Gap detection in audio chunks. Mark partial. Alert user. Process what was captured. |
| No audio for a meeting | transcript_status=no_audio. Still track attendees/title/duration for relationship value. |
| Back-to-back overlap | Detect adjacent events, truncate padding, midpoint boundary. Screen OCR for double-booked. |
| Calendar noise | Pre-filter: skip all-day, cancelled, declined, solo, OOO, focus. Keyword exclusion list. |
| Meeting runs over | Overage detection: continuous audio past scheduled end → auto-extend transcript window. |
| Email noise (newsletters, automation) | Rule-based filter → LLM classification. Only extract from human emails. |
| Teams volume overwhelms extraction | Pre-filter noise. Batch channel messages. Triage before extraction. |
| LLM extraction hallucinations | temp=0. Validate against KB. Dedup by source+similarity. Merge, never duplicate. |
| Extraction not idempotent | temp=0, last_extracted_at tracking, source_id dedup. |
| Person changes roles | people_history audit trail. Org inference detects from new patterns. |
| Same person, multiple emails | aliases[] field. Entity resolution suggests merge. |
| External participants | is_external flag. No org chart position. Still in relationship graphs. |
| Sensitive meetings | Keyword auto-exclusion + manual exclude toggle. No transcript extraction. |
| OAuth token expiry | MSAL auto-refresh. Alert on auth failure. Token cache chmod 600. |
| Graph API rate limits | Exponential backoff + jitter. Retry-After headers. Spread polling. |
| PostgreSQL down | Health check on startup. Docker container has `restart: unless-stopped`. Manual recovery: `docker compose up -d`. |
| Cold start (empty system) | Historical backfill: 90d email + 60d calendar + 30d Teams. CSV import option. |
| Database grows unbounded | Retention tiers: hot/warm/cold. Daily pg_dump backup, 30-day rotation. |
| Timezone mismatches | ALL timestamps UTC. Convert on ingestion. Display in local tz. |
| Duplicate meetings | Upsert by calendar_event_id. |
| LLM costs spike | Triage layer prevents unnecessary extraction. Haiku for all batch work. Sonnet only for user-facing. |
| PII in logs | Never log raw content. Metadata only. |
| Workstream auto-detection too aggressive | Conservative defaults: 3+ items, 2+ sources, >0.7 confidence. Low-confidence = suggested, not auto-created. |
| Sentiment false positives | Aggregate over 30d windows. Surface patterns, not individual moments. Dismissible. |
| Voice profile inaccurate | User editable. Custom rules override. Monthly re-learning. Edit diff tracking. |
| Readiness score misleading | Caveat in UI: "reflects workload visible to you." Relative scoring vs peers. |

---

## 11. Review Findings — Gaps Fixed in This Spec

During the final review, the following disconnects were identified and resolved:

**1. Missing admin_settings table.** The admin page needs to write ~70 configurable values at runtime, but there was no database table for this. All settings were in `.env` which can't be edited from the web UI. **Fix**: Added `admin_settings` table (key/value with JSONB). On startup, admin_settings values override .env defaults.

**2. Missing sentiment_aggregations table.** Department health, workstream sentiment, and friction detection all require pre-computed sentiment scores across time windows. Individual items have a `sentiment` column, but there was no table for storing the aggregated per-relationship and per-department scores. **Fix**: Added `sentiment_aggregations` table with scope_type (person, relationship, department, cross_department, workstream) and rolling window computation.

**3. Pipeline ordering ambiguity.** Triage, extraction, and workstream assignment all run "every 30 minutes" but the spec didn't clarify whether they're sequential or parallel. **Fix**: Added "Pipeline Flow Clarification" section specifying the strict ordering: polling → noise filter → triage → extraction → workstream assignment. Also clarified that meeting transcripts bypass triage (always substantive).

**4. Email noise → triage handoff unclear.** The email poller classifies as human/automated/newsletter (rule-based), then the triage layer also classifies items. Without clarification, Claude Code might double-classify emails or skip the triage step for emails. **Fix**: Added clarification that email_class is the FIRST filter (rule-based, no LLM) and triage_class is the SECOND filter (LLM batch, applied only to human-classified emails).

**5. Phase 0 backfill creates voice profile, but voice system built in Phase 4.** The backfill generates an initial voice profile from sent emails, but the voice_profile table and the learning/editing system aren't built until Phase 4. **Fix**: Added note that the voice_profile table must exist in the initial Alembic migration, and the backfill creates the initial record. Phase 4 adds the full management UI and edit diff tracking.

**6. Phase 3 department health page vs Phase 4 sentiment.** Phase 3 builds the department health page, but sentiment aggregation and friction detection aren't added until Phase 4. **Fix**: Added note that Phase 3 builds basic department health (open items, overdue counts) and Phase 4 adds sentiment bars and friction indicators.

**7. Dashboard cache keys unspecified.** The dashboard_cache table existed but the actual cache keys weren't defined. **Fix**: Added explicit key list: workstream_cards, pending_decisions, awaiting_response, stale_items, todays_meetings, drafts_pending, readiness_scores, department_health.

**8. Readiness scores have no storage location.** The readiness scoring formula was defined but there was no mention of where scores are stored or how the page reads them. **Fix**: Added note that readiness scores are computed by the dashboard cache refresh job and stored in dashboard_cache.

**9. SQL table creation order.** The schema lists tables organized by domain, but some tables reference others that are defined later (e.g., people references departments). **Fix**: Added explicit Alembic-friendly creation order note.

**10. Contextual items and embeddings.** The triage layer was described in the architecture but the spec didn't explicitly state that contextual items get embeddings. **Fix**: Added clarification in Pipeline Flow section.

### Remaining Notes for Claude Code

- **The GraphClient implementation** (all methods for calendar, email, Teams read/write) is described by contract and permissions. Claude Code should implement it following the Graph API patterns in the Microsoft documentation.
- **The setup wizard script** (`setup_graph.py`) should follow the interactive flow described in the architecture — guide user through Azure Portal, collect IDs, trigger device code flow, validate access, set file permissions.
- **The .env.example** file should list all environment variables from the prerequisites document.
- **All admin settings categories** (Connections, Polling, Triage, Workstream Detection, Meeting Processing, Intelligence Schedule, Notifications, Communication/Voice, Org Chart, Sentiment, Data Retention, LLM Config, System) should be represented as collapsible sections in the admin page, each reading from and writing to the admin_settings table.
