"""Tests for voice-note integration in the RAG search corpus (Track 6E.8).

The RAG semantic search now runs a 4th query against ``voice_notes``
alongside meetings/emails/chat_messages. We patch ``embed_text`` so no
real embedding call happens, and patch ``session.execute`` so we can
assert which SQL strings are issued and verify per-target filtering.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.chat import rag


def _row(**kwargs) -> dict:
    base = {
        "id": 1,
        "label": "x",
        "content": "y",
        "dt": datetime.now(timezone.utc),
        "source_type": "voice_note",
        "similarity": 0.9,
        "triage_weight": 1.0,
    }
    base.update(kwargs)
    return base


class _StubResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        m = MagicMock()
        m.all.return_value = self._rows
        return m


@pytest.fixture
def patched_embed():
    with patch.object(rag, "embed_text", new=AsyncMock(return_value=[0.1] * 1536)) as m:
        yield m


class TestSemanticSearchCorpus:
    @pytest.mark.asyncio
    async def test_voice_notes_query_is_executed(self, patched_embed):
        """Semantic search runs 4 queries (meeting/email/chat/voice_note)."""
        session = MagicMock()
        # Capture every executed SQL string.
        seen_sqls: list[str] = []

        async def fake_execute(sql, params):
            seen_sqls.append(str(sql))
            # Return only one row for voice_notes; other queries empty.
            if "voice_notes" in str(sql):
                return _StubResult([_row(source_type="voice_note", id=42)])
            return _StubResult([])

        session.execute = AsyncMock(side_effect=fake_execute)
        session.rollback = AsyncMock()

        results = await rag._semantic_search(session, "What did I say about X?", limit=5)

        # Voice notes query was issued.
        assert any("voice_notes" in s for s in seen_sqls)
        # Result includes the voice_note row.
        assert any(r.get("source_type") == "voice_note" and r.get("id") == 42 for r in results)

    @pytest.mark.asyncio
    async def test_filter_by_person_id_constrains_voice_notes(self, patched_embed):
        """When filter_person_id is set, voice notes SQL joins through attachments."""
        session = MagicMock()
        seen_sqls: list[str] = []
        seen_params: list[dict] = []

        async def fake_execute(sql, params):
            seen_sqls.append(str(sql))
            seen_params.append(dict(params))
            return _StubResult([])

        session.execute = AsyncMock(side_effect=fake_execute)
        session.rollback = AsyncMock()

        await rag._semantic_search(
            session, "what did I say about Sarah", limit=5, filter_person_id=7
        )

        # Voice-note SQL must include the attachment join + filter.
        vn_sqls = [s for s in seen_sqls if "voice_notes" in s]
        assert vn_sqls, "voice_notes query missing"
        joined = vn_sqls[0]
        assert "voice_note_attachments" in joined
        assert "target_type = 'person'" in joined
        # Filter param threaded through.
        vn_params = [p for p, s in zip(seen_params, seen_sqls) if "voice_notes" in s][0]
        assert vn_params.get("filter_person_id") == 7

    @pytest.mark.asyncio
    async def test_filter_by_workstream_id_constrains_voice_notes(self, patched_embed):
        session = MagicMock()
        seen_sqls: list[str] = []

        async def fake_execute(sql, params):
            seen_sqls.append(str(sql))
            return _StubResult([])

        session.execute = AsyncMock(side_effect=fake_execute)
        session.rollback = AsyncMock()

        await rag._semantic_search(
            session, "status of the migration", limit=5, filter_workstream_id=3
        )

        vn_sqls = [s for s in seen_sqls if "voice_notes" in s]
        assert vn_sqls
        assert "voice_note_attachments" in vn_sqls[0]
        assert "target_type = 'workstream'" in vn_sqls[0]


class TestVoiceNoteCitation:
    @pytest.mark.asyncio
    async def test_voice_note_source_uses_positional_citation(self):
        """Voice notes cite as [N] (Warning #2 — the old [Voice Note #N]
        tag was positional and the model never emitted it). The source
        panel still labels the entry as "Voice Note: <label>" so the
        user can distinguish it.
        """
        # _generate_answer takes context with at least one voice_note entry.
        context = [
            {
                "source_type": "voice_note",
                "id": 13,
                "label": "Need to follow up with Sarah",
                "content": "Note to self: follow up with Sarah next week.",
                "dt": datetime.now(timezone.utc),
                "similarity": 0.9,
                "triage_weight": 1.0,
            },
        ]
        fake_resp = SimpleNamespace(
            content=[SimpleNamespace(text="Per [1] you mentioned Sarah.")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
        fake_client = MagicMock()
        fake_client.messages.create = AsyncMock(return_value=fake_resp)
        with patch.object(rag.anthropic, "AsyncAnthropic", return_value=fake_client), \
             patch.object(rag, "_track_llm_usage", new=AsyncMock()):
            answer, sources = await rag._generate_answer("question?", context)

        assert "[1]" in answer
        assert len(sources) == 1
        assert sources[0]["source_type"] == "voice_note"
        assert sources[0]["url"] == "/voice-notes/13"
        assert sources[0]["ref"] == "[1]"
        assert sources[0]["label"] == "Voice Note: Need to follow up with Sarah"


class TestBuildSourceUrlVoiceNote:
    def test_voice_note_url(self):
        assert rag._build_source_url("voice_note", 7) == "/voice-notes/7"

    def test_voice_note_url_no_id(self):
        assert rag._build_source_url("voice_note", None) is None
