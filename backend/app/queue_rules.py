from __future__ import annotations

from datetime import date
from typing import Any, Iterable
from zoneinfo import ZoneInfo


SCHEDULER_START = 0
SCHEDULER_END = 24 * 60
# Production posts need a short handoff buffer. Personal-time blocks opt out
# because their displayed duration is their complete scheduler footprint.
SCHEDULER_BUFFER_MINUTES = 10
SCHEDULER_TIMEZONE = ZoneInfo("America/Costa_Rica")


def intervals_conflict(
    start: int,
    duration: int,
    other_start: int,
    other_duration: int,
    buffer_minutes: int = SCHEDULER_BUFFER_MINUTES,
    other_buffer_minutes: int | None = None,
) -> bool:
    end = start + duration
    other_end = other_start + other_duration
    other_buffer = buffer_minutes if other_buffer_minutes is None else max(0, int(other_buffer_minutes))
    return start < other_end + other_buffer and end + max(0, int(buffer_minutes)) > other_start


def schedule_absolute(date_value: str, start_minutes: int) -> int:
    return date.fromisoformat(date_value).toordinal() * SCHEDULER_END + int(start_minutes)


def split_schedule_absolute(value: int) -> tuple[str, int]:
    ordinal, start_minutes = divmod(int(value), SCHEDULER_END)
    return date.fromordinal(ordinal).isoformat(), start_minutes


def next_available_slot(
    date_value: str,
    start_minutes: int,
    duration: int,
    occupied: Iterable[dict[str, Any]],
) -> tuple[str, int]:
    """Advance to the first collision-free 10-minute slot, across days.

    Existing blocks always keep their position. There is deliberately no
    capacity failure: the sequence of calendar days is the available space.
    """
    candidate = schedule_absolute(date_value, start_minutes)
    candidate = max(0, round(candidate / 10) * 10)
    normalized = [
        (
            schedule_absolute(str(item["date"]), int(item["start"])),
            max(10, int(item["duration"])),
            max(0, int(item.get("buffer_minutes", SCHEDULER_BUFFER_MINUTES))),
        )
        for item in occupied
        if item.get("date") is not None and item.get("start") is not None
    ]
    while True:
        conflicts = [
            (other_start, other_duration, other_buffer)
            for other_start, other_duration, other_buffer in normalized
            if intervals_conflict(
                candidate, duration, other_start, other_duration,
                other_buffer_minutes=other_buffer,
            )
        ]
        if not conflicts:
            return split_schedule_absolute(candidate)
        candidate = max(
            other_start + other_duration + other_buffer
            for other_start, other_duration, other_buffer in conflicts
        )
        candidate = ((candidate + 9) // 10) * 10
