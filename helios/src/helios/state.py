"""Daemon state machine.

Single source of truth for Helios daemon mode (armed / recording / paused /
error / not_running) plus auxiliary flags (active session, pause expiry,
component errors, Aegis reachability).

Public surface:

    Mode                  Literal alias for the five mode strings
    ComponentError        per-subsystem error info
    DaemonState           dataclass snapshot
    StateListener         callback signature (sync or async)
    DaemonStateMachine    mutable holder + observer

Only ``transition()`` mutates state; listeners fire after the new state is
committed. Listener exceptions are logged and swallowed — one bad listener
never breaks others or the transition itself.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from .log import get_logger

_log = get_logger("state")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Mode = Literal["armed", "recording", "paused", "error", "not_running"]

# Sentinel used internally so callers can explicitly pass `None` to clear
# nullable fields (active_session_id / paused_until / last_error) without
# colliding with "field omitted, leave alone" semantics.
_UNSET: Any = object()


@dataclass
class ComponentError:
    """Error info for a degraded subsystem (mic, system_audio, transcription, ...).

    ``occurred_at`` is UTC epoch seconds (float). The state machine does not
    interpret these timestamps; they are passed through verbatim for
    observability and downstream display.
    """

    component: str
    message: str
    occurred_at: float


@dataclass
class DaemonState:
    """Snapshot of daemon state.

    Conceptually immutable. The state machine produces a new ``DaemonState``
    via ``dataclasses.replace`` on every transition. Callers receive
    snapshots from ``current()`` — mutating the returned dataclass does NOT
    affect the state machine's internal value (a fresh copy is returned each
    call).
    """

    mode: Mode = "armed"
    active_session_id: int | None = None
    paused_until: float | None = None
    aegis_unreachable: bool = False
    component_errors: dict[str, ComponentError] = field(default_factory=dict)
    last_error: str | None = None


# Listener signature. May be sync or async. Receives (old_state, new_state).
StateListener = Callable[["DaemonState", "DaemonState"], Awaitable[None] | None]


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class DaemonStateMachine:
    """Mutable holder + observer for :class:`DaemonState`.

    Only :meth:`transition` mutates the held state. After the new state is
    committed, every subscribed listener is invoked with ``(old, new)``.
    Sync listeners run inline; async listeners (coroutines) are awaited.
    Listener exceptions are logged at WARNING and never re-raised: one
    misbehaving listener does not block other listeners or undo the
    transition.
    """

    def __init__(self, initial: DaemonState | None = None) -> None:
        self._state: DaemonState = initial if initial is not None else DaemonState()
        # Use a list to preserve insertion order; subscribe() is idempotent
        # so the same callable is never registered twice.
        self._listeners: list[StateListener] = []

    # -- read --------------------------------------------------------------

    def current(self) -> DaemonState:
        """Return an independent snapshot of the current state.

        ``component_errors`` is shallow-copied so callers cannot mutate the
        machine's internal dict via the returned dataclass.
        """
        return dataclasses.replace(
            self._state,
            component_errors=dict(self._state.component_errors),
        )

    # -- write -------------------------------------------------------------

    async def transition(
        self,
        mode: Mode | None = None,
        *,
        active_session_id: Any = _UNSET,
        paused_until: Any = _UNSET,
        aegis_unreachable: Any = _UNSET,
        component_errors: dict[str, ComponentError] | None = None,
        last_error: Any = _UNSET,
    ) -> DaemonState:
        """Update the state and fire listeners.

        Parameters
        ----------
        mode:
            New mode. Pass ``None`` (the default) to keep the current mode.
        active_session_id, paused_until, last_error:
            Pass ``None`` explicitly to clear; omit to leave unchanged.
        aegis_unreachable:
            Boolean flag. Omit to leave unchanged. NOTE: this is a flag
            independent of ``mode``; setting it does NOT push the daemon
            into ``mode="error"``.
        component_errors:
            Dict of component -> ComponentError to MERGE into the existing
            dict. Pass an empty dict (``{}``) to clear all component errors.
            To clear specific keys, use :meth:`clear_component_errors`.

        Returns
        -------
        DaemonState
            A snapshot of the new committed state.
        """
        old = self._state

        updates: dict[str, Any] = {}
        if mode is not None:
            updates["mode"] = mode
        if active_session_id is not _UNSET:
            updates["active_session_id"] = active_session_id
        if paused_until is not _UNSET:
            updates["paused_until"] = paused_until
        if aegis_unreachable is not _UNSET:
            updates["aegis_unreachable"] = bool(aegis_unreachable)
        if last_error is not _UNSET:
            updates["last_error"] = last_error

        if component_errors is not None:
            if component_errors == {}:
                # Explicit clear-all.
                updates["component_errors"] = {}
            else:
                # Merge — new keys overwrite existing ones with the same key.
                updates["component_errors"] = {
                    **old.component_errors,
                    **component_errors,
                }

        new = dataclasses.replace(old, **updates)
        self._state = new

        await self._fire_listeners(old, new)
        return self.current()

    def clear_component_errors(self, *components: str) -> DaemonState:
        """Remove the named component errors from the current state.

        If no components are passed, this is a no-op. To clear ALL component
        errors, call ``transition(component_errors={})``.

        Returns the new (committed) state. Listeners are NOT fired by this
        helper — clearing a component error is a synchronous bookkeeping
        operation; if a listener cares, the caller should follow up with a
        ``transition(...)`` to reflect any mode change.
        """
        if not components:
            return self.current()
        remaining = {
            k: v for k, v in self._state.component_errors.items() if k not in components
        }
        self._state = dataclasses.replace(self._state, component_errors=remaining)
        return self.current()

    # -- observers ---------------------------------------------------------

    def subscribe(self, listener: StateListener) -> None:
        """Register a listener. Idempotent — registering twice has no extra effect."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unsubscribe(self, listener: StateListener) -> None:
        """Remove a listener. No-op if not registered."""
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    # -- internals ---------------------------------------------------------

    async def _fire_listeners(self, old: DaemonState, new: DaemonState) -> None:
        # Snapshot the listener list so that subscribe/unsubscribe inside a
        # listener does not mutate the iteration.
        for listener in list(self._listeners):
            try:
                result = listener(old, new)
                if inspect.iscoroutine(result):
                    await result
                elif asyncio.isfuture(result):
                    await result
            except Exception as exc:  # noqa: BLE001 — listeners must never break
                qualname = getattr(listener, "__qualname__", repr(listener))
                _log.warning(
                    "state_listener_error",
                    listener=qualname,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
