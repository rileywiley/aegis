"""Calendar-driven capture scheduler.

Per HELIOS.md §11, the :class:`Scheduler` is the brain of calendar-driven
capture. It:

1. Polls Aegis's ``/api/meetings/upcoming`` endpoint every
   ``scheduler.calendar_poll_seconds`` (default 60s).
2. Maintains a local view of near-future events.
3. Reconciles scheduled timers against the event list on each poll
   (idempotent — same event list ⇒ same timer set).
4. Fires session-start timers ``capture.calendar_pre_start_seconds``
   (default 60s) before each non-excluded event.
5. Adjacency: if a session end is within ``calendar_post_end_seconds +
   calendar_pre_start_seconds`` of the next event's start, the events
   merge into one continuous session.
6. Enforces the 5:30 PM hard stop for continuous-mode sessions
   (timezone-aware, fires once per day).
7. Fires the 4-hour prompt for continuous-mode sessions.
8. Honours pause-until: future scheduled sessions are suppressed; active
   sessions continue.
9. Gracefully degrades when Aegis is unreachable
   (:class:`AegisUnreachable` flips ``state.aegis_unreachable=True`` and
    polling continues).
10. Voice notes are exempt from all of the above
    (no hard stop, no 4-hour prompt, no pause suppression, no merging
    into calendar groups). Voice notes have their own duration cap
    timers (see :meth:`schedule_voice_note_caps`).

Calendar-source duck-typing
---------------------------
The scheduler accepts either Wave 1/2B's :class:`CalendarClient` (HTTP
client to Aegis, expects ``horizon_minutes``) or
:class:`ReplayCalendarSource` (fixture-driven, expects
``horizon_seconds``). A small private adapter normalises the surface so
the scheduler always calls ``await source.get_upcoming(horizon_seconds)``
internally.

PII
---
Calendar event titles, attendee identifiers, and meeting bodies are PII.
Logs include only event ids, timestamps, and counts.
"""

from __future__ import annotations

import asyncio
import datetime
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

from helios.clock import Clock, TimerHandle
from helios.config import HeliosConfig
from helios.log import get_logger
from helios.scheduler.calendar import AegisProtocolError, AegisUnreachable
from helios.sources.interface import CalendarEvent
from helios.state import Mode

_log = get_logger("scheduler")


SessionKind = Literal["calendar", "continuous", "manual_screen", "voice_note"]


# ---------------------------------------------------------------------------
# Calendar source adapter (duck-typing)
# ---------------------------------------------------------------------------


class _CalendarSourceLike(Protocol):
    """Either a CalendarClient or a CalendarSource — both expose get_upcoming."""

    async def get_upcoming(self, *args: Any, **kwargs: Any) -> list[CalendarEvent]:
        ...


class _CalendarSourceAdapter:
    """Normalises CalendarClient (horizon_minutes) and CalendarSource
    (horizon_seconds) onto a single ``get_upcoming(horizon_seconds)`` API.

    Detection runs once at construction by inspecting the underlying
    callable's signature; subsequent calls dispatch via the chosen kwarg
    (no per-call introspection cost).
    """

    def __init__(self, source: _CalendarSourceLike) -> None:
        self._source = source
        # Detect whether the source uses horizon_minutes or horizon_seconds.
        try:
            sig = inspect.signature(source.get_upcoming)
            params = sig.parameters
        except (TypeError, ValueError):
            params = {}
        if "horizon_minutes" in params:
            self._kw = "horizon_minutes"
            self._scale = 1 / 60.0
        elif "horizon_seconds" in params:
            self._kw = "horizon_seconds"
            self._scale = 1.0
        else:
            # Fall back: pass positional via horizon_seconds — tests / stubs
            # using **kwargs may not declare either name.
            self._kw = "horizon_seconds"
            self._scale = 1.0

    async def get_upcoming(self, horizon_seconds: int) -> list[CalendarEvent]:
        value = max(1, int(horizon_seconds * self._scale))
        return await self._source.get_upcoming(**{self._kw: value})


# ---------------------------------------------------------------------------
# State machine + orchestrator protocols (so we don't drag in concrete deps)
# ---------------------------------------------------------------------------


class _StateMachineLike(Protocol):
    async def transition(self, *args: Any, **kwargs: Any) -> Any:
        ...


class _OrchestratorLike(Protocol):
    @property
    def active_session_id(self) -> int | None: ...
    async def start_session(
        self, kind: str, calendar_events: list[CalendarEvent] | None = None
    ) -> int: ...
    async def stop_session(self, session_id: int, reason: str) -> None: ...


# ---------------------------------------------------------------------------
# Internal bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _ActiveContinuous:
    """Tracks an active continuous session for the 4-hour prompt loop."""

    session_id: int
    started_at: float
    next_prompt_at: float
    awaiting_response: bool = False
    pending_response_event: asyncio.Event | None = None
    # Handle to the prompt-timeout TimerHandle so we can cancel it when
    # the user replies before timeout fires (and at scheduler shutdown).
    timeout_handle: TimerHandle | None = None


@dataclass
class _VoiceNoteTimers:
    """Cap-warning + force-stop handles for a single voice-note session."""

    warning: TimerHandle | None = None
    force_stop: TimerHandle | None = None


@dataclass
class _PendingTimer:
    """A scheduled start for an adjacency group."""

    handle: TimerHandle
    group_start_ts: float
    group_end_ts: float


@dataclass
class ActiveSessionInfo:
    """Minimal metadata about an active session known to the scheduler.

    Exposed to status / diagnostics endpoints via
    :meth:`Scheduler.get_active_session_info` so route handlers don't
    need to reach into private attrs to build the §7.3 ``active_session``
    object.
    """

    session_id: int
    kind: SessionKind
    started_at: float
    calendar_event_ids: list[str]
    screen_capture_override_until: float | None = None


@dataclass
class NextCalendarEventInfo:
    """Next near-future calendar event the scheduler is watching.

    ``pre_start_at`` is the wall-clock ts at which the start timer will
    fire (``starts_at - calendar_pre_start_seconds``). Powers the §7.3
    ``next_calendar_event`` field on ``GET /v1/status``.
    """

    calendar_event_id: str
    title: str
    starts_at: float
    pre_start_at: float


# ---------------------------------------------------------------------------
# Public scheduler
# ---------------------------------------------------------------------------


# Sentinels used so callers can pass explicit None.
_NO_SESSION: Any = object()


class Scheduler:
    """Calendar-driven capture scheduler.

    Construction is pure DI: clock, calendar source, capture orchestrator,
    daemon state machine, and the loaded :class:`HeliosConfig`. Construct
    once at daemon startup; call :meth:`start` to spawn the background
    loops, :meth:`stop` to tear them down.

    The scheduler does not touch the database directly — session DB writes
    flow through the orchestrator.
    """

    def __init__(
        self,
        clock: Clock,
        calendar_source: _CalendarSourceLike,
        orchestrator: _OrchestratorLike,
        state_machine: _StateMachineLike,
        config: HeliosConfig,
    ) -> None:
        self._clock = clock
        self._calendar = _CalendarSourceAdapter(calendar_source)
        self._orchestrator = orchestrator
        self._sm = state_machine
        self._config = config

        # Pause window. ``None`` means not paused; a float is the wall-clock
        # ts at which the pause expires.
        self._pause_until: float | None = None

        # Pending start timers, keyed by group-key (the joined event ids of
        # the group). On reconciliation, timers for keys no longer present
        # are cancelled; new keys get fresh timers.
        self._pending_timers: dict[str, _PendingTimer] = {}

        # Per-session stop timers (one per active calendar group). Keyed by
        # session_id so :meth:`stop` can cancel them all.
        self._stop_timers: dict[int, TimerHandle] = {}

        # Per-session voice-note cap timers.
        self._voice_note_timers: dict[int, _VoiceNoteTimers] = {}

        # Local view of session kinds for sessions the scheduler knows
        # about (calendar groups it started + sessions API agents notified
        # about via :meth:`notify_session_started`).
        self._session_kinds: dict[int, SessionKind] = {}

        # Per-session metadata (started_at, calendar_event_ids,
        # screen_capture_override_until) for the §7.3 active_session
        # status payload. Mirrors ``_session_kinds`` lifecycle.
        self._active_sessions: dict[int, ActiveSessionInfo] = {}

        # Active continuous-session tracking for the 4-hour prompt loop.
        self._active_continuous: _ActiveContinuous | None = None

        # Hard-stop "fired today" guard — date string ``YYYY-MM-DD``.
        self._hard_stop_last_fired_date: str | None = None

        # Background loop task handles.
        self._poll_task: asyncio.Task | None = None
        self._hard_stop_task: asyncio.Task | None = None
        self._four_hour_task: asyncio.Task | None = None

        # Strong references to fire-and-forget tasks spawned by timer
        # callbacks. Without this, Python may garbage-collect a running
        # task before it completes (CPython prints RuntimeWarning and the
        # work can vanish depending on event-loop state).
        self._tasks: set[asyncio.Task] = set()

        self._stopping = False
        self._aegis_was_unreachable = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _spawn_task(self, coro: Awaitable[Any]) -> asyncio.Task:
        """Schedule ``coro`` as a task and keep a strong reference.

        Use this instead of bare ``asyncio.create_task`` for any
        fire-and-forget coroutine spawned from a timer callback so the
        task isn't garbage-collected mid-flight.
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._poll_task is not None and not self._poll_task.done()

    @property
    def paused_until(self) -> float | None:
        """Current pause expiry, or ``None`` if not paused.

        Auto-clears when the wall-clock crosses the expiry — readers see a
        live value.
        """
        if self._pause_until is None:
            return None
        if self._clock.time() >= self._pause_until:
            self._pause_until = None
            return None
        return self._pause_until

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn background loops. Idempotent — second call is a no-op."""
        if self.is_running:
            return
        self._stopping = False
        self._poll_task = asyncio.create_task(self._poll_loop(), name="scheduler-poll")
        self._hard_stop_task = asyncio.create_task(
            self._hard_stop_loop(), name="scheduler-hard-stop"
        )
        self._four_hour_task = asyncio.create_task(
            self._four_hour_prompt_loop(), name="scheduler-4hr-prompt"
        )
        _log.info("scheduler_started")

    async def stop(self) -> None:
        """Cancel all loops + pending timers. Idempotent."""
        self._stopping = True

        # Cancel all pending start timers.
        for key, pt in list(self._pending_timers.items()):
            try:
                pt.handle.cancel()
            except Exception:  # noqa: BLE001 — defensive
                pass
        self._pending_timers.clear()

        # Cancel all stop timers.
        for sid, h in list(self._stop_timers.items()):
            try:
                h.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._stop_timers.clear()

        # Cancel all voice-note cap timers.
        for sid, t in list(self._voice_note_timers.items()):
            for handle in (t.warning, t.force_stop):
                if handle is not None:
                    try:
                        handle.cancel()
                    except Exception:  # noqa: BLE001
                        pass
        self._voice_note_timers.clear()

        # Cancel the orphan 4-hr-prompt timeout handle if armed.
        if self._active_continuous is not None:
            handle = self._active_continuous.timeout_handle
            if handle is not None:
                try:
                    handle.cancel()
                except Exception:  # noqa: BLE001
                    pass
                self._active_continuous.timeout_handle = None

        # Cancel background tasks.
        for task in (self._poll_task, self._hard_stop_task, self._four_hour_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._poll_task = None
        self._hard_stop_task = None
        self._four_hour_task = None

        # Cancel any fire-and-forget tasks spawned by timer callbacks.
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()

        _log.info("scheduler_stopped")

    # ------------------------------------------------------------------
    # Public — pause / resume
    # ------------------------------------------------------------------

    async def set_pause(self, until_ts: float) -> None:
        """Set a pause window — new schedule decisions suppressed until ``until_ts``.

        Active sessions continue (per HELIOS.md §11.6 / Q8k). Voice notes
        are not affected. Cancels any pending start timers whose group
        starts within the pause window; the next poll re-reconciles them
        if they still fall outside the window.
        """
        self._pause_until = float(until_ts)
        cancelled: list[str] = []
        for key, pt in list(self._pending_timers.items()):
            if pt.group_start_ts < self._pause_until:
                pt.handle.cancel()
                del self._pending_timers[key]
                cancelled.append(key)
        await self._sm.transition(mode="paused", paused_until=self._pause_until)
        _log.info(
            "pause_set",
            until_ts=self._pause_until,
            cancelled_pending=len(cancelled),
        )

    async def resume(self) -> None:
        """Clear the pause window early. Next poll re-reconciles.

        Transitions the daemon mode back to ``recording`` if a session is
        still tracked (rare — pause+active per HELIOS.md §11.6 keeps the
        active session running), otherwise back to ``armed``.
        """
        self._pause_until = None
        new_mode: Mode = "recording" if self._session_kinds else "armed"
        await self._sm.transition(mode=new_mode, paused_until=None)
        _log.info("pause_cleared", new_mode=new_mode)

    # ------------------------------------------------------------------
    # Public — session-kind notification (for sessions started outside
    # the scheduler, e.g. continuous mode kicked off by the API)
    # ------------------------------------------------------------------

    def get_session_kind(self, session_id: int) -> SessionKind | None:
        """Public accessor for the kind of an active session known to the scheduler.

        Returns ``None`` if the scheduler has no record (e.g. excerpt
        voice-note path runs the lookup in routes/voice_note before the
        new session is registered).
        """
        return self._session_kinds.get(session_id)

    def get_active_session_info(
        self, session_id: int | None = None
    ) -> ActiveSessionInfo | None:
        """Active-session metadata for the §7.3 ``active_session`` field.

        With ``session_id=None`` (the default), returns the unique
        active session's info if there is exactly one — convenient for
        status route handlers that just want "the" active session.
        """
        if session_id is not None:
            return self._active_sessions.get(session_id)
        if len(self._active_sessions) == 1:
            return next(iter(self._active_sessions.values()))
        return None

    def get_next_calendar_event(self) -> NextCalendarEventInfo | None:
        """Earliest pending calendar group, or None if nothing is queued.

        Powers the §7.3 ``next_calendar_event`` field. Returns the first
        event of the earliest-pending group (a group is a set of
        adjacent events that will fire one capture session). PII-safe
        because Helios only knows the title from the event itself; the
        title is intended for the menu bar display.
        """
        if not self._pending_timers:
            return None
        # The pending_timers dict isn't sorted; pick the earliest by start_ts.
        earliest_key, earliest_pt = min(
            self._pending_timers.items(), key=lambda kv: kv[1].group_start_ts
        )
        # Earliest key is "id1|id2|..." — first id wins.
        first_id = earliest_key.split("|", 1)[0]
        pre = self._config.capture.calendar_pre_start_seconds
        return NextCalendarEventInfo(
            calendar_event_id=first_id,
            title="",  # Title not retained on the timer; route can backfill.
            starts_at=earliest_pt.group_start_ts,
            pre_start_at=earliest_pt.group_start_ts - pre,
        )

    async def notify_session_started(
        self, session_id: int, kind: SessionKind
    ) -> None:
        """Inform the scheduler that a session of ``kind`` is now active.

        Required for hard-stop / 4-hr-prompt logic on sessions the
        scheduler did not start itself (e.g. continuous mode kicked off
        from the API). Also fires the daemon state-machine transition
        ``mode="recording"`` so ``/v1/status`` reflects the active
        session — covers both the API path (capture.py / voice_note.py
        call this directly) and the scheduler path (``_start_group``
        calls this after orchestrator.start_session).
        """
        self._session_kinds[session_id] = kind
        started_at = self._clock.time()
        # Track active-session metadata for the §7.3 status payload. Note
        # ``_start_group`` overwrites this entry with the real
        # calendar_event_ids list.
        self._active_sessions[session_id] = ActiveSessionInfo(
            session_id=session_id,
            kind=kind,
            started_at=started_at,
            calendar_event_ids=[],
        )
        if kind == "continuous":
            next_prompt_at = (
                started_at + self._config.capture.continuous_prompt_hours * 3600
            )
            self._active_continuous = _ActiveContinuous(
                session_id=session_id,
                started_at=started_at,
                next_prompt_at=next_prompt_at,
            )
        # Mode = recording while ANY session is active. We always overwrite
        # active_session_id with the just-started session so /v1/status
        # consistently surfaces the most-recent start.
        await self._sm.transition(mode="recording", active_session_id=session_id)
        _log.info("session_kind_notified", session_id=session_id, kind=kind)

    async def notify_session_stopped(self, session_id: int) -> None:
        """Forget about a session and roll the daemon back to ``armed``.

        Cancels its stop timer + 4hr-prompt timeout (if any). Fires the
        state-machine transition back to ``mode="armed"`` ONLY when no
        other session is still tracked — defensive guard for the (not
        currently supported) overlap case.
        """
        self._session_kinds.pop(session_id, None)
        self._active_sessions.pop(session_id, None)
        if self._active_continuous and self._active_continuous.session_id == session_id:
            # Cancel the prompt-timeout timer so it doesn't fire after teardown.
            handle = self._active_continuous.timeout_handle
            if handle is not None:
                try:
                    handle.cancel()
                except Exception:  # noqa: BLE001
                    pass
            self._active_continuous = None
        h = self._stop_timers.pop(session_id, None)
        if h is not None:
            try:
                h.cancel()
            except Exception:  # noqa: BLE001
                pass
        if not self._session_kinds:
            # No other session tracked → back to armed.
            await self._sm.transition(mode="armed", active_session_id=None)

    # ------------------------------------------------------------------
    # Public — voice-note cap scheduling (Task 2C.7)
    # ------------------------------------------------------------------

    async def schedule_voice_note_caps(
        self,
        voice_note_session_id: int,
        started_at: float,
        max_duration_seconds: int,
    ) -> None:
        """Schedule cap_warning (max-30s) + force_stop (max) callbacks.

        Idempotent: scheduling twice for the same session replaces the
        previous handles.
        """
        # Cancel any previous timers for this session first.
        await self.cancel_voice_note_caps(voice_note_session_id)

        # Record kind so other loops know to skip.
        self._session_kinds[voice_note_session_id] = "voice_note"

        now = self._clock.time()
        warning_delay = max(0.0, started_at + max_duration_seconds - 30 - now)
        stop_delay = max(0.0, started_at + max_duration_seconds - now)

        timers = _VoiceNoteTimers()

        def _warning_cb() -> None:
            self._spawn_task(self._on_voice_note_warning(voice_note_session_id))

        def _force_stop_cb() -> None:
            self._spawn_task(self._on_voice_note_force_stop(voice_note_session_id))

        timers.warning = self._clock.call_later(warning_delay, _warning_cb)
        timers.force_stop = self._clock.call_later(stop_delay, _force_stop_cb)
        self._voice_note_timers[voice_note_session_id] = timers
        _log.info(
            "voice_note_caps_scheduled",
            session_id=voice_note_session_id,
            warning_delay=warning_delay,
            stop_delay=stop_delay,
        )

    async def cancel_voice_note_caps(self, voice_note_session_id: int) -> None:
        """Cancel the cap_warning + force_stop callbacks. Idempotent."""
        timers = self._voice_note_timers.pop(voice_note_session_id, None)
        if timers is None:
            return
        for handle in (timers.warning, timers.force_stop):
            if handle is not None:
                try:
                    handle.cancel()
                except Exception:  # noqa: BLE001
                    pass
        _log.info("voice_note_caps_cancelled", session_id=voice_note_session_id)

    # ------------------------------------------------------------------
    # Public — 4-hour prompt response (Phase 4 will wire UI; for now
    # this is the response channel the scheduler uses)
    # ------------------------------------------------------------------

    async def respond_to_prompt(self, continue_session: bool) -> None:
        """User response to the 4-hour continue/stop prompt.

        ``True`` → reset the prompt clock; the next prompt fires after
        another ``continuous_prompt_hours``. ``False`` → stop the active
        continuous session.
        """
        cont = self._active_continuous
        if cont is None or not cont.awaiting_response:
            _log.info("prompt_response_no_pending")
            return
        cont.awaiting_response = False
        # Cancel the orphan-timeout timer — user beat the deadline.
        if cont.timeout_handle is not None:
            try:
                cont.timeout_handle.cancel()
            except Exception:  # noqa: BLE001 — defensive
                pass
            cont.timeout_handle = None
        if cont.pending_response_event is not None:
            cont.pending_response_event.set()
        # Clear ``last_error="4hr_prompt_pending"`` set in
        # ``_maybe_fire_4hr_prompt`` so the menu bar's transition
        # detector sees pending→cleared→pending across consecutive
        # prompts and posts a fresh banner each time. Without this
        # the menu bar's dedup keeps the second prompt silent.
        await self._sm.transition(last_error=None)
        if continue_session:
            now = self._clock.time()
            cont.next_prompt_at = (
                now + self._config.capture.continuous_prompt_hours * 3600
            )
            _log.info(
                "prompt_response_continue",
                session_id=cont.session_id,
                next_prompt_at=cont.next_prompt_at,
            )
        else:
            sid = cont.session_id
            _log.info("prompt_response_stop", session_id=sid)
            await self._safe_stop(sid, reason="4hr_prompt_stop")
            self._session_kinds.pop(sid, None)
            self._active_sessions.pop(sid, None)
            self._active_continuous = None

    # ==================================================================
    # Internals — adjacency grouping (pure logic, exposed via _group_adjacent)
    # ==================================================================

    def _group_adjacent(
        self, events: list[CalendarEvent]
    ) -> list[list[CalendarEvent]]:
        """Group events whose edges fall within the merge window.

        Two events merge when the next event's start is within
        ``calendar_post_end_seconds + calendar_pre_start_seconds`` of the
        previous event's end (overlap also counts).

        Voice-note kinds never reach the scheduler's reconciliation
        path, but as belt-and-suspenders any event tagged with a
        ``kind`` attribute equal to ``voice_note`` is filtered out
        defensively.
        """
        if not events:
            return []
        # Defensive filtering: drop any event marked excluded or
        # voice-note. CalendarEvent does not currently carry these
        # fields, but stub/test events may.
        filtered = [
            e
            for e in events
            if not getattr(e, "is_excluded", False)
            and getattr(e, "kind", None) != "voice_note"
        ]
        sorted_events = sorted(filtered, key=lambda e: e.start_ts)
        buffer = self._config.capture.calendar_post_end_seconds
        pre = self._config.capture.calendar_pre_start_seconds
        merge_window = buffer + pre

        groups: list[list[CalendarEvent]] = []
        for event in sorted_events:
            if not groups:
                groups.append([event])
                continue
            # Track the LATEST end across the current group so nested
            # events (B fully inside A) still merge.
            current_end = max(e.end_ts for e in groups[-1])
            gap = event.start_ts - current_end
            if gap <= merge_window:
                groups[-1].append(event)
            else:
                groups.append([event])
        return groups

    @staticmethod
    def _group_key(group: list[CalendarEvent]) -> str:
        """Stable key so reconciliation can match groups across polls.

        Uses the joined event ids — order is determined by the input,
        which is sorted by ``start_ts`` in :meth:`_group_adjacent`.
        """
        return "|".join(e.id for e in group)

    # ==================================================================
    # Internals — poll loop
    # ==================================================================

    async def _poll_loop(self) -> None:
        """Poll the calendar source forever; reconcile after each success."""
        try:
            while not self._stopping:
                try:
                    events = await self._calendar.get_upcoming(horizon_seconds=3600)
                except AegisUnreachable as exc:
                    if not self._aegis_was_unreachable:
                        await self._sm.transition(aegis_unreachable=True)
                        self._aegis_was_unreachable = True
                    _log.warning("aegis_unreachable", error=str(exc))
                except AegisProtocolError as exc:
                    _log.warning("aegis_protocol_error", error=str(exc))
                    # Don't flip the unreachable flag; this is a schema
                    # bug, not a connectivity issue. Skip this cycle.
                except Exception as exc:  # noqa: BLE001 — never crash the loop
                    _log.warning(
                        "calendar_poll_failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                else:
                    if self._aegis_was_unreachable:
                        await self._sm.transition(aegis_unreachable=False)
                        self._aegis_was_unreachable = False
                    await self._reconcile(events)

                if self._stopping:
                    return
                await self._clock.sleep(self._config.scheduler.calendar_poll_seconds)
        except asyncio.CancelledError:
            raise

    async def _reconcile(self, events: list[CalendarEvent]) -> None:
        """Sync pending start timers to the current event list."""
        groups = self._group_adjacent(events)
        target_keys: dict[str, list[CalendarEvent]] = {
            self._group_key(g): g for g in groups
        }

        # 1) Cancel timers for groups no longer present.
        for stale_key in [k for k in self._pending_timers if k not in target_keys]:
            self._pending_timers[stale_key].handle.cancel()
            del self._pending_timers[stale_key]
            _log.info("pending_timer_cancelled", group_key=stale_key)

        # 2) Schedule new groups.
        now = self._clock.time()
        for key, group in target_keys.items():
            if key in self._pending_timers:
                # Already have a timer; check that its window matches
                # (rescheduled events may need a new fire time).
                pt = self._pending_timers[key]
                desired_start = group[0].start_ts
                if pt.group_start_ts != desired_start:
                    pt.handle.cancel()
                    del self._pending_timers[key]
                else:
                    continue

            # Pause window: skip groups whose start falls inside it.
            if (
                self._pause_until is not None
                and group[0].start_ts < self._pause_until
            ):
                continue

            pre_start_ts = group[0].start_ts - self._config.capture.calendar_pre_start_seconds
            delay = max(0.0, pre_start_ts - now)

            def _fire(g: list[CalendarEvent] = group) -> None:
                self._spawn_task(self._start_group(g))

            handle = self._clock.call_later(delay, _fire)
            self._pending_timers[key] = _PendingTimer(
                handle=handle,
                group_start_ts=group[0].start_ts,
                group_end_ts=group[-1].end_ts,
            )
            _log.info(
                "pending_timer_scheduled",
                group_key=key,
                event_count=len(group),
                fire_in_seconds=delay,
            )

    # ==================================================================
    # Internals — group start / stop
    # ==================================================================

    async def _start_group(self, group: list[CalendarEvent]) -> None:
        """Start a calendar session for ``group`` and schedule its stop."""
        key = self._group_key(group)
        # Drop the pending entry — the timer has fired.
        self._pending_timers.pop(key, None)

        try:
            session_id = await self._orchestrator.start_session(
                kind="calendar",
                calendar_events=group,
            )
        except RuntimeError as exc:
            # Concurrent active session — surface and bail. Next poll
            # cycle re-reconciles.
            _log.warning(
                "start_group_concurrent_active",
                group_key=key,
                error=str(exc),
            )
            return
        except Exception as exc:  # noqa: BLE001 — never crash scheduler
            _log.warning(
                "start_group_failed",
                group_key=key,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return

        # Centralized notify — also fires state_machine.transition so
        # /v1/status.mode flips to "recording". Overwrite the
        # active_sessions entry afterwards with the real calendar event
        # ids (notify_session_started seeds it with []).
        await self.notify_session_started(session_id, "calendar")
        self._active_sessions[session_id] = ActiveSessionInfo(
            session_id=session_id,
            kind="calendar",
            started_at=self._clock.time(),
            calendar_event_ids=[e.id for e in group],
        )

        # Schedule the stop at the last event's end + post-end buffer.
        end_ts = group[-1].end_ts + self._config.capture.calendar_post_end_seconds
        delay = max(0.0, end_ts - self._clock.time())

        def _stop_cb(sid: int = session_id) -> None:
            self._spawn_task(self._stop_group_callback(sid))

        self._stop_timers[session_id] = self._clock.call_later(delay, _stop_cb)
        _log.info(
            "group_started",
            group_key=key,
            session_id=session_id,
            event_count=len(group),
            stop_in_seconds=delay,
        )

    async def _stop_group_callback(self, session_id: int) -> None:
        """Stop callback for a calendar group."""
        self._stop_timers.pop(session_id, None)
        await self._safe_stop(session_id, reason="scheduled_end")

    async def _safe_stop(self, session_id: int, reason: str) -> None:
        """Stop a session via the orchestrator, swallowing all errors.

        Also fires ``notify_session_stopped`` (which transitions the
        daemon state machine back to ``armed`` when no session remains).
        """
        try:
            await self._orchestrator.stop_session(session_id, reason=reason)
        except ValueError as exc:
            # Session already stopped or id mismatch — fine, log and move on.
            _log.info(
                "stop_session_no_op",
                session_id=session_id,
                reason=reason,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "stop_session_failed",
                session_id=session_id,
                reason=reason,
                error=str(exc),
                error_type=type(exc).__name__,
            )
        # Always run notify_session_stopped — clears scheduler bookkeeping
        # AND transitions the daemon state machine back to ``armed`` (when
        # no other session remains). Run even on the failure paths above so
        # we don't leak _session_kinds / _active_sessions entries.
        await self.notify_session_stopped(session_id)

    # ==================================================================
    # Internals — hard stop loop (5:30 PM)
    # ==================================================================

    def _parse_hard_stop_time(self) -> tuple[int, int]:
        """Parse ``capture.continuous_hard_stop_local`` into (hour, minute)."""
        raw = self._config.capture.continuous_hard_stop_local
        try:
            hh, mm = raw.split(":", 1)
            return int(hh), int(mm)
        except Exception:  # noqa: BLE001
            return 17, 30

    async def _hard_stop_loop(self) -> None:
        """Once per minute, fire the hard stop if a continuous session is active."""
        try:
            while not self._stopping:
                try:
                    await self._maybe_fire_hard_stop()
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "hard_stop_iteration_failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                if self._stopping:
                    return
                # Use the same cadence as polling for predictable test
                # advancement. Defaults to 60s.
                await self._clock.sleep(self._config.scheduler.calendar_poll_seconds)
        except asyncio.CancelledError:
            raise

    async def _maybe_fire_hard_stop(self) -> None:
        """Check whether the configured hard stop should fire NOW."""
        now_local = self._clock.now_local()
        today_str = now_local.date().isoformat()
        if self._hard_stop_last_fired_date == today_str:
            return  # Already fired today.
        hour, minute = self._parse_hard_stop_time()
        stop_time_today = now_local.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if now_local < stop_time_today:
            return

        active_sid = self._orchestrator.active_session_id
        if active_sid is None:
            # No active session at all — record the date so we don't fire later.
            self._hard_stop_last_fired_date = today_str
            return

        kind = self._session_kinds.get(active_sid)
        if kind != "continuous":
            # Only continuous sessions are subject to the 5:30 PM hard
            # stop; calendar / voice_note are exempt. Mark as fired for
            # today either way to avoid re-checking every minute.
            self._hard_stop_last_fired_date = today_str
            _log.info(
                "hard_stop_skipped_non_continuous",
                session_id=active_sid,
                kind=kind,
            )
            return

        _log.info("hard_stop_firing", session_id=active_sid)
        # _safe_stop calls notify_session_stopped which handles the
        # _session_kinds / _active_sessions / _active_continuous cleanup
        # AND fires the state-machine transition back to "armed".
        await self._safe_stop(active_sid, reason="hard_stop_530")
        self._hard_stop_last_fired_date = today_str

    # ==================================================================
    # Internals — 4-hour prompt loop
    # ==================================================================

    async def _four_hour_prompt_loop(self) -> None:
        """Watch active continuous session; fire prompt every N hours."""
        # Use a tighter cadence for the prompt loop so virtual-clock tests
        # don't have to advance in 60s steps to observe behavior.
        sleep_seconds = max(1, self._config.scheduler.calendar_poll_seconds // 6)
        try:
            while not self._stopping:
                try:
                    await self._maybe_fire_4hr_prompt()
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "four_hour_iteration_failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                if self._stopping:
                    return
                await self._clock.sleep(sleep_seconds)
        except asyncio.CancelledError:
            raise

    async def _maybe_fire_4hr_prompt(self) -> None:
        cont = self._active_continuous
        if cont is None or cont.awaiting_response:
            return
        # Only fire if the active session is still alive.
        if self._orchestrator.active_session_id != cont.session_id:
            self._active_continuous = None
            return
        if self._clock.time() < cont.next_prompt_at:
            return

        # Fire the prompt: state transition only (Phase 4 wires real UI).
        cont.awaiting_response = True
        cont.pending_response_event = asyncio.Event()
        await self._sm.transition(last_error="4hr_prompt_pending")
        _log.info(
            "four_hour_prompt_fired",
            session_id=cont.session_id,
            elapsed_seconds=self._clock.time() - cont.started_at,
        )

        timeout = self._config.capture.continuous_prompt_timeout_seconds

        def _timeout_cb(sid: int = cont.session_id) -> None:
            self._spawn_task(self._on_prompt_timeout(sid))

        # Schedule a timeout — if the user doesn't respond, we stop.
        # Hold the handle so stop() and respond_to_prompt() can cancel it.
        cont.timeout_handle = self._clock.call_later(timeout, _timeout_cb)

    async def _on_prompt_timeout(self, session_id: int) -> None:
        """Fire when the user didn't respond to the 4-hr prompt in time."""
        cont = self._active_continuous
        if cont is None or cont.session_id != session_id:
            return
        if not cont.awaiting_response:
            return  # User responded between prompt and timeout.
        cont.awaiting_response = False
        cont.timeout_handle = None  # Already firing; clear for cleanliness.
        if cont.pending_response_event is not None:
            cont.pending_response_event.set()
        _log.info("prompt_timeout_stop", session_id=session_id)
        await self._safe_stop(session_id, reason="4hr_prompt_stop")
        self._session_kinds.pop(session_id, None)
        self._active_sessions.pop(session_id, None)
        self._active_continuous = None

    # ==================================================================
    # Internals — voice-note callbacks
    # ==================================================================

    async def _on_voice_note_warning(self, session_id: int) -> None:
        """Cap-warning fired for a voice note.

        Logs the event for tests and posts a macOS notification so the
        user knows the voice note is about to hit the duration cap.
        Notification delivery is best-effort — a failure is logged and
        does not break the timer chain (the force-stop still fires).
        """
        # Drop the warning handle but keep the force_stop one.
        timers = self._voice_note_timers.get(session_id)
        if timers is not None:
            timers.warning = None
        _log.info("voice_note_cap_warning", session_id=session_id)
        await self._notify_voice_note_cap(
            identifier=f"voice_note_cap_warning_{session_id}",
            title="Voice note ending soon",
            body="Your voice note will auto-save in 30 seconds.",
        )

    async def _on_voice_note_force_stop(self, session_id: int) -> None:
        """Force-stop fired — voice note ran past max_duration_seconds."""
        timers = self._voice_note_timers.pop(session_id, None)
        if timers is None:
            return  # Already cancelled.
        if self._orchestrator.active_session_id != session_id:
            return  # Session already gone.
        _log.info("voice_note_force_stop", session_id=session_id)
        await self._notify_voice_note_cap(
            identifier=f"voice_note_force_stop_{session_id}",
            title="Voice note auto-saved",
            body="Reached the duration cap; recording stopped.",
        )
        await self._safe_stop(session_id, reason="voice_note_cap_reached")
        self._session_kinds.pop(session_id, None)
        self._active_sessions.pop(session_id, None)

    async def _notify_voice_note_cap(
        self,
        *,
        identifier: str,
        title: str,
        body: str,
    ) -> None:
        """Best-effort macOS banner for cap warning + force-stop.

        Fire-and-forget: spawn ``notify(...)`` on a background task so
        the cap chain (warning → ``_safe_stop`` for force-stop) doesn't
        block waiting on the notification completion handler. In tests
        the completion handler never fires, and a 5s ``wait_for`` in
        ``DaemonNotifier.post`` would stall ``virtual_clock`` based
        tests for 5 real seconds — long enough to mask ``_safe_stop``
        from running before the test asserts.
        """

        async def _fire() -> None:
            try:
                from helios.notifications.notify import notify

                await notify(title=title, body=body, identifier=identifier)
            except Exception as exc:  # noqa: BLE001 — best-effort
                _log.warning(
                    "voice_note_cap_notify_failed",
                    identifier=identifier,
                    error_type=type(exc).__name__,
                )

        self._spawn_task(_fire())


__all__ = [
    "ActiveSessionInfo",
    "NextCalendarEventInfo",
    "Scheduler",
    "SessionKind",
]
