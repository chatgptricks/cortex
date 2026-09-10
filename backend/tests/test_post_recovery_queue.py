from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from app import post_recovery_queue as queue
from app import apify_sync


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


def test_paged_recovery_processes_each_dataset_page_without_retaining_raw_items(monkeypatch):
    configs = {
        "alpha": {"handle": "alpha", "scrape_mode": "posts"},
        "beta": {"handle": "beta", "scrape_mode": "reels"},
    }
    monkeypatch.setattr(apify_sync, "get_account_config", lambda handle: configs[handle])

    def fetch(payload, **kwargs):
        if payload.get("resultsType") == "posts":
            kwargs["on_page"]([{"shortCode": "a", "ownerUsername": "alpha"}])
        else:
            kwargs["on_page"]([{"shortCode": "b", "owner": {"username": "beta"}}])
        return []

    monkeypatch.setattr(apify_sync, "_run_apify_actor_and_fetch", fetch)
    monkeypatch.setattr(
        apify_sync,
        "_process_short_term_items",
        lambda account, cfg, items, now, **kwargs: {
            "new_posts": {"added": len(items), "failed": 0, "items": [{"raw": "not retained"}]},
            "engagement": {"checked": len(items), "updated": len(items), "hot_marked": 0, "unmatched": 0},
            "transcripts_updated": 0,
        },
    )

    result = apify_sync.run_short_term_cycle_batch_paged(
        ["alpha", "beta"], include_posts=True, include_reels=True, lookback_hours=168
    )

    assert result["alpha"]["new_posts"]["added"] == 1
    assert result["beta"]["new_posts"]["added"] == 1
    assert "items" not in result["alpha"]["new_posts"]
