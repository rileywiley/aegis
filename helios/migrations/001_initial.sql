-- Helios initial schema
-- All timestamps are REAL (UTC epoch seconds)

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);

CREATE TABLE capture_sessions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('calendar', 'continuous', 'manual_screen', 'voice_note')),
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    diarization_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (diarization_status IN ('pending', 'running', 'complete', 'failed', 'not_applicable')),
    diarization_attempts INTEGER NOT NULL DEFAULT 0,
    screen_capture_override_until REAL
);
CREATE INDEX idx_sessions_time ON capture_sessions(started_at, ended_at);
CREATE INDEX idx_sessions_active ON capture_sessions(ended_at) WHERE ended_at IS NULL;

CREATE TABLE session_calendar_links (
    session_id INTEGER NOT NULL REFERENCES capture_sessions(id) ON DELETE CASCADE,
    calendar_event_id TEXT NOT NULL,
    overlap_start REAL NOT NULL,
    overlap_end REAL NOT NULL,
    PRIMARY KEY (session_id, calendar_event_id)
);
CREATE INDEX idx_links_calendar ON session_calendar_links(calendar_event_id);

CREATE TABLE audio_chunks (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES capture_sessions(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('mic', 'system')),
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    path TEXT,
    samples INTEGER NOT NULL,
    partial INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'recorded'
        CHECK (status IN ('recorded', 'no_audio', 'unavailable', 'transcribed', 'transcription_failed')),
    unavailable_reason TEXT,
    transcribed_at REAL,
    transcription_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_chunks_session ON audio_chunks(session_id);
CREATE INDEX idx_chunks_time ON audio_chunks(start_ts, end_ts);
CREATE INDEX idx_chunks_pending ON audio_chunks(status, transcribed_at)
    WHERE status = 'recorded' AND transcribed_at IS NULL;

CREATE TABLE transcript_segments (
    id INTEGER PRIMARY KEY,
    chunk_id INTEGER NOT NULL REFERENCES audio_chunks(id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    text TEXT NOT NULL,
    speaker TEXT,
    words TEXT
);
CREATE INDEX idx_segments_chunk ON transcript_segments(chunk_id);
CREATE INDEX idx_segments_time ON transcript_segments(start_ts, end_ts);

CREATE TABLE diarization_turns (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES capture_sessions(id) ON DELETE CASCADE,
    speaker_label TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    embedding BLOB
);
CREATE INDEX idx_diar_session ON diarization_turns(session_id);
CREATE INDEX idx_diar_time ON diarization_turns(start_ts, end_ts);

CREATE TABLE ocr_frames (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES capture_sessions(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    app_bundle TEXT NOT NULL,
    display_id INTEGER,
    phash BLOB NOT NULL,
    text TEXT NOT NULL,
    avg_confidence REAL NOT NULL,
    thumbnail_path TEXT
);
CREATE INDEX idx_ocr_session ON ocr_frames(session_id);
CREATE INDEX idx_ocr_time ON ocr_frames(ts);

CREATE TABLE permission_checks (
    id INTEGER PRIMARY KEY,
    checked_at REAL NOT NULL,
    mic_granted INTEGER NOT NULL,
    screen_recording_granted INTEGER NOT NULL
);
CREATE INDEX idx_perm_time ON permission_checks(checked_at);

CREATE TABLE component_status (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    component TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    detail TEXT,
    action TEXT
);
CREATE INDEX idx_component_time ON component_status(component, ts);

CREATE TABLE daemon_events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('info', 'warn', 'error')),
    component TEXT NOT NULL,
    event TEXT NOT NULL,
    details TEXT
);
CREATE INDEX idx_events_time ON daemon_events(ts);
CREATE INDEX idx_events_component ON daemon_events(component, ts);

CREATE TABLE voice_notes (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES capture_sessions(id) ON DELETE CASCADE,
    started_at REAL NOT NULL,
    ended_at REAL,
    excerpt_of_session_id INTEGER REFERENCES capture_sessions(id),
    excerpt_start_ts REAL,
    excerpt_end_ts REAL,
    triggered_by TEXT NOT NULL CHECK (triggered_by IN ('menu_bar', 'hotkey', 'dashboard'))
);
CREATE INDEX idx_vn_session ON voice_notes(session_id);
CREATE INDEX idx_vn_excerpt ON voice_notes(excerpt_of_session_id) WHERE excerpt_of_session_id IS NOT NULL;
CREATE INDEX idx_vn_time ON voice_notes(started_at);
