"""Helpers for the Helios Settings page + HuggingFace wizard.

The Aegis venv does NOT import ``helios.config`` (the helios package
isn't installed into ``aegis/.venv`` — Helios runs from a bundled venv
under ``/Applications/Helios.app``). To validate user-submitted settings
without taking a hard dependency on the helios source tree, we duplicate
the Pydantic schema here. The shape mirrors
``helios/src/helios/config.py`` as of Phase 6 Wave 3; if Helios adds /
removes fields the Aegis Settings page is allowed to lag — the unknown
fields are simply preserved in the TOML untouched (we only mutate the
keys the user actually edits).

HELIOS.md references:
    §5.4 — hot-reload classification (which fields can apply immediately
           vs require a daemon restart).
    §13.7 — settings page sections.
    §15.5 — HF speaker-ID wizard.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Duplicated Pydantic schema (mirror of helios.config.HeliosConfig)
# ─────────────────────────────────────────────────────────────────────


class ApiConfig(BaseModel):
    port: int = Field(3031, ge=1024, le=65535)
    bearer_token: str = ""
    bind_address: str = "127.0.0.1"


class CaptureConfig(BaseModel):
    calendar_triggered: bool = True
    continuous_enabled: bool = True
    calendar_pre_start_seconds: int = Field(60, ge=0, le=600)
    calendar_post_end_seconds: int = Field(300, ge=0, le=1800)
    continuous_prompt_hours: float = Field(4.0, ge=0.0, le=24.0)
    continuous_prompt_timeout_seconds: int = Field(300, ge=10, le=3600)
    continuous_hard_stop_local: str = "17:30"
    continuous_hard_stop_timezone: str = "follow_user"
    pause_morning_hour: int = Field(8, ge=0, le=23)


class ExclusionConfig(BaseModel):
    keywords: list[str] = ["confidential", "HR", "legal", "personal"]


class AudioConfig(BaseModel):
    sample_rate: int = 16000
    chunk_seconds: int = Field(30, ge=5, le=600)
    silence_threshold_db: int = Field(-50, ge=-120, le=0)
    system_source: str = "sck"


class TranscriptionConfig(BaseModel):
    runtime: str = "whisperx"
    model: str = "distil-large-v3"
    language: str = "en"
    compute_type: str = "float16"
    word_timestamps: bool = True
    vad_filter: bool = True
    vad_min_silence_ms: int = 1000
    vad_method: str = "silero"
    worker_nice: int = 10
    transcription_max_attempts: int = 3
    model_load_timeout_seconds: int = Field(30, ge=5, le=600)


class DiarizationConfig(BaseModel):
    enabled: bool = False
    min_speakers: int = Field(1, ge=1, le=20)
    max_speakers: int = Field(8, ge=1, le=20)
    store_embeddings: bool = True
    hf_token_location: str = "keychain"
    # The wizard persists step progress here so re-loads pick up where
    # the user left off. Not strictly part of helios.config but Helios's
    # Pydantic model accepts the field via the open ``model_config``.
    setup_progress: dict[str, Any] = Field(default_factory=dict)


class OcrConfig(BaseModel):
    enabled: bool = True
    fps: int = 1
    dedup_hamming_threshold: int = 4
    dedup_window_frames: int = 10
    min_text_chars: int = 20
    confidence_threshold: float = 0.5
    thumbnail_confidence_threshold: float = 0.7
    gate_by_allowlist: bool = False
    meeting_apps: list[str] = [
        "com.microsoft.teams2",
        "com.microsoft.teams",
        "us.zoom.xos",
        "com.webex.meetingmanager",
        "com.cisco.webexmeetingsapp",
    ]


class RetentionConfig(BaseModel):
    raw_audio_days: int = Field(7, ge=1, le=365)
    trash_hold_hours: int = Field(24, ge=0, le=720)
    cleanup_hour_local: int = Field(3, ge=0, le=23)
    disk_space_warning_gb: int = Field(5, ge=1, le=500)


class StorageConfig(BaseModel):
    root: str = "~/.aegis/capture"


class SchedulerConfig(BaseModel):
    calendar_source_url: str = "http://127.0.0.1:8000/api/meetings/upcoming"
    calendar_poll_seconds: int = Field(60, ge=10, le=3600)
    permission_check_minutes: int = Field(5, ge=1, le=60)
    aegis_connect_timeout_seconds: int = Field(5, ge=1, le=60)


class NotificationsConfig(BaseModel):
    time_sensitive_allowed: bool = True
    enable_sound_on_prompts: bool = True


class VoiceNoteIndicatorConfig(BaseModel):
    floating_pill_position: str = "top_right"
    show_elapsed_time: bool = True
    show_audio_level: bool = True
    last_position: dict[str, int] | None = None


class VoiceNoteConfig(BaseModel):
    enabled: bool = True
    max_duration_seconds: int = Field(300, ge=10, le=1800)
    soft_cap_notification: bool = True
    # Spec calls this "auto_save_delay_seconds" in the form; helios.config
    # ships it as ``auto_save_timeout_seconds``. We use the daemon's name
    # for the TOML key and label it "auto-save delay" in the UI.
    auto_save_timeout_seconds: int = Field(10, ge=2, le=60)
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
    """Duplicate of ``helios.config.HeliosConfig`` for Aegis-side validation.

    Extra keys are preserved at the dict level (we only Pydantic-validate
    sections the user mutates). See ``_write_capture_toml``.
    """

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


# ─────────────────────────────────────────────────────────────────────
# Hot-reload classification (HELIOS.md §5.4)
# ─────────────────────────────────────────────────────────────────────

# Each tuple is ``(section, field)`` matching the TOML key path. Anything
# NOT in this set is assumed restart-required (conservative — the page
# surfaces "Restart Daemon" prompt rather than silently failing to apply).
HOT_RELOADABLE_FIELDS: set[tuple[str, str]] = {
    # HELIOS.md §5.4 enumerates this exact set: exclusion keywords, OCR
    # meeting_apps + thresholds, retention days, notification settings,
    # logging level. ``capture.*`` and ``voice_note.*`` were previously
    # listed here but the daemon has no hot-reload handler for them —
    # they MUST trigger a "Restart Daemon" banner. (Warning #1.)
    ("exclusion", "keywords"),
    ("ocr", "meeting_apps"),
    ("ocr", "gate_by_allowlist"),
    ("ocr", "fps"),
    ("ocr", "min_text_chars"),
    ("ocr", "confidence_threshold"),
    ("ocr", "thumbnail_confidence_threshold"),
    ("retention", "raw_audio_days"),
    ("retention", "trash_hold_hours"),
    ("retention", "cleanup_hour_local"),
    ("retention", "disk_space_warning_gb"),
    ("notifications", "time_sensitive_allowed"),
    ("notifications", "enable_sound_on_prompts"),
    ("logging", "level"),
}

RESTART_REQUIRED_LABEL = "Requires restart"
HOT_RELOADABLE_LABEL = "Applies immediately"


def field_label(section: str, field: str) -> str:
    return (
        HOT_RELOADABLE_LABEL
        if (section, field) in HOT_RELOADABLE_FIELDS
        else RESTART_REQUIRED_LABEL
    )


# ─────────────────────────────────────────────────────────────────────
# TOML I/O — atomic write + tomllib read
# ─────────────────────────────────────────────────────────────────────


def _load_toml(path: Path) -> dict[str, Any]:
    """Read ``capture.toml`` as a dict. Returns empty dict if missing."""
    try:
        import tomllib
    except ImportError:  # pragma: no cover — Python <3.11
        import tomli as tomllib  # type: ignore[no-redef]
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _dump_toml(data: dict[str, Any]) -> str:
    """Serialize a dict to TOML.

    Uses ``tomli_w`` when available (preferred — proper escaping) and
    falls back to a minimal hand-rolled dumper that covers the subset
    of TOML capture.toml actually uses (str / int / float / bool / list
    of strs / dict-of-table). The fallback is only exercised in Aegis
    venvs that don't have ``tomli_w`` installed; production installs
    include it via the Helios bundle's site-packages (the file already
    exists on disk, so writes go through tomllib's symmetric helpers
    when available).
    """
    try:
        import tomli_w  # type: ignore[import-not-found]

        return tomli_w.dumps(data)
    except ImportError:
        pass

    # Hand-rolled fallback. Sufficient for the capture.toml shape.
    lines: list[str] = []

    def _fmt_value(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, float):
            return repr(v)
        if isinstance(v, str):
            # TOML string with conservative escaping.
            esc = v.replace("\\", "\\\\").replace('"', '\\"')
            esc = esc.replace("\n", "\\n").replace("\t", "\\t")
            return f'"{esc}"'
        if v is None:
            # TOML has no null — emit empty string. Caller should avoid
            # writing None values, but defensive.
            return '""'
        if isinstance(v, list):
            return "[" + ", ".join(_fmt_value(x) for x in v) + "]"
        if isinstance(v, dict):
            # Inline table.
            inner = ", ".join(f"{k} = {_fmt_value(val)}" for k, val in v.items())
            return "{" + inner + "}"
        raise TypeError(f"Unsupported TOML type: {type(v)!r}")

    # Emit top-level scalars first (none expected, but defensive), then tables.
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    for k, v in scalars.items():
        lines.append(f"{k} = {_fmt_value(v)}")
    for table_name, table_data in tables.items():
        lines.append("")
        lines.append(f"[{table_name}]")
        sub_tables: dict[str, dict] = {}
        for k, v in table_data.items():
            if isinstance(v, dict) and not _is_inline_table(v):
                sub_tables[k] = v
                continue
            lines.append(f"{k} = {_fmt_value(v)}")
        for sub_name, sub_data in sub_tables.items():
            lines.append("")
            lines.append(f"[{table_name}.{sub_name}]")
            for k, v in sub_data.items():
                lines.append(f"{k} = {_fmt_value(v)}")
    return "\n".join(lines) + "\n"


def _is_inline_table(d: dict) -> bool:
    """Heuristic: a dict is "inline" if all values are scalars and the
    dict has 3 or fewer keys. Otherwise emit as a sub-table.
    """
    if len(d) > 3:
        return False
    return all(not isinstance(v, (dict, list)) for v in d.values())


def write_capture_toml(path: Path, data: dict[str, Any]) -> None:
    """Atomically write ``data`` to ``path`` as TOML.

    Strategy: write to ``path.tmp`` in the same directory, fsync, rename
    over the target. Sets 0600 permissions on the new file (matches the
    daemon's first-run behavior). Monkeypatch-friendly entry point used
    by the POST handler and the wizard.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _dump_toml(data)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as fh:
        tmp_name = fh.name
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover — best effort
        pass
    os.replace(tmp_name, path)


# ─────────────────────────────────────────────────────────────────────
# Form-payload → TOML diff
# ─────────────────────────────────────────────────────────────────────


def _coerce(value: str, target_type: type) -> Any:
    """Best-effort form-string → target_type coercion."""
    if target_type is bool:
        return value.lower() in ("on", "true", "1", "yes")
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    return value


# Fields the form is NEVER allowed to overwrite via the generic POST
# handler (Warning #6). The bearer token is masked in the rendered form
# (``settings_section_advanced.html`` outputs ``••••``), so a real form
# submit never includes it; but a hand-crafted POST could. Guard at the
# server.
PROTECTED_FIELDS: set[tuple[str, str]] = {
    ("api", "bearer_token"),
}


def apply_form_updates(
    current: dict[str, Any],
    form: dict[str, Any],
    *,
    allow_bearer_overwrite: bool = False,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Merge ``form`` updates into ``current`` (a dict already loaded
    from TOML) and return (new_dict, changed_field_paths).

    ``form`` keys use ``"section.field"`` notation, e.g.
    ``"capture.calendar_pre_start_seconds"``. List-valued fields are
    submitted as ``"<key>[]"`` (one input per item). Unknown sections /
    fields are ignored — we never touch keys outside the schema.

    The list of changes is used by the response template to drive the
    "Restart Daemon" prompt.

    Critical: ``api.bearer_token`` is in ``PROTECTED_FIELDS`` — the
    generic settings POST CANNOT overwrite the bearer. A future
    "Regenerate token" flow must pass ``allow_bearer_overwrite=True``
    after taking its own audit step. (Warning #6.)
    """
    # Build a lookup of (section, field) -> python type from the schema.
    type_map: dict[tuple[str, str], type] = {}
    list_fields: set[tuple[str, str]] = set()
    schema_fields = HeliosConfig.model_fields
    for section_name, section_field_info in schema_fields.items():
        section_cls = section_field_info.annotation
        if not isinstance(section_cls, type) or not issubclass(
            section_cls, BaseModel
        ):
            continue
        for fname, finfo in section_cls.model_fields.items():
            ann = finfo.annotation
            if ann is list or getattr(ann, "__origin__", None) is list:
                list_fields.add((section_name, fname))
                type_map[(section_name, fname)] = list
            elif ann in (bool, int, float, str):
                type_map[(section_name, fname)] = ann
            # Everything else (dict, optional, etc.) we leave alone.

    new = {k: dict(v) if isinstance(v, dict) else v for k, v in current.items()}
    changed: list[tuple[str, str]] = []

    # Gather all submitted keys, normalizing list-suffix "[]".
    keyed: dict[tuple[str, str], Any] = {}
    list_keyed: dict[tuple[str, str], list[str]] = {}
    for raw_key, raw_val in form.items():
        if "." not in raw_key:
            continue
        section, field = raw_key.split(".", 1)
        if field.endswith("[]"):
            field = field[:-2]
            values = raw_val if isinstance(raw_val, list) else [raw_val]
            list_keyed[(section, field)] = [
                v for v in (values if isinstance(values, list) else [values])
                if str(v).strip()
            ]
        else:
            keyed[(section, field)] = raw_val

    # Apply list fields (overwrite — chip lists are full set semantics).
    for (section, field), values in list_keyed.items():
        if (section, field) not in list_fields:
            continue
        new.setdefault(section, {})
        if new[section].get(field) != values:
            new[section][field] = values
            changed.append((section, field))

    # Apply scalar fields with type coercion + Pydantic validation.
    for (section, field), val in keyed.items():
        if (section, field) not in type_map:
            continue
        # Warning #6 — never overwrite protected fields (bearer token)
        # through the generic POST handler.
        if (section, field) in PROTECTED_FIELDS and not allow_bearer_overwrite:
            continue
        target_type = type_map[(section, field)]
        if target_type is list:
            continue  # handled above
        try:
            coerced = _coerce(val if isinstance(val, str) else str(val), target_type)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{section}.{field}: {exc}") from exc
        new.setdefault(section, {})
        if new[section].get(field) != coerced:
            new[section][field] = coerced
            changed.append((section, field))

    # For boolean fields not present in form (unchecked checkboxes) we
    # need a hidden marker. We use a parallel key ``__bool__.<section>.<field>``
    # to signal "this checkbox was present in the form; treat absence as False".
    for raw_key in list(form.keys()):
        if not raw_key.startswith("__bool__."):
            continue
        rest = raw_key.split(".", 2)
        if len(rest) != 3:
            continue
        _, section, field = rest
        if (section, field) not in type_map:
            continue
        if type_map[(section, field)] is not bool:
            continue
        # If the actual key is in the form, it was already handled.
        if (section, field) in keyed:
            continue
        new.setdefault(section, {})
        if new[section].get(field) is not False:
            new[section][field] = False
            changed.append((section, field))

    # Validate the resulting structure section-by-section.
    for section, _ in changed:
        section_cls = HeliosConfig.model_fields.get(section)
        if section_cls is None:
            continue
        section_model = section_cls.annotation
        if not (isinstance(section_model, type) and issubclass(section_model, BaseModel)):
            continue
        # Pydantic will raise ValidationError if out-of-range.
        section_model(**new[section])

    return new, changed


def split_changes_by_reload(
    changes: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Partition changed fields into (hot_reloadable, restart_required)."""
    hot = [c for c in changes if c in HOT_RELOADABLE_FIELDS]
    cold = [c for c in changes if c not in HOT_RELOADABLE_FIELDS]
    return hot, cold


# ─────────────────────────────────────────────────────────────────────
# HF wizard helpers (mockable seams)
# ─────────────────────────────────────────────────────────────────────


async def validate_hf_token(token: str) -> tuple[bool, str]:
    """Call ``huggingface_hub.whoami(token=...)`` in a thread pool.

    Returns ``(ok, detail)`` — ``ok=True`` with the username when the
    token is valid; ``ok=False`` with an inline error message otherwise.
    Importing ``huggingface_hub`` lazily so the dashboard doesn't pull
    it in at process start (it's only on the Helios bundle's PYTHONPATH).
    """
    import asyncio

    def _sync() -> tuple[bool, str]:
        try:
            import huggingface_hub  # type: ignore[import-not-found]
        except ImportError:
            return (False, "huggingface_hub not installed in Aegis venv")
        try:
            info = huggingface_hub.whoami(token=token)
        except Exception as exc:  # noqa: BLE001 — surface to UI
            return (False, str(exc))
        name = info.get("name") if isinstance(info, dict) else None
        return (True, str(name or "ok"))

    return await asyncio.to_thread(_sync)


def store_hf_token_in_keychain(token: str) -> bool:
    """Stash the HF token where the Helios daemon will find it.

    The daemon reads from ``service="helios", account="huggingface"``
    (see ``helios/keychain.py``). The wizard must write to the same
    keys — earlier versions used ``("aegis-helios", "hf_token")`` and
    the daemon silently failed to load diarization.

    Returns True on success, False if keyring is unavailable or fails.
    Never logs the token.
    """
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("keyring_not_installed")
        return False
    try:
        keyring.set_password("helios", "huggingface", token)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("keyring_set_failed", extra={"error": str(exc)})
        return False


# ─────────────────────────────────────────────────────────────────────
# Accessibility (AXIsProcessTrusted) check
# ─────────────────────────────────────────────────────────────────────


async def check_accessibility_granted() -> bool:
    """Stub for ``AXIsProcessTrusted`` (HELIOS.md §13.7).

    The daemon doesn't currently expose an endpoint for this check. To
    avoid adding daemon endpoints from Track 6C (out of scope), we
    return False here — the UI then surfaces the "open System Settings"
    deep-link + "Click here when granted" prompt and the user can flip
    the hotkey toggle when they're ready. When Helios adds an endpoint
    this function should call ``HeliosClient.check_accessibility()``.

    Returns ``True`` if Accessibility is granted, ``False`` otherwise.
    """
    return False
