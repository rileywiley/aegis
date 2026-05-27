"""Tests for voice-note integration in briefings (Track 6E.7).

Voice notes are user-generated context. The morning, Monday, and
Friday briefings each include a ``voice_notes`` list in the prompt
context. Prompts handle the empty list gracefully — the section is
conditional so we don't emit an empty ``Voice notes:`` header.

We patch ``_call_sonnet`` so no real Anthropic call is made; the
test asserts on the context payload (what we'd send to the LLM)
and on the prompt-level instructions.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.intelligence import briefings


def _vn(**overrides) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    defaults = {
        "id": 1,
        "started_at": now - timedelta(hours=2),
        "duration_seconds": 45.0,
        "triggered_by": "menu_bar",
        "is_excerpt": False,
        "transcript_text": "Note to self: follow up on Q3 plan.",
        "transcript_text_edited": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        s = MagicMock()
        s.all.return_value = self._items
        return s


class TestVoiceNotesHelper:
    @pytest.mark.asyncio
    async def test_voice_notes_serialised_to_dicts(self):
        """Helper produces JSON-friendly dicts capped at 600 chars."""
        long_text = "x" * 1000
        notes = [
            _vn(id=1, transcript_text=long_text),
            _vn(id=2, transcript_text="Short note", transcript_text_edited="Edited shorter"),
        ]
        session = MagicMock()
        session.execute = AsyncMock(return_value=_FakeResult(notes))

        start = datetime.now(timezone.utc) - timedelta(days=1)
        end = datetime.now(timezone.utc)
        result = await briefings._get_voice_notes_in_range(session, start, end)

        assert len(result) == 2
        assert result[0]["id"] == 1
        assert len(result[0]["transcript"]) == 600  # capped
        assert result[1]["transcript"] == "Edited shorter"  # prefers edited
        assert result[1]["triggered_by"] == "menu_bar"

    @pytest.mark.asyncio
    async def test_empty_window_returns_empty(self):
        session = MagicMock()
        session.execute = AsyncMock(return_value=_FakeResult([]))

        start = datetime.now(timezone.utc) - timedelta(days=1)
        end = datetime.now(timezone.utc)
        result = await briefings._get_voice_notes_in_range(session, start, end)
        assert result == []


class TestMorningBriefingVoiceNotes:
    @pytest.mark.asyncio
    async def test_morning_briefing_includes_voice_notes(self):
        """Morning briefing context includes voice_notes list."""
        notes = [_vn(id=42, transcript_text="Remember to ping platform team")]

        captured: dict = {}

        async def fake_sonnet(session, system_prompt, user_prompt, task):
            captured["user_prompt"] = user_prompt
            captured["task"] = task
            return "BRIEFING TEXT"

        session = MagicMock()
        session.add = MagicMock()
        session.commit = AsyncMock()

        with patch.object(briefings, "_get_todays_meetings", AsyncMock(return_value=[])), \
             patch.object(briefings, "_get_requires_action", AsyncMock(return_value={})), \
             patch.object(briefings, "_get_overnight_activity", AsyncMock(return_value={})), \
             patch.object(briefings, "_get_workstream_health", AsyncMock(return_value=[])), \
             patch.object(briefings, "_get_pending_drafts_count", AsyncMock(return_value=0)), \
             patch.object(briefings, "_get_voice_notes_in_range", AsyncMock(return_value=[
                 {"id": 42, "transcript": "Remember to ping platform team",
                  "triggered_by": "menu_bar", "duration_seconds": 45.0,
                  "is_excerpt": False, "started_at": "now"}
             ])), \
             patch.object(briefings, "_call_sonnet", side_effect=fake_sonnet), \
             patch.object(briefings, "generate_meeting_prep", AsyncMock()):
            result = await briefings.generate_morning_briefing(session)

        assert result == "BRIEFING TEXT"
        # voice_notes appears in the JSON context passed to Sonnet
        assert "voice_notes" in captured["user_prompt"]
        assert "ping platform team" in captured["user_prompt"]
        assert captured["task"] == "morning_briefing"

    @pytest.mark.asyncio
    async def test_morning_briefing_empty_voice_notes(self):
        """When voice notes list is empty, prompt still works (no crash)."""
        captured: dict = {}

        async def fake_sonnet(session, system_prompt, user_prompt, task):
            captured["user_prompt"] = user_prompt
            return "BRIEFING"

        session = MagicMock()
        session.add = MagicMock()
        session.commit = AsyncMock()

        with patch.object(briefings, "_get_todays_meetings", AsyncMock(return_value=[])), \
             patch.object(briefings, "_get_requires_action", AsyncMock(return_value={})), \
             patch.object(briefings, "_get_overnight_activity", AsyncMock(return_value={})), \
             patch.object(briefings, "_get_workstream_health", AsyncMock(return_value=[])), \
             patch.object(briefings, "_get_pending_drafts_count", AsyncMock(return_value=0)), \
             patch.object(briefings, "_get_voice_notes_in_range", AsyncMock(return_value=[])), \
             patch.object(briefings, "_call_sonnet", side_effect=fake_sonnet), \
             patch.object(briefings, "generate_meeting_prep", AsyncMock()):
            result = await briefings.generate_morning_briefing(session)

        assert result == "BRIEFING"
        # voice_notes key is present but empty
        ctx = json.loads(captured["user_prompt"].split("Data:\n", 1)[1])
        assert ctx["voice_notes"] == []


class TestMondayAndFridayVoiceNotes:
    @pytest.mark.asyncio
    async def test_monday_brief_includes_voice_notes(self):
        captured: dict = {}

        async def fake_sonnet(session, system_prompt, user_prompt, task):
            captured["user_prompt"] = user_prompt
            return "MONDAY BRIEF"

        # Build a session that returns empty for all DB queries (we patch
        # _get_voice_notes_in_range so its underlying query doesn't run).
        session = MagicMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.execute = AsyncMock(return_value=_FakeResult([]))

        with patch.object(briefings, "_get_voice_notes_in_range", AsyncMock(return_value=[
                 {"id": 5, "transcript": "Plan Q3 roadmap",
                  "triggered_by": "menu_bar", "duration_seconds": 30.0,
                  "is_excerpt": False, "started_at": "now"}
             ])), \
             patch.object(briefings, "_get_todays_meetings", AsyncMock(return_value=[])), \
             patch.object(briefings, "_call_sonnet", side_effect=fake_sonnet), \
             patch.object(briefings, "generate_meeting_prep", AsyncMock()):
            await briefings.generate_monday_brief(session)

        assert "voice_notes" in captured["user_prompt"]
        assert "Plan Q3 roadmap" in captured["user_prompt"]

    @pytest.mark.asyncio
    async def test_friday_recap_includes_voice_notes(self):
        captured: dict = {}

        async def fake_sonnet(session, system_prompt, user_prompt, task):
            captured["user_prompt"] = user_prompt
            return "FRIDAY RECAP"

        session = MagicMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.execute = AsyncMock(return_value=_FakeResult([]))
        # session.execute is also used for scalar counts; return a flexible mock
        result_count = MagicMock()
        result_count.scalar_one = MagicMock(return_value=0)
        result_count.scalars = MagicMock(return_value=MagicMock(all=lambda: []))
        session.execute = AsyncMock(return_value=result_count)

        with patch.object(briefings, "_get_voice_notes_in_range", AsyncMock(return_value=[
                 {"id": 99, "transcript": "Need to circle back on budget",
                  "triggered_by": "menu_bar", "duration_seconds": 60.0,
                  "is_excerpt": False, "started_at": "now"}
             ])), \
             patch.object(briefings, "_call_sonnet", side_effect=fake_sonnet):
            await briefings.generate_friday_recap(session)

        assert "voice_notes" in captured["user_prompt"]
        assert "circle back on budget" in captured["user_prompt"]
