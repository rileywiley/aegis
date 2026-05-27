"""Pydantic models for database rows."""

from typing import Literal

from pydantic import BaseModel


class CaptureSessionRow(BaseModel):
    id: int
    kind: Literal["calendar", "continuous", "manual_screen", "voice_note"]
    started_at: float
    ended_at: float | None = None
    end_reason: str | None = None
    diarization_status: Literal[
        "pending", "running", "complete", "failed", "not_applicable"
    ] = "pending"
    diarization_attempts: int = 0
    # Appended for parity with migration schema (Wave 2C):
    screen_capture_override_until: float | None = None


class AudioChunkRow(BaseModel):
    id: int
    session_id: int
    channel: Literal["mic", "system"]
    start_ts: float
    end_ts: float
    path: str | None = None
    samples: int
    partial: bool = False
    status: Literal[
        "recorded",
        "no_audio",
        "unavailable",
        "transcribed",
        "transcription_failed",
    ] = "recorded"
    transcription_attempts: int = 0
    # Appended for parity with migration schema (Wave 2C):
    unavailable_reason: str | None = None
    transcribed_at: float | None = None


class TranscriptSegmentRow(BaseModel):
    id: int
    chunk_id: int
    segment_index: int
    start_ts: float
    end_ts: float
    text: str
    speaker: str | None = None
    words: str | None = None  # JSON string


class VoiceNoteRow(BaseModel):
    id: int
    session_id: int
    started_at: float
    ended_at: float | None = None
    triggered_by: str
    excerpt_of_session_id: int | None = None
    excerpt_start_ts: float | None = None
    excerpt_end_ts: float | None = None


# ─────────────────────────────────────────────────────────────────────
# Wave 2C additions — row models for remaining tables
# ─────────────────────────────────────────────────────────────────────


class SessionCalendarLinkRow(BaseModel):
    """Row in session_calendar_links (composite PK: session_id + calendar_event_id)."""

    session_id: int
    calendar_event_id: str
    overlap_start: float
    overlap_end: float


class DiarizationTurnRow(BaseModel):
    id: int
    session_id: int
    speaker_label: str
    start_ts: float
    end_ts: float
    embedding: bytes | None = None


class OCRFrameRow(BaseModel):
    id: int
    session_id: int
    ts: float
    app_bundle: str
    display_id: int | None = None
    phash: bytes
    text: str
    avg_confidence: float
    thumbnail_path: str | None = None


class PermissionCheckRow(BaseModel):
    id: int
    checked_at: float
    mic_granted: bool
    screen_recording_granted: bool


class ComponentStatusRow(BaseModel):
    id: int
    ts: float
    component: str
    status: str
    reason: str | None = None
    detail: str | None = None
    action: str | None = None


class DaemonEventRow(BaseModel):
    id: int
    ts: float
    level: Literal["info", "warn", "error"]
    component: str
    event: str
    details: str | None = None
