# Aegis Codebase Summary for Helios Integration

> This document describes the current state of the Aegis codebase to inform the design of Helios (Screenpipe replacement). Written for an AI agent that needs to understand Aegis's architecture, data contracts, and integration points.

---

## 1. Repository Layout

**Structure**: Single repo, single Python package. NOT a monorepo.

```
aegis/                          # Project root (git repo)
├── aegis/                      # Python package
│   ├── __init__.py
│   ├── main.py                 # FastAPI app + lifespan + scheduler
│   ├── config.py               # Pydantic-settings (~70 config values)
│   ├── db/
│   │   ├── models.py           # 34 SQLAlchemy ORM tables
│   │   ├── engine.py           # AsyncPG connection pool
│   │   ├── repositories.py     # Data access layer
│   │   └── admin_config.py     # Runtime settings override
│   ├── ingestion/
│   │   ├── screenpipe.py       # Screenpipe REST client (GET /health, /search)
│   │   ├── meeting_detector.py # Transcript builder, back-to-back, overage
│   │   ├── calendar_sync.py    # Microsoft Graph calendar polling
│   │   ├── email_poller.py     # Email ingestion + noise classification
│   │   ├── teams_poller.py     # Teams chat/channel ingestion
│   │   ├── graph_client.py     # Microsoft Graph API client (MSAL auth)
│   │   └── poller.py           # Polling orchestrator (asyncio loops)
│   ├── processing/
│   │   ├── pipeline.py         # LangGraph: classify → extract → resolve → store
│   │   ├── meeting_extractor.py
│   │   ├── email_extractor.py
│   │   ├── chat_extractor.py
│   │   ├── triage.py           # Haiku batch classification
│   │   ├── resolver.py         # Entity resolution (fuzzy + LLM)
│   │   ├── embeddings.py       # OpenAI text-embedding-3-small
│   │   ├── workstream_detector.py  # 3-layer auto-detection
│   │   └── org_inference.py    # Org chart from calendar/Teams patterns
│   ├── intelligence/
│   │   ├── briefings.py        # Morning/Monday/Friday generators
│   │   ├── meeting_prep.py     # Pre-meeting context + talking points
│   │   ├── scheduler.py        # APScheduler job registration
│   │   ├── voice_profile.py    # Voice learning + draft generation
│   │   ├── draft_generator.py  # Auto-nudges, recaps
│   │   ├── readiness.py        # Workload scoring
│   │   └── sentiment.py        # Per-person/dept/workstream sentiment
│   ├── chat/
│   │   └── rag.py              # RAG: intent classify → search → answer
│   ├── web/
│   │   ├── routes/             # 14 FastAPI route files
│   │   └── templates/          # Jinja2 + HTMX + Tailwind
│   └── notifications/
│       └── macos.py            # osascript notifications
├── scripts/                    # CLI tools (seed, verify, backup, aegis_ctl)
├── tests/                      # 133 tests
├── alembic/                    # Database migrations
├── docs/                       # Specs, checklists, prompts
├── pyproject.toml              # Python 3.13+, all dependencies
├── docker-compose.yml          # PostgreSQL + pgvector on port 5434
└── CLAUDE.md                   # Full project specification
```

**Helios placement**: Helios would be a **sibling directory** at the same level as `aegis/`, or a separate repo entirely. No monorepo restructure needed. Helios just needs to serve the same REST API that `aegis/ingestion/screenpipe.py` expects at `localhost:3030`.

---

## 2. Does Aegis Expose an HTTP API?

**Mostly HTML, with a few JSON endpoints.**

Aegis is a **server-rendered web app** using FastAPI + Jinja2 + HTMX. Most routes return HTML templates, not JSON.

**JSON-returning endpoints** (the few that exist):
- `GET /api/meetings-today` — HTMX partial (HTML fragment, not JSON)
- `POST /api/drafts/{id}/send` — returns `{"status": "sent"}`
- `POST /api/drafts/{id}/discard` — returns `{"status": "discarded"}`
- Dashboard cache internals use JSONB but serve HTML

**No `/api/` prefix convention** — all routes are at root level (`/meetings`, `/asks`, `/search`, etc.)

**For Helios**: Aegis does NOT need a REST API from Helios. Aegis calls Helios (the Screenpipe replacement) — not the other way around. The integration point is `aegis/ingestion/screenpipe.py` which makes HTTP calls TO the capture service.

---

## 3. Meetings Table — Transcript Behavior

**Yes, meetings are created BEFORE transcription.**

The flow:

1. **Calendar sync** (every 30 min) fetches events from Microsoft Graph
2. **Meeting row created immediately** via `upsert_meeting()` with:
   - `transcript_status = "pending"` (default)
   - `transcript_text = NULL`
   - `processing_status = "pending"`
   - All calendar metadata (title, attendees, start/end, organizer, recurring series)

3. **After meeting ends** (`end_time < now`), `MeetingDetector.process_completed_meetings()` runs:
   - Finds meetings with `transcript_status = "pending"` and `is_excluded = False`
   - Queries Screenpipe for audio in the meeting's time window (±5 min buffer)
   - **If audio found**: stitches transcript, sets `transcript_status = "captured"` or `"partial"`
   - **If no audio**: sets `transcript_status = "no_audio"`

4. **Extraction pipeline** only processes meetings with `transcript_text IS NOT NULL`:
   ```python
   # pipeline.py line 157-159
   if not meeting.transcript_text:
       logger.info("Meeting %d has no transcript, skipping", meeting_id)
       return False
   ```

**Meetings with `no_audio` remain in the database** — they still provide:
- Calendar metadata (who was in the meeting, when, how long)
- Attendee relationship data (for org inference, 1:1 detection)
- Recurring series tracking
- They just don't get: action items, decisions, commitments, topics, sentiment, or embeddings

**Key columns on the `meetings` table**:
```
id, title, start_time, end_time, duration, status,
transcript_status (pending|captured|partial|no_audio|processing|failed),
transcript_text (nullable TEXT),
processing_status (pending|processing|completed|failed),
calendar_event_id (unique, for upsert),
recurring_series_id, organizer_email, online_meeting_url,
summary, sentiment, embedding(1536), screen_context(JSONB)
```

---

## 4. Screenpipe Integration — What Was Built and What Helios Replaces

### Fully built and operational:

#### `aegis/ingestion/screenpipe.py` — REST Client (91 lines)

```python
class ScreenpipeClient:
    base_url = settings.screenpipe_url  # default: http://localhost:3030

    async def health_check() -> bool
        # GET /health → returns True if 200

    async def get_audio(start: datetime, end: datetime) -> list[dict]
        # GET /search?content_type=audio&start_time=...&end_time=...&limit=1000
        # Returns: [{"content": {"text": "...", "timestamp": "...", "speaker": {"name": "..."}}}]

    async def get_screen_ocr(start: datetime, end: datetime) -> list[dict]
        # GET /search?content_type=ocr&start_time=...&end_time=...&limit=1000
        # Returns: [{"content": {"text": "...", "app_name": "...", "timestamp": "..."}}]
```

**This is the exact API contract Helios must implement.** The URLs, parameters, and response shapes are what Aegis expects.

#### `aegis/ingestion/meeting_detector.py` — Transcript Builder (346 lines)

Fully built with sophisticated logic:

| Feature | Implementation | Helios Impact |
|---------|---------------|---------------|
| **Back-to-back detection** | Finds adjacent meetings within 5 min gap, uses midpoint as boundary | Keep as-is — calendar logic, independent of capture |
| **Buffer padding** | ±5 min around meeting start/end for Screenpipe queries | Keep as-is — just widens the time window |
| **Overage detection** | If audio continues past `end_time`, extends window up to 30 min | Keep as-is — queries Helios for extended window |
| **Transcript stitching** | Concatenates audio chunks with `[Speaker]: text` format | **Replace if Helios provides pre-stitched transcripts** |
| **Status determination** | Calculates coverage: `captured` (≥50%), `partial` (<50%), `no_audio` (0%) | Keep as-is — logic applies regardless of source |
| **Unattributed audio** | Scans for multi-speaker audio outside calendar windows | Keep as-is — detection logic, queries same API |
| **Screen OCR for overlaps** | When meetings overlap, uses OCR to detect active meeting app | **Replace if Helios has better active-window detection** |

#### `aegis/config.py` — Screenpipe Settings

```python
screenpipe_url: str = "http://localhost:3030"
polling_screenpipe_seconds: int = 300  # Defined but NOT actively used in polling loop
```

Note: `polling_screenpipe_seconds` is defined in config but there's no dedicated Screenpipe polling loop in `poller.py`. Transcript building is triggered on-demand after meetings complete, not on a timer.

#### `aegis/notifications/macos.py` — Health Alerts

Generic macOS notification system — not Screenpipe-specific. Used for:
- Meeting prep notifications (15 min before)
- Morning briefing alerts
- Could be used for Screenpipe/Helios health alerts

### What was NOT built / is a stub:

- **No dedicated Screenpipe health monitoring loop** — `polling_screenpipe_seconds` config exists but no polling loop uses it
- **No automatic retry on Screenpipe connection failure** — `screenpipe.py` returns empty lists on error, doesn't retry
- **No gap detection** — if Screenpipe is down during a meeting, the meeting gets `no_audio` permanently (no re-check later)
- **No Screenpipe status in system_health table** — unlike email_poller/teams_poller/calendar_sync, Screenpipe doesn't report heartbeats

### Recommended Helios integration changes:

1. **API compatibility**: Helios should serve `GET /health` and `GET /search` at the same URL with the same response format
2. **Or**: Create a `HeliosClient` that replaces `ScreenpipeClient` with a richer API (push-based transcripts, real-time streaming, webhook notifications)
3. **Transcript stitching**: If Helios provides complete transcripts with speaker diarization, `meeting_detector.py`'s `_stitch_transcript()` becomes unnecessary
4. **Health monitoring**: Add Helios to `system_health` table and the polling loop
5. **Gap recovery**: Add logic to re-check `no_audio` meetings when Helios comes back online

---

## 5. Tech Stack Summary

| Component | Technology |
|-----------|-----------|
| Language | Python 3.13+ |
| Web framework | FastAPI + Jinja2 + HTMX + Tailwind CSS |
| Database | PostgreSQL 16 + pgvector (port 5434, Docker) |
| ORM | SQLAlchemy 2.0 (async with asyncpg) |
| Migrations | Alembic |
| Auth | MSAL (Microsoft Graph OAuth, device code flow) |
| LLM | Claude Haiku 4.5 (extraction, triage, briefings) |
| Embeddings | OpenAI text-embedding-3-small (1536 dimensions) |
| Pipeline | LangGraph (state graph) |
| Scheduler | APScheduler (AsyncIOScheduler) |
| Fuzzy matching | rapidfuzz |
| Config | pydantic-settings (.env + admin_settings DB override) |
| Deployment | Single-user, macOS, localhost:8000 |

---

## 6. Data Flow Summary

```
Calendar (Graph API)  ──→  meetings table (status=pending)
                              │
Screenpipe / Helios   ──→  Audio chunks after meeting ends
                              │
                              ▼
                     MeetingDetector.build_transcript()
                     ├── Query audio in time window
                     ├── Stitch with speaker labels
                     ├── Set transcript_status
                     └── Update meeting row
                              │
                              ▼
                     Processing Cycle (every 30 min)
                     ├── Triage (Haiku batch)
                     ├── Extraction (Haiku structured)
                     ├── Entity Resolution (fuzzy + LLM)
                     ├── Embedding Generation (OpenAI)
                     ├── Workstream Assignment
                     ├── Nudge Draft Generation
                     └── Signature Parsing
                              │
                              ▼
                     Intelligence Layer (scheduled)
                     ├── Morning Briefing (daily)
                     ├── Meeting Prep (pre-computed)
                     ├── Friday Recap (weekly)
                     ├── Sentiment Aggregation
                     └── Dashboard Cache Refresh
```

---

## 7. Key Integration Points for Helios

| Aegis File | What It Does | Helios Relevance |
|-----------|-------------|-----------------|
| `ingestion/screenpipe.py` | REST client that calls Screenpipe at :3030 | **Primary integration point** — Helios must serve this API |
| `ingestion/meeting_detector.py` | Builds transcripts from audio chunks | May simplify if Helios provides complete transcripts |
| `config.py` | `screenpipe_url`, `polling_screenpipe_seconds` | Config for Helios URL |
| `db/models.py` (Meeting) | `transcript_text`, `transcript_status`, `screen_context` | Data contract for transcripts |
| `processing/pipeline.py` | Skips meetings without transcripts | Helios reliability determines extraction coverage |
| `ingestion/poller.py` | No Screenpipe loop currently | Should add Helios health monitoring |

### Screenpipe API Contract (what Helios must serve):

**`GET /health`**
- Returns: `{"status": "ok"}` with HTTP 200
- Used by: `ScreenpipeClient.health_check()`

**`GET /search`**
- Parameters:
  - `content_type`: `"audio"` or `"ocr"`
  - `start_time`: ISO-8601 datetime string
  - `end_time`: ISO-8601 datetime string
  - `limit`: integer (default 1000)
- Returns:
  ```json
  {
    "data": [
      {
        "content": {
          "text": "transcribed speech here",
          "timestamp": "2026-04-22T10:05:00Z",
          "speaker": {
            "name": "Alice",
            "id": "optional-speaker-id"
          }
        }
      }
    ]
  }
  ```
- For OCR: same structure but `content` includes `app_name` field
- Used by: `ScreenpipeClient.get_audio()`, `ScreenpipeClient.get_screen_ocr()`