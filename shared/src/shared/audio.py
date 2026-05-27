"""Helios → Aegis audio/transcript response contract."""

from pydantic import BaseModel


class Word(BaseModel):
    word: str
    start: float
    end: float


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[Word] | None = None


class Coverage(BaseModel):
    start: float
    end: float
    channel: str  # "mic" or "system"


class TranscriptResponse(BaseModel):
    segments: list[TranscriptSegment]
    coverage: list[Coverage]
    diarization_status: str = "pending"


class OCRFrame(BaseModel):
    ts: float
    app_bundle: str
    text: str
    avg_confidence: float


class OCRResponse(BaseModel):
    frames: list[OCRFrame]
