"""Aegis configuration — pydantic-settings loading from .env + admin_settings override."""

from functools import lru_cache

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Required ──────────────────────────────────────────
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    azure_client_id: str = ""
    azure_tenant_id: str = ""
    database_url: str = "postgresql+asyncpg://postgres@localhost:5434/aegis"

    # ── Helios (capture daemon) ──────────────────────────
    helios_url: str = "http://127.0.0.1:3031"
    helios_token_path: str = "~/.aegis/capture.toml"
    helios_heartbeat_seconds: int = 60
    helios_heartbeat_timeout_seconds: int = 5

    # ── Screenpipe ────────────────────────────────────────
    # DEPRECATED: replaced by helios_url in Phase 3; will remove in Phase 4
    screenpipe_url: str = "http://localhost:3030"

    # ── Timezone ──────────────────────────────────────────
    aegis_timezone: str = "America/New_York"

    # ── Server ────────────────────────────────────────────
    aegis_host: str = "127.0.0.1"
    aegis_port: int = 8000
    log_level: str = "INFO"

    # ── Polling intervals (seconds) ──────────────────────
    polling_calendar_seconds: int = 1800
    polling_email_seconds: int = 900
    polling_teams_seconds: int = 600
    # DEPRECATED: replaced by helios_heartbeat_seconds in Phase 3; will remove in Phase 4
    polling_screenpipe_seconds: int = 300

    # ── MSAL silent-refresh timeouts (seconds) ───────────
    # Passed straight to the underlying ``requests`` library on every
    # ``login.microsoftonline.com`` token-refresh call. Without these,
    # MSAL waits ~60s for OS-level TCP timeout on flaky connections,
    # blocking the asyncio event loop and starving the pollers.
    msal_connect_timeout_seconds: float = 5.0
    msal_read_timeout_seconds: float = 15.0

    # ── Intelligence schedule ────────────────────────────
    morning_briefing_time: str = "07:30"
    monday_brief_time: str = "07:30"
    friday_recap_time: str = "16:00"
    meeting_prep_minutes_before: int = 15

    # ── Triage thresholds ────────────────────────────────
    triage_substantive_threshold: float = 0.7
    triage_contextual_threshold: float = 0.3

    # ── Workstream detection ─────────────────────────────
    workstream_auto_create_confidence: float = 0.7
    workstream_assign_high_confidence: float = 0.55
    workstream_assign_low_confidence: float = 0.35
    workstream_default_quiet_days: int = 14
    # Voice-note specific floor: cosine similarity below this never
    # auto-links a voice note to a workstream. Conservative default —
    # voice notes are short and chatty so we err on the side of leaving
    # them unlinked rather than mis-linking. HELIOS.md §16.12.
    voice_note_workstream_floor: float = 0.55

    # ── Stale item thresholds ────────────────────────────
    stale_action_item_days: int = 7
    stale_ask_hours: int = 72
    stale_nudge_threshold_days: int = 3

    # ── Noise filtering ──────────────────────────────────
    email_skip_noreply: bool = True
    teams_min_message_length: int = 15
    teams_channel_batch_minutes: int = 30

    # ── Data retention (days) ────────────────────────────
    retention_hot_days: int = 90
    retention_warm_days: int = 365

    # ── Dashboard ────────────────────────────────────────
    dashboard_cache_ttl_seconds: int = 900
    dashboard_max_workstream_slots: int = 8

    # ── Notifications ────────────────────────────────────
    notify_macos: bool = True
    notify_email_self: bool = False
    notify_teams_self: bool = False

    # ── Meeting exclusion keywords ───────────────────────
    meeting_exclusion_keywords: str = (
        "confidential,HR,performance review,legal,board session,"
        "personnel,disciplinary,termination"
    )

    @computed_field
    @property
    def exclusion_keywords_list(self) -> list[str]:
        return [s.strip() for s in self.meeting_exclusion_keywords.split(",") if s.strip()]

    # ── Readiness score thresholds ───────────────────────
    readiness_light_max: int = 40
    readiness_moderate_max: int = 70
    readiness_heavy_max: int = 85

    # ── Sentiment ────────────────────────────────────────
    sentiment_rolling_window_days: int = 30
    sentiment_trend_window_days: int = 14
    sentiment_friction_threshold: int = 60

    # ── User identity ────────────────────────────────────
    user_email: str = "delemos.ricardo@gmail.com"

    # ── Org domains ─────────────────────────────────────
    org_email_domains: str = "hawthorneheath.com"  # comma-separated internal domains

    # ── LLM off-hours cost control ───────────────────────
    # Gates the scheduled `processing_cycle` job + briefing crons to a
    # working-hours window. User-initiated calls (chat, re-extract,
    # voice notes) stay ungated. See aegis/intelligence/llm_gate.py.
    llm_work_hours_enabled: bool = True
    llm_work_hours_start: str = "08:00"    # local time, HH:MM
    llm_work_hours_end: str = "18:00"      # local time, HH:MM
    llm_work_days: str = "1,2,3,4,5"       # ISO day numbers Mon=1..Sun=7
    llm_force_pause: bool = False          # admin kill switch
    llm_force_active: bool = False         # bypass gate (rare: stress test / backfill)


@lru_cache
def get_settings() -> Settings:
    return Settings()
