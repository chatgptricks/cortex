from datetime import datetime

from app.queue_rules import fits_deadline, intervals_conflict, parse_deadline


def test_naive_deadline_is_costa_rica_time() -> None:
    normalized = datetime.fromisoformat(parse_deadline("2026-09-01T12:00"))
    assert normalized.isoformat() == "2026-09-01T18:00:00+00:00"


def test_schedule_must_finish_before_deadline() -> None:
    deadline = parse_deadline("2026-09-01T12:00")
    assert fits_deadline("2026-09-01", 11 * 60 + 30, 30, deadline)
    assert not fits_deadline("2026-09-01", 11 * 60 + 40, 30, deadline)


def test_ten_minute_buffer_is_enforced() -> None:
    assert not intervals_conflict(8 * 60, 30, 8 * 60 + 40, 20)
    assert intervals_conflict(8 * 60, 30, 8 * 60 + 39, 20)
