from __future__ import annotations

from zoneinfo import ZoneInfo


SCHEDULER_START = 8 * 60
SCHEDULER_END = 20 * 60
SCHEDULER_BUFFER_MINUTES = 10
SCHEDULER_TIMEZONE = ZoneInfo("America/Costa_Rica")


def intervals_conflict(start: int, duration: int, other_start: int, other_duration: int, buffer_minutes: int = SCHEDULER_BUFFER_MINUTES) -> bool:
    end = start + duration
    other_end = other_start + other_duration
    return start < other_end + buffer_minutes and end + buffer_minutes > other_start
