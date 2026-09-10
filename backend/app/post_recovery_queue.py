"""Durable full-library post recovery, owned by the Render worker.

An ad-hoc browser request cannot safely import a seven-day Apify dataset: it
can outlive the HTTP request and a Render shell disappears when its browser
session closes. This queue keeps the recovery intent in Postgres and lets the
dedicated worker resume the same journalled Apify run until its rows are
persisted.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import logging
import threading
import uuid
from typing import Any

from . import db, ingestion_jobs

logger = logging.getLogger(__name__)
_WAKE = threading.Event()
_LOCK = threading.Lock()
_STARTED = False
_STALE_SECONDS = 300
_CLAIM_LOCK_KEY = 7042198362


def initialize(conn: Any) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS post_recovery_jobs (
            job_id TEXT PRIMARY KEY,
            journal_key TEXT NOT NULL UNIQUE,
            slot TEXT NOT NULL,
            include_posts INTEGER NOT NULL,
            include_reels INTEGER NOT NULL,
            lookback_hours INTEGER NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT,
            requested_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            heartbeat_at TEXT,
            finished_at TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_post_recovery_jobs_status ON post_recovery_jobs(status, requested_at)")


def _item(row: Any) -> dict[str, Any]:
    item = dict(row)
    for field in ("result_json",):
        try:
            item[field.removesuffix("_json")] = json.loads(item.get(field) or "{}")
        except (TypeError, ValueError):
            item[field.removesuffix("_json")] = {}
    return item


def enqueue(
    *,
    journal_key: str,
    slot: str,
    lookback_hours: int,
    include_posts: bool,
    include_reels: bool,
) -> dict[str, Any]:
    if not journal_key or not slot:
        raise ValueError("A recovery journal key and slot are required.")
    if not 1 <= int(lookback_hours) <= 168:
        raise ValueError("lookback_hours must be between 1 and 168.")
    if not include_posts and not include_reels:
        raise ValueError("A recovery must include posts, Reels, or both.")
    now = db.utc_now()
    with db.connect() as conn:
        initialize(conn)
        conn.execute(
            """INSERT INTO post_recovery_jobs(
                job_id, journal_key, slot, include_posts, include_reels,
                lookback_hours, status, requested_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            ON CONFLICT(journal_key) DO NOTHING""",
            (
                uuid.uuid4().hex,
                journal_key,
                slot,
                int(include_posts),
                int(include_reels),
                int(lookback_hours),
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM post_recovery_jobs WHERE journal_key = ?", (journal_key,)).fetchone()
    _WAKE.set()
    return _item(row)


def status(journal_key: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        initialize(conn)
        row = conn.execute("SELECT * FROM post_recovery_jobs WHERE journal_key = ?", (journal_key,)).fetchone()
    return _item(row) if row else None


def _recover_stale() -> None:
    stale_before = (datetime.now(UTC) - timedelta(seconds=_STALE_SECONDS)).isoformat(timespec="seconds")
    with db.connect() as conn:
        initialize(conn)
        conn.execute(
            """UPDATE post_recovery_jobs
               SET status = 'queued', error = NULL, updated_at = ?
               WHERE status = 'running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)""",
            (db.utc_now(), stale_before),
        )


def _claim_next() -> dict[str, Any] | None:
    with db.connect() as conn:
        initialize(conn)
        # A rolling deploy can overlap two worker processes. Claim inside a
        # database lock so that never turns into two paid recoveries.
        if getattr(conn, "is_postgres", False):
            lock = conn.execute(
                "SELECT pg_try_advisory_xact_lock(?) AS locked", (_CLAIM_LOCK_KEY,)
            ).fetchone()
            if not lock or not lock.get("locked"):
                return None
        else:
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM post_recovery_jobs WHERE status = 'running' LIMIT 1"
        ).fetchone():
            return None
        row = conn.execute(
            "SELECT * FROM post_recovery_jobs WHERE status = 'queued' ORDER BY requested_at LIMIT 1"
        ).fetchone()
        if not row:
            return None
        now = db.utc_now()
        changed = conn.execute(
            """UPDATE post_recovery_jobs
               SET status = 'running', heartbeat_at = ?, updated_at = ?
               WHERE job_id = ? AND status = 'queued'""",
            (now, now, row["job_id"]),
        ).rowcount
        if changed != 1:
            return None
        row = conn.execute(
            "SELECT * FROM post_recovery_jobs WHERE job_id = ?", (row["job_id"],)
        ).fetchone()
        return _item(row)


def _heartbeat(job_id: str, stop: threading.Event) -> None:
    while not stop.wait(20):
        try:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE post_recovery_jobs SET heartbeat_at = ?, updated_at = ? WHERE job_id = ? AND status = 'running'",
                    (db.utc_now(), db.utc_now(), job_id),
                )
        except Exception:
            logger.exception("Could not update post recovery heartbeat")


def _summary(results: dict[str, dict[str, Any]]) -> dict[str, int]:
    added = sum(int((result.get("new_posts") or {}).get("added") or 0) for result in results.values())
    failed = sum(int((result.get("new_posts") or {}).get("failed") or 0) for result in results.values())
    accounts_with_errors = sum(1 for result in results.values() if result.get("error"))
    return {"accounts": len(results), "added": added, "failed": failed, "accounts_with_errors": accounts_with_errors}


def _run(task: dict[str, Any]) -> None:
    job_id = task["job_id"]
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(target=_heartbeat, args=(job_id, heartbeat_stop), daemon=True)
    heartbeat_thread.start()
    result_box: dict[str, Any] = {}
    try:
        def work() -> None:
            from .apify_sync import list_accounts, run_short_term_cycle_batch_paged

            handles = [account["handle"] for account in list_accounts(active_only=True)]
            result_box["results"] = run_short_term_cycle_batch_paged(
                handles,
                results_limit=50,
                include_posts=bool(task["include_posts"]),
                include_reels=bool(task["include_reels"]),
                lookback_hours=int(task["lookback_hours"]),
            )

        completed = ingestion_jobs.run(task["journal_key"], task["slot"], work)
        with db.connect() as conn:
            if completed:
                conn.execute(
                    """UPDATE post_recovery_jobs
                       SET status = 'done', result_json = ?, error = NULL,
                           heartbeat_at = ?, finished_at = ?, updated_at = ?
                       WHERE job_id = ?""",
                    (
                        json.dumps(_summary(result_box.get("results") or {})),
                        db.utc_now(),
                        db.utc_now(),
                        db.utc_now(),
                        job_id,
                    ),
                )
            else:
                journal = conn.execute(
                    "SELECT error FROM ingestion_jobs WHERE job_key = ?", (task["journal_key"],)
                ).fetchone()
                conn.execute(
                    """UPDATE post_recovery_jobs
                       SET status = 'queued', error = ?, heartbeat_at = ?, updated_at = ?
                       WHERE job_id = ?""",
                    (str(journal["error"] if journal else "Recovery will resume from the saved dataset.")[:2000], db.utc_now(), db.utc_now(), job_id),
                )
                _WAKE.set()
    except Exception as exc:  # pragma: no cover - defensive worker boundary
        logger.exception("Post recovery worker failed")
        with db.connect() as conn:
            conn.execute(
                """UPDATE post_recovery_jobs
                   SET status = 'queued', error = ?, heartbeat_at = ?, updated_at = ?
                   WHERE job_id = ?""",
                (str(exc)[:2000], db.utc_now(), db.utc_now(), job_id),
            )
        _WAKE.set()
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)


def _loop() -> None:
    while True:
        try:
            _recover_stale()
            task = _claim_next()
            if task:
                _run(task)
                continue
        except Exception:
            logger.exception("Post recovery queue worker failed")
        _WAKE.wait(2)
        _WAKE.clear()


def start_worker() -> None:
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
        threading.Thread(target=_loop, daemon=True, name="post-recovery-queue").start()
