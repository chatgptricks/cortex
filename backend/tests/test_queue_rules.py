from app.queue_rules import intervals_conflict, next_available_slot


def test_adjacent_blocks_do_not_overlap() -> None:
    assert not intervals_conflict(8 * 60, 30, 8 * 60 + 30, 20)
    assert intervals_conflict(8 * 60, 31, 8 * 60 + 30, 20)


def test_new_work_advances_after_an_active_block() -> None:
    date_value, start = next_available_slot(
        "2026-09-01",
        9 * 60 + 10,
        30,
        [{"date": "2026-09-01", "start": 9 * 60, "duration": 30}],
    )
    assert date_value == "2026-09-01"
    assert start == 9 * 60 + 30


def test_collision_chain_advances_without_overlaps() -> None:
    date_value, start = next_available_slot(
        "2026-09-01",
        9 * 60 + 10,
        30,
        [
            {"date": "2026-09-01", "start": 9 * 60, "duration": 30},
            {"date": "2026-09-01", "start": 9 * 60 + 30, "duration": 30},
        ],
    )
    assert date_value == "2026-09-01"
    assert start == 10 * 60


def test_full_day_rolls_into_next_day() -> None:
    date_value, start = next_available_slot(
        "2026-09-01",
        23 * 60 + 50,
        30,
        [{"date": "2026-09-01", "start": 23 * 60 + 50, "duration": 30}],
    )
    assert date_value == "2026-09-02"
    assert start == 20
