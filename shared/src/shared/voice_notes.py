"""Voice note schemas shared between Helios and Aegis."""

from pydantic import BaseModel


class VoiceNoteMetadata(BaseModel):
    id: int
    session_id: int
    started_at: float
    ended_at: float | None = None
    triggered_by: str  # "menu_bar", "hotkey", "dashboard"
    excerpt_of_session_id: int | None = None


class VoiceNoteTranscript(BaseModel):
    voice_note_id: int
    segments: list[dict]  # TranscriptSegment dicts
    duration_seconds: float
