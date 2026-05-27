"""Helpers shared across the Helios dashboard routes.

The dashboard composes Helios daemon API responses (via HeliosClient) with
Aegis DB enrichment. Pure helpers — never own network I/O or DB sessions
themselves; callers pass already-fetched data in.
"""

from __future__ import annotations

from typing import Any, Iterable

from aegis.db.models import Meeting, Person  # noqa: F401  # re-exported types


def resolve_speaker_names(
    transcript: dict | None,
    meeting: Meeting | None,
    attendees: Iterable[Person] | None,
    *,
    user_email: str | None = None,
) -> dict[str, str]:
    """Build a ``SPEAKER_XX → display name`` map per HELIOS.md §16.4.

    Heuristic: when the transcript has N raw ``SPEAKER_XX`` labels and the
    meeting has N-1 non-user attendees, map speakers in order of first
    appearance. The user (organizer / ``user_email``) is excluded because
    one of the speakers will be them and we render that one as ``You`` or
    leave it as the raw label.

    Returns ``{}`` when:

    * ``transcript`` is missing / has no segments,
    * ``meeting`` is None,
    * the speaker count doesn't match attendee_count - 1.

    The caller is responsible for falling back to the raw ``SPEAKER_00``
    label when a speaker is not in the returned mapping.
    """
    if not transcript or not meeting or not attendees:
        return {}
    segments = transcript.get("segments") or []
    if not segments:
        return {}

    # Discover the unique SPEAKER_XX labels + their first appearance time
    first_appearance: dict[str, float] = {}
    for seg in segments:
        speaker = seg.get("speaker")
        if not isinstance(speaker, str) or not speaker.startswith("SPEAKER_"):
            continue
        start = seg.get("start", 0.0)
        if speaker not in first_appearance:
            first_appearance[speaker] = float(start)

    if not first_appearance:
        return {}

    # Filter attendees: drop the user (organizer or explicit user_email match)
    organizer_email = (meeting.organizer_email or "").lower() or None
    non_user_attendees: list[Person] = []
    for person in attendees:
        person_email = (person.email or "").lower() or None
        if user_email and person_email and person_email == user_email.lower():
            continue
        if organizer_email and person_email == organizer_email:
            continue
        non_user_attendees.append(person)

    if len(first_appearance) != len(non_user_attendees):
        # Heuristic doesn't apply — caller renders raw SPEAKER_XX labels.
        return {}

    speaker_order = sorted(
        first_appearance.keys(), key=lambda s: first_appearance[s]
    )
    return {
        speaker: (non_user_attendees[idx].name or speaker)
        for idx, speaker in enumerate(speaker_order)
    }


def daemon_unreachable_banner_context() -> dict[str, Any]:
    """Standard context flag templates use to render the offline banner."""
    return {"helios_online": False, "helios_unreachable": True}


def daemon_online_context() -> dict[str, Any]:
    return {"helios_online": True, "helios_unreachable": False}
