"""Component status reporting — surfaces per-subsystem health to /v1/status.

Subsystems include: ``audio_capture``, ``transcription``, ``diarization``,
``ocr``. The reporter persists status to the ``component_status`` table
(via :mod:`helios.db.queries`) AND fires a state-machine transition
when a component flips into error so the daemon's ``mode`` reflects it.

Status values per HELIOS.md §9.7:

* ``ok``           — subsystem is healthy
* ``unavailable``  — model not loaded / token missing / disabled in config
* ``degraded``     — running but with reduced capability
* ``error``        — failed; daemon should escalate
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import aiosqlite

from helios.db import queries
from helios.log import get_logger
from helios.state import ComponentError, DaemonStateMachine

_log = get_logger("components")

ComponentStatus = Literal["ok", "unavailable", "degraded", "error"]


@dataclass
class ComponentRecord:
    """In-memory snapshot of a single subsystem's most recently reported status."""

    component: str
    status: ComponentStatus
    reason: str | None = None
    detail: str | None = None
    action: str | None = None  # user-facing action ("Open Settings → ...")
    updated_at: float = 0.0


class ComponentStatusReporter:
    """Persist + propagate per-subsystem status.

    Reporters are owned by the daemon and shared across workers. The
    contract is intentionally narrow: callers ``set()`` a status; the
    reporter handles dedup, DB persistence, and state-machine wiring.

    Idempotency: a repeat call with the same ``status`` AND ``reason``
    is a no-op (no DB write, no state-machine churn). Any change in
    either field re-persists.

    State-machine wiring: a flip TO ``error`` adds a ``ComponentError``
    entry under the component's key. A flip FROM ``error`` to anything
    else clears that key (other components' errors are preserved).
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        state_machine: DaemonStateMachine,
    ) -> None:
        self._db = db
        self._sm = state_machine
        # Cache the last-known status for each component so transitions
        # can be diffed (avoids duplicate state-machine churn on every
        # periodic ``ok`` re-report).
        self._current: dict[str, ComponentRecord] = {}

    async def set(
        self,
        component: str,
        status: ComponentStatus,
        *,
        reason: str | None = None,
        detail: str | None = None,
        action: str | None = None,
    ) -> None:
        """Record a status change. Idempotent — a no-op if status+reason unchanged."""
        now = time.time()
        prev = self._current.get(component)

        # Deduplicate: if exact same status+reason already current, skip
        # both DB write and SM transition.
        if (
            prev is not None
            and prev.status == status
            and prev.reason == reason
        ):
            return

        record = ComponentRecord(
            component=component,
            status=status,
            reason=reason,
            detail=detail,
            action=action,
            updated_at=now,
        )
        self._current[component] = record

        # Persist to DB. ``upsert_component_status`` was extended by
        # Wave 1A to accept ``reason`` and ``action`` as keyword-only
        # arguments (the older positional ``details`` parameter still
        # maps to the ``detail`` column for backward compatibility).
        await queries.upsert_component_status(
            self._db,
            component=component,
            status=status,
            details=detail,
            reason=reason,
            action=action,
        )

        # Reflect into the daemon state machine: any subsystem in error
        # contributes a component_errors entry. ``ok`` (or anything else)
        # clears the entry on the way out of error.
        if status == "error":
            await self._sm.transition(
                component_errors={
                    component: ComponentError(
                        component=component,
                        message=reason or "error",
                        occurred_at=now,
                    ),
                },
            )
        elif prev is not None and prev.status == "error" and status != "error":
            # Recovery — clear this component's entry only.
            self._sm.clear_component_errors(component)

        _log.info(
            "component_status_set",
            component=component,
            status=status,
            reason=reason,
        )

    def get(self, component: str) -> ComponentRecord | None:
        """Return the last-known record for ``component`` or ``None`` if never set."""
        return self._current.get(component)

    def all(self) -> dict[str, ComponentRecord]:
        """Return a shallow copy of the in-memory cache (component → record)."""
        return dict(self._current)
