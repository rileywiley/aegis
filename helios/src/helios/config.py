"""Helios configuration — loads from ~/.aegis/capture.toml with env overrides."""

import os
import secrets
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiConfig(BaseModel):
    port: int = 3031
    bearer_token: str = ""
    bind_address: str = "127.0.0.1"


class CaptureConfig(BaseModel):
    calendar_triggered: bool = True
    continuous_enabled: bool = True
    calendar_pre_start_seconds: int = Field(60, ge=0, le=600)
    calendar_post_end_seconds: int = Field(300, ge=0, le=1800)
    # Float so the §12.5 smoke test can dial this down to a fraction of
    # an hour (e.g. ``1/60`` ≈ 60 s) and exercise the prompt path without
    # waiting four real hours. Production values are still whole hours
    # (default 4).
    continuous_prompt_hours: float = 4.0
    continuous_prompt_timeout_seconds: int = 300
    continuous_hard_stop_local: str = "17:30"
    continuous_hard_stop_timezone: str = "follow_user"
    pause_morning_hour: int = 8


class ExclusionConfig(BaseModel):
    keywords: list[str] = ["confidential", "HR", "legal", "personal"]


class AudioConfig(BaseModel):
    sample_rate: int = 16000
    chunk_seconds: int = 30
    silence_threshold_db: int = -50
    system_source: str = "sck"


class TranscriptionConfig(BaseModel):
    runtime: str = "whisperx"
    model: str = "distil-large-v3"
    language: str = "en"
    compute_type: str = "float16"
    word_timestamps: bool = True
    vad_filter: bool = True
    vad_min_silence_ms: int = 1000
    # WhisperX 3.x supports 'pyannote' (default upstream) and 'silero'.
    # Silero is preferred here because it doesn't drag pyannote.audio +
    # opentelemetry into the import chain — pyannote's metadata-driven
    # entry-point lookups break when bundled by py2app. Run-from-source
    # users can override via capture.toml if they want pyannote VAD.
    vad_method: str = "silero"
    worker_nice: int = 10
    transcription_max_attempts: int = 3
    model_load_timeout_seconds: int = 30


class DiarizationConfig(BaseModel):
    enabled: bool = False
    min_speakers: int = 1
    max_speakers: int = 8
    store_embeddings: bool = True
    hf_token_location: str = "keychain"


class OcrConfig(BaseModel):
    enabled: bool = True
    fps: int = 1
    dedup_hamming_threshold: int = 4
    dedup_window_frames: int = 10
    min_text_chars: int = 20
    confidence_threshold: float = 0.5
    thumbnail_confidence_threshold: float = 0.7
    # When True, OCR frames are gated by the ``meeting_apps`` allowlist
    # (Teams/Zoom/Webex bundle ids must be frontmost). When False, every
    # frame passes the gate — the user-initiated override still applies
    # but is no longer required. Default is False because Teams desktop
    # minimizes during screen-share which hands frontmost to the shared
    # app (e.g. Safari) and starves the allowlist. Smarter screen-share-
    # aware gating is a follow-up.
    gate_by_allowlist: bool = False
    meeting_apps: list[str] = [
        "com.microsoft.teams2",
        "com.microsoft.teams",
        "us.zoom.xos",
        "com.webex.meetingmanager",
        "com.cisco.webexmeetingsapp",
    ]


class RetentionConfig(BaseModel):
    raw_audio_days: int = 7
    trash_hold_hours: int = 24
    cleanup_hour_local: int = 3
    disk_space_warning_gb: int = 5


class StorageConfig(BaseModel):
    root: str = "~/.aegis/capture"


class SchedulerConfig(BaseModel):
    calendar_source_url: str = "http://127.0.0.1:8000/api/meetings/upcoming"
    calendar_poll_seconds: int = 60
    permission_check_minutes: int = 5
    aegis_connect_timeout_seconds: int = 5


class NotificationsConfig(BaseModel):
    time_sensitive_allowed: bool = True
    enable_sound_on_prompts: bool = True


class VoiceNoteIndicatorConfig(BaseModel):
    floating_pill_position: str = "top_right"
    show_elapsed_time: bool = True
    show_audio_level: bool = True
    # Persisted final on-screen position from the last drag (in screen
    # coordinates). ``None`` means "use floating_pill_position default".
    # Schema: ``{"x": int, "y": int}``.
    last_position: dict[str, int] | None = None


class VoiceNoteConfig(BaseModel):
    enabled: bool = True
    max_duration_seconds: int = 300
    soft_cap_notification: bool = True
    auto_save_timeout_seconds: int = 10
    default_save_action: str = "save_with_suggestions"
    hotkey_enabled: bool = False
    hotkey_combo: str = "cmd+option+v"
    indicator: VoiceNoteIndicatorConfig = VoiceNoteIndicatorConfig()


class LoggingConfig(BaseModel):
    level: str = "info"
    file_rotate_days: int = 14
    file_max_size_mb: int = 50


class LaunchAgentConfig(BaseModel):
    plist_path: str = "~/Library/LaunchAgents/com.aegis.helios.plist"
    auto_install: bool = True


class HeliosConfig(BaseModel):
    api: ApiConfig = ApiConfig()
    capture: CaptureConfig = CaptureConfig()
    exclusion: ExclusionConfig = ExclusionConfig()
    audio: AudioConfig = AudioConfig()
    transcription: TranscriptionConfig = TranscriptionConfig()
    diarization: DiarizationConfig = DiarizationConfig()
    ocr: OcrConfig = OcrConfig()
    retention: RetentionConfig = RetentionConfig()
    storage: StorageConfig = StorageConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    voice_note: VoiceNoteConfig = VoiceNoteConfig()
    logging: LoggingConfig = LoggingConfig()
    launchagent: LaunchAgentConfig = LaunchAgentConfig()


_CONFIG_PATH = Path("~/.aegis/capture.toml").expanduser()
_cached_config: HeliosConfig | None = None


def _generate_default_toml(token: str) -> str:
    """Generate default TOML config content."""
    return f'''[api]
port = 3031
bearer_token = "{token}"
bind_address = "127.0.0.1"

[capture]
calendar_triggered = true
continuous_enabled = true
calendar_pre_start_seconds = 60
calendar_post_end_seconds = 300
continuous_hard_stop_local = "17:30"

[exclusion]
keywords = ["confidential", "HR", "legal", "personal"]

[audio]
sample_rate = 16000
chunk_seconds = 30

[transcription]
model = "distil-large-v3"
language = "en"

[diarization]
enabled = false

[ocr]
enabled = true

[retention]
raw_audio_days = 7

[storage]
root = "~/.aegis/capture"

[scheduler]
calendar_source_url = "http://127.0.0.1:8000/api/meetings/upcoming"

[voice_note]
enabled = true
max_duration_seconds = 300

[logging]
level = "info"
'''


def load_config(path: Path | None = None) -> HeliosConfig:
    """Load config from TOML file, creating with defaults if missing."""
    global _cached_config

    config_path = path or _CONFIG_PATH

    if not config_path.exists():
        # First run — generate config with fresh bearer token
        config_path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(32)
        config_path.write_text(_generate_default_toml(token))
        os.chmod(config_path, stat.S_IRUSR | stat.S_IWUSR)  # 600

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    # Read as UTF-8 explicitly. ``Path.read_text()`` without an encoding
    # falls back to the locale's default — on a bundled LaunchAgent that
    # often resolves to ``ascii``, and any non-ASCII byte in capture.toml
    # (a unicode char in a user name, a UTF-8 BOM, etc.) crashes daemon
    # startup with UnicodeDecodeError. Lock the encoding here so the
    # file's text contents survive the bundle launch path.
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    config = HeliosConfig(**data)
    _cached_config = config
    return config


def get_config() -> HeliosConfig:
    """Get the cached config, loading if needed."""
    if _cached_config is None:
        return load_config()
    return _cached_config
