"""Tests for the calendar-driven Scheduler (Track 2C).

Three waves of tests matching Tasks 2C.1, 2C.2, 2C.3, plus voice-note
exemption + cap-timer tests (2C.6, 2C.7).

Wave 1 — pure adjacency grouping (no clock, no async)
Wave 2 — reconciliation under VirtualClock + stub orchestrator/calendar
Wave 3 — time-of-day logic (hard stop, 4-hr prompt, voice-note caps)
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass, field
from typing import Any

import pytest

from helios.clock import VirtualClock
from helios.config import HeliosConfig
from helios.scheduler.calendar import AegisProtocolError, AegisUnreachable
from helios.scheduler.scheduler import Scheduler
from helios.sources.interface import CalendarEvent
from helios.state import DaemonStateMachine


# ---------------------------------------------------------------------------
# Shared helpers / stubs
# ---------------------------------------------------------------------------


def _ev(
    ev_id: str, start_ts: float, end_ts: float, title: str = "T", **extra: Any
) -> CalendarEvent:
    """Build a CalendarEvent. ``extra`` lets tests inject ``is_excluded`` /
    ``kind`` for the defensive filter path even though those aren't first-class
    fields on the model."""
    e = CalendarEvent(
        id=ev_id,
        title=title,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    for k, v in extra.items():
        object.__setattr__(e, k, v)
    return e


@dataclass
class _StubOrchestrator:
    """Records start_session / stop_session calls."""

    next_session_id: int = 1000
    active_session_id: int | None = None
    starts: list[dict] = field(default_factory=list)
    stops: list[dict] = field(default_factory=list)
    raise_on_start: Exception | None = None
    raise_on_stop: Exception | None = None

    async def start_session(
        self, kind: str, calendar_events: list[CalendarEvent] | None = None
    ) -> int:
        if self.raise_on_start is not None:
            raise self.raise_on_start
        sid = self.next_session_id
        self.next_session_id += 1
        self.active_session_id = sid
        self.starts.append({"id": sid, "kind": kind, "events": calendar_events or []})
        return sid

    async def stop_session(self, session_id: int, reason: str) -> None:
        if self.raise_on_stop is not None:
            raise self.raise_on_stop
        self.stops.append({"id": session_id, "reason": reason})
        if self.active_session_id == session_id:
            self.active_session_id = None


class _StubCalendarSource:
    """Returns canned event lists per call.

    Use ``set_events(list)`` to control what every poll returns. To raise
    on a specific call, use ``raise_for_calls = [exc_or_None, ...]``.
    """

    def __init__(self) -> None:
        self._events: list[CalendarEvent] = []
        self.calls = 0
        self.raise_for_calls: list[Exception | None] = []

    def set_events(self, events: list[CalendarEvent]) -> None:
        self._events = events

    async def get_upcoming(self, horizon_minutes: int = 60) -> list[CalendarEvent]:
        idx = self.calls
        self.calls += 1
        if idx < len(self.raise_for_calls) and self.raise_for_calls[idx] is not None:
            raise self.raise_for_calls[idx]
        return list(self._events)


def _make_scheduler(
    clock: VirtualClock,
    *,
    orchestrator: _StubOrchestrator | None = None,
    calendar_source: _StubCalendarSource | None = None,
    state_machine: DaemonStateMachine | None = None,
    config: HeliosConfig | None = None,
) -> Scheduler:
    cfg = config or HeliosConfig()
    return Scheduler(
        clock=clock,
        calendar_source=calendar_source or _StubCalendarSource(),
        orchestrator=orchestrator or _StubOrchestrator(),
        state_machine=state_machine or DaemonStateMachine(),
        config=cfg,
    )


async def _settle(n: int = 6) -> None:
    """Yield enough times to let newly spawned tasks reach their first await."""
    for _ in range(n):
        await asyncio.sleep(0)


# ===========================================================================
# Wave 1 — Pure adjacency grouping (Task 2C.1)
# ===========================================================================


def test_group_adjacent_empty_list_returns_empty(virtual_clock):
    sched = _make_scheduler(virtual_clock)
    assert sched._group_adjacent([]) == []


def test_group_adjacent_two_meetings_six_minutes_apart_split(virtual_clock):
    """6 min gap > 5 min adjacency window → 2 groups."""
    cfg = HeliosConfig()
    # post=300, pre=60 → merge_window = 360s = 6 min
    # gap of 7 min should split.
    cfg.capture.calendar_post_end_seconds = 300
    cfg.capture.calendar_pre_start_seconds = 60
    sched = _make_scheduler(virtual_clock, config=cfg)

    a = _ev("a", 0, 100)
    b = _ev("b", 100 + 7 * 60, 200 + 7 * 60)  # 7 min after A ends
    groups = sched._group_adjacent([a, b])
    assert len(groups) == 2
    assert [g[0].id for g in groups] == ["a", "b"]


def test_group_adjacent_two_meetings_five_minutes_apart_merge(virtual_clock):
    """A 5-min gap is within the merge window → 1 group."""
    cfg = HeliosConfig()
    cfg.capture.calendar_post_end_seconds = 300
    cfg.capture.calendar_pre_start_seconds = 60
    sched = _make_scheduler(virtual_clock, config=cfg)

    a = _ev("a", 0, 100)
    b = _ev("b", 100 + 5 * 60, 200 + 5 * 60)  # 5 min after A ends
    groups = sched._group_adjacent([a, b])
    assert len(groups) == 1
    assert [e.id for e in groups[0]] == ["a", "b"]


def test_group_adjacent_overlapping_meetings_merge(virtual_clock):
    """Overlapping events always merge."""
    sched = _make_scheduler(virtual_clock)
    a = _ev("a", 0, 600)
    b = _ev("b", 300, 900)
    groups = sched._group_adjacent([a, b])
    assert len(groups) == 1
    assert {e.id for e in groups[0]} == {"a", "b"}


def test_group_adjacent_ABC_pattern(virtual_clock):
    """A(14:00-14:30), B(14:30-15:00), C(16:00-16:30) → [(A,B), (C)].

    With post=300 + pre=60 = 360s window:
    - A ends 14:30, B starts 14:30 → 0s gap → merge
    - B ends 15:00, C starts 16:00 → 3600s gap >> 360s → split
    """
    cfg = HeliosConfig()
    cfg.capture.calendar_post_end_seconds = 300
    cfg.capture.calendar_pre_start_seconds = 60
    sched = _make_scheduler(virtual_clock, config=cfg)

    base = 50_000.0
    a = _ev("a", base + 14 * 3600, base + 14.5 * 3600)
    b = _ev("b", base + 14.5 * 3600, base + 15 * 3600)
    c = _ev("c", base + 16 * 3600, base + 16.5 * 3600)
    groups = sched._group_adjacent([a, b, c])
    assert len(groups) == 2
    assert [e.id for e in groups[0]] == ["a", "b"]
    assert [e.id for e in groups[1]] == ["c"]


def test_group_adjacent_nested_events_merge(virtual_clock):
    """A(14:00-15:00) + B(14:15-14:45) → 1 group (nested)."""
    sched = _make_scheduler(virtual_clock)
    base = 50_000.0
    a = _ev("a", base, base + 3600)
    b = _ev("b", base + 900, base + 2700)
    groups = sched._group_adjacent([a, b])
    assert len(groups) == 1
    assert {e.id for e in groups[0]} == {"a", "b"}


def test_group_adjacent_excluded_filtered_out(virtual_clock):
    """Defensive: events with is_excluded=True are dropped before grouping."""
    sched = _make_scheduler(virtual_clock)
    a = _ev("a", 0, 100, is_excluded=True)
    b = _ev("b", 1000, 1100)
    groups = sched._group_adjacent([a, b])
    assert len(groups) == 1
    assert groups[0][0].id == "b"


def test_group_adjacent_voice_note_kind_filtered_out(virtual_clock):
    """Defensive: events tagged kind='voice_note' never enter a group."""
    sched = _make_scheduler(virtual_clock)
    a = _ev("a", 0, 100, kind="voice_note")
    b = _ev("b", 1000, 1100)
    groups = sched._group_adjacent([a, b])
    assert len(groups) == 1
    assert groups[0][0].id == "b"


def test_group_adjacent_unsorted_input_sorted_internally(virtual_clock):
    """Input order doesn't matter — _group_adjacent sorts by start_ts."""
    sched = _make_scheduler(virtual_clock)
    base = 50_000.0
    a = _ev("a", base + 100, base + 200)
    b = _ev("b", base + 0, base + 50)
    groups = sched._group_adjacent([a, b])
    # Two separate groups (gap >> merge window unless using big buffers).
    # With default config gap = 50, merge_window=360 → merge.
    assert len(groups) == 1
    # First group's first event is the chronologically earliest.
    assert groups[0][0].id == "b"


# ===========================================================================
# Wave 2 — Reconciliation under VirtualClock (Task 2C.2)
# ===========================================================================


async def test_reconcile_empty_no_ops(virtual_clock):
    sched = _make_scheduler(virtual_clock)
    await sched._reconcile([])
    assert sched._pending_timers == {}


async def test_new_event_schedules_timer_and_fires(virtual_clock):
    """A future event schedules a start timer; advancing past pre_start fires it."""
    orch = _StubOrchestrator()
    cs = _StubCalendarSource()
    cfg = HeliosConfig()
    cfg.scheduler.calendar_poll_seconds = 60
    sched = _make_scheduler(
        virtual_clock, orchestrator=orch, calendar_source=cs, config=cfg
    )

    now = virtual_clock.time()
    # Event starts in 5 minutes.
    e = _ev("e1", now + 300, now + 600)
    cs.set_events([e])

    await sched._reconcile([e])
    assert "e1" in sched._pending_timers

    # Pre-start = 60s; we need to advance now+240 to hit the fire time.
    await virtual_clock.advance(241.0)
    await _settle(8)

    assert len(orch.starts) == 1
    assert orch.starts[0]["kind"] == "calendar"
    assert orch.starts[0]["events"][0].id == "e1"
    # Pending timer entry was popped after firing.
    assert "e1" not in sched._pending_timers


async def test_event_removed_cancels_pending_timer(virtual_clock):
    """If an event vanishes from the next poll, its timer is cancelled."""
    orch = _StubOrchestrator()
    cs = _StubCalendarSource()
    sched = _make_scheduler(
        virtual_clock, orchestrator=orch, calendar_source=cs
    )

    now = virtual_clock.time()
    e = _ev("e1", now + 300, now + 600)
    await sched._reconcile([e])
    assert "e1" in sched._pending_timers

    # Next reconcile with empty list → drop the timer.
    await sched._reconcile([])
    assert "e1" not in sched._pending_timers

    # Advance past where it would have fired — orchestrator should be untouched.
    await virtual_clock.advance(400.0)
    await _settle(8)
    assert orch.starts == []


async def test_event_rescheduled_earlier_reschedules_timer(virtual_clock):
    """Same event id with a new start_ts replaces the existing timer."""
    orch = _StubOrchestrator()
    sched = _make_scheduler(virtual_clock, orchestrator=orch)

    now = virtual_clock.time()
    e_late = _ev("e1", now + 600, now + 900)
    await sched._reconcile([e_late])
    pt_late = sched._pending_timers["e1"]

    e_early = _ev("e1", now + 200, now + 500)
    await sched._reconcile([e_early])
    pt_early = sched._pending_timers["e1"]

    # New PendingTimer object replaced the old one.
    assert pt_early is not pt_late
    assert pt_early.group_start_ts == now + 200

    # Advance to the new fire time (200 - 60 = 140).
    await virtual_clock.advance(141.0)
    await _settle(8)
    assert len(orch.starts) == 1


async def test_active_session_for_cancelled_event_continues(virtual_clock):
    """Once the timer has fired and the session is active, removing the event
    from the next poll does not stop the active session."""
    orch = _StubOrchestrator()
    sched = _make_scheduler(virtual_clock, orchestrator=orch)

    now = virtual_clock.time()
    e = _ev("e1", now + 100, now + 600)
    await sched._reconcile([e])
    # Fire the start.
    await virtual_clock.advance(50.0)  # crosses pre_start (100-60=40s)
    await _settle(8)
    assert len(orch.starts) == 1
    assert orch.active_session_id is not None

    # Now reconcile with empty — should not stop the active session.
    await sched._reconcile([])
    await _settle(4)
    assert orch.stops == []
    assert orch.active_session_id is not None


async def test_pause_until_suppresses_event_in_window(virtual_clock):
    """An event whose start falls inside the pause window is not scheduled."""
    orch = _StubOrchestrator()
    sched = _make_scheduler(virtual_clock, orchestrator=orch)

    now = virtual_clock.time()
    # Pause for the next hour.
    await sched.set_pause(now + 3600)

    e = _ev("e1", now + 600, now + 900)
    await sched._reconcile([e])
    assert "e1" not in sched._pending_timers


async def test_pause_until_cancels_existing_pending_timers(virtual_clock):
    """Calling set_pause cancels timers for groups that fall inside the window."""
    orch = _StubOrchestrator()
    sched = _make_scheduler(virtual_clock, orchestrator=orch)

    now = virtual_clock.time()
    e = _ev("e1", now + 600, now + 900)
    await sched._reconcile([e])
    assert "e1" in sched._pending_timers

    # Pause through the event window.
    await sched.set_pause(now + 3600)
    assert "e1" not in sched._pending_timers


async def test_pause_until_does_not_suppress_event_after_window(virtual_clock):
    """Events starting AFTER the pause window are still scheduled."""
    orch = _StubOrchestrator()
    sched = _make_scheduler(virtual_clock, orchestrator=orch)

    now = virtual_clock.time()
    await sched.set_pause(now + 600)  # Pause 10 min.

    e = _ev("e1", now + 1800, now + 2400)  # 30 min from now > pause.
    await sched._reconcile([e])
    assert "e1" in sched._pending_timers


async def test_paused_until_property_auto_clears_on_expiry(virtual_clock):
    sched = _make_scheduler(virtual_clock)
    now = virtual_clock.time()
    await sched.set_pause(now + 100)
    assert sched.paused_until == now + 100
    await virtual_clock.advance(150.0)
    assert sched.paused_until is None


async def test_resume_clears_pause(virtual_clock):
    sched = _make_scheduler(virtual_clock)
    now = virtual_clock.time()
    await sched.set_pause(now + 3600)
    assert sched.paused_until is not None
    await sched.resume()
    assert sched.paused_until is None


async def test_concurrent_active_session_logs_and_skips(virtual_clock):
    """If start_session raises RuntimeError, the timer firing is a no-op."""
    orch = _StubOrchestrator(raise_on_start=RuntimeError("already active"))
    sched = _make_scheduler(virtual_clock, orchestrator=orch)
    now = virtual_clock.time()
    e = _ev("e1", now + 100, now + 200)
    await sched._reconcile([e])
    await virtual_clock.advance(50.0)  # cross pre_start
    await _settle(8)
    # No second crash, no stop_session calls, no recorded start.
    assert orch.starts == []


async def test_poll_loop_handles_aegis_unreachable(virtual_clock):
    """AegisUnreachable flips state.aegis_unreachable=True."""
    sm = DaemonStateMachine()
    cs = _StubCalendarSource()
    cs.raise_for_calls = [AegisUnreachable("connect refused")]
    sched = _make_scheduler(
        virtual_clock,
        calendar_source=cs,
        state_machine=sm,
    )

    await sched.start()
    # Let the first poll attempt run.
    await _settle(8)
    assert sm.current().aegis_unreachable is True

    # Replace error → next call succeeds → flag clears.
    cs.raise_for_calls = []
    cs.set_events([])
    await virtual_clock.advance(60.0)
    await _settle(8)
    assert sm.current().aegis_unreachable is False
    await sched.stop()


async def test_poll_loop_handles_protocol_error_no_flag_flip(virtual_clock):
    """AegisProtocolError logs + skips but does not flip the unreachable flag."""
    sm = DaemonStateMachine()
    cs = _StubCalendarSource()
    cs.raise_for_calls = [AegisProtocolError("schema mismatch")]
    sched = _make_scheduler(
        virtual_clock,
        calendar_source=cs,
        state_machine=sm,
    )

    await sched.start()
    await _settle(8)
    assert sm.current().aegis_unreachable is False
    await sched.stop()


# ===========================================================================
# Wave 3 — Time-of-day logic + voice-note timing (Tasks 2C.3, 2C.6, 2C.7)
# ===========================================================================


def _make_clock_at_local(hour: int, minute: int) -> VirtualClock:
    """Build a VirtualClock seeded so now_local() returns the given local HH:MM today."""
    today = datetime.date.today()
    target = datetime.datetime(
        today.year, today.month, today.day, hour, minute, 0
    )
    return VirtualClock(initial_ts=target.timestamp())


async def test_hard_stop_fires_for_continuous_session_at_1730():
    """Continuous session active at 17:30 local → stopped with hard_stop_530."""
    clock = _make_clock_at_local(17, 25)
    orch = _StubOrchestrator()
    cfg = HeliosConfig()
    cfg.capture.continuous_hard_stop_local = "17:30"
    sched = _make_scheduler(clock, orchestrator=orch, config=cfg)

    sid = await orch.start_session(kind="continuous")
    await sched.notify_session_started(sid, "continuous")

    # Cross 17:30.
    await clock.advance(6 * 60.0)
    await sched._maybe_fire_hard_stop()

    assert orch.stops == [{"id": sid, "reason": "hard_stop_530"}]


async def test_hard_stop_does_not_fire_for_calendar_session_at_1730():
    """Calendar session active at 17:30 → not stopped (exempt)."""
    clock = _make_clock_at_local(17, 25)
    orch = _StubOrchestrator()
    sched = _make_scheduler(clock, orchestrator=orch)

    sid = await orch.start_session(kind="calendar")
    await sched.notify_session_started(sid, "calendar")

    await clock.advance(6 * 60.0)
    await sched._maybe_fire_hard_stop()

    assert orch.stops == []
    assert orch.active_session_id == sid


async def test_hard_stop_does_not_fire_for_voice_note_at_1730():
    """Voice note at 17:30 → not stopped (exempt)."""
    clock = _make_clock_at_local(17, 25)
    orch = _StubOrchestrator()
    sched = _make_scheduler(clock, orchestrator=orch)

    sid = await orch.start_session(kind="voice_note")
    await sched.notify_session_started(sid, "voice_note")

    await clock.advance(6 * 60.0)
    await sched._maybe_fire_hard_stop()

    assert orch.stops == []


async def test_hard_stop_fires_only_once_per_day():
    """After the hard stop fires today, subsequent checks today no-op."""
    clock = _make_clock_at_local(17, 25)
    orch = _StubOrchestrator()
    sched = _make_scheduler(clock, orchestrator=orch)

    sid = await orch.start_session(kind="continuous")
    await sched.notify_session_started(sid, "continuous")

    await clock.advance(6 * 60.0)  # cross 17:30
    await sched._maybe_fire_hard_stop()
    assert len(orch.stops) == 1

    # Start a NEW continuous session later in the same day; the daily
    # latch must keep the loop quiet.
    sid2 = await orch.start_session(kind="continuous")
    await sched.notify_session_started(sid2, "continuous")
    await clock.advance(60 * 60.0)  # 18:30
    await sched._maybe_fire_hard_stop()
    assert len(orch.stops) == 1  # still just the first stop


async def test_hard_stop_skipped_when_no_active_session():
    """No active session at 17:30 → no error; latch records date."""
    clock = _make_clock_at_local(17, 25)
    orch = _StubOrchestrator()
    sched = _make_scheduler(clock, orchestrator=orch)

    await clock.advance(6 * 60.0)
    await sched._maybe_fire_hard_stop()
    assert orch.stops == []
    today = datetime.date.today().isoformat()
    assert sched._hard_stop_last_fired_date == today


async def test_four_hour_prompt_fires_after_four_hours(virtual_clock):
    """A continuous session running 4 hours fires the prompt
    (state.last_error == '4hr_prompt_pending')."""
    sm = DaemonStateMachine()
    orch = _StubOrchestrator()
    cfg = HeliosConfig()
    cfg.capture.continuous_prompt_hours = 4
    sched = _make_scheduler(
        virtual_clock, orchestrator=orch, state_machine=sm, config=cfg
    )

    sid = await orch.start_session(kind="continuous")
    await sched.notify_session_started(sid, "continuous")

    # Just before 4hr — no prompt yet.
    await virtual_clock.advance(4 * 3600 - 10)
    await sched._maybe_fire_4hr_prompt()
    assert sm.current().last_error != "4hr_prompt_pending"

    # Cross 4hr.
    await virtual_clock.advance(20)
    await sched._maybe_fire_4hr_prompt()
    assert sm.current().last_error == "4hr_prompt_pending"
    assert sched._active_continuous is not None
    assert sched._active_continuous.awaiting_response is True


async def test_prompt_response_continue_resets_clock(virtual_clock):
    sm = DaemonStateMachine()
    orch = _StubOrchestrator()
    cfg = HeliosConfig()
    cfg.capture.continuous_prompt_hours = 4
    sched = _make_scheduler(
        virtual_clock, orchestrator=orch, state_machine=sm, config=cfg
    )

    sid = await orch.start_session(kind="continuous")
    await sched.notify_session_started(sid, "continuous")

    await virtual_clock.advance(4 * 3600 + 1)
    await sched._maybe_fire_4hr_prompt()
    assert sched._active_continuous.awaiting_response is True

    await sched.respond_to_prompt(continue_session=True)
    assert sched._active_continuous.awaiting_response is False
    # Next prompt is now 4h in the future.
    expected = virtual_clock.time() + 4 * 3600
    assert sched._active_continuous.next_prompt_at == pytest.approx(expected)
    # No stop occurred.
    assert orch.stops == []


async def test_prompt_response_stop_stops_session(virtual_clock):
    sm = DaemonStateMachine()
    orch = _StubOrchestrator()
    cfg = HeliosConfig()
    cfg.capture.continuous_prompt_hours = 4
    sched = _make_scheduler(
        virtual_clock, orchestrator=orch, state_machine=sm, config=cfg
    )

    sid = await orch.start_session(kind="continuous")
    await sched.notify_session_started(sid, "continuous")

    await virtual_clock.advance(4 * 3600 + 1)
    await sched._maybe_fire_4hr_prompt()
    await sched.respond_to_prompt(continue_session=False)

    assert orch.stops == [{"id": sid, "reason": "4hr_prompt_stop"}]
    assert sched._active_continuous is None


async def test_prompt_timeout_stops_session(virtual_clock):
    """No response within continuous_prompt_timeout_seconds → session stops."""
    sm = DaemonStateMachine()
    orch = _StubOrchestrator()
    cfg = HeliosConfig()
    cfg.capture.continuous_prompt_hours = 4
    cfg.capture.continuous_prompt_timeout_seconds = 300
    sched = _make_scheduler(
        virtual_clock, orchestrator=orch, state_machine=sm, config=cfg
    )

    sid = await orch.start_session(kind="continuous")
    await sched.notify_session_started(sid, "continuous")

    await virtual_clock.advance(4 * 3600 + 1)
    await sched._maybe_fire_4hr_prompt()
    assert sched._active_continuous.awaiting_response is True

    # Advance past the timeout — the timeout callback fires.
    await virtual_clock.advance(301.0)
    await _settle(8)

    assert orch.stops == [{"id": sid, "reason": "4hr_prompt_stop"}]


# --- Voice note (2C.6, 2C.7) -----------------------------------------------


async def test_schedule_voice_note_caps_warning_then_force_stop(virtual_clock):
    orch = _StubOrchestrator()
    sched = _make_scheduler(virtual_clock, orchestrator=orch)

    sid = await orch.start_session(kind="voice_note")
    started_at = virtual_clock.time()
    await sched.schedule_voice_note_caps(sid, started_at, max_duration_seconds=300)

    # Cap warning fires at started_at + 270.
    await virtual_clock.advance(270.0)
    await _settle(8)
    timers = sched._voice_note_timers.get(sid)
    # Warning handle is cleared once it fires; force_stop still scheduled.
    assert timers is not None
    assert timers.warning is None
    assert orch.stops == []  # not stopped yet

    # Force stop at started_at + 300.
    await virtual_clock.advance(31.0)
    await _settle(8)
    assert orch.stops == [{"id": sid, "reason": "voice_note_cap_reached"}]
    assert sid not in sched._voice_note_timers


async def test_voice_note_caps_cancelled_when_stopped_early(virtual_clock):
    """Stopping the voice note early cancels both timers cleanly."""
    orch = _StubOrchestrator()
    sched = _make_scheduler(virtual_clock, orchestrator=orch)

    sid = await orch.start_session(kind="voice_note")
    started_at = virtual_clock.time()
    await sched.schedule_voice_note_caps(sid, started_at, max_duration_seconds=300)

    # User stops at 0:30; API cancels the caps.
    await virtual_clock.advance(30.0)
    await sched.cancel_voice_note_caps(sid)

    # Advance past both cap times — neither callback should run.
    await virtual_clock.advance(400.0)
    await _settle(8)
    assert orch.stops == []
    assert sid not in sched._voice_note_timers


async def test_voice_note_cap_warning_idempotent_reschedule(virtual_clock):
    """Calling schedule_voice_note_caps twice replaces the handles."""
    orch = _StubOrchestrator()
    sched = _make_scheduler(virtual_clock, orchestrator=orch)

    sid = await orch.start_session(kind="voice_note")
    started_at = virtual_clock.time()
    await sched.schedule_voice_note_caps(sid, started_at, max_duration_seconds=300)
    first_handles = sched._voice_note_timers[sid]

    # Reschedule with a longer duration.
    await sched.schedule_voice_note_caps(sid, started_at, max_duration_seconds=600)
    second_handles = sched._voice_note_timers[sid]
    assert first_handles is not second_handles


async def test_voice_note_force_stop_skips_when_session_already_gone(virtual_clock):
    """If the session has already been stopped externally, force_stop is a no-op."""
    orch = _StubOrchestrator()
    sched = _make_scheduler(virtual_clock, orchestrator=orch)

    sid = await orch.start_session(kind="voice_note")
    started_at = virtual_clock.time()
    await sched.schedule_voice_note_caps(sid, started_at, max_duration_seconds=300)

    # Session is stopped externally; force-stop should not double-stop.
    orch.active_session_id = None
    await virtual_clock.advance(310.0)
    await _settle(8)
    assert orch.stops == []


async def test_voice_note_adjacent_to_calendar_event_does_not_merge(virtual_clock):
    """A voice_note-tagged calendar entry never merges into a calendar group."""
    sched = _make_scheduler(virtual_clock)
    base = 50_000.0
    a = _ev("a", base, base + 600)
    vn = _ev("vn", base + 700, base + 1000, kind="voice_note")
    groups = sched._group_adjacent([a, vn])
    assert len(groups) == 1
    assert [e.id for e in groups[0]] == ["a"]


# --- Lifecycle / scheduler.start / scheduler.stop --------------------------


async def test_start_idempotent(virtual_clock):
    sched = _make_scheduler(virtual_clock)
    await sched.start()
    first_task = sched._poll_task
    await sched.start()
    assert sched._poll_task is first_task
    await sched.stop()


async def test_stop_idempotent_and_cancels_pending(virtual_clock):
    """stop() cancels pending timers + voice-note timers; repeat is safe."""
    orch = _StubOrchestrator()
    sched = _make_scheduler(virtual_clock, orchestrator=orch)

    now = virtual_clock.time()
    e = _ev("e1", now + 600, now + 900)
    await sched._reconcile([e])
    assert "e1" in sched._pending_timers

    sid = await orch.start_session(kind="voice_note")
    await sched.schedule_voice_note_caps(sid, now, max_duration_seconds=300)

    await sched.start()
    await sched.stop()
    assert sched._pending_timers == {}
    assert sched._voice_note_timers == {}
    # Repeat call — no error.
    await sched.stop()


async def test_notify_session_stopped_clears_active_continuous(virtual_clock):
    sched = _make_scheduler(virtual_clock)
    await sched.notify_session_started(42, "continuous")
    assert sched._active_continuous is not None
    await sched.notify_session_stopped(42)
    assert sched._active_continuous is None
    assert 42 not in sched._session_kinds


async def test_respond_to_prompt_no_pending_is_noop(virtual_clock):
    """Calling respond_to_prompt without an active prompt is safe."""
    orch = _StubOrchestrator()
    sched = _make_scheduler(virtual_clock, orchestrator=orch)
    await sched.respond_to_prompt(continue_session=True)
    await sched.respond_to_prompt(continue_session=False)
    assert orch.stops == []


# --- State machine transitions (task #21 fix) -----------------------------


async def test_notify_session_started_transitions_sm_to_recording(virtual_clock):
    sm = DaemonStateMachine()
    sched = _make_scheduler(virtual_clock, state_machine=sm)
    assert sm.current().mode == "armed"
    await sched.notify_session_started(42, "continuous")
    snap = sm.current()
    assert snap.mode == "recording"
    assert snap.active_session_id == 42


async def test_notify_session_stopped_transitions_sm_to_armed(virtual_clock):
    sm = DaemonStateMachine()
    sched = _make_scheduler(virtual_clock, state_machine=sm)
    await sched.notify_session_started(42, "continuous")
    assert sm.current().mode == "recording"
    await sched.notify_session_stopped(42)
    snap = sm.current()
    assert snap.mode == "armed"
    assert snap.active_session_id is None


async def test_notify_session_stopped_keeps_recording_with_other_active(
    virtual_clock,
):
    """Defensive: if another session is still tracked, stay in recording."""
    sm = DaemonStateMachine()
    sched = _make_scheduler(virtual_clock, state_machine=sm)
    await sched.notify_session_started(1, "continuous")
    await sched.notify_session_started(2, "voice_note")  # nested (defensive case)
    assert sm.current().mode == "recording"
    await sched.notify_session_stopped(1)
    # Session 2 still tracked → mode must remain recording.
    assert sm.current().mode == "recording"
    await sched.notify_session_stopped(2)
    assert sm.current().mode == "armed"


async def test_safe_stop_transitions_sm_back_to_armed(virtual_clock):
    """_safe_stop is the central scheduler stop site (hard-stop, scheduled
    end, 4hr-prompt all flow through it). It must transition SM."""
    orch = _StubOrchestrator()
    sm = DaemonStateMachine()
    sched = _make_scheduler(virtual_clock, orchestrator=orch, state_machine=sm)
    sid = await orch.start_session(kind="continuous")
    await sched.notify_session_started(sid, "continuous")
    assert sm.current().mode == "recording"
    await sched._safe_stop(sid, reason="hard_stop_530")
    assert sm.current().mode == "armed"
    assert sm.current().active_session_id is None


async def test_resume_to_armed_when_no_active_session(virtual_clock):
    sm = DaemonStateMachine()
    sched = _make_scheduler(virtual_clock, state_machine=sm)
    await sched.set_pause(virtual_clock.time() + 600)
    assert sm.current().mode == "paused"
    await sched.resume()
    snap = sm.current()
    assert snap.mode == "armed"
    assert snap.paused_until is None


async def test_resume_to_recording_when_session_active(virtual_clock):
    """Per HELIOS.md §11.6 active sessions persist across pause; resume
    must reflect that the daemon is still recording, not armed."""
    sm = DaemonStateMachine()
    sched = _make_scheduler(virtual_clock, state_machine=sm)
    await sched.notify_session_started(7, "continuous")
    await sched.set_pause(virtual_clock.time() + 600)
    # set_pause overrides mode to paused even though a session is active —
    # this matches the semantic that the SCHEDULER is paused (won't fire
    # new sessions) while the active session itself continues.
    assert sm.current().mode == "paused"
    await sched.resume()
    snap = sm.current()
    assert snap.mode == "recording"
    assert snap.paused_until is None


# --- Calendar source adapter (duck-typing) ---------------------------------


async def test_calendar_adapter_routes_horizon_minutes(virtual_clock):
    """If the source's get_upcoming takes horizon_minutes, pass minutes."""
    received: list[dict] = []

    class _MinutesSource:
        async def get_upcoming(self, horizon_minutes: int = 60):
            received.append({"horizon_minutes": horizon_minutes})
            return []

    sched = _make_scheduler(virtual_clock, calendar_source=_MinutesSource())
    await sched._calendar.get_upcoming(horizon_seconds=3600)
    assert received == [{"horizon_minutes": 60}]


async def test_calendar_adapter_routes_horizon_seconds(virtual_clock):
    """If the source's get_upcoming takes horizon_seconds, pass seconds."""
    received: list[dict] = []

    class _SecondsSource:
        async def get_upcoming(self, horizon_seconds: int = 60):
            received.append({"horizon_seconds": horizon_seconds})
            return []

    sched = _make_scheduler(virtual_clock, calendar_source=_SecondsSource())
    await sched._calendar.get_upcoming(horizon_seconds=3600)
    assert received == [{"horizon_seconds": 3600}]


# --- 4-hour prompt orphan-timer cancellation -------------------------------


async def test_prompt_response_cancels_timeout_handle(virtual_clock):
    """User responding before timeout cancels the orphan TimerHandle.

    Wave-4 critical: previously the call_later handle was discarded, so
    ``Scheduler.stop()`` couldn't cancel it — orphan timer fires after
    teardown.
    """
    sm = DaemonStateMachine()
    orch = _StubOrchestrator()
    cfg = HeliosConfig()
    cfg.capture.continuous_prompt_hours = 4
    cfg.capture.continuous_prompt_timeout_seconds = 300
    sched = _make_scheduler(
        virtual_clock, orchestrator=orch, state_machine=sm, config=cfg
    )

    sid = await orch.start_session(kind="continuous")
    await sched.notify_session_started(sid, "continuous")

    await virtual_clock.advance(4 * 3600 + 1)
    await sched._maybe_fire_4hr_prompt()

    # Handle should be armed.
    cont = sched._active_continuous
    assert cont is not None
    assert cont.timeout_handle is not None
    assert cont.timeout_handle.cancelled is False

    # User replies before the deadline.
    await sched.respond_to_prompt(continue_session=True)

    # Handle was cancelled and cleared so stop() has nothing to cancel.
    assert cont.timeout_handle is None
    # Advance past the original timeout — the orphan must NOT fire and
    # therefore must not stop the session.
    await virtual_clock.advance(301.0)
    await _settle(8)
    assert orch.stops == []


async def test_scheduler_stop_cancels_active_prompt_timeout(virtual_clock):
    """Scheduler.stop() cancels an in-flight prompt-timeout handle."""
    orch = _StubOrchestrator()
    cfg = HeliosConfig()
    cfg.capture.continuous_prompt_hours = 4
    cfg.capture.continuous_prompt_timeout_seconds = 300
    sched = _make_scheduler(virtual_clock, orchestrator=orch, config=cfg)

    sid = await orch.start_session(kind="continuous")
    await sched.notify_session_started(sid, "continuous")

    await virtual_clock.advance(4 * 3600 + 1)
    await sched._maybe_fire_4hr_prompt()

    handle = sched._active_continuous.timeout_handle
    assert handle is not None
    assert handle.cancelled is False

    await sched.stop()
    assert handle.cancelled is True


# --- Public accessors (Wave-5 added) ---------------------------------------


async def test_get_session_kind_returns_known_kind(virtual_clock):
    sched = _make_scheduler(virtual_clock)
    await sched.notify_session_started(7, "continuous")
    assert sched.get_session_kind(7) == "continuous"
    assert sched.get_session_kind(999) is None


async def test_get_active_session_info_for_continuous(virtual_clock):
    orch = _StubOrchestrator()
    sched = _make_scheduler(virtual_clock, orchestrator=orch)
    sid = await orch.start_session(kind="continuous")
    await sched.notify_session_started(sid, "continuous")
    info = sched.get_active_session_info()
    assert info is not None
    assert info.session_id == sid
    assert info.kind == "continuous"
    assert info.calendar_event_ids == []


async def test_get_next_calendar_event_returns_earliest(virtual_clock):
    orch = _StubOrchestrator()
    cs = _StubCalendarSource()
    cfg = HeliosConfig()
    cfg.capture.calendar_pre_start_seconds = 60
    sched = _make_scheduler(
        virtual_clock, orchestrator=orch, calendar_source=cs, config=cfg
    )

    now = virtual_clock.time()
    e1 = _ev("e1", now + 600, now + 900)
    e2 = _ev("e2", now + 7200, now + 7500)
    await sched._reconcile([e1, e2])

    nxt = sched.get_next_calendar_event()
    assert nxt is not None
    assert nxt.calendar_event_id == "e1"
    assert nxt.starts_at == now + 600
    assert nxt.pre_start_at == now + 540


def test_get_next_calendar_event_none_when_idle(virtual_clock):
    sched = _make_scheduler(virtual_clock)
    assert sched.get_next_calendar_event() is None
