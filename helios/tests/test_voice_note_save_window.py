"""Tests for the floating voice-note save window (Track 4H.1).

The save window owns a PyObjC NSWindow on macOS, but the test
environment mocks AppKit/Foundation so we can run on any platform.
Tests focus on LOGIC — suggestion fetching, checkbox / countdown
interactions, save / discard flows, error handling — not pixel
rendering.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from helios.menubar.voice_note_save_window import (
    Suggestion,
    VoiceNoteSaveWindow,
    _resolve_auto_save_timeout,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_config(auto_save_timeout_seconds: int = 10) -> Any:
    """Construct a minimal HeliosConfig stub with voice_note settings."""
    voice_note = SimpleNamespace(
        auto_save_timeout_seconds=auto_save_timeout_seconds,
    )
    return SimpleNamespace(voice_note=voice_note)


def _stub_appkit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``_import_appkit`` with a stub that returns mock modules."""
    from helios.menubar import voice_note_save_window as mod

    appkit = MagicMock(name="AppKit")
    foundation = MagicMock(name="Foundation")
    objc = MagicMock(name="objc")

    bundle = mod._PyObjCImports()
    bundle.AppKit = appkit
    bundle.Foundation = foundation
    bundle.objc = objc

    monkeypatch.setattr(mod, "_import_appkit", lambda: bundle)
    # Stub objc.selector for the late-import inside _start_countdown.
    objc_module = MagicMock(name="objc_module")
    objc_module.selector = lambda fn, signature=b"v@:@": fn
    monkeypatch.setitem(sys.modules, "objc", objc_module)


def _make_window(
    monkeypatch: pytest.MonkeyPatch,
    *,
    helios_client: Any | None = None,
    config: Any | None = None,
    on_close: Any | None = None,
    aegis_base_url: str = "http://127.0.0.1:8000",
) -> VoiceNoteSaveWindow:
    """Build a save window with PyObjC stubbed and an injectable client."""
    _stub_appkit(monkeypatch)
    return VoiceNoteSaveWindow(
        helios_client=helios_client or MagicMock(),
        aegis_base_url=aegis_base_url,
        config=config or _make_config(),
        on_close=on_close,
    )


def _show(
    win: VoiceNoteSaveWindow,
    *,
    transcript_text: str = "Note to self, follow up with Sarah.",
    voice_note_id: int = 1,
    helios_session_id: int = 5,
    started_at: float = 1000.0,
    ended_at: float = 1034.0,
    duration_seconds: float = 34.0,
    triggered_by: str = "menu_bar",
    is_excerpt: bool = False,
    skip_preview_thread: bool = True,
) -> None:
    """Show the window with sensible defaults; tests can pass overrides.

    By default, monkey-patches the preview-attachments background
    thread launcher to a no-op so each test controls suggestion state
    deterministically.
    """
    if skip_preview_thread:
        win._launch_preview_attachments = lambda: None  # type: ignore[method-assign]
    win.show(
        voice_note_id=voice_note_id,
        helios_session_id=helios_session_id,
        transcript_text=transcript_text,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        triggered_by=triggered_by,
        is_excerpt=is_excerpt,
    )


def _mock_transport(
    handler,
) -> httpx.Client:
    """Build an httpx.Client whose every request is handled by ``handler``."""
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, timeout=5.0)


# ---------------------------------------------------------------------------
# 1) show() opens window with the given transcript
# ---------------------------------------------------------------------------


def test_show_opens_window_with_transcript(monkeypatch):
    win = _make_window(monkeypatch)
    _show(win, transcript_text="Hello world from a voice note.")
    assert win.visible is True
    assert win.transcript_text == "Hello world from a voice note."
    assert win.loading is True
    assert win.suggestions == []
    win.dismiss()


# ---------------------------------------------------------------------------
# 2) Suggestions load via mock httpx response → checkboxes populate
# ---------------------------------------------------------------------------


def test_apply_suggestions_response_populates_list(monkeypatch):
    win = _make_window(monkeypatch)
    _show(win)

    # Real Aegis ``PreviewAttachmentsResponse`` shape:
    # ``matches`` is a list of ``EntityMatch`` (target_type / target_id / label /
    # confidence / span); ``suggested_attachments`` is a list of
    # ``AttachmentSuggestion`` (target_type / target_id / label / confidence).
    payload = {
        "suggested_attachments": [
            {
                "target_type": "person",
                "target_id": 42,
                "label": "Sarah Lin",
                "confidence": 0.85,
            },
        ],
        "matches": [
            {
                "target_type": "person",
                "target_id": 42,
                "label": "Sarah Lin",
                "confidence": 0.85,
                "span": "Sarah",
            },
            {
                "target_type": "workstream",
                "target_id": 11,
                "label": "Q2 Budget Review",
                "confidence": 0.72,
                "span": "Q2 budget",
            },
        ],
    }
    win._apply_suggestions_response(payload)

    assert win.loading is False
    assert win.suggestions_unavailable is False
    assert len(win.suggestions) == 2
    s0, s1 = win.suggestions
    assert (s0.type, s0.id, s0.label) == ("person", 42, "Sarah Lin")
    # confidence 0.85 ≥ 0.8 → pre-selected
    assert s0.selected is True
    # confidence 0.72 < 0.8 → not pre-selected
    assert s1.selected is False
    win.dismiss()


def test_preview_attachments_via_mock_httpx(monkeypatch):
    """End-to-end fetch path using httpx.MockTransport."""
    win = _make_window(monkeypatch)

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/voice-notes/preview-attachments"
        body = request.read()
        # Body should contain transcript_text
        assert b"transcript_text" in body
        return httpx.Response(
            200,
            json={
                "suggested_attachments": [
                    {
                        "target_type": "person",
                        "target_id": 7,
                        "label": "Alex",
                        "confidence": 0.91,
                    }
                ],
                "matches": [
                    {
                        "target_type": "person",
                        "target_id": 7,
                        "label": "Alex",
                        "confidence": 0.91,
                        "span": "Alex",
                    }
                ],
            },
        )

    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win, skip_preview_thread=True)
    # Manually invoke the worker (skipping the thread) — same code path.
    win._preview_attachments_worker()

    assert win.loading is False
    assert len(win.suggestions) == 1
    assert win.suggestions[0].label == "Alex"
    assert win.suggestions[0].selected is True
    win.dismiss()


# ---------------------------------------------------------------------------
# 3) Checkbox click cancels countdown
# ---------------------------------------------------------------------------


def test_checkbox_click_cancels_countdown(monkeypatch):
    win = _make_window(monkeypatch)
    _show(win)
    # Plant a suggestion to toggle.
    win._apply_suggestions_response(
        {
            "matches": [
                {
                    "target_type": "person",
                    "target_id": 1,
                    "label": "Sarah",
                    "confidence": 0.5,
                    "span": "Sarah",
                }
            ]
        }
    )
    assert win.countdown_active is True
    assert win.suggestions[0].selected is False

    win.handle_checkbox_clicked(0)

    assert win.suggestions[0].selected is True
    assert win.countdown_active is False
    win.dismiss()


def test_checkbox_click_with_invalid_index_still_cancels_countdown(monkeypatch):
    win = _make_window(monkeypatch)
    _show(win)
    assert win.countdown_active is True
    win.handle_checkbox_clicked(99)
    assert win.countdown_active is False
    win.dismiss()


# ---------------------------------------------------------------------------
# 4) "Add attachment" click cancels countdown (and triggers picker open)
# ---------------------------------------------------------------------------


def test_add_attachment_click_cancels_countdown(monkeypatch):
    win = _make_window(monkeypatch)
    _show(win)
    assert win.countdown_active is True

    win.handle_add_attachment_clicked()
    assert win.countdown_active is False
    win.dismiss()


# ---------------------------------------------------------------------------
# 5) Save button → POSTs VoiceNoteCreateRequest to Aegis with selected attachments
# ---------------------------------------------------------------------------


def test_save_button_posts_to_aegis(monkeypatch):
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/voice-notes":
            captured["url"] = str(request.url)
            captured["body"] = request.read()
            return httpx.Response(
                200,
                json={"voice_note_id": 99, "extraction_status": "queued"},
            )
        return httpx.Response(404)

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)

    win._apply_suggestions_response(
        {
            "matches": [
                {
                    "target_type": "person",
                    "target_id": 42,
                    "label": "Sarah Lin",
                    "confidence": 0.9,
                    "span": "Sarah",
                },
                {
                    "target_type": "workstream",
                    "target_id": 11,
                    "label": "Q2 Budget",
                    "confidence": 0.4,  # below pre-select threshold
                    "span": "Q2",
                },
            ]
        }
    )
    # User checks the workstream suggestion explicitly.
    win.handle_checkbox_clicked(1)

    win.handle_save_clicked()

    assert win.saved is True
    assert win.save_error is None
    assert "url" in captured
    import json as _json

    body = _json.loads(captured["body"])
    assert body["helios_voice_note_id"] == 1
    assert body["helios_session_id"] == 5
    assert body["transcript_text"].startswith("Note to self")
    assert body["triggered_by"] == "menu_bar"
    assert body["is_excerpt"] is False
    attachments = body["attachments"]
    assert len(attachments) == 2
    keys = {(a["target_type"], a["target_id"]) for a in attachments}
    assert ("person", 42) in keys
    assert ("workstream", 11) in keys
    # All attachments here came from the suggestion list.
    assert all(a["is_suggested"] is True for a in attachments)


# ---------------------------------------------------------------------------
# 6) Save payload structure: required keys present
# ---------------------------------------------------------------------------


def test_save_payload_structure(monkeypatch):
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(
            200, json={"voice_note_id": 1, "extraction_status": "queued"}
        )

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(
        win,
        voice_note_id=12,
        helios_session_id=34,
        started_at=2000.5,
        ended_at=2030.5,
        duration_seconds=30.0,
        triggered_by="hotkey",
        is_excerpt=True,
        transcript_text="Quick voice note.",
    )
    win.handle_save_clicked()

    import json as _json

    body = _json.loads(captured["body"])
    for key in (
        "helios_voice_note_id",
        "helios_session_id",
        "started_at",
        "ended_at",
        "duration_seconds",
        "transcript_text",
        "triggered_by",
        "is_excerpt",
        "attachments",
    ):
        assert key in body, f"missing {key} in save payload"
    assert body["helios_voice_note_id"] == 12
    assert body["triggered_by"] == "hotkey"
    assert body["is_excerpt"] is True
    assert body["duration_seconds"] == 30.0
    # Top-level shape must match VoiceNoteCreateRequest exactly — no legacy
    # ``confirmed_attachments`` / dict-form ``suggested_attachments``.
    assert "confirmed_attachments" not in body
    assert "suggested_attachments" not in body


# ---------------------------------------------------------------------------
# 7) Discard button → POSTs to Helios /v1/voice-note/cancel; does NOT POST to Aegis
# ---------------------------------------------------------------------------


def test_discard_calls_helios_cancel_and_skips_aegis(monkeypatch):
    aegis_calls: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        aegis_calls.append(request)
        return httpx.Response(500)

    helios_client = MagicMock()
    win = _make_window(monkeypatch, helios_client=helios_client)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)

    win.handle_discard_clicked()

    helios_client.post_voice_note_cancel.assert_called_once()
    # No Aegis POST should have happened.
    assert aegis_calls == []
    assert win.saved is False
    # The save payload was never assembled either.
    assert win.last_save_payload is None


def test_discard_swallows_helios_cancel_failure(monkeypatch):
    """If Helios already finalized the note, /cancel may 4xx — that's OK."""
    helios_client = MagicMock()
    helios_client.post_voice_note_cancel.side_effect = RuntimeError(
        "already finalized"
    )

    win = _make_window(monkeypatch, helios_client=helios_client)
    _show(win)

    # Should not raise.
    win.handle_discard_clicked()
    assert win.visible is False


# ---------------------------------------------------------------------------
# 8) Auto-save countdown fires after 10s of inactivity → triggers Save
# ---------------------------------------------------------------------------


def test_countdown_fires_save_after_timeout(monkeypatch):
    save_calls: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        save_calls.append(request)
        return httpx.Response(
            200, json={"voice_note_id": 1, "extraction_status": "queued"}
        )

    win = _make_window(monkeypatch, config=_make_config(auto_save_timeout_seconds=10))
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)

    assert win.countdown_active is True
    assert win.countdown_remaining == 10
    # Tick down to 0.
    for _ in range(10):
        win.handle_countdown_tick()

    assert win.saved is True
    assert win.countdown_active is False
    assert len(save_calls) == 1


def test_countdown_uses_config_value(monkeypatch):
    win = _make_window(
        monkeypatch, config=_make_config(auto_save_timeout_seconds=3)
    )
    _show(win)
    assert win.countdown_remaining == 3


# ---------------------------------------------------------------------------
# 9) Click-away (windowDidResignKey) → triggers Save
# ---------------------------------------------------------------------------


def test_click_away_triggers_save(monkeypatch):
    save_calls: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        save_calls.append(request)
        return httpx.Response(
            200, json={"voice_note_id": 1, "extraction_status": "queued"}
        )

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)

    win.handle_window_did_resign_key()

    assert win.saved is True
    assert len(save_calls) == 1


def test_close_button_triggers_save(monkeypatch):
    save_calls: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        save_calls.append(request)
        return httpx.Response(
            200, json={"voice_note_id": 1, "extraction_status": "queued"}
        )

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)

    win.handle_window_close_clicked()

    assert win.saved is True
    assert len(save_calls) == 1


def test_click_away_after_save_is_noop(monkeypatch):
    """Once saved, click-away should not double-POST."""
    save_calls: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        save_calls.append(request)
        return httpx.Response(
            200, json={"voice_note_id": 1, "extraction_status": "queued"}
        )

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)
    win.handle_save_clicked()
    assert win.saved is True

    win.handle_window_did_resign_key()
    assert len(save_calls) == 1


# ---------------------------------------------------------------------------
# 10) Save failure (Aegis 5xx) → shows error state, "Retry" button visible
# ---------------------------------------------------------------------------


def test_save_5xx_sets_error_state_and_keeps_window_open(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unhappy")

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)

    win.handle_save_clicked()

    assert win.saved is False
    assert win.save_error is not None
    assert "503" in win.save_error
    assert win.visible is True
    # Save button label should now read "Retry"
    assert win._save_button_title() == "Retry"


def test_save_4xx_also_sets_error_state(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "validation"})

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)

    win.handle_save_clicked()

    assert win.saved is False
    assert win.save_error is not None
    assert "422" in win.save_error
    assert win._save_button_title() == "Retry"


def test_save_network_error_sets_error_state(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)

    win.handle_save_clicked()

    assert win.saved is False
    assert win.save_error is not None
    assert win.save_error.startswith("network:")
    assert win.visible is True


# ---------------------------------------------------------------------------
# 11) Retry after failure → re-POSTs same payload
# ---------------------------------------------------------------------------


def test_retry_after_failure_reposts(monkeypatch):
    bodies: list[bytes] = []
    call_counter = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(
            200, json={"voice_note_id": 1, "extraction_status": "queued"}
        )

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)

    win.handle_save_clicked()
    assert win.saved is False
    assert win.save_error is not None
    first_payload = win.last_save_payload

    # Retry — clear error and click Save again.
    win._save_error = None  # what the production button does on Retry
    win.handle_save_clicked()

    assert win.saved is True
    assert call_counter["n"] == 2
    # Same payload structurally.
    assert win.last_save_payload == first_payload
    assert bodies[0] == bodies[1]


# ---------------------------------------------------------------------------
# 12) preview-attachments timeout → "Suggestions unavailable" but lets user save
# ---------------------------------------------------------------------------


def test_preview_attachments_timeout_marks_unavailable(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)
    # Run the worker synchronously (we skipped the thread launch).
    win._preview_attachments_worker()

    assert win.loading is False
    assert win.suggestions_unavailable is True
    assert win.suggestions == []

    # User can still save.
    save_calls: list[httpx.Request] = []

    def _save_handler(request: httpx.Request) -> httpx.Response:
        save_calls.append(request)
        return httpx.Response(
            200, json={"voice_note_id": 1, "extraction_status": "queued"}
        )

    win.set_http_client_for_test(_mock_transport(_save_handler))
    win.handle_save_clicked()
    assert win.saved is True
    assert len(save_calls) == 1


def test_preview_attachments_5xx_marks_unavailable(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)
    win._preview_attachments_worker()

    assert win.suggestions_unavailable is True
    assert win.loading is False


# ---------------------------------------------------------------------------
# 13) PyObjC ImportError → show() raises clean RuntimeError on non-mac
# ---------------------------------------------------------------------------


def test_show_on_non_mac_raises_runtime_error(monkeypatch):
    from helios.menubar import voice_note_save_window as mod

    def _broken_import() -> Any:
        raise RuntimeError(
            "PyObjC (AppKit/Foundation) not available — "
            "VoiceNoteSaveWindow requires macOS"
        )

    monkeypatch.setattr(mod, "_import_appkit", _broken_import)

    win = VoiceNoteSaveWindow(
        helios_client=MagicMock(),
        aegis_base_url="http://127.0.0.1:8000",
        config=_make_config(),
    )
    with pytest.raises(RuntimeError, match="requires macOS"):
        win.show(
            voice_note_id=1,
            helios_session_id=1,
            transcript_text="x",
            started_at=0.0,
            ended_at=1.0,
            duration_seconds=1.0,
            triggered_by="menu_bar",
            is_excerpt=False,
        )


# ---------------------------------------------------------------------------
# 14) Manual attachment picker: /api/search returns 3 results → all 3 displayed
# ---------------------------------------------------------------------------


def test_manual_search_returns_all_results(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/search"
        # Verify query string contains q + types
        qs = dict(request.url.params)
        assert qs.get("q") == "foo"
        assert "person" in qs.get("types", "")
        return httpx.Response(
            200,
            json=[
                {"type": "person", "id": 1, "label": "Foo Bar", "snippet": "Eng"},
                {
                    "type": "workstream",
                    "id": 7,
                    "label": "Foo Project",
                    "snippet": "Active",
                },
                {"type": "ask", "id": 5, "label": "Foo deliverable", "snippet": ""},
            ],
        )

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)

    results = win.search_attachments("foo")
    assert len(results) == 3
    types = [r.type for r in results]
    assert types == ["person", "workstream", "ask"]
    # Manual results are unselected; source="manual"
    assert all(r.selected is False for r in results)
    assert all(r.source == "manual" for r in results)


def test_manual_search_handles_dict_response_shape(monkeypatch):
    """Aegis may return either a list or {"results": [...]}; both should work."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"type": "person", "id": 9, "label": "Bob", "snippet": ""},
                ]
            },
        )

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)
    results = win.search_attachments("bob")
    assert len(results) == 1
    assert results[0].label == "Bob"


def test_manual_search_failure_returns_empty(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)
    assert win.search_attachments("anything") == []


def test_add_manual_attachment_appends_and_cancels_countdown(monkeypatch):
    win = _make_window(monkeypatch)
    _show(win)
    assert win.countdown_active is True

    sug = Suggestion(
        type="person", id=99, label="Manual Person", source="manual"
    )
    win.add_manual_attachment(sug)
    assert len(win.suggestions) == 1
    assert win.suggestions[0].selected is True
    assert win.suggestions[0].source == "manual"
    assert win.countdown_active is False


def test_manual_attachment_appears_in_save_payload(monkeypatch):
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(
            200, json={"voice_note_id": 1, "extraction_status": "queued"}
        )

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)

    win.add_manual_attachment(
        Suggestion(type="person", id=99, label="Manual Person")
    )
    win.handle_save_clicked()

    import json as _json

    body = _json.loads(captured["body"])
    attachments = body["attachments"]
    manual = [
        a for a in attachments
        if a["target_type"] == "person" and a["target_id"] == 99
    ]
    assert len(manual) == 1
    # Manual rows are flagged ``is_suggested=False`` so Aegis records them
    # separately from the resolver's suggestions.
    assert manual[0]["is_suggested"] is False


# ---------------------------------------------------------------------------
# Auto-save timeout config resolution
# ---------------------------------------------------------------------------


def test_resolve_auto_save_timeout_from_top_level():
    config = _make_config(auto_save_timeout_seconds=15)
    assert _resolve_auto_save_timeout(config) == 15


def test_resolve_auto_save_timeout_from_save_window_subconfig():
    save_window = SimpleNamespace(auto_save_timeout_seconds=7)
    voice_note = SimpleNamespace(
        auto_save_timeout_seconds=10, save_window=save_window
    )
    config = SimpleNamespace(voice_note=voice_note)
    assert _resolve_auto_save_timeout(config) == 7


def test_resolve_auto_save_timeout_default_when_missing():
    config = SimpleNamespace()
    assert _resolve_auto_save_timeout(config) == 10


# ---------------------------------------------------------------------------
# Window dismissal triggers on_close callback
# ---------------------------------------------------------------------------


def test_dismiss_invokes_on_close(monkeypatch):
    closed: list[bool] = []
    win = _make_window(monkeypatch, on_close=lambda: closed.append(True))
    _show(win)
    win.dismiss()
    assert closed == [True]


def test_dismiss_swallows_on_close_failure(monkeypatch):
    def _bad_on_close() -> None:
        raise RuntimeError("nope")

    win = _make_window(monkeypatch, on_close=_bad_on_close)
    _show(win)
    # Should not raise.
    win.dismiss()
    assert win.visible is False


def test_dismiss_is_idempotent(monkeypatch):
    win = _make_window(monkeypatch)
    _show(win)
    win.dismiss()
    # Second call should not raise.
    win.dismiss()
    assert win.visible is False


# ---------------------------------------------------------------------------
# hide() vs dismiss()
# ---------------------------------------------------------------------------


def test_hide_keeps_state_for_re_show(monkeypatch):
    win = _make_window(monkeypatch)
    _show(win, transcript_text="ABC")
    win.hide()
    assert win.visible is False
    # State preserved.
    assert win.transcript_text == "ABC"


# ---------------------------------------------------------------------------
# Save payload routes suggested vs manual into right buckets
# ---------------------------------------------------------------------------


def test_save_payload_separates_suggested_and_manual(monkeypatch):
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(
            200, json={"voice_note_id": 1, "extraction_status": "queued"}
        )

    win = _make_window(monkeypatch)
    win.set_http_client_for_test(_mock_transport(_handler))
    _show(win)

    win._apply_suggestions_response(
        {
            "matches": [
                {
                    "target_type": "person",
                    "target_id": 1,
                    "label": "S",
                    "confidence": 0.9,
                    "span": "S",
                },
                {
                    "target_type": "workstream",
                    "target_id": 11,
                    "label": "W",
                    "confidence": 0.9,
                    "span": "W",
                },
                {
                    "target_type": "ask",
                    "target_id": 22,
                    "label": "A",
                    "confidence": 0.9,
                    "span": "A",
                },
            ]
        }
    )
    win.add_manual_attachment(Suggestion(type="person", id=99, label="Manual"))
    win.handle_save_clicked()

    import json as _json

    body = _json.loads(captured["body"])
    # All four end up in attachments (3 pre-selected + 1 manual).
    attachments = body["attachments"]
    keys = {(a["target_type"], a["target_id"]) for a in attachments}
    assert ("person", 99) in keys
    assert ("person", 1) in keys
    assert ("workstream", 11) in keys
    assert ("ask", 22) in keys
    # Manual rows are marked is_suggested=False; suggested rows True.
    by_id = {(a["target_type"], a["target_id"]): a for a in attachments}
    assert by_id[("person", 1)]["is_suggested"] is True
    assert by_id[("workstream", 11)]["is_suggested"] is True
    assert by_id[("ask", 22)]["is_suggested"] is True
    assert by_id[("person", 99)]["is_suggested"] is False


# ---------------------------------------------------------------------------
# Malformed match rows in preview-attachments are skipped without crashing
# ---------------------------------------------------------------------------


def test_build_content_view_creates_all_widgets(monkeypatch):
    """``_build_content_view`` should create header/transcript/buttons.

    Smoke test — verifies the production UI now contains every widget
    HELIOS.md §12.11 calls for, not just a Save button.
    """
    win = _make_window(monkeypatch)
    _show(win)
    # All five primary widgets should be non-None after show().
    assert win._save_button is not None
    assert win._discard_button is not None
    assert win._add_attachment_button is not None
    assert win._header_label is not None
    assert win._transcript_view is not None
    assert win._suggestions_stack is not None
    win.dismiss()


def test_malformed_match_rows_are_skipped(monkeypatch):
    win = _make_window(monkeypatch)
    _show(win)
    win._apply_suggestions_response(
        {
            "matches": [
                {"target_type": "person"},  # missing target_id
                {"target_id": 1},  # missing target_type
                {"target_type": "person", "target_id": "not-a-number"},  # bad id
                {
                    "target_type": "person",
                    "target_id": 5,
                    "label": "Real Person",
                    "confidence": 0.5,
                    "span": "Real",
                },
            ]
        }
    )
    assert len(win.suggestions) == 1
    assert win.suggestions[0].label == "Real Person"
