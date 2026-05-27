"""Tests for database migration runner."""

import pytest

from helios.db.migrations import run_migrations


@pytest.mark.asyncio
async def test_initial_migration(tmp_db, migrations_dir):
    """001_initial.sql creates all expected tables."""
    version = await run_migrations(tmp_db, migrations_dir)
    assert version == 1

    # Verify key tables exist
    cursor = await tmp_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in await cursor.fetchall()]
    expected = [
        "audio_chunks", "capture_sessions", "component_status",
        "daemon_events", "diarization_turns", "ocr_frames",
        "permission_checks", "schema_version", "session_calendar_links",
        "transcript_segments", "voice_notes",
    ]
    for table in expected:
        assert table in tables, f"Missing table: {table}"


@pytest.mark.asyncio
async def test_migration_idempotent(tmp_db, migrations_dir):
    """Running migrations twice is safe."""
    v1 = await run_migrations(tmp_db, migrations_dir)
    v2 = await run_migrations(tmp_db, migrations_dir)
    assert v1 == v2
