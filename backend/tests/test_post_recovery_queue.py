from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from app import post_recovery_queue as queue


def test_recovery_is_deduplicated_and_only_one_job_can_run(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(queue.db, "connect", connect)
    first = queue.enqueue(
        journal_key="manual-post-catchup",
        slot="2026-09-10T1900",
        lookback_hours=168,
        include_posts=True,
        include_reels=True,
    )
    duplicate = queue.enqueue(
        journal_key="manual-post-catchup",
        slot="2026-09-10T1900",
        lookback_hours=168,
        include_posts=True,
        include_reels=True,
    )
    second = queue.enqueue(
        journal_key="another-recovery",
        slot="2026-09-10T1901",
        lookback_hours=24,
        include_posts=True,
        include_reels=False,
    )

    assert duplicate["job_id"] == first["job_id"]
    claimed = queue._claim_next()
    assert claimed["status"] == "running"
    assert queue._claim_next() is None
    assert second["status"] == "queued"


def test_recovery_summary_counts_saved_rows_and_account_errors():
    assert queue._summary(
        {
            "good": {"new_posts": {"added": 4, "failed": 0}},
            "partial": {"new_posts": {"added": 2, "failed": 1}, "error": "source unavailable"},
        }
    ) == {"accounts": 2, "added": 6, "failed": 1, "accounts_with_errors": 1}
