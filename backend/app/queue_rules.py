from __future__ import annotations

from datetime import date
from typing import Any, Iterable
from zoneinfo import ZoneInfo


SCHEDULER_START = 0
SCHEDULER_END = 24 * 60
SCHEDULER_BUFFER_MINUTES = 0
SCHEDULER_TIMEZONE = ZoneInfo("America/Costa_Rica")


def intervals_conflict(
    start: int,
    duration: int,
    other_start: int,
    other_duration: int,
    buffer_minutes: int = SCHEDULER_BUFFER_MINUTES,
) -> bool:
    end = start + duration
    other_end = other_start + other_duration
    return start < other_end + buffer_minutes and end + buffer_minutes > other_start


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
        )
        for item in occupied
        if item.get("date") is not None and item.get("start") is not None
    ]
    while True:
        conflicts = [
            (other_start, other_duration)
            for other_start, other_duration in normalized
            if intervals_conflict(candidate, duration, other_start, other_duration)
        ]
        if not conflicts:
            return split_schedule_absolute(candidate)
        candidate = max(other_start + other_duration for other_start, other_duration in conflicts)
        candidate = ((candidate + 9) // 10) * 10
