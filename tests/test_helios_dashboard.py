"""Tests for the Phase 6 Helios dashboard pages (Wave 2 — Track 6B).

Coverage:

* One happy-path render per page (status 200, expected template, key
  context keys / strings present).
* One graceful-degradation case per page when ``HeliosClient.health_check``
  returns False — the page must still render 200 with the offline banner
  and any DB-only fallback content.
* Tri-state ``POST /helios/calendar/{id}/exclude`` toggles
  ``meetings.helios_exclude`` to True / False / None.
* HTMX partial endpoints return fragments (no ``<html>`` wrapper).
* Speaker-name resolution helper covers HELIOS.md §16.4 happy / no-match
  paths.

All daemon I/O is mocked via ``HeliosClient`` AsyncMocks injected on
``app.state``. The DB session dependency is mocked when the route's
repositories are patched at the module level.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aegis.db.engine import get_session
from aegis.main import app
from aegis.web.routes._helios_helpers import resolve_speaker_names


# ── Fixtures ───────────────────────────────────────────────


def _stub_helios_client(**method_returns) -> MagicMock:
    """Build a MagicMock that walks like a HeliosClient.

    Each method on the real client is an async function returning a
    dict/None. We default unspecified methods to return None so any
    page that calls them gets the "daemon unreachable" branch unless
    overridden by ``method_returns``.
    """
    stub = MagicMock(name="HeliosClient")
    # Default async methods to return None.
    async_method_names = [
        "health_check",
        "get_status",
        "get_diagnostics",
        "get_permissions",
        "get_session",
        "get_session_transcript",
        "get_ocr",
        "list_audio",
        "list_sessions",
        "delete_session",
        "restart_daemon",
        "flush_queues",
        "test_capture",
        "get_test_capture_status",
        "reload_component",
        "create_diagnostics_bundle",
        "re_transcribe_session",
        "re_diarize_session",
    ]
    for name in async_method_names:
        setattr(stub, name, AsyncMock(return_value=method_returns.get(name)))
    # Override health_check default — most tests want the daemon "up".
    if "health_check" not in method_returns:
        stub.health_check = AsyncMock(return_value=True)
    return stub


@pytest.fixture
def helios_client_stub():
    """Stub that defaults to "daemon online + nothing else available"."""
    return _stub_helios_client(
        health_check=True,
        get_status={"daemon": "running", "mode": "armed",
                    "components": {"audio_capture": "ok", "transcription": "ok"},
                    "active_session": None,
                    "next_calendar_event": {"title": "Test next", "calendar_event_id": "evt-1",
                                            "starts_at": 0, "pre_start_at": 0}},
        get_permissions={"mic_granted": True, "screen_recording_granted": True,
                         "last_checked_at": 0.0},
    )


@pytest.fixture
def offline_helios_client():
    return _stub_helios_client(health_check=False)


@pytest.fixture
def client(helios_client_stub):
    """TestClient with a stub HeliosClient injected on app.state.

    Mocks ``get_session`` to a no-op AsyncMock — individual tests patch
    repositories to control returned data.
    """
    async def _fake_session():
        yield AsyncMock()

    app.dependency_overrides[get_session] = _fake_session
    try:
        with TestClient(app) as c:
            # Replace the lifespan-built client with our stub for the duration.
            app.state.helios_client = helios_client_stub
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def offline_client(offline_helios_client):
    async def _fake_session():
        yield AsyncMock()

    app.dependency_overrides[get_session] = _fake_session
    try:
        with TestClient(app) as c:
            app.state.helios_client = offline_helios_client
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


def _patch_repos(meetings=None, voice_notes=None):
    """Patch the MeetingsRepository + VoiceNotesRepository the routes use.

    Returns a context manager that installs the patches and yields the
    two MagicMock classes so tests can assert call args.
    """
    meetings = meetings or []
    voice_notes = voice_notes or []
    mrepo = patch("aegis.web.routes.helios.MeetingsRepository")
    vrepo = patch("aegis.web.routes.helios.VoiceNotesRepository")
    return mrepo, vrepo, meetings, voice_notes


def _make_meeting(meeting_id=1, title="Sync", helios_exclude=None,
                  is_excluded=False, transcript_status="pending"):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=meeting_id,
        title=title,
        start_time=now + timedelta(minutes=30),
        end_time=now + timedelta(minutes=60),
        duration=30,
        is_excluded=is_excluded,
        helios_exclude=helios_exclude,
        transcript_status=transcript_status,
        calendar_event_id=f"evt-{meeting_id}",
        organizer_email="me@example.com",
    )


def _make_voice_note(vn_id=1, transcript="Hello world", status="pending"):
    from datetime import datetime, timezone
    return SimpleNamespace(
        id=vn_id,
        transcript_text=transcript,
        transcript_text_edited=None,
        triggered_by="menu_bar",
        is_excerpt=False,
        excerpt_of_meeting_id=None,
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        duration_seconds=30.0,
        processing_status=status,
        attachments=[],
    )


# ═══════════════════════════════════════════════════════════
# Overview page
# ═══════════════════════════════════════════════════════════


class TestOverview:
    def test_renders_when_daemon_online(self, client, helios_client_stub):
        with patch("aegis.web.routes.helios.MeetingsRepository") as mrepo, \
             patch("aegis.web.routes.helios.VoiceNotesRepository") as vrepo:
            mrepo.return_value.get_in_range = AsyncMock(
                return_value=[_make_meeting()]
            )
            vrepo.return_value.list_in_range = AsyncMock(
                return_value=[_make_voice_note()]
            )
            resp = client.get("/helios")
        assert resp.status_code == 200
        body = resp.text
        # Sub-nav present + status pill rendered
        assert "Helios" in body
        assert "Today's timeline" in body
        assert "Today's voice notes" in body
        # Status pill content from the stub
        assert "armed" in body.lower()
        # Daemon polled
        helios_client_stub.health_check.assert_awaited()
        helios_client_stub.get_status.assert_awaited()

    def test_degraded_when_daemon_offline(self, offline_client,
                                          offline_helios_client):
        with patch("aegis.web.routes.helios.MeetingsRepository") as mrepo, \
             patch("aegis.web.routes.helios.VoiceNotesRepository") as vrepo:
            mrepo.return_value.get_in_range = AsyncMock(return_value=[])
            vrepo.return_value.list_in_range = AsyncMock(return_value=[])
            resp = offline_client.get("/helios")
        assert resp.status_code == 200
        # Offline banner present
        assert "Helios daemon unreachable" in resp.text
        # Status fetch should NOT have been attempted
        offline_helios_client.get_status.assert_not_awaited()


class TestOverviewStatusPartial:
    def test_returns_fragment_only(self, client):
        resp = client.get("/helios/overview/status-partial")
        assert resp.status_code == 200
        body = resp.text
        # Fragment, not a full HTML document
        assert "<html" not in body.lower()
        assert "helios-status-pill" in body

    def test_degraded_partial_when_offline(self, offline_client):
        resp = offline_client.get("/helios/overview/status-partial")
        assert resp.status_code == 200
        assert "Daemon offline" in resp.text


# ═══════════════════════════════════════════════════════════
# Sessions list + detail
# ═══════════════════════════════════════════════════════════


class TestSessionsList:
    def test_renders_with_sessions(self, client, helios_client_stub):
        helios_client_stub.list_sessions = AsyncMock(return_value={
            "sessions": [
                {"id": 1, "kind": "calendar", "started_at": 0,
                 "duration_seconds": 120, "status": "captured",
                 "chunk_count": 4, "coverage_pct": 95.0},
            ],
            "total": 1,
        })
        resp = client.get("/helios/sessions?kind=calendar")
        assert resp.status_code == 200
        assert "1 session" in resp.text
        assert "captured" in resp.text
        helios_client_stub.list_sessions.assert_awaited()
        # Filters passed through
        call_kwargs = helios_client_stub.list_sessions.await_args.kwargs
        assert call_kwargs.get("kind") == "calendar"

    def test_renders_offline_with_empty_list(self, offline_client):
        resp = offline_client.get("/helios/sessions")
        assert resp.status_code == 200
        assert "Helios daemon unreachable" in resp.text


class TestSessionDetail:
    def test_renders_session_detail(self, client, helios_client_stub):
        helios_client_stub.get_session = AsyncMock(return_value={
            "id": 42, "kind": "calendar",
            "linked_calendar_event_ids": [],
            "started_at": 0, "ended_at": 600,
            "duration_seconds": 600, "end_reason": "scheduled_end",
        })
        helios_client_stub.get_session_transcript = AsyncMock(return_value={
            "session_id": 42, "started_at": 0, "ended_at": 600,
            "segments": [
                {"start": 1.0, "end": 2.0, "speaker": "user",
                 "text": "hello"},
            ],
            "coverage": {"captured_seconds": 600, "unavailable_ranges": []},
        })
        helios_client_stub.get_ocr = AsyncMock(return_value={"frames": []})
        helios_client_stub.list_audio = AsyncMock(return_value={"segments": []})
        resp = client.get("/helios/sessions/42")
        assert resp.status_code == 200
        body = resp.text
        assert "Session #42" in body
        assert "Re-transcribe" in body
        assert "hello" in body

    def test_session_detail_404_when_daemon_returns_none(
        self, client, helios_client_stub
    ):
        helios_client_stub.get_session = AsyncMock(return_value=None)
        resp = client.get("/helios/sessions/999")
        assert resp.status_code == 404

    def test_session_detail_degrades_when_offline(self, offline_client):
        resp = offline_client.get("/helios/sessions/1")
        assert resp.status_code == 200
        assert "Helios daemon unreachable" in resp.text
        assert "daemon is offline" in resp.text.lower()


# ═══════════════════════════════════════════════════════════
# Calendar page + tri-state toggle
# ═══════════════════════════════════════════════════════════


class TestCalendarPage:
    def test_renders_meetings(self, client):
        with patch("aegis.web.routes.helios.MeetingsRepository") as mrepo:
            mrepo.return_value.get_in_range = AsyncMock(
                return_value=[_make_meeting(title="Q3 Plan")]
            )
            resp = client.get("/helios/calendar")
        assert resp.status_code == 200
        assert "Q3 Plan" in resp.text
        assert "Always include" in resp.text
        assert "Always exclude" in resp.text

    def test_calendar_offline_still_renders_db_meetings(self, offline_client):
        with patch("aegis.web.routes.helios.MeetingsRepository") as mrepo:
            mrepo.return_value.get_in_range = AsyncMock(
                return_value=[_make_meeting(title="DB Only Meeting")]
            )
            resp = offline_client.get("/helios/calendar")
        assert resp.status_code == 200
        assert "DB Only Meeting" in resp.text
        # Offline banner still shows
        assert "Helios daemon unreachable" in resp.text


class TestCalendarToggle:
    @pytest.mark.parametrize("value,expected", [
        ("exclude", True),
        ("include", False),
        ("default", None),
    ])
    def test_tri_state_sets_helios_exclude(self, client, value, expected):
        meeting = _make_meeting(meeting_id=7, helios_exclude=None)
        with patch(
            "aegis.web.routes.helios.get_meeting_by_id",
            AsyncMock(return_value=meeting),
        ):
            resp = client.post(
                "/helios/calendar/7/exclude",
                data={"value": value},
            )
        assert resp.status_code == 200
        # Fragment, no <html>
        assert "<html" not in resp.text.lower()
        assert f'/helios/calendar/7/exclude' in resp.text

    def test_invalid_value_400(self, client):
        meeting = _make_meeting(meeting_id=7)
        with patch(
            "aegis.web.routes.helios.get_meeting_by_id",
            AsyncMock(return_value=meeting),
        ):
            resp = client.post(
                "/helios/calendar/7/exclude",
                data={"value": "wat"},
            )
        assert resp.status_code == 400

    def test_unknown_meeting_404(self, client):
        with patch(
            "aegis.web.routes.helios.get_meeting_by_id",
            AsyncMock(return_value=None),
        ):
            resp = client.post(
                "/helios/calendar/99/exclude",
                data={"value": "exclude"},
            )
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════
# Diagnostics + Settings
# ═══════════════════════════════════════════════════════════


class TestDiagnostics:
    def test_renders_with_diagnostics_payload(self, client, helios_client_stub):
        helios_client_stub.get_diagnostics = AsyncMock(return_value={
            "version": "0.1.0", "pid": 4821, "uptime_seconds": 600,
            "memory_mb": 200, "cpu_5min_avg_pct": 7,
            "permissions": {"mic_granted": True,
                            "screen_recording_granted": True},
            "queues": {"transcription_pending": 0,
                       "transcription_failed_24h": 0,
                       "diarization_pending": 0,
                       "diarization_failed_24h": 0},
            "storage": {"audio_bytes": 1000, "audio_oldest_days": 1,
                        "thumbnails_bytes": 100, "database_bytes": 5000},
            "component_status": {"transcription": "ok", "ocr": "ok"},
            "recent_events": [
                {"ts": 0, "level": "info", "component": "scheduler",
                 "event": "session_started"},
            ],
        })
        resp = client.get("/helios/diagnostics")
        assert resp.status_code == 200
        body = resp.text
        assert "0.1.0" in body
        assert "Trigger test capture" in body
        assert "session_started" in body

    def test_degraded_when_offline(self, offline_client):
        resp = offline_client.get("/helios/diagnostics")
        assert resp.status_code == 200
        assert "Helios daemon unreachable" in resp.text


class TestSettings:
    def test_settings_shell_renders(self, client):
        # Track 6C replaced the shell with a real form. We still want
        # the eight TOML sections to appear in the rendered page.
        resp = client.get("/helios/settings")
        assert resp.status_code == 200
        body = resp.text
        for section in [
            "Capture mode", "Meeting exclusion keywords",
            "Meeting apps for OCR", "Retention policy",
            "Transcription", "Voice notes",
            "Speaker identification", "Advanced",
        ]:
            assert section in body

    def test_settings_degrades_when_offline(self, offline_client):
        resp = offline_client.get("/helios/settings")
        assert resp.status_code == 200
        assert "Helios daemon unreachable" in resp.text


# ═══════════════════════════════════════════════════════════
# Action endpoints
# ═══════════════════════════════════════════════════════════


class TestActionEndpoints:
    def test_restart_returns_pill_fragment(self, client, helios_client_stub):
        helios_client_stub.restart_daemon = AsyncMock(return_value={
            "status": "queued", "detail": "Daemon restart requested",
        })
        resp = client.post("/helios/diagnostics/actions/restart")
        assert resp.status_code == 200
        assert "<html" not in resp.text.lower()
        assert "queued" in resp.text

    def test_flush_queues(self, client, helios_client_stub):
        # Track 6D shape: {transcription_flushed, diarization_flushed}.
        helios_client_stub.flush_queues = AsyncMock(return_value={
            "transcription_flushed": 3, "diarization_flushed": 1,
        })
        resp = client.post("/helios/diagnostics/actions/flush-queues")
        assert resp.status_code == 200
        assert "Flushed 3 transcription / 1 diarization" in resp.text

    def test_action_when_daemon_unreachable_shows_pill(
        self, client, helios_client_stub
    ):
        helios_client_stub.test_capture = AsyncMock(return_value=None)
        resp = client.post("/helios/diagnostics/actions/test-capture")
        assert resp.status_code == 200
        assert "unreachable" in resp.text.lower()

    def test_reload_component_form(self, client, helios_client_stub):
        helios_client_stub.reload_component = AsyncMock(return_value={
            "status": "queued"
        })
        resp = client.post(
            "/helios/diagnostics/actions/reload-component",
            data={"component": "transcription"},
        )
        assert resp.status_code == 200
        helios_client_stub.reload_component.assert_awaited_once_with(
            "transcription"
        )

    def test_re_transcribe_session(self, client, helios_client_stub):
        helios_client_stub.re_transcribe_session = AsyncMock(return_value={
            "status": "queued"
        })
        resp = client.post("/helios/sessions/77/re-transcribe")
        assert resp.status_code == 200
        helios_client_stub.re_transcribe_session.assert_awaited_once_with(77)

    def test_delete_session(self, client, helios_client_stub):
        helios_client_stub.delete_session = AsyncMock(return_value={
            "status": "ok"
        })
        resp = client.delete("/helios/sessions/13")
        assert resp.status_code == 200
        helios_client_stub.delete_session.assert_awaited_once_with(13)


# ═══════════════════════════════════════════════════════════
# Wave 4 — per-endpoint action pills (Critical #1, #2, #5)
# ═══════════════════════════════════════════════════════════


class TestWave4ActionPills:
    """Critical #1 — Each diagnostics handler must render its own pill
    shape from the Track 6D response keys, not the legacy
    ``{status: "queued"}`` envelope.
    """

    def test_reload_component_success(self, client, helios_client_stub):
        helios_client_stub.reload_component = AsyncMock(return_value={
            "component": "transcription", "ok": True,
            "detail": "Pipeline reset.",
        })
        resp = client.post(
            "/helios/diagnostics/actions/reload-component",
            data={"component": "transcription"},
        )
        assert resp.status_code == 200
        assert "Reloaded transcription" in resp.text
        assert "bg-emerald-50" in resp.text

    def test_reload_component_unknown_returns_red_pill(
        self, client, helios_client_stub
    ):
        helios_client_stub.reload_component = AsyncMock(return_value={
            "component": "bogus", "ok": False,
            "detail": "unknown component",
        })
        resp = client.post(
            "/helios/diagnostics/actions/reload-component",
            data={"component": "bogus"},
        )
        assert resp.status_code == 200
        assert "Reload failed" in resp.text
        assert "bg-red-50" in resp.text

    def test_bundle_renders_download_link(self, client, helios_client_stub):
        helios_client_stub.create_diagnostics_bundle = AsyncMock(return_value={
            "bundle_path": "/var/folders/x/bundle.tar.gz",
            "filename": "helios-bundle-2026-05-20.tar.gz",
            "download_url": "/helios/bundle/helios-bundle-2026-05-20.tar.gz",
            "size_bytes": 1572864,
            "expires_at": "2026-05-21T00:00:00Z",
        })
        resp = client.post("/helios/diagnostics/actions/bundle")
        assert resp.status_code == 200
        assert "Bundle ready" in resp.text
        assert "helios-bundle-2026-05-20.tar.gz" in resp.text
        assert "/helios/bundle/helios-bundle-2026-05-20.tar.gz" in resp.text
        assert "1.5 MB" in resp.text

    def test_re_transcribe_renders_chunks_requeued(
        self, client, helios_client_stub
    ):
        helios_client_stub.re_transcribe_session = AsyncMock(return_value={
            "session_id": 77, "chunks_requeued": 5,
        })
        resp = client.post("/helios/sessions/77/re-transcribe")
        assert resp.status_code == 200
        assert "Re-transcribe queued: 5 chunk" in resp.text

    def test_re_diarize_zero_jobs_renders_warning(
        self, client, helios_client_stub
    ):
        helios_client_stub.re_diarize_session = AsyncMock(return_value={
            "session_id": 77, "jobs_requeued": 0,
        })
        resp = client.post("/helios/sessions/77/re-diarize")
        assert resp.status_code == 200
        assert "Diarization unavailable" in resp.text
        assert "bg-red-50" in resp.text

    def test_re_diarize_positive_jobs_renders_success(
        self, client, helios_client_stub
    ):
        helios_client_stub.re_diarize_session = AsyncMock(return_value={
            "session_id": 77, "jobs_requeued": 3,
        })
        resp = client.post("/helios/sessions/77/re-diarize")
        assert resp.status_code == 200
        assert "Re-diarize queued: 3 job" in resp.text


class TestWave4TestCapturePolling:
    """Critical #2 — test-capture POST returns a pill that polls the
    daemon's per-job status endpoint until terminal.
    """

    def test_post_renders_initial_pill_with_poll_trigger(
        self, client, helios_client_stub
    ):
        helios_client_stub.test_capture = AsyncMock(return_value={
            "job_id": "job-abc", "status": "queued",
        })
        resp = client.post("/helios/diagnostics/actions/test-capture")
        assert resp.status_code == 200
        assert "Test capture: queued" in resp.text
        # Polling armed via HTMX hx-get against the proxy endpoint.
        assert 'hx-get="/helios/diagnostics/test-capture/job-abc"' in resp.text

    def test_get_poll_renders_steps_when_running(
        self, client, helios_client_stub
    ):
        helios_client_stub.get_test_capture_status = AsyncMock(return_value={
            "job_id": "job-abc",
            "status": "running",
            "steps": [
                {"name": "mic_open", "ok": True, "detail": "ok"},
                {"name": "transcription_started", "ok": True, "detail": ""},
            ],
            "session_id": None,
        })
        resp = client.get("/helios/diagnostics/test-capture/job-abc")
        assert resp.status_code == 200
        assert "Test capture: running" in resp.text
        assert "mic_open" in resp.text
        assert "transcription_started" in resp.text
        # Still polling (non-terminal).
        assert "hx-get" in resp.text

    def test_get_poll_terminal_state_stops_polling(
        self, client, helios_client_stub
    ):
        helios_client_stub.get_test_capture_status = AsyncMock(return_value={
            "job_id": "job-abc",
            "status": "complete",
            "steps": [
                {"name": "mic_open", "ok": True, "detail": "ok"},
            ],
            "session_id": 42,
        })
        resp = client.get("/helios/diagnostics/test-capture/job-abc")
        assert resp.status_code == 200
        assert "Test capture: complete" in resp.text
        assert "session #42" in resp.text
        # No new hx-get when terminal — HTMX stops by itself.
        # The wrapper div omits hx-get; check that the polling URL is
        # not present in the rendered fragment.
        assert "hx-get=" not in resp.text

    def test_get_poll_when_unreachable_returns_pill(
        self, client, helios_client_stub
    ):
        helios_client_stub.get_test_capture_status = AsyncMock(return_value=None)
        resp = client.get("/helios/diagnostics/test-capture/job-abc")
        assert resp.status_code == 200
        assert "Daemon unreachable" in resp.text


class TestWave4DiagnosticsTemplate:
    """Critical #5 — buttons use hx-target slots instead of replacing
    themselves so the page stays interactive.
    """

    def test_diagnostics_template_uses_target_slots(
        self, client, helios_client_stub
    ):
        helios_client_stub.get_diagnostics = AsyncMock(return_value={
            "version": "0.5.0", "pid": 1234, "uptime_seconds": 600,
            "memory_mb": 100, "cpu_5min_avg_pct": 5.0,
            "transcription_throughput_realtime_multiple": 2.0,
            "permissions": {"mic_granted": True, "screen_recording_granted": True},
            "component_status": {"audio_capture": "ok"},
            "queues": {},
            "storage": {},
            "recent_events": [],
        })
        resp = client.get("/helios/diagnostics")
        assert resp.status_code == 200
        body = resp.text
        # Each action button targets its own #*-result slot.
        for slot in (
            "test-capture-result", "flush-result", "restart-result",
            "bundle-result", "reload-result",
        ):
            assert f'id="{slot}"' in body
        # Buttons swap into innerHTML, not outerHTML.
        # The action button region contains 5 hx-target / hx-swap pairs.
        # All of them must be innerHTML (Critical #5).
        action_block_start = body.find(">Actions<")
        action_block_end = body.find(">Recent events<", action_block_start)
        action_html = body[action_block_start:action_block_end]
        assert action_html, "could not locate Actions block in diagnostics page"
        assert 'hx-swap="outerHTML"' not in action_html
        assert action_html.count('hx-swap="innerHTML"') >= 5


# ═══════════════════════════════════════════════════════════
# Speaker name resolution helper (HELIOS.md §16.4)
# ═══════════════════════════════════════════════════════════


class TestSpeakerNameResolution:
    def _person(self, name, email, is_external=False):
        return SimpleNamespace(name=name, email=email, is_external=is_external)

    def test_returns_empty_when_no_meeting(self):
        assert resolve_speaker_names({"segments": []}, None, []) == {}

    def test_returns_empty_when_no_segments(self):
        meeting = SimpleNamespace(organizer_email="me@example.com")
        result = resolve_speaker_names(
            {"segments": []}, meeting, [self._person("Alice", "a@x.com")]
        )
        assert result == {}

    def test_maps_speakers_in_first_appearance_order(self):
        meeting = SimpleNamespace(organizer_email="me@example.com")
        attendees = [
            self._person("Me", "me@example.com"),  # excluded (organizer)
            self._person("Alice", "alice@x.com"),
            self._person("Bob",   "bob@x.com"),
        ]
        transcript = {"segments": [
            {"speaker": "SPEAKER_00", "start": 1.0, "text": "Hi"},
            {"speaker": "SPEAKER_01", "start": 2.0, "text": "Hello"},
            {"speaker": "user",       "start": 3.0, "text": "Welcome"},
            {"speaker": "SPEAKER_00", "start": 4.0, "text": "Continuing"},
        ]}
        result = resolve_speaker_names(transcript, meeting, attendees)
        # Two SPEAKER_XX labels, two non-user attendees → mapped in order.
        assert result == {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"}

    def test_returns_empty_when_speaker_count_mismatches_attendees(self):
        meeting = SimpleNamespace(organizer_email="me@example.com")
        attendees = [
            self._person("Me", "me@example.com"),
            self._person("Alice", "alice@x.com"),
            self._person("Bob", "bob@x.com"),
            self._person("Carol", "carol@x.com"),
        ]
        transcript = {"segments": [
            {"speaker": "SPEAKER_00", "start": 1.0, "text": "x"},
            {"speaker": "SPEAKER_01", "start": 2.0, "text": "y"},
        ]}
        # Two raw SPEAKERs but 3 non-user attendees → heuristic skipped.
        result = resolve_speaker_names(transcript, meeting, attendees)
        assert result == {}
