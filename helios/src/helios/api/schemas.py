"""Pydantic v2 request/response schemas for the Helios HTTP API.

Mirrors HELIOS.md §7.4. The shapes here are the wire contract Aegis +
the menu bar consume; if you need to change a field, update §7.4 first.

Phase-2 placeholders (returned as empty lists / null transcripts):
``TranscriptResponse``, ``AudioListResponse``, ``OcrListResponse``,
``VoiceNoteStopResponse.transcript``. Phase 3 / 5 land the real data.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Error envelope (HELIOS.md §7.2)
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """Stable shape for every non-2xx response."""

    model_config = ConfigDict(extra="forbid")

    error: str
    detail: str
    code: int


# ---------------------------------------------------------------------------
# /v1/capture/*
# ---------------------------------------------------------------------------


class CaptureStartRequest(BaseModel):
    """Body for ``POST /v1/capture/start``.

    ``duration_minutes`` is required when ``kind="manual_screen"`` and
    ignored otherwise; HELIOS.md §7.3.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["continuous", "manual_screen"] = "continuous"
    duration_minutes: int | None = None

    @model_validator(mode="after")
    def _duration_required_for_manual_screen(self) -> "CaptureStartRequest":
        if self.kind == "manual_screen":
            if self.duration_minutes is None or self.duration_minutes <= 0:
                raise ValueError(
                    "duration_minutes (positive integer) required for manual_screen"
                )
        return self


class CaptureStartResponse(BaseModel):
    session_id: int
    kind: Literal["continuous", "manual_screen"]
    started_at: float


class CaptureStopResponse(BaseModel):
    """Idempotent stop response — ``session_id`` is null when nothing was active."""

    session_id: int | None
    ended_at: float | None
    end_reason: str | None


class CapturePauseUntilRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    until_ts: float = Field(..., gt=0)


class CapturePauseUntilResponse(BaseModel):
    paused_until: float


class CaptureResumeResponse(BaseModel):
    paused: bool = False


class CaptureScreenOverrideRequest(BaseModel):
    """Body for ``POST /v1/capture/enable-screen-override``.

    HELIOS_BUILD_PLAN §5B / §12.6 wire shape is ``{session_id,
    duration_seconds}``; we still accept the legacy ``duration_minutes``
    field shipped in Phase 2 (the menu bar's pre-§5B clients) so older
    clients keep working. Exactly one of the duration fields must be
    supplied. When ``session_id`` is omitted, the override applies to
    whatever session the orchestrator currently has active.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: int | None = None
    duration_seconds: int | None = Field(default=None, gt=0, le=24 * 60 * 60)
    duration_minutes: int | None = Field(default=None, gt=0, le=24 * 60)

    @model_validator(mode="after")
    def _one_duration_required(self) -> "CaptureScreenOverrideRequest":
        if self.duration_seconds is None and self.duration_minutes is None:
            raise ValueError(
                "one of duration_seconds or duration_minutes is required"
            )
        if self.duration_seconds is not None and self.duration_minutes is not None:
            raise ValueError(
                "provide only one of duration_seconds or duration_minutes"
            )
        return self

    @property
    def effective_duration_seconds(self) -> int:
        """Resolve duration into seconds regardless of which field was sent."""
        if self.duration_seconds is not None:
            return int(self.duration_seconds)
        # duration_minutes guaranteed non-None by the validator above.
        assert self.duration_minutes is not None  # for type checkers
        return int(self.duration_minutes) * 60


class CaptureScreenOverrideResponse(BaseModel):
    """§5B response — exposes both the legacy ``duration_minutes`` echo
    (so Phase-2 clients keep parsing) and the new
    ``screen_capture_override_until`` UTC epoch float.
    """

    ok: bool = True
    session_id: int | None = None
    duration_minutes: int
    duration_seconds: int
    screen_capture_override_until: float | None = None


class PromptResponseRequest(BaseModel):
    """Body for ``POST /v1/capture/prompt-response``.

    The wire field is named ``continue`` (a Python keyword); mapped to
    ``continue_session`` via Pydantic's ``alias=``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    continue_session: bool = Field(..., alias="continue")


class PromptResponseResponse(BaseModel):
    ok: bool = True


# ---------------------------------------------------------------------------
# /v1/status, /v1/permissions, /v1/diagnostics
# ---------------------------------------------------------------------------


class ComponentErrorResponse(BaseModel):
    """Per-component error envelope nested inside ``component_errors``.

    HELIOS.md §7.3 prescribes ``reason`` / ``detail`` / ``action``;
    ``component`` and ``message`` are kept for backward compatibility
    with earlier callers that read either flat field.
    """

    component: str | None = None
    message: str | None = None
    occurred_at: float | None = None
    reason: str | None = None
    detail: str | None = None
    action: str | None = None


class QueueCountsResponse(BaseModel):
    transcription_pending: int = 0
    transcription_failed_24h: int = 0
    diarization_pending: int = 0
    diarization_failed_24h: int = 0


class PermissionsResponse(BaseModel):
    mic_granted: bool
    screen_recording_granted: bool
    last_checked_at: float | None


class ActiveSessionResponse(BaseModel):
    """Per-§7.3 ``active_session`` payload."""

    id: int
    kind: Literal["calendar", "continuous", "manual_screen", "voice_note"]
    calendar_event_ids: list[str] = Field(default_factory=list)
    started_at: float
    screen_capture_override_until: float | None = None


class NextCalendarEventResponse(BaseModel):
    """Per-§7.3 ``next_calendar_event`` payload."""

    calendar_event_id: str
    title: str = ""
    starts_at: float
    pre_start_at: float


class LastChunkInfo(BaseModel):
    """Per-channel last-chunk freshness for ``GET /v1/diagnostics``."""

    ts: float | None = None
    age_seconds: float | None = None


class LastChunksResponse(BaseModel):
    mic: LastChunkInfo = Field(default_factory=LastChunkInfo)
    system: LastChunkInfo = Field(default_factory=LastChunkInfo)


class StatusResponse(BaseModel):
    """Snapshot for menu-bar polling + dashboard overview (§7.3).

    The wire shape mirrors HELIOS.md §7.3 ``GET /v1/status``: nested
    ``components``, ``component_errors``, ``active_session``, and
    ``next_calendar_event`` blocks. ``active_session_id`` is also kept
    (legacy) so older menu-bar code keeps working alongside the new
    nested object.
    """

    daemon: Literal["running", "stopped"] = "running"
    mode: Literal["armed", "recording", "paused", "error", "not_running"]
    components: dict[str, str] = Field(default_factory=dict)
    component_errors: dict[str, ComponentErrorResponse] = Field(default_factory=dict)
    active_session: ActiveSessionResponse | None = None
    next_calendar_event: NextCalendarEventResponse | None = None
    # Legacy convenience field. Equal to active_session.id when active.
    active_session_id: int | None = None
    paused_until: float | None = None
    aegis_unreachable: bool = False
    last_error: str | None = None
    queue: QueueCountsResponse = Field(default_factory=QueueCountsResponse)
    scheduler_running: bool
    permissions: PermissionsResponse


class DaemonEventResponse(BaseModel):
    ts: float
    level: Literal["info", "warn", "error"]
    component: str
    event: str
    details: str | None = None


class ComponentStatusResponse(BaseModel):
    component: str
    status: str
    ts: float
    reason: str | None = None
    detail: str | None = None
    action: str | None = None


class StorageDiagnosticsResponse(BaseModel):
    audio_bytes: int = 0
    audio_oldest_days: int = 0
    thumbnails_bytes: int = 0
    database_bytes: int = 0


class DiagnosticsResponse(BaseModel):
    """Full diagnostic dump (§7.3 ``GET /v1/diagnostics``).

    Adds ``active_session`` (object), ``next_event`` (object),
    ``last_chunks``, and ``transcription_throughput_realtime_multiple``
    per the spec. Legacy ``active_session_id`` is retained.
    """

    version: str
    pid: int
    uptime_seconds: float
    memory_mb: float | None = None
    cpu_5min_avg_pct: float | None = None
    permissions: PermissionsResponse
    active_session: ActiveSessionResponse | None = None
    next_event: NextCalendarEventResponse | None = None
    last_chunks: LastChunksResponse = Field(default_factory=LastChunksResponse)
    transcription_throughput_realtime_multiple: float | None = None
    # Legacy convenience field. Equal to active_session.id when active.
    active_session_id: int | None = None
    queues: QueueCountsResponse
    storage: StorageDiagnosticsResponse
    component_status: list[ComponentStatusResponse] = Field(default_factory=list)
    recent_events: list[DaemonEventResponse] = Field(default_factory=list)


class DiagnosticsAcceptedResponse(BaseModel):
    """Generic 202 response for fire-and-forget diagnostics actions."""

    status: Literal["queued"] = "queued"
    detail: str | None = None


class ReloadComponentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# /v1/sessions/*
# ---------------------------------------------------------------------------


class SessionResponse(BaseModel):
    """Single capture session row (§7.3 ``GET /v1/sessions/{id}``)."""

    id: int
    kind: Literal["calendar", "continuous", "manual_screen", "voice_note"]
    started_at: float
    ended_at: float | None
    end_reason: str | None
    duration_seconds: float | None
    diarization_status: str
    screen_capture_override_until: float | None = None


class SessionsListResponse(BaseModel):
    sessions: list[SessionResponse]
    total: int
    offset: int
    limit: int


class WordResponse(BaseModel):
    """Word-level timestamp (filled in Phase 3)."""

    start: float
    end: float
    word: str
    score: float | None = None


class TranscriptSegmentResponse(BaseModel):
    start: float
    end: float
    speaker: str | None = None
    text: str
    words: list[WordResponse] | None = None


class TranscriptCoverageRange(BaseModel):
    start: float
    end: float
    reason: str


class TranscriptCoverage(BaseModel):
    captured_seconds: float = 0.0
    unavailable_ranges: list[TranscriptCoverageRange] = Field(default_factory=list)
    transcription_pending_seconds: float = 0.0


class TranscriptResponse(BaseModel):
    """Phase-2 returns ``segments=[]`` and ``diarization_status=pending``."""

    session_id: int | None = None
    started_at: float | None = None
    ended_at: float | None = None
    segments: list[TranscriptSegmentResponse] = Field(default_factory=list)
    coverage: TranscriptCoverage = Field(default_factory=TranscriptCoverage)
    diarization_status: str = "pending"


class SessionActionResponse(BaseModel):
    status: Literal["queued"] = "queued"
    session_id: int


class SessionDeleteResponse(BaseModel):
    """``DELETE /v1/sessions/{session_id}`` response (HELIOS.md §7.3 / §17).

    * ``session_id`` — the id of the session that was just removed.
    * ``chunks_trashed`` — how many on-disk WAVs were moved to
      ``<storage.root>/trash/``. Chunks whose ``path`` was already NULL
      (archived / no_audio / unavailable) do not count toward this number.
    * ``was_active`` — ``True`` when the session was the orchestrator's
      active session and Helios had to stop it before deletion.
    """

    session_id: int
    chunks_trashed: int = 0
    was_active: bool = False


# ---------------------------------------------------------------------------
# /v1/audio + /v1/ocr (Phase 5 placeholders)
# ---------------------------------------------------------------------------


class AudioChunkResponse(BaseModel):
    id: int
    session_id: int
    channel: Literal["mic", "system"]
    start_ts: float
    end_ts: float
    samples: int
    status: str
    partial: bool = False


class AudioListResponse(BaseModel):
    """Response for ``GET /v1/audio?start=&end=`` — HELIOS.md §9.6.

    Wave 3I made this concrete: it now returns transcript segments
    spanning the requested time window across whatever sessions
    intersect it (the typical case is a meeting that crosses a session
    boundary because the user took a break mid-meeting). The
    ``chunks`` field is retained for legacy (always empty) so any
    pre-Phase-3 caller keeps parsing.
    """

    segments: list[TranscriptSegmentResponse] = Field(default_factory=list)
    chunks: list[AudioChunkResponse] = Field(default_factory=list)


class OcrFrameResponse(BaseModel):
    ts: float
    app_bundle: str
    display_id: int | None = None
    text: str
    confidence: float
    thumbnail_url: str | None = None


class OcrListResponse(BaseModel):
    frames: list[OcrFrameResponse] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# /v1/voice-note/*
# ---------------------------------------------------------------------------


class VoiceNoteStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    triggered_by: Literal["menu_bar", "hotkey", "dashboard"] = "menu_bar"


class VoiceNoteStartResponse(BaseModel):
    voice_note_id: int
    session_id: int
    started_at: float
    is_excerpt: bool
    parent_session_id: int | None = None
    max_duration_seconds: int


class VoiceNoteStopResponse(BaseModel):
    voice_note_id: int
    session_id: int
    started_at: float
    ended_at: float
    duration_seconds: float
    transcript: TranscriptResponse | None = None
    is_excerpt: bool = False


class VoiceNoteCancelResponse(BaseModel):
    cancelled_voice_note_id: int


class ActiveVoiceNote(BaseModel):
    voice_note_id: int
    session_id: int
    started_at: float
    elapsed_seconds: float
    is_excerpt: bool
    max_duration_seconds: int
    approaching_cap: bool = False
    # Track 4G: most-recent-window mic RMS, normalised to [0.0, 1.0]. The
    # floating indicator polls this every 250ms to drive its audio level
    # bar. ``0.0`` ⇔ no audio chunks yet OR computation failed.
    current_rms: float = 0.0


class ActiveVoiceNoteResponse(BaseModel):
    """Wraps the optional active note. ``active=null`` ⇔ no voice note running."""

    active: ActiveVoiceNote | None = None


__all__ = [
    "ActiveSessionResponse",
    "ActiveVoiceNote",
    "ActiveVoiceNoteResponse",
    "AudioChunkResponse",
    "AudioListResponse",
    "CapturePauseUntilRequest",
    "CapturePauseUntilResponse",
    "CaptureResumeResponse",
    "CaptureScreenOverrideRequest",
    "CaptureScreenOverrideResponse",
    "CaptureStartRequest",
    "CaptureStartResponse",
    "CaptureStopResponse",
    "ComponentErrorResponse",
    "ComponentStatusResponse",
    "DaemonEventResponse",
    "DiagnosticsAcceptedResponse",
    "DiagnosticsResponse",
    "ErrorResponse",
    "LastChunkInfo",
    "LastChunksResponse",
    "NextCalendarEventResponse",
    "OcrFrameResponse",
    "OcrListResponse",
    "PermissionsResponse",
    "PromptResponseRequest",
    "PromptResponseResponse",
    "QueueCountsResponse",
    "ReloadComponentRequest",
    "SessionActionResponse",
    "SessionDeleteResponse",
    "SessionResponse",
    "SessionsListResponse",
    "StatusResponse",
    "StorageDiagnosticsResponse",
    "TranscriptCoverage",
    "TranscriptCoverageRange",
    "TranscriptResponse",
    "TranscriptSegmentResponse",
    "VoiceNoteCancelResponse",
    "VoiceNoteStartRequest",
    "VoiceNoteStartResponse",
    "VoiceNoteStopResponse",
    "WordResponse",
]
