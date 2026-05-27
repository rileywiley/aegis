"""Helios dashboard routes — HELIOS.md §13.

Six server-rendered Jinja2 pages plus their HTMX partials and action
endpoints. All daemon calls go through ``HeliosClient`` (Wave 1) on
``app.state.helios_client``; the dashboard never opens its own httpx
client. When the daemon is unreachable, pages render an "offline" banner
and degrade to Aegis-DB-only views where possible.

Companion files:

* ``aegis/web/routes/_helios_helpers.py`` — speaker-name resolution +
  shared context flags.
* ``aegis/web/templates/helios/`` — base layout + page templates +
  ``_partials/`` for HTMX fragments.

Settings page + HF wizard ship in Track 6C; per-endpoint action result
pills (Critical #1) and test-capture polling (Critical #2) ship in
Wave 4 of Phase 6.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.clients.helios import HeliosClient
from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.models import Meeting
from aegis.db.repositories import (
    MeetingsRepository,
    get_meeting_attendees,
    get_meeting_by_id,
)
from aegis.db.voice_notes_repository import VoiceNotesRepository
from aegis.web import templates
from aegis.web.routes._helios_helpers import resolve_speaker_names
from aegis.web.routes._helios_settings_helpers import (
    HOT_RELOADABLE_LABEL,
    RESTART_REQUIRED_LABEL,
    check_accessibility_granted,
    field_label,
    split_changes_by_reload,
    validate_hf_token,
)
from aegis.web.routes._helios_settings_helpers import (
    _load_toml as _load_capture_toml,
)


# Diarization model list used in step 3.
_DIARIZATION_MODELS: list[dict[str, str]] = [
    {
        "id": "pyannote/segmentation-3.0",
        "url": "https://huggingface.co/pyannote/segmentation-3.0",
        "label": "Segmentation 3.0",
    },
    {
        "id": "pyannote/wespeaker-voxceleb-resnet34-LM",
        "url": "https://huggingface.co/pyannote/wespeaker-voxceleb-resnet34-LM",
        "label": "WeSpeaker (Voxceleb)",
    },
    {
        "id": "pyannote/speaker-diarization-3.1",
        "url": "https://huggingface.co/pyannote/speaker-diarization-3.1",
        "label": "Speaker diarization 3.1",
    },
]

router = APIRouter(prefix="/helios", tags=["helios"])
settings = get_settings()


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.aegis_timezone)


def _current_time_str() -> str:
    return datetime.now(_tz()).strftime("%-I:%M %p %Z")


def _client(request: Request) -> HeliosClient:
    """Return the app-scoped ``HeliosClient`` instance.

    Tests can override by setting ``app.state.helios_client`` to a stub /
    AsyncMock before the request fires.
    """
    return request.app.state.helios_client


async def _enrich_sessions_with_meetings(
    db: AsyncSession, sessions: list[dict]
) -> None:
    """Mutate ``sessions`` in place, adding ``linked_meeting`` to each row.

    Best-effort heuristic: a session is "linked" to a meeting when the
    meeting's [start_time, end_time] window overlaps the session's
    [started_at, ended_at]. The daemon doesn't expose the underlying
    calendar_event_id correlation in the list payload yet, so we fall
    back to time overlap. If multiple meetings overlap, pick the one
    with the largest temporal intersection.
    """
    if not sessions:
        return
    # Compute the smallest UTC window that covers every session in the
    # response so we only make a single DB query.
    starts = [s.get("started_at") for s in sessions if s.get("started_at")]
    ends = [s.get("ended_at") or s.get("started_at") for s in sessions]
    timestamps = [t for t in starts + ends if t]
    if not timestamps:
        return
    window_start = datetime.fromtimestamp(min(timestamps) - 60, tz=timezone.utc)
    window_end = datetime.fromtimestamp(max(timestamps) + 60, tz=timezone.utc)
    repo = MeetingsRepository(db)
    meetings = await repo.get_in_range(window_start, window_end)
    if not meetings:
        return
    for s in sessions:
        s_start = s.get("started_at")
        s_end = s.get("ended_at") or s_start
        if not s_start:
            continue
        best: tuple[float, Meeting] | None = None
        for m in meetings:
            if m.start_time is None or m.end_time is None:
                continue
            m_start = m.start_time.timestamp()
            m_end = m.end_time.timestamp()
            overlap = min(m_end, s_end) - max(m_start, s_start)
            if overlap <= 0:
                continue
            if best is None or overlap > best[0]:
                best = (overlap, m)
        if best is not None:
            s["linked_meeting"] = {
                "id": best[1].id,
                "title": best[1].title,
            }


def _todays_window_utc() -> tuple[datetime, datetime]:
    """[00:00, 23:59:59] local-time bounds expressed in UTC."""
    tz = _tz()
    today_local = datetime.now(tz).date()
    start_local = datetime.combine(today_local, time.min, tzinfo=tz)
    end_local = datetime.combine(today_local, time.max, tzinfo=tz)
    return (
        start_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
    )


# ═════════════════════════════════════════════════════════════════════
# Pages
# ═════════════════════════════════════════════════════════════════════


@router.get("", response_class=HTMLResponse)
async def helios_overview(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Overview / landing page. Composes daemon ``/v1/status`` with
    today's meetings + today's voice notes from Aegis's DB.
    """
    client = _client(request)
    online = await client.health_check()
    status_payload = await client.get_status() if online else None
    permissions = await client.get_permissions() if online else None

    # Aegis-DB enrichment (always available, regardless of daemon state)
    start_utc, end_utc = _todays_window_utc()
    repo = MeetingsRepository(session)
    todays_meetings = await repo.get_in_range(start_utc, end_utc)

    vn_repo = VoiceNotesRepository(session)
    todays_voice_notes = await vn_repo.list_in_range(start_utc, end_utc)

    # Upcoming = next 5 events from now in the next 24h
    now_utc = datetime.now(timezone.utc)
    upcoming = await repo.get_in_range(now_utc, now_utc + timedelta(hours=24))
    upcoming = [m for m in upcoming if m.start_time >= now_utc][:5]

    return templates.TemplateResponse(
        request,
        "helios/overview.html",
        {
            "helios_online": online,
            "status": status_payload,
            "permissions": permissions,
            "todays_meetings": todays_meetings,
            "todays_voice_notes": todays_voice_notes,
            "upcoming": upcoming,
            "tz": _tz(),
            "current_time": _current_time_str(),
        },
    )


@router.get("/overview/status-partial", response_class=HTMLResponse)
async def overview_status_partial(request: Request):
    """HTMX partial polled every 5s by the overview page.

    Returns ONLY the status-pill fragment (no ``<html>`` wrapper).
    """
    client = _client(request)
    online = await client.health_check()
    status_payload = await client.get_status() if online else None
    return templates.TemplateResponse(
        request,
        "helios/_partials/status_pill.html",
        {
            "helios_online": online,
            "status": status_payload,
            "current_time": _current_time_str(),
        },
    )


@router.get("/sessions", response_class=HTMLResponse)
async def helios_sessions(
    request: Request,
    date_filter: str | None = Query(None, alias="date"),
    kind: str | None = Query(None),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_session),
):
    """Sessions list — daemon-only data. Filters pass straight through
    to ``HeliosClient.list_sessions``; the daemon may ignore ``status``
    or ``date`` (Wave 1 flagged these as not yet wired). When the daemon
    is unreachable, render an empty list + offline banner.

    Each session row is enriched with ``linked_meeting`` (Aegis meeting
    title + id) when the session's ``started_at`` overlaps an Aegis
    meeting window. Best-effort: no exact daemon→meeting link exists yet,
    so we match on time-window overlap.
    """
    client = _client(request)
    online = await client.health_check()
    payload = None
    if online:
        payload = await client.list_sessions(
            kind=kind or None,
            status=status or None,
            date=date_filter or None,
            limit=100,
        )
    sessions = (payload or {}).get("sessions", []) or []
    total = (payload or {}).get("total", len(sessions))
    await _enrich_sessions_with_meetings(db, sessions)
    return templates.TemplateResponse(
        request,
        "helios/sessions_list.html",
        {
            "helios_online": online,
            "sessions": sessions,
            "total": total,
            "date_filter": date_filter or "",
            "kind_filter": kind or "",
            "status_filter": status or "",
            "kind_options": ["calendar", "continuous", "manual_screen", "voice_note"],
            "status_options": ["captured", "partial", "no_audio", "failed"],
            "tz": _tz(),
            "current_time": _current_time_str(),
        },
    )


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def helios_session_detail(
    request: Request,
    session_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Session detail with Transcript / OCR / Audio tabs + Actions panel."""
    client = _client(request)
    online = await client.health_check()
    if not online:
        return templates.TemplateResponse(
            request,
            "helios/session_detail.html",
            {
                "helios_online": False,
                "session": None,
                "transcript": None,
                "ocr": None,
                "audio": None,
                "session_id": session_id,
                "speaker_names": {},
                "linked_meeting": None,
                "tz": _tz(),
                "current_time": _current_time_str(),
            },
        )

    session_payload = await client.get_session(session_id)
    if session_payload is None:
        raise HTTPException(status_code=404, detail="Helios session not found")

    transcript = await client.get_session_transcript(session_id)
    ocr = await client.get_ocr(session_id=session_id)
    audio = await client.list_audio(session_id=session_id)

    # Speaker-name resolution (HELIOS.md §16.4). The daemon's
    # SessionResponse doesn't expose ``linked_calendar_event_ids`` yet,
    # so prefer that path when present and fall back to the same
    # time-window overlap matching the sessions-list enrichment uses.
    # Without this fallback every transcript shows raw SPEAKER_00 etc.
    linked_meeting: Meeting | None = None
    linked_event_ids = session_payload.get("linked_calendar_event_ids") or []
    if linked_event_ids:
        from sqlalchemy import select

        stmt = select(Meeting).where(
            Meeting.calendar_event_id.in_(linked_event_ids)
        )
        result = await session.execute(stmt)
        linked_meeting = result.scalars().first()
    else:
        s_start = session_payload.get("started_at")
        s_end = session_payload.get("ended_at") or s_start
        if s_start:
            window_start = datetime.fromtimestamp(
                s_start - 60, tz=timezone.utc
            )
            window_end = datetime.fromtimestamp(
                (s_end or s_start) + 60, tz=timezone.utc
            )
            repo = MeetingsRepository(session)
            candidates = await repo.get_in_range(window_start, window_end)
            best: tuple[float, Meeting] | None = None
            for m in candidates:
                if m.start_time is None or m.end_time is None:
                    continue
                m_start = m.start_time.timestamp()
                m_end = m.end_time.timestamp()
                overlap = min(m_end, s_end or s_start) - max(m_start, s_start)
                if overlap <= 0:
                    continue
                if best is None or overlap > best[0]:
                    best = (overlap, m)
            if best is not None:
                linked_meeting = best[1]

    speaker_names: dict[str, str] = {}
    if linked_meeting is not None:
        attendees = await get_meeting_attendees(session, linked_meeting.id)
        speaker_names = resolve_speaker_names(
            transcript, linked_meeting, attendees
        )

    return templates.TemplateResponse(
        request,
        "helios/session_detail.html",
        {
            "helios_online": True,
            "session": session_payload,
            "transcript": transcript,
            "ocr": ocr,
            "audio": audio,
            "session_id": session_id,
            "speaker_names": speaker_names,
            "linked_meeting": linked_meeting,
            "tz": _tz(),
            "current_time": _current_time_str(),
        },
    )


@router.get("/sessions/{session_id}/audio/{chunk_id}")
async def helios_audio_proxy(
    request: Request,
    session_id: int,
    chunk_id: int,
) -> Response:
    """Proxy a WAV chunk from the daemon to the browser.

    The browser's ``<audio>`` element can't send the bearer header to
    Helios. We fetch the bytes here (with auth) and re-serve them to
    the user. Daemon is localhost-only so the extra hop costs ~nothing.
    """
    client = _client(request)
    result = await client.stream_audio_chunk(
        session_id=session_id, chunk_id=chunk_id
    )
    if result is None:
        raise HTTPException(status_code=502, detail="Helios unreachable")
    status_code, body, content_type = result
    if status_code != 200:
        raise HTTPException(status_code=status_code, detail="Audio fetch failed")
    return Response(content=body, media_type=content_type)


@router.get("/calendar", response_class=HTMLResponse)
async def helios_calendar(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """7-day forward view of meetings with per-meeting tri-state toggle.

    The calendar page is DB-only — it works even when the daemon is down
    (the toggle just changes Aegis state; the daemon picks it up on the
    next /api/meetings/upcoming poll).
    """
    client = _client(request)
    online = await client.health_check()

    now_utc = datetime.now(timezone.utc)
    end_utc = now_utc + timedelta(days=7)
    repo = MeetingsRepository(session)
    meetings = await repo.get_in_range(now_utc, end_utc)

    return templates.TemplateResponse(
        request,
        "helios/calendar.html",
        {
            "helios_online": online,
            "meetings": meetings,
            "tz": _tz(),
            "current_time": _current_time_str(),
        },
    )


@router.post("/calendar/{meeting_id}/exclude")
async def calendar_set_exclude(
    request: Request,
    meeting_id: int,
    value: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Update ``meetings.helios_exclude`` tri-state.

    Accepts form value ``default`` | ``include`` | ``exclude`` mapping to
    NULL / FALSE / TRUE. Returns the refreshed toggle row partial so HTMX
    can swap it in place.
    """
    mapping = {
        "default": None,
        "include": False,
        "exclude": True,
    }
    if value not in mapping:
        raise HTTPException(status_code=400, detail="invalid value")
    meeting = await get_meeting_by_id(session, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")

    new_state = mapping[value]
    from sqlalchemy import update as sa_update

    await session.execute(
        sa_update(Meeting)
        .where(Meeting.id == meeting_id)
        .values(helios_exclude=new_state)
    )
    await session.commit()
    meeting = await get_meeting_by_id(session, meeting_id)
    return templates.TemplateResponse(
        request,
        "helios/_partials/calendar_toggle.html",
        {"meeting": meeting},
    )


@router.get("/diagnostics", response_class=HTMLResponse)
async def helios_diagnostics(request: Request):
    """Full daemon state view."""
    client = _client(request)
    online = await client.health_check()
    diag = await client.get_diagnostics() if online else None
    return templates.TemplateResponse(
        request,
        "helios/diagnostics.html",
        {
            "helios_online": online,
            "diagnostics": diag,
            "tz": _tz(),
            "current_time": _current_time_str(),
        },
    )


def _capture_toml_path() -> Path:
    """Resolve the path to ``capture.toml`` from aegis settings.

    Wrapped in a function so tests can monkeypatch this single seam to
    redirect writes into a ``tmp_path``.
    """
    return Path(settings.helios_token_path).expanduser()


def _settings_view_context(online: bool) -> dict:
    """Shared context for the Settings GET handler.

    Loads the TOML (with defaults filled in via the Pydantic schema),
    plus the wizard step state from ``[diarization.setup_progress]``.
    """
    from aegis.web.routes._helios_settings_helpers import HeliosConfig

    raw = _load_capture_toml(_capture_toml_path())
    # Fill in defaults for any missing sections so the template can
    # render every input without try/except hoops.
    try:
        validated = HeliosConfig(**raw)
        toml_view = validated.model_dump()
    except Exception:  # noqa: BLE001 — fall back to raw if validation fails
        toml_view = raw

    wizard_state = (
        (toml_view.get("diarization") or {}).get("setup_progress") or {}
    )
    return {
        "helios_online": online,
        "toml": toml_view,
        "raw_toml": raw,
        "wizard_state": wizard_state,
        "diarization_models": _DIARIZATION_MODELS,
        "hot_reloadable_label": HOT_RELOADABLE_LABEL,
        "restart_required_label": RESTART_REQUIRED_LABEL,
        "field_label": field_label,
        "tz": _tz(),
        "current_time": _current_time_str(),
    }


@router.get("/settings", response_class=HTMLResponse)
async def helios_settings(request: Request):
    """Settings page — full form + HF wizard.

    Reads ``~/.aegis/capture.toml`` (filling in defaults via the
    duplicated Pydantic schema) and renders every section. The POST
    handler at the same path applies form updates and the wizard
    endpoints under ``/helios/settings/diarization/*`` drive the
    speaker-ID setup flow.
    """
    client = _client(request)
    online = await client.health_check()
    ctx = _settings_view_context(online)
    return templates.TemplateResponse(request, "helios/settings.html", ctx)


@router.post("/settings", response_class=HTMLResponse)
async def helios_settings_post(request: Request):
    """Apply form updates to ``capture.toml`` and re-render the page.

    Always returns 200 with an inline banner — success on apply, error
    on validation failure. Doesn't redirect (HTMX needs the full body
    back for ``hx-swap="outerHTML"`` on the form).
    """
    form = await request.form()
    # Convert MultiDict to a plain dict, collapsing repeats into lists.
    form_dict: dict[str, Any] = {}
    for key in form:
        values = form.getlist(key)
        form_dict[key] = values if len(values) > 1 else values[0]

    path = _capture_toml_path()
    current = _load_capture_toml(path)

    online = await _client(request).health_check()
    banner = {"status": "ok", "message": "", "changed": [], "restart_needed": []}

    # Guard the hotkey toggle. Persisting `voice_note.hotkey_enabled=true`
    # without macOS Accessibility access produces an enabled-but-broken
    # state — the daemon believes the hotkey is wired while the OS
    # silently drops the keypresses. Drop the field from the form
    # payload (the existing TOML value stays unchanged) and surface a
    # warning banner so the user sees why the toggle didn't stick.
    hotkey_blocked = False
    if (
        form_dict.get("voice_note.hotkey_enabled")
        and not (current.get("voice_note") or {}).get("hotkey_enabled")
    ):
        if not await check_accessibility_granted():
            form_dict.pop("voice_note.hotkey_enabled", None)
            hotkey_blocked = True

    try:
        from aegis.web.routes._helios_settings_helpers import apply_form_updates as _apply  # local re-bind for monkeypatch

        new_data, changed = _apply(current, form_dict)
    except Exception as exc:  # noqa: BLE001
        banner = {
            "status": "error",
            "message": f"Validation failed: {exc}",
            "changed": [],
            "restart_needed": [],
        }
        ctx = _settings_view_context(online)
        ctx["banner"] = banner
        return templates.TemplateResponse(request, "helios/settings.html", ctx)

    if changed:
        try:
            from aegis.web.routes import _helios_settings_helpers as _hsh

            _hsh.write_capture_toml(path, new_data)
        except Exception as exc:  # noqa: BLE001
            banner = {
                "status": "error",
                "message": f"Write failed: {exc}",
                "changed": [],
                "restart_needed": [],
            }
            ctx = _settings_view_context(online)
            ctx["banner"] = banner
            return templates.TemplateResponse(request, "helios/settings.html", ctx)

        hot, cold = split_changes_by_reload(changed)
        banner["message"] = (
            f"Saved {len(changed)} change(s)."
            + (
                f" {len(hot)} apply immediately."
                if hot else ""
            )
            + (
                f" {len(cold)} require a daemon restart."
                if cold else ""
            )
        )
        banner["changed"] = [".".join(c) for c in changed]
        banner["restart_needed"] = [".".join(c) for c in cold]
    else:
        banner["message"] = "No changes."

    if hotkey_blocked:
        banner["status"] = "warning"
        suffix = (
            "Hotkey not enabled — grant Accessibility in System Settings → "
            "Privacy & Security, then toggle it again."
        )
        banner["message"] = (
            f"{banner['message']} {suffix}" if banner["message"] else suffix
        )

    ctx = _settings_view_context(online)
    ctx["banner"] = banner
    return templates.TemplateResponse(request, "helios/settings.html", ctx)


@router.post(
    "/settings/hotkey-permission-check", response_class=HTMLResponse
)
async def helios_settings_hotkey_permission_check(request: Request):
    """AX permission probe for the voice-note hotkey toggle.

    Returns an inline HTMX partial: ``granted`` shows a success pill,
    ``not_granted`` shows the System Settings deep-link + "click here
    when granted" form. The actual hotkey enable goes through the main
    POST handler once the user confirms.
    """
    granted = await check_accessibility_granted()
    return templates.TemplateResponse(
        request,
        "helios/_partials/settings_hotkey_permission.html",
        {"granted": granted},
    )


# ═════════════════════════════════════════════════════════════════════
# HuggingFace speaker-ID wizard (HELIOS.md §15.5)
# ═════════════════════════════════════════════════════════════════════


def _wizard_load_state() -> dict[str, Any]:
    raw = _load_capture_toml(_capture_toml_path())
    diar = raw.get("diarization") or {}
    progress = diar.get("setup_progress") or {}
    return progress


def _wizard_save_state(state: dict[str, Any]) -> None:
    path = _capture_toml_path()
    raw = _load_capture_toml(path)
    raw.setdefault("diarization", {})
    raw["diarization"]["setup_progress"] = state
    from aegis.web.routes import _helios_settings_helpers as _hsh

    _hsh.write_capture_toml(path, raw)


def _render_wizard_step(
    request: Request, step: int, *, extra: dict[str, Any] | None = None
) -> HTMLResponse:
    state = _wizard_load_state()
    ctx = {
        "step": step,
        "state": state,
        "models": _DIARIZATION_MODELS,
        **(extra or {}),
    }
    return templates.TemplateResponse(
        request, f"helios/_partials/wizard_step_{step}.html", ctx
    )


@router.get("/settings/diarization/step/{step}", response_class=HTMLResponse)
async def wizard_get_step(request: Request, step: int):
    if step not in (1, 2, 3, 4, 5):
        raise HTTPException(status_code=404)
    return _render_wizard_step(request, step)


@router.post("/settings/diarization/step1", response_class=HTMLResponse)
async def wizard_step1_continue(request: Request):
    state = _wizard_load_state()
    state["step1_completed"] = True
    _wizard_save_state(state)
    return _render_wizard_step(request, 2)


@router.post("/settings/diarization/step2", response_class=HTMLResponse)
async def wizard_step2_continue(request: Request):
    state = _wizard_load_state()
    state["step2_completed"] = True
    _wizard_save_state(state)
    return _render_wizard_step(request, 3)


@router.post("/settings/diarization/step3", response_class=HTMLResponse)
async def wizard_step3_continue(
    request: Request,
):
    """Step 3 — user confirms license acceptance for all 3 models.

    Requires all three model checkboxes to be checked in the submitted
    form. If any are missing, re-render step 3 with an inline error.
    """
    form = await request.form()
    accepted = {
        m["id"]
        for m in _DIARIZATION_MODELS
        if form.get(f"accept.{m['id']}")
    }
    needed = {m["id"] for m in _DIARIZATION_MODELS}
    if accepted != needed:
        return _render_wizard_step(
            request,
            3,
            extra={
                "error": "Please confirm you've accepted all three model licenses.",
                "accepted": list(accepted),
            },
        )
    state = _wizard_load_state()
    state["step3_completed"] = True
    state["licenses_accepted"] = sorted(accepted)
    _wizard_save_state(state)
    return _render_wizard_step(request, 4)


@router.post(
    "/settings/diarization/validate-token", response_class=HTMLResponse
)
async def wizard_validate_token(request: Request):
    """Step 4 — validate the pasted HF token via ``whoami``.

    Returns an inline result partial (success → advance button shown;
    error → message + retry form). The token itself is NOT persisted
    here — step 5 takes care of keychain storage after the user clicks
    "Download models".
    """
    form = await request.form()
    token = (form.get("hf_token") or "").strip()
    if not token:
        return templates.TemplateResponse(
            request,
            "helios/_partials/wizard_step_4_result.html",
            {"ok": False, "detail": "Paste a token first."},
        )
    ok, detail = await validate_hf_token(token)
    if ok:
        state = _wizard_load_state()
        state["step4_completed"] = True
        state["token_username"] = detail
        _wizard_save_state(state)
    return templates.TemplateResponse(
        request,
        "helios/_partials/wizard_step_4_result.html",
        {"ok": ok, "detail": detail, "token": token if ok else ""},
    )


@router.post("/settings/diarization/reset", response_class=HTMLResponse)
async def wizard_reset(request: Request):
    """Clear the wizard's setup_progress so the user can re-run the flow.

    Returns the step 1 fragment so the wizard immediately re-mounts. The
    HF token stays in the keychain — only the TOML state is cleared.
    """
    path = _capture_toml_path()
    raw = _load_capture_toml(path)
    raw.setdefault("diarization", {})
    raw["diarization"]["setup_progress"] = {}
    from aegis.web.routes import _helios_settings_helpers as _hsh

    _hsh.write_capture_toml(path, raw)
    return _render_wizard_step(request, 1)


@router.post(
    "/settings/diarization/download-models", response_class=HTMLResponse
)
async def wizard_download_models(request: Request):
    """Step 5 — persist token in keychain, flip ``diarization.enabled``,
    and ask the daemon to reload its diarization component.

    The actual model download happens inside the daemon's diarization
    worker on next init; this endpoint just sets up the prerequisites
    and surfaces the reload result.
    """
    form = await request.form()
    token = (form.get("hf_token") or "").strip()
    if not token:
        return templates.TemplateResponse(
            request,
            "helios/_partials/wizard_step_5_result.html",
            {"ok": False, "detail": "Token missing — re-validate in step 4."},
        )

    # Re-bind helper imports through module so monkeypatching works.
    from aegis.web.routes import _helios_settings_helpers as _hsh

    keychain_ok = _hsh.store_hf_token_in_keychain(token)
    if not keychain_ok:
        return templates.TemplateResponse(
            request,
            "helios/_partials/wizard_step_5_result.html",
            {
                "ok": False,
                "detail": (
                    "Could not store token in macOS keychain. "
                    "Check that the ``keyring`` package is installed."
                ),
            },
        )

    # Update capture.toml: enable diarization + set token location.
    path = _capture_toml_path()
    raw = _load_capture_toml(path)
    raw.setdefault("diarization", {})
    raw["diarization"]["enabled"] = True
    raw["diarization"]["hf_token_location"] = "keychain"
    state = raw["diarization"].get("setup_progress") or {}
    state["step5_completed"] = True
    raw["diarization"]["setup_progress"] = state
    try:
        _hsh.write_capture_toml(path, raw)
    except Exception as exc:  # noqa: BLE001
        return templates.TemplateResponse(
            request,
            "helios/_partials/wizard_step_5_result.html",
            {"ok": False, "detail": f"Failed to update config: {exc}"},
        )

    # Ask the daemon to reload its diarization component.
    reload_payload = await _client(request).reload_component("diarization")
    if reload_payload is None:
        return templates.TemplateResponse(
            request,
            "helios/_partials/wizard_step_5_result.html",
            {
                "ok": True,
                "detail": (
                    "Token saved, config updated. Daemon unreachable — "
                    "diarization will activate on next daemon start."
                ),
            },
        )
    # Track 6D ``ReloadComponentResponse`` shape: {component, ok, detail}.
    ok = bool(reload_payload.get("ok", False))
    return templates.TemplateResponse(
        request,
        "helios/_partials/wizard_step_5_result.html",
        {
            "ok": ok,
            "detail": reload_payload.get("detail") or (
                "Diarization reloaded." if ok else "Daemon reported failure."
            ),
        },
    )


# ═════════════════════════════════════════════════════════════════════
# Action endpoints (proxy to Helios daemon — see HeliosClient methods)
# ═════════════════════════════════════════════════════════════════════


def _render_action_pill(
    request: Request,
    *,
    kind: str,
    headline: str,
    detail: str = "",
    download_url: str | None = None,
) -> HTMLResponse:
    """Render the generic action pill partial.

    ``kind`` drives the colour palette in the template:
    ``success`` (green), ``error`` (red), ``warning`` (amber),
    ``pending`` (aegis blue, pulsing), ``unreachable`` (red).
    """
    return templates.TemplateResponse(
        request,
        "helios/_partials/action_pill.html",
        {
            "kind": kind,
            "headline": headline,
            "detail": detail,
            "download_url": download_url,
        },
    )


def _unreachable_pill(request: Request) -> HTMLResponse:
    return _render_action_pill(
        request, kind="unreachable", headline="Daemon unreachable"
    )


# ── Per-endpoint action handlers ────────────────────────────────────


@router.post("/diagnostics/actions/restart")
async def diagnostics_restart(request: Request):
    payload = await _client(request).restart_daemon()
    if payload is None:
        return _unreachable_pill(request)
    # ``restart_daemon`` still returns ``DiagnosticsAcceptedResponse``:
    # {status: "queued", detail: ...}.
    status_label = payload.get("status", "queued")
    detail = payload.get("detail") or ""
    kind = "success" if status_label in ("queued", "ok", "accepted") else "warning"
    return _render_action_pill(
        request,
        kind=kind,
        headline=f"Restart {status_label}",
        detail=detail,
    )


@router.post("/diagnostics/actions/flush-queues")
async def diagnostics_flush(request: Request):
    payload = await _client(request).flush_queues()
    if payload is None:
        return _unreachable_pill(request)
    tx = int(payload.get("transcription_flushed", 0) or 0)
    di = int(payload.get("diarization_flushed", 0) or 0)
    return _render_action_pill(
        request,
        kind="success",
        headline=f"Flushed {tx} transcription / {di} diarization",
        detail="Queues drained.",
    )


@router.post("/diagnostics/actions/test-capture")
async def diagnostics_test_capture(request: Request):
    """Start a 60-second self-test. Returns a pill that polls the
    daemon's per-job status endpoint until the run terminates.
    """
    payload = await _client(request).test_capture()
    if payload is None:
        return _unreachable_pill(request)
    job_id = payload.get("job_id")
    status_label = payload.get("status", "queued")
    return templates.TemplateResponse(
        request,
        "helios/_partials/action_pill_test_capture.html",
        {
            "job_id": job_id,
            "status": status_label,
            "steps": [],
            "session_id": None,
        },
    )


@router.get("/diagnostics/test-capture/{job_id}", response_class=HTMLResponse)
async def diagnostics_test_capture_poll(request: Request, job_id: str):
    """HTMX poll proxy for the test-capture job status (Critical #2).

    Renders ``action_pill_test_capture.html`` again with the current
    status + step list. When the job is in a terminal state the
    template omits the poll trigger so HTMX stops by itself.
    """
    payload = await _client(request).get_test_capture_status(job_id)
    if payload is None:
        return _unreachable_pill(request)
    return templates.TemplateResponse(
        request,
        "helios/_partials/action_pill_test_capture.html",
        {
            "job_id": job_id,
            "status": payload.get("status", "queued"),
            "steps": payload.get("steps", []) or [],
            "session_id": payload.get("session_id"),
        },
    )


@router.post("/diagnostics/actions/reload-component")
async def diagnostics_reload_component(
    request: Request,
    component: str = Form(...),
):
    payload = await _client(request).reload_component(component)
    if payload is None:
        return _unreachable_pill(request)
    ok = bool(payload.get("ok", False))
    name = payload.get("component", component)
    detail = payload.get("detail") or ""
    return _render_action_pill(
        request,
        kind="success" if ok else "error",
        headline=f"Reloaded {name}" if ok else f"Reload failed: {name}",
        detail=detail,
    )


@router.post("/diagnostics/actions/bundle")
async def diagnostics_create_bundle(request: Request):
    payload = await _client(request).create_diagnostics_bundle()
    if payload is None:
        return _unreachable_pill(request)
    filename = payload.get("filename") or "bundle.tar.gz"
    size = int(payload.get("size_bytes", 0) or 0)
    size_mb = size / (1024 * 1024) if size else 0
    detail = f"{size_mb:.1f} MB" if size else "ready"
    return _render_action_pill(
        request,
        kind="success",
        headline=f"Bundle ready: {filename}",
        detail=detail,
        download_url=payload.get("download_url"),
    )


@router.post("/sessions/{session_id}/re-transcribe")
async def session_re_transcribe(request: Request, session_id: int):
    payload = await _client(request).re_transcribe_session(session_id)
    if payload is None:
        return _unreachable_pill(request)
    n = int(payload.get("chunks_requeued", 0) or 0)
    sid = payload.get("session_id", session_id)
    return _render_action_pill(
        request,
        kind="success" if n > 0 else "warning",
        headline=f"Re-transcribe queued: {n} chunk(s)",
        detail=f"session #{sid}",
    )


@router.post("/sessions/{session_id}/re-diarize")
async def session_re_diarize(request: Request, session_id: int):
    payload = await _client(request).re_diarize_session(session_id)
    if payload is None:
        return _unreachable_pill(request)
    n = int(payload.get("jobs_requeued", 0) or 0)
    sid = payload.get("session_id", session_id)
    if n == 0:
        return _render_action_pill(
            request,
            kind="error",
            headline="Diarization unavailable",
            detail="Component disabled or not configured.",
        )
    return _render_action_pill(
        request,
        kind="success",
        headline=f"Re-diarize queued: {n} job(s)",
        detail=f"session #{sid}",
    )


@router.delete("/sessions/{session_id}")
async def session_delete(request: Request, session_id: int):
    payload = await _client(request).delete_session(session_id)
    if payload is None:
        return _unreachable_pill(request)
    # delete_session may return {} (204) or a small envelope; treat any
    # 2xx-derived response as success.
    return _render_action_pill(
        request,
        kind="success",
        headline=f"Deleted session #{session_id}",
        detail=payload.get("detail", "") or "",
    )
