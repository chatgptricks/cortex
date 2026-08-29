from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


SCHEDULER_START = 8 * 60
SCHEDULER_END = 20 * 60
SCHEDULER_BUFFER_MINUTES = 10
SCHEDULER_TIMEZONE = ZoneInfo("America/Costa_Rica")


def parse_deadline(value: str) -> str:
    """Normalize a Queue deadline to UTC.

    ``datetime-local`` inputs do not carry a timezone. Queue is explicitly a
    Costa Rica production schedule, so naive values must be interpreted in
    that timezone rather than as UTC.
    """
    clean = (value or "").strip()
    if not clean:
        raise ValueError("A deadline is required.")
    parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SCHEDULER_TIMEZONE)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def scheduled_datetime(date: str, start_minutes: int) -> datetime:
    day = datetime.strptime(date, "%Y-%m-%d").date()
    return datetime(day.year, day.month, day.day, tzinfo=SCHEDULER_TIMEZONE).replace(
        hour=start_minutes // 60,
        minute=start_minutes % 60,
    )


def fits_deadline(date: str, start_minutes: int, duration_minutes: int, deadline_at: str) -> bool:
    deadline = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=SCHEDULER_TIMEZONE)
    return scheduled_datetime(date, start_minutes).timestamp() + duration_minutes * 60 <= deadline.timestamp()


def intervals_conflict(start: int, duration: int, other_start: int, other_duration: int, buffer_minutes: int = SCHEDULER_BUFFER_MINUTES) -> bool:
    end = start + duration
    other_end = other_start + other_duration
    return start < other_end + buffer_minutes and end + buffer_minutes > other_start
