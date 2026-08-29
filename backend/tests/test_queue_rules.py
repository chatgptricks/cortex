from app.queue_rules import intervals_conflict


def test_ten_minute_buffer_is_enforced() -> None:
    assert not intervals_conflict(8 * 60, 30, 8 * 60 + 40, 20)
    assert intervals_conflict(8 * 60, 30, 8 * 60 + 39, 20)
