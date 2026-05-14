"""Typed query functions — no raw SQL outside this module."""

import time

import aiosqlite

from helios.db.rows import (
    AudioChunkRow,
    CaptureSessionRow,
    ComponentStatusRow,
    DaemonEventRow,
    DiarizationTurnRow,
    OCRFrameRow,
    PermissionCheckRow,
    TranscriptSegmentRow,
    VoiceNoteRow,
)
from helios.log import get_logger

log = get_logger("db.queries")


def _row_to_dict(cursor: aiosqlite.Cursor, row: tuple) -> dict:
    """Convert a sqlite row tuple + cursor description into a dict."""
    return dict(zip([d[0] for d in cursor.description], row))


async def create_session(
    db: aiosqlite.Connection,
    kind: str,
    started_at: float | None = None,
) -> int:
    """Create a new capture session. Returns the session ID."""
    ts = started_at or time.time()
    cursor = await db.execute(
        "INSERT INTO capture_sessions (kind, started_at) VALUES (?, ?)",
        (kind, ts),
    )
    await db.commit()
    return cursor.lastrowid


async def end_session(
    db: aiosqlite.Connection,
    session_id: int,
    end_reason: str,
) -> None:
    """End an active capture session."""
    await db.execute(
        "UPDATE capture_sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
        (time.time(), end_reason, session_id),
    )
    await db.commit()


async def get_active_session(db: aiosqlite.Connection) -> CaptureSessionRow | None:
    """Get the currently active session (ended_at IS NULL)."""
    cursor = await db.execute(
        "SELECT * FROM capture_sessions WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cursor.description]
    return CaptureSessionRow(**dict(zip(cols, row)))


async def insert_audio_chunk(
    db: aiosqlite.Connection,
    session_id: int,
    channel: str,
    start_ts: float,
    end_ts: float,
    path: str | None,
    samples: int,
    partial: bool = False,
) -> int:
    """Insert an audio chunk record. Returns the chunk ID."""
    cursor = await db.execute(
        "INSERT INTO audio_chunks (session_id, channel, start_ts, end_ts, path, samples, partial) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, channel, start_ts, end_ts, path, samples, 1 if partial else 0),
    )
    await db.commit()
    return cursor.lastrowid


async def log_daemon_event(
    db: aiosqlite.Connection,
    level: str,
    component: str,
    event: str,
    details: str | None = None,
) -> None:
    """Log a daemon event to the database."""
    await db.execute(
        "INSERT INTO daemon_events (ts, level, component, event, details) VALUES (?, ?, ?, ?, ?)",
        (time.time(), level, component, event, details),
    )
    await db.commit()


# ─────────────────────────────────────────────────────────────────────
# Wave 2C additions — chunks, sessions, calendar links, component status
# ─────────────────────────────────────────────────────────────────────


async def get_chunk_by_id(
    db: aiosqlite.Connection,
    chunk_id: int,
) -> AudioChunkRow | None:
    """Fetch a single audio chunk by its primary key."""
    cursor = await db.execute(
        "SELECT * FROM audio_chunks WHERE id = ?",
        (chunk_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return AudioChunkRow(**_row_to_dict(cursor, row))


async def get_pending_chunks(
    db: aiosqlite.Connection,
    limit: int = 20,
) -> list[AudioChunkRow]:
    """Chunks recorded but not yet transcribed, oldest first."""
    cursor = await db.execute(
        "SELECT * FROM audio_chunks "
        "WHERE status = 'recorded' AND transcribed_at IS NULL "
        "ORDER BY start_ts ASC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [AudioChunkRow(**_row_to_dict(cursor, r)) for r in rows]


async def get_session_audio_chunks(
    db: aiosqlite.Connection,
    session_id: int,
) -> list[AudioChunkRow]:
    """All chunks for a session, ordered by start_ts."""
    cursor = await db.execute(
        "SELECT * FROM audio_chunks WHERE session_id = ? ORDER BY start_ts ASC",
        (session_id,),
    )
    rows = await cursor.fetchall()
    return [AudioChunkRow(**_row_to_dict(cursor, r)) for r in rows]


async def get_chunks_in_range(
    db: aiosqlite.Connection,
    start_ts: float,
    end_ts: float,
) -> list[AudioChunkRow]:
    """Chunks whose [start_ts, end_ts) overlaps the given window."""
    cursor = await db.execute(
        "SELECT * FROM audio_chunks "
        "WHERE start_ts < ? AND end_ts > ? "
        "ORDER BY start_ts ASC",
        (end_ts, start_ts),
    )
    rows = await cursor.fetchall()
    return [AudioChunkRow(**_row_to_dict(cursor, r)) for r in rows]


async def mark_chunk_transcribed(
    db: aiosqlite.Connection,
    chunk_id: int,
    transcribed_at: float | None = None,
) -> None:
    """Mark a chunk as transcribed, recording when."""
    ts = transcribed_at if transcribed_at is not None else time.time()
    await db.execute(
        "UPDATE audio_chunks SET status = 'transcribed', transcribed_at = ? WHERE id = ?",
        (ts, chunk_id),
    )
    await db.commit()


async def mark_chunk_transcription_failed(
    db: aiosqlite.Connection,
    chunk_id: int,
    attempts: int,
) -> None:
    """Mark a chunk's transcription as failed and record attempt count."""
    await db.execute(
        "UPDATE audio_chunks "
        "SET status = 'transcription_failed', transcription_attempts = ? "
        "WHERE id = ?",
        (attempts, chunk_id),
    )
    await db.commit()


async def mark_chunk_archived(
    db: aiosqlite.Connection,
    chunk_id: int,
) -> None:
    """Mark a chunk as archived (its WAV has been removed from disk).

    The schema has no 'archived' status — instead we set ``status =
    'transcribed'`` (a terminal status the migration's CHECK allows) and
    clear ``path``. Forcing the terminal status guarantees that even if
    a caller archives a chunk that hadn't yet been transcribed, the row
    won't be picked up by ``get_pending_chunks`` (which filters on
    ``status = 'recorded' AND transcribed_at IS NULL``) — that would
    cause the transcription worker to retry forever against a NULL path.
    """
    await db.execute(
        "UPDATE audio_chunks SET path = NULL, status = 'transcribed' "
        "WHERE id = ?",
        (chunk_id,),
    )
    await db.commit()


async def mark_chunk_unavailable(
    db: aiosqlite.Connection,
    chunk_id: int,
    reason: str,
) -> None:
    """Mark a chunk as unavailable (capture failed) with a reason."""
    await db.execute(
        "UPDATE audio_chunks SET status = 'unavailable', unavailable_reason = ? WHERE id = ?",
        (reason, chunk_id),
    )
    await db.commit()


async def update_session_ended(
    db: aiosqlite.Connection,
    session_id: int,
    ended_at: float,
    end_reason: str,
) -> None:
    """End a session with an explicit timestamp (vs end_session which uses now()).

    Used by crash recovery, where the recorded end time may be in the past.
    """
    await db.execute(
        "UPDATE capture_sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
        (ended_at, end_reason, session_id),
    )
    await db.commit()


async def get_sessions_overlapping(
    db: aiosqlite.Connection,
    start_ts: float,
    end_ts: float,
) -> list[CaptureSessionRow]:
    """Sessions whose [started_at, COALESCE(ended_at, +inf)) overlaps the window.

    A still-active session (ended_at IS NULL) is treated as extending forever
    forward, so it overlaps if its started_at < end_ts.
    """
    cursor = await db.execute(
        "SELECT * FROM capture_sessions "
        "WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?) "
        "ORDER BY started_at ASC",
        (end_ts, start_ts),
    )
    rows = await cursor.fetchall()
    return [CaptureSessionRow(**_row_to_dict(cursor, r)) for r in rows]


async def insert_session_calendar_link(
    db: aiosqlite.Connection,
    session_id: int,
    calendar_event_id: str,
    overlap_start: float,
    overlap_end: float,
) -> int:
    """Link a session to a calendar event with the actual overlap window.

    ``overlap_start`` / ``overlap_end`` should be the intersection of the
    session window and the calendar event window. The orchestrator computes
    these from ``CalendarEvent.start_ts`` / ``end_ts`` (clipped to the
    session bounds when the session has finished).

    Validates the session exists (FK is enforced by the schema, but we
    surface a clearer error). Returns the session_id (composite PK; no
    autoincrement id here).
    """
    # Validate the session exists so callers get a friendly error rather
    # than an opaque IntegrityError from the FK.
    cursor = await db.execute(
        "SELECT 1 FROM capture_sessions WHERE id = ?",
        (session_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise ValueError(f"session {session_id} does not exist")
    await db.execute(
        "INSERT INTO session_calendar_links "
        "(session_id, calendar_event_id, overlap_start, overlap_end) "
        "VALUES (?, ?, ?, ?)",
        (session_id, calendar_event_id, overlap_start, overlap_end),
    )
    await db.commit()
    return session_id


async def get_sessions_for_calendar_event(
    db: aiosqlite.Connection,
    calendar_event_id: str,
) -> list[CaptureSessionRow]:
    """All sessions linked to a given calendar event."""
    cursor = await db.execute(
        "SELECT s.* FROM capture_sessions s "
        "JOIN session_calendar_links l ON l.session_id = s.id "
        "WHERE l.calendar_event_id = ? "
        "ORDER BY s.started_at ASC",
        (calendar_event_id,),
    )
    rows = await cursor.fetchall()
    return [CaptureSessionRow(**_row_to_dict(cursor, r)) for r in rows]


async def upsert_component_status(
    db: aiosqlite.Connection,
    component: str,
    status: str,
    details: str | None = None,
    *,
    reason: str | None = None,
    action: str | None = None,
) -> None:
    """Upsert the latest health status for a component.

    The schema has no UNIQUE on component (multiple rows allowed for history),
    so we delete prior rows for this component and insert a fresh one. This
    keeps get_component_status returning a single current value.

    ``reason`` and ``action`` were added by Wave 1A (Phase 3) to surface
    machine-readable error codes and user-facing remediation hints to
    /v1/status. Older callers passing only ``details`` continue to work
    unchanged.
    """
    await db.execute(
        "DELETE FROM component_status WHERE component = ?",
        (component,),
    )
    await db.execute(
        "INSERT INTO component_status (ts, component, status, reason, detail, action) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (time.time(), component, status, reason, details, action),
    )
    await db.commit()


async def get_component_status(
    db: aiosqlite.Connection,
    component: str,
) -> ComponentStatusRow | None:
    """Get the most recent status row for a component."""
    cursor = await db.execute(
        "SELECT * FROM component_status WHERE component = ? ORDER BY ts DESC LIMIT 1",
        (component,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return ComponentStatusRow(**_row_to_dict(cursor, row))


# ─────────────────────────────────────────────────────────────────────
# Wave 2D additions — chunker support (coordinate with Wave 2C)
# ─────────────────────────────────────────────────────────────────────


async def insert_no_audio_chunk(
    db: aiosqlite.Connection,
    session_id: int,
    channel: str,
    start_ts: float,
    end_ts: float,
    samples: int,
    partial: bool = False,
) -> int:
    """Insert a no_audio chunk row (silent buffer; no WAV file written)."""
    cursor = await db.execute(
        "INSERT INTO audio_chunks "
        "(session_id, channel, start_ts, end_ts, path, samples, partial, status) "
        "VALUES (?, ?, ?, ?, NULL, ?, ?, 'no_audio')",
        (session_id, channel, start_ts, end_ts, samples, 1 if partial else 0),
    )
    await db.commit()
    return cursor.lastrowid


# ─────────────────────────────────────────────────────────────────────
# Wave 4F additions — orchestrator support (stall → unavailable chunk)
# ─────────────────────────────────────────────────────────────────────


async def insert_unavailable_chunk(
    db: aiosqlite.Connection,
    session_id: int,
    channel: str,
    start_ts: float,
    end_ts: float,
    samples: int,
    reason: str,
    partial: bool = False,
) -> int:
    """Insert an ``unavailable`` chunk row in a single statement.

    Mirrors :func:`insert_no_audio_chunk` but pins ``status='unavailable'``
    and stores the supplied ``unavailable_reason``. Used by the
    orchestrator's ``on_stall`` callback so a watchdog gap is recorded
    in one round trip (vs. insert-then-mark).
    """
    cursor = await db.execute(
        "INSERT INTO audio_chunks "
        "(session_id, channel, start_ts, end_ts, path, samples, partial, "
        " status, unavailable_reason) "
        "VALUES (?, ?, ?, ?, NULL, ?, ?, 'unavailable', ?)",
        (
            session_id,
            channel,
            start_ts,
            end_ts,
            samples,
            1 if partial else 0,
            reason,
        ),
    )
    await db.commit()
    return cursor.lastrowid


# ─────────────────────────────────────────────────────────────────────
# Wave 1 / 2F additions — permission checks
# ─────────────────────────────────────────────────────────────────────


async def insert_permission_check(
    db: aiosqlite.Connection,
    checked_at: float,
    mic_granted: bool,
    screen_recording_granted: bool,
) -> int:
    """Insert a permission check row. Returns the new row id."""
    cursor = await db.execute(
        "INSERT INTO permission_checks "
        "(checked_at, mic_granted, screen_recording_granted) "
        "VALUES (?, ?, ?)",
        (
            checked_at,
            1 if mic_granted else 0,
            1 if screen_recording_granted else 0,
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def get_last_permission_check(
    db: aiosqlite.Connection,
) -> PermissionCheckRow | None:
    """Return the most recent permission check, or None if none recorded."""
    cursor = await db.execute(
        "SELECT * FROM permission_checks ORDER BY checked_at DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return PermissionCheckRow(**_row_to_dict(cursor, row))


# ─────────────────────────────────────────────────────────────────────
# Wave 3 / 2E additions — session listing + diagnostics audit trail
# ─────────────────────────────────────────────────────────────────────


async def list_sessions(
    db: aiosqlite.Connection,
    *,
    kind: str | None = None,
    start_ts_gte: float | None = None,
    start_ts_lte: float | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[CaptureSessionRow]:
    """Filterable session list, newest started_at first.

    ``status`` is a logical filter mapped onto our concrete columns:

    * ``"active"`` → ``ended_at IS NULL``
    * ``"ended"`` / ``"completed"`` / ``"captured"`` → ``ended_at IS NOT NULL``
    * ``"failed"``    → ``end_reason LIKE '%fail%'`` or ``LIKE '%error%'``

    Other / unknown values are ignored (no rows excluded). The status
    enum on the wire is captured/partial/no_audio/failed (HELIOS.md §7.3),
    but Phase 2 doesn't yet compute coverage — partial/no_audio always
    return the full list.
    """
    where: list[str] = []
    args: list = []
    if kind is not None:
        where.append("kind = ?")
        args.append(kind)
    if start_ts_gte is not None:
        where.append("started_at >= ?")
        args.append(float(start_ts_gte))
    if start_ts_lte is not None:
        where.append("started_at <= ?")
        args.append(float(start_ts_lte))
    if status == "active":
        where.append("ended_at IS NULL")
    elif status in ("ended", "completed", "captured"):
        where.append("ended_at IS NOT NULL")
    elif status == "failed":
        where.append("(end_reason LIKE '%fail%' OR end_reason LIKE '%error%')")

    sql = "SELECT * FROM capture_sessions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
    args.extend([int(limit), int(offset)])

    cursor = await db.execute(sql, tuple(args))
    rows = await cursor.fetchall()
    return [CaptureSessionRow(**_row_to_dict(cursor, r)) for r in rows]


async def count_sessions(
    db: aiosqlite.Connection,
    *,
    kind: str | None = None,
    start_ts_gte: float | None = None,
    start_ts_lte: float | None = None,
    status: str | None = None,
) -> int:
    """Match ``list_sessions`` filters and return total row count (no pagination)."""
    where: list[str] = []
    args: list = []
    if kind is not None:
        where.append("kind = ?")
        args.append(kind)
    if start_ts_gte is not None:
        where.append("started_at >= ?")
        args.append(float(start_ts_gte))
    if start_ts_lte is not None:
        where.append("started_at <= ?")
        args.append(float(start_ts_lte))
    if status == "active":
        where.append("ended_at IS NULL")
    elif status in ("ended", "completed", "captured"):
        where.append("ended_at IS NOT NULL")
    elif status == "failed":
        where.append("(end_reason LIKE '%fail%' OR end_reason LIKE '%error%')")

    sql = "SELECT COUNT(*) FROM capture_sessions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    cursor = await db.execute(sql, tuple(args))
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def get_session_by_id(
    db: aiosqlite.Connection,
    session_id: int,
) -> CaptureSessionRow | None:
    """Look up a single session row by its primary key."""
    cursor = await db.execute(
        "SELECT * FROM capture_sessions WHERE id = ?",
        (session_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return CaptureSessionRow(**_row_to_dict(cursor, row))


async def update_session_screen_override(
    db: aiosqlite.Connection,
    session_id: int,
    override_until: float | None,
) -> bool:
    """Set ``capture_sessions.screen_capture_override_until`` for a session.

    Used by the Phase-5 ``POST /v1/capture/enable-screen-override``
    endpoint (HELIOS_BUILD_PLAN §5B / §12.6) to flip the column so the
    OCR worker (Agent 5A) can bypass the app allowlist while the
    override window is still in the future. Pass ``None`` to clear.

    Returns True when the UPDATE matched a row, False when ``session_id``
    does not exist — callers should map False to a 404 response.
    """
    cursor = await db.execute(
        "UPDATE capture_sessions SET screen_capture_override_until = ? WHERE id = ?",
        (override_until, session_id),
    )
    await db.commit()
    return (cursor.rowcount or 0) > 0


async def delete_session(
    db: aiosqlite.Connection,
    session_id: int,
) -> bool:
    """Delete a session (cascades to chunks / segments / diarization / OCR / voice notes).

    Returns True when a row was deleted, False when ``session_id`` did
    not exist. Cascades are enforced by the migration's ``ON DELETE
    CASCADE`` foreign keys.
    """
    cursor = await db.execute(
        "DELETE FROM capture_sessions WHERE id = ?",
        (session_id,),
    )
    await db.commit()
    return (cursor.rowcount or 0) > 0


async def get_recent_daemon_events(
    db: aiosqlite.Connection,
    limit: int = 50,
) -> list[DaemonEventRow]:
    """Most recent daemon_events, newest first (for diagnostics dump)."""
    cursor = await db.execute(
        "SELECT * FROM daemon_events ORDER BY ts DESC LIMIT ?",
        (int(limit),),
    )
    rows = await cursor.fetchall()
    return [DaemonEventRow(**_row_to_dict(cursor, r)) for r in rows]


async def get_recent_component_status(
    db: aiosqlite.Connection,
) -> list[ComponentStatusRow]:
    """Latest status row per component (one row each)."""
    cursor = await db.execute(
        # Pick the row with the max ts per component using a window-style
        # subquery; SQLite supports correlated subqueries here.
        "SELECT cs.* FROM component_status cs "
        "WHERE cs.ts = ("
        "    SELECT MAX(ts) FROM component_status WHERE component = cs.component"
        ") "
        "ORDER BY cs.component"
    )
    rows = await cursor.fetchall()
    return [ComponentStatusRow(**_row_to_dict(cursor, r)) for r in rows]


# ─────────────────────────────────────────────────────────────────────
# Wave 2F additions — diarization helpers
# ─────────────────────────────────────────────────────────────────────


async def insert_diarization_turn(
    db: aiosqlite.Connection,
    session_id: int,
    speaker_label: str,
    start_ts: float,
    end_ts: float,
    embedding: bytes | None = None,
) -> int:
    """Insert one diarization_turns row. Returns the row id.

    ``embedding`` is a raw float32 BLOB (numpy.tobytes()) or NULL when
    DiarizationConfig.store_embeddings is False or pyannote did not
    yield an embedding for the speaker.
    """
    cursor = await db.execute(
        "INSERT INTO diarization_turns "
        "(session_id, speaker_label, start_ts, end_ts, embedding) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, speaker_label, start_ts, end_ts, embedding),
    )
    await db.commit()
    return cursor.lastrowid


async def get_diarization_turns_for_session(
    db: aiosqlite.Connection, session_id: int
) -> list[DiarizationTurnRow]:
    """All diarization turns for a session, ordered by start_ts ASC.

    Consumed by Wave 3I's merge worker, which joins these turns to
    transcript_segments by time-overlap to assign speaker labels.
    """
    cursor = await db.execute(
        "SELECT * FROM diarization_turns WHERE session_id = ? ORDER BY start_ts ASC",
        (session_id,),
    )
    rows = await cursor.fetchall()
    return [DiarizationTurnRow(**_row_to_dict(cursor, r)) for r in rows]


async def update_session_diarization_status(
    db: aiosqlite.Connection,
    session_id: int,
    status: str,
) -> None:
    """Set capture_sessions.diarization_status for a session.

    Valid values per the migration's CHECK constraint:
    ``pending`` | ``running`` | ``complete`` | ``failed`` | ``not_applicable``.
    """
    await db.execute(
        "UPDATE capture_sessions SET diarization_status = ? WHERE id = ?",
        (status, session_id),
    )
    await db.commit()


# ─────────────────────────────────────────────────────────────────────
# Wave 2E additions — transcript_segments + chunk attempts
# ─────────────────────────────────────────────────────────────────────


async def insert_transcript_segment(
    db: aiosqlite.Connection,
    chunk_id: int,
    segment_index: int,
    start_ts: float,
    end_ts: float,
    text: str,
    speaker: str | None = None,
    words: str | None = None,
) -> int:
    """Insert one ``transcript_segments`` row. Returns the row id.

    ``words`` is a JSON-encoded string (or NULL) containing the word-level
    timestamps that WhisperX produces when ``word_timestamps=True``. The
    transcription worker serialises the list before calling this helper.
    """
    cursor = await db.execute(
        "INSERT INTO transcript_segments "
        "(chunk_id, segment_index, start_ts, end_ts, text, speaker, words) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chunk_id, segment_index, start_ts, end_ts, text, speaker, words),
    )
    await db.commit()
    return cursor.lastrowid


async def get_segments_for_chunk(
    db: aiosqlite.Connection, chunk_id: int
) -> list[TranscriptSegmentRow]:
    """Return all transcript_segments rows for a chunk, ordered by start_ts."""
    cursor = await db.execute(
        "SELECT * FROM transcript_segments WHERE chunk_id = ? ORDER BY start_ts",
        (chunk_id,),
    )
    rows = await cursor.fetchall()
    return [TranscriptSegmentRow(**_row_to_dict(cursor, r)) for r in rows]


async def update_chunk_transcription_attempts(
    db: aiosqlite.Connection,
    chunk_id: int,
    attempts: int,
) -> None:
    """Bump ``transcription_attempts`` without changing the chunk's status.

    Used by the transcription worker between retries: the chunk stays
    ``status='recorded'`` (so ``get_pending_chunks`` will pick it up again
    next loop), only the attempts counter moves up. A separate call to
    :func:`mark_chunk_transcription_failed` flips status when ``attempts``
    has reached ``transcription_max_attempts``.
    """
    await db.execute(
        "UPDATE audio_chunks SET transcription_attempts = ? WHERE id = ?",
        (attempts, chunk_id),
    )
    await db.commit()


# ─────────────────────────────────────────────────────────────────────
# Wave 3I additions — transcript serving + merge worker
# ─────────────────────────────────────────────────────────────────────


async def get_segments_for_session(
    db: aiosqlite.Connection, session_id: int
) -> list[TranscriptSegmentRow]:
    """All ``transcript_segments`` for a session, ordered by ``start_ts``.

    Joins through ``audio_chunks`` because ``transcript_segments`` has no
    ``session_id`` column — segments belong to chunks, chunks belong to
    sessions.
    """
    cursor = await db.execute(
        "SELECT s.* FROM transcript_segments s "
        "JOIN audio_chunks c ON s.chunk_id = c.id "
        "WHERE c.session_id = ? "
        "ORDER BY s.start_ts ASC",
        (session_id,),
    )
    rows = await cursor.fetchall()
    return [TranscriptSegmentRow(**_row_to_dict(cursor, r)) for r in rows]


async def get_segments_with_channel_for_session(
    db: aiosqlite.Connection, session_id: int
) -> list[tuple[TranscriptSegmentRow, str]]:
    """All segments plus their owning chunk's channel.

    Used by Wave 3I's merge worker: it needs to skip mic-channel
    segments (already ``speaker='user'``) and only assign speakers on
    system-channel segments.
    """
    cursor = await db.execute(
        "SELECT s.*, c.channel FROM transcript_segments s "
        "JOIN audio_chunks c ON s.chunk_id = c.id "
        "WHERE c.session_id = ? "
        "ORDER BY s.start_ts ASC",
        (session_id,),
    )
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    out: list[tuple[TranscriptSegmentRow, str]] = []
    for r in rows:
        d = dict(zip(cols, r))
        channel = d.pop("channel")
        out.append((TranscriptSegmentRow(**d), channel))
    return out


async def get_segments_in_range(
    db: aiosqlite.Connection,
    start_ts: float,
    end_ts: float,
) -> list[TranscriptSegmentRow]:
    """Segments whose ``[start_ts, end_ts]`` overlaps the given window.

    Used by ``GET /v1/audio?start=&end=`` for the cross-session
    time-window query (HELIOS.md §9.6) — the typical case is a meeting
    that crosses a session boundary because two back-to-back captures
    sandwich its scheduled end.
    """
    cursor = await db.execute(
        "SELECT * FROM transcript_segments "
        "WHERE start_ts < ? AND end_ts > ? "
        "ORDER BY start_ts ASC",
        (end_ts, start_ts),
    )
    rows = await cursor.fetchall()
    return [TranscriptSegmentRow(**_row_to_dict(cursor, r)) for r in rows]


async def update_segment_speaker(
    db: aiosqlite.Connection,
    segment_id: int,
    speaker: str,
) -> None:
    """Set ``transcript_segments.speaker``. Used by the merge worker."""
    await db.execute(
        "UPDATE transcript_segments SET speaker = ? WHERE id = ?",
        (speaker, segment_id),
    )
    await db.commit()


# ─────────────────────────────────────────────────────────────────────
# OCR — frame persistence + retrieval  (Phase 5 / Track 5A)
# ─────────────────────────────────────────────────────────────────────


async def insert_ocr_frame(
    db: aiosqlite.Connection,
    *,
    session_id: int,
    ts: float,
    app_bundle: str,
    display_id: int | None,
    phash: bytes,
    text: str,
    avg_confidence: float,
    thumbnail_path: str | None = None,
) -> int:
    """Insert an ``ocr_frames`` row. Returns the new row id.

    Used by :class:`helios.workers.ocr.OcrWorker`. The worker is
    responsible for honouring its dedup window (``phash`` Hamming
    distance) BEFORE calling this — the row is unconditionally
    persisted once we get here. PII safety is the caller's
    responsibility: ``text`` is stored verbatim and MUST NOT appear in
    any log message.
    """
    cursor = await db.execute(
        "INSERT INTO ocr_frames "
        "(session_id, ts, app_bundle, display_id, phash, text, "
        "avg_confidence, thumbnail_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            ts,
            app_bundle,
            display_id,
            phash,
            text,
            avg_confidence,
            thumbnail_path,
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def get_ocr_frames_in_range(
    db: aiosqlite.Connection,
    *,
    start_ts: float,
    end_ts: float,
    min_confidence: float | None = None,
    app_bundle: str | None = None,
) -> list[OCRFrameRow]:
    """Frames whose ``ts`` falls in ``[start_ts, end_ts]``, oldest first.

    Backs ``GET /v1/ocr?start=&end=`` per HELIOS.md §7.3 / §9.4.
    Optional filters narrow by minimum confidence and/or app bundle id
    — both correspond to the query-string params on the endpoint.
    """
    sql = (
        "SELECT * FROM ocr_frames "
        "WHERE ts >= ? AND ts <= ?"
    )
    params: list[float | str] = [start_ts, end_ts]
    if min_confidence is not None:
        sql += " AND avg_confidence >= ?"
        params.append(float(min_confidence))
    if app_bundle:
        sql += " AND app_bundle = ?"
        params.append(app_bundle)
    sql += " ORDER BY ts ASC"
    cursor = await db.execute(sql, tuple(params))
    rows = await cursor.fetchall()
    return [OCRFrameRow(**_row_to_dict(cursor, r)) for r in rows]


async def get_recent_ocr_frame_phashes(
    db: aiosqlite.Connection,
    *,
    session_id: int,
    limit: int = 10,
) -> list[bytes]:
    """Most recent N perceptual hashes for the given session, newest first.

    Used by the OCR worker's near-duplicate filter: each new frame's
    Hamming distance is computed against this window, and the frame is
    dropped when the minimum distance is at or below the configured
    threshold. Returning bytes (rather than full rows) keeps this hot
    path cheap.
    """
    cursor = await db.execute(
        "SELECT phash FROM ocr_frames "
        "WHERE session_id = ? "
        "ORDER BY ts DESC LIMIT ?",
        (session_id, limit),
    )
    rows = await cursor.fetchall()
    return [r[0] for r in rows]


# ─────────────────────────────────────────────────────────────────────
# Wave 5C additions — retention cleanup + per-session deletion (Phase 5)
# ─────────────────────────────────────────────────────────────────────
#
# These helpers back :mod:`helios.workers.cleanup` and the
# ``DELETE /v1/sessions/{session_id}`` endpoint. They live in a clearly
# labelled block so the existing query surface stays untouched.


async def get_transcribed_chunks_older_than(
    db: aiosqlite.Connection,
    cutoff_ts: float,
) -> list[AudioChunkRow]:
    """Return chunks whose ``start_ts < cutoff_ts`` AND are safely trashable.

    "Safely trashable" per HELIOS.md §17 means:

    * ``status = 'transcribed'`` — untranscribed WAVs (``recorded``,
      ``no_audio``, ``unavailable``, ``transcription_failed``) are
      preserved so the user never loses content that hasn't been
      processed yet.
    * ``path IS NOT NULL`` — chunks already archived (path cleared)
      have nothing on disk left to trash.

    Diarization is treated as a session-level concern handled by the
    cleanup worker after fetching candidates: it checks
    ``capture_sessions.diarization_status`` per chunk's session before
    moving the file. We keep this query simple (chunk-level) so the
    worker can apply additional filtering policy without baking it into
    SQL.
    """
    cursor = await db.execute(
        "SELECT * FROM audio_chunks "
        "WHERE start_ts < ? "
        "  AND status = 'transcribed' "
        "  AND path IS NOT NULL "
        "ORDER BY start_ts ASC",
        (cutoff_ts,),
    )
    rows = await cursor.fetchall()
    return [AudioChunkRow(**_row_to_dict(cursor, r)) for r in rows]


async def get_audio_chunks_with_path_for_session(
    db: aiosqlite.Connection,
    session_id: int,
) -> list[AudioChunkRow]:
    """All audio chunks for ``session_id`` whose ``path`` is non-NULL.

    Used by per-meeting deletion to know which WAVs to trash. Skips
    rows whose ``path`` is already NULL (archived / no_audio /
    unavailable) — nothing on disk to move.
    """
    cursor = await db.execute(
        "SELECT * FROM audio_chunks "
        "WHERE session_id = ? AND path IS NOT NULL "
        "ORDER BY start_ts ASC",
        (session_id,),
    )
    rows = await cursor.fetchall()
    return [AudioChunkRow(**_row_to_dict(cursor, r)) for r in rows]
