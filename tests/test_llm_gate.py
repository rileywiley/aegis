"""Unit tests for the off-hours LLM gate.

The gate's contract:
1. ``llm_force_active`` beats everything → calls allowed.
2. ``llm_force_pause`` blocks when force_active is off → calls denied.
3. ``llm_work_hours_enabled = False`` → calls allowed (gate disabled).
4. Otherwise, time-of-day + day-of-week against the configured window.

We stub ``_read_settings`` directly to keep the tests pure-logic.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from aegis.intelligence import llm_gate


def _aware(year, month, day, hour, minute=0, tz="America/New_York"):
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz))


def _stub_settings(monkeypatch, **overrides):
    """Replace ``llm_gate._read_settings`` with a coroutine returning ``overrides``."""
    base = {
        "llm_force_active": False,
        "llm_force_pause": False,
        "llm_work_hours_enabled": True,
        "llm_work_hours_start": "08:00",
        "llm_work_hours_end": "18:00",
        "llm_work_days": "1,2,3,4,5",
    }
    base.update(overrides)

    async def _fake_read(session, keys):
        return base

    monkeypatch.setattr(llm_gate, "_read_settings", _fake_read)


@pytest.mark.asyncio
async def test_force_active_bypasses_everything(monkeypatch):
    _stub_settings(monkeypatch, llm_force_active=True, llm_force_pause=True)
    # Sunday 2 AM with force_pause AND outside hours — force_active wins.
    sat_2am = _aware(2026, 5, 31, 2, 0)  # Sunday
    allowed, reason = await llm_gate.llm_calls_allowed(
        AsyncMock(), now=sat_2am
    )
    assert allowed is True
    assert "force-active" in reason.lower()


@pytest.mark.asyncio
async def test_force_pause_blocks_during_work_hours(monkeypatch):
    _stub_settings(monkeypatch, llm_force_pause=True)
    tue_10am = _aware(2026, 5, 26, 10, 0)  # Tuesday in-hours
    allowed, reason = await llm_gate.llm_calls_allowed(
        AsyncMock(), now=tue_10am
    )
    assert allowed is False
    assert reason == "force-paused"


@pytest.mark.asyncio
async def test_master_disabled_allows_outside_hours(monkeypatch):
    _stub_settings(monkeypatch, llm_work_hours_enabled=False)
    sun_3am = _aware(2026, 5, 31, 3, 0)
    allowed, reason = await llm_gate.llm_calls_allowed(
        AsyncMock(), now=sun_3am
    )
    assert allowed is True
    assert "disabled" in reason.lower()


@pytest.mark.asyncio
async def test_inside_work_hours_allowed(monkeypatch):
    _stub_settings(monkeypatch)
    tue_1030 = _aware(2026, 5, 26, 10, 30)
    allowed, reason = await llm_gate.llm_calls_allowed(
        AsyncMock(), now=tue_1030
    )
    assert allowed is True
    assert "18:00" in reason


@pytest.mark.asyncio
async def test_after_work_hours_blocked(monkeypatch):
    _stub_settings(monkeypatch)
    tue_2200 = _aware(2026, 5, 26, 22, 0)
    allowed, reason = await llm_gate.llm_calls_allowed(
        AsyncMock(), now=tue_2200
    )
    assert allowed is False
    assert "outside work hours" in reason
    assert "next active" in reason  # mentions tomorrow's open time


@pytest.mark.asyncio
async def test_weekend_blocked(monkeypatch):
    _stub_settings(monkeypatch)
    sat_2pm = _aware(2026, 5, 30, 14, 0)  # Saturday
    allowed, reason = await llm_gate.llm_calls_allowed(
        AsyncMock(), now=sat_2pm
    )
    assert allowed is False
    # The next open day is Monday — verify the label points there.
    assert "Mon" in reason


@pytest.mark.asyncio
async def test_custom_work_days_include_saturday(monkeypatch):
    _stub_settings(monkeypatch, llm_work_days="1,2,3,4,5,6")
    sat_2pm = _aware(2026, 5, 30, 14, 0)  # Saturday in-hours
    allowed, _ = await llm_gate.llm_calls_allowed(AsyncMock(), now=sat_2pm)
    assert allowed is True


@pytest.mark.asyncio
async def test_empty_work_days_treated_as_paused(monkeypatch):
    _stub_settings(monkeypatch, llm_work_days="")
    tue_1030 = _aware(2026, 5, 26, 10, 30)
    allowed, reason = await llm_gate.llm_calls_allowed(
        AsyncMock(), now=tue_1030
    )
    assert allowed is False
    assert "no work days" in reason


@pytest.mark.asyncio
async def test_invalid_window_blocks(monkeypatch):
    """If admin saves nonsense (start >= end), fail closed."""
    _stub_settings(
        monkeypatch,
        llm_work_hours_start="18:00",
        llm_work_hours_end="08:00",
    )
    tue_1030 = _aware(2026, 5, 26, 10, 30)
    allowed, reason = await llm_gate.llm_calls_allowed(
        AsyncMock(), now=tue_1030
    )
    assert allowed is False
    assert "invalid" in reason.lower()


@pytest.mark.asyncio
async def test_exact_open_boundary(monkeypatch):
    """08:00 sharp on a workday = allowed (closed interval at start)."""
    _stub_settings(monkeypatch)
    tue_0800 = _aware(2026, 5, 26, 8, 0)
    allowed, _ = await llm_gate.llm_calls_allowed(AsyncMock(), now=tue_0800)
    assert allowed is True


@pytest.mark.asyncio
async def test_exact_close_boundary_excluded(monkeypatch):
    """18:00 sharp = NOT allowed (half-open interval [start, end))."""
    _stub_settings(monkeypatch)
    tue_1800 = _aware(2026, 5, 26, 18, 0)
    allowed, _ = await llm_gate.llm_calls_allowed(AsyncMock(), now=tue_1800)
    assert allowed is False


def test_parse_work_days_handles_garbage():
    """Defensive parsing — non-numeric tokens dropped, out-of-range dropped."""
    assert llm_gate._parse_work_days("1,2,abc,3,8,0,4,5") == {1, 2, 3, 4, 5}
    assert llm_gate._parse_work_days("") == set()
    assert llm_gate._parse_work_days(None) == set()
    assert llm_gate._parse_work_days("  1 , 2 ,3  ") == {1, 2, 3}


def test_unwrap_jsonb_shapes():
    """Both ``{"v": x}`` and ``{"value": x}`` storage shapes unwrap."""
    assert llm_gate._unwrap({"v": 42}) == 42
    assert llm_gate._unwrap({"value": "ten"}) == "ten"
    assert llm_gate._unwrap("plain") == "plain"
    assert llm_gate._unwrap(None) is None
