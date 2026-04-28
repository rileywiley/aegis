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

    # ── Screenpipe ────────────────────────────────────────
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
    polling_screenpipe_seconds: int = 300

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
    workstream_assign_high_confidence: float = 0.8
    workstream_assign_low_confidence: float = 0.6
    workstream_default_quiet_days: int = 14

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
