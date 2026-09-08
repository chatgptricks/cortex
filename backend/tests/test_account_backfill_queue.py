from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from app import account_backfill_queue as queue


def test_account_backfills_are_claimed_in_request_order_and_deduplicated(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(queue.db, "connect", connect)
    monkeypatch.setattr(queue, "start_worker", lambda: None)
    monkeypatch.setattr(queue, "_wake_worker", lambda: None)

    first = queue.enqueue("@first", results_limit=10)
    second = queue.enqueue("second", results_limit=20)
    duplicate = queue.enqueue("@second", results_limit=99)

    assert first["status"] == "queued"
    assert second["position"] == 2
    assert duplicate["duplicate"] is True
    assert duplicate["job_id"] == second["job_id"]
    assert duplicate["results_limit"] == 20

    calls = []

    def fake_backfill(handle, **kwargs):
        calls.append(handle)
        kwargs["on_progress"]({"phase": "waiting_apify"})
        return {"added": 3}

    monkeypatch.setattr(queue, "run_backfill", fake_backfill)
    claimed_first = queue._claim_next()
    # A second worker must not claim another account while the first import is
    # still in progress.
    assert queue._claim_next() is None
    queue._run(claimed_first)
    claimed_second = queue._claim_next()
    queue._run(claimed_second)

    assert calls == ["first", "second"]
    rows = connection.execute(
        "SELECT handle, status, result_json FROM account_backfill_jobs ORDER BY requested_at"
    ).fetchall()
    assert [(row["handle"], row["status"]) for row in rows] == [("first", "done"), ("second", "done")]
    assert '"added": 3' in rows[0]["result_json"]


def test_status_exposes_active_queue_and_recent_results(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(queue.db, "connect", connect)
    monkeypatch.setattr(queue, "start_worker", lambda: None)
    monkeypatch.setattr(queue, "_wake_worker", lambda: None)
    first = queue.enqueue("first")
    second = queue.enqueue("second")
    queue._claim_next()

    value = queue.status()
    assert value["running"] is True
    assert value["active"]["handle"] == "first"
    assert [item["handle"] for item in value["queue"]] == ["second"]
    assert {item["handle"] for item in value["tasks"]} == {"first", "second"}


def test_failed_attempt_is_requeued_without_starting_a_second_apify_run(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(queue.db, "connect", connect)
    monkeypatch.setattr(queue, "start_worker", lambda: None)
    monkeypatch.setattr(queue, "_wake_worker", lambda: None)
    task = queue.enqueue("retry-account")
    claimed = queue._claim_next()
    connection.execute(
        "CREATE TABLE ingestion_jobs (job_key TEXT PRIMARY KEY, status TEXT, error TEXT)"
    )
    connection.execute(
        "INSERT INTO ingestion_jobs(job_key, status, error) VALUES (?, 'retry', ?)",
        (f"account-backfill:{task['job_id']}", "temporary cover failure"),
    )
    monkeypatch.setattr(queue.ingestion_jobs, "run", lambda *args, **kwargs: False)

    queue._run(claimed)

    row = connection.execute(
        "SELECT status, progress_json, next_attempt_at, error FROM account_backfill_jobs WHERE job_id = ?",
        (task["job_id"],),
    ).fetchone()
    assert row["status"] == "queued"
    assert '"retrying"' in row["progress_json"]
    assert row["next_attempt_at"]
    assert row["error"] == "temporary cover failure"
