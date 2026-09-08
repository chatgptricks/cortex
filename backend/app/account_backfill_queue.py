"""Persistent, single-file queue for account history backfills.

Account creation can happen faster than an Instagram import completes.  The
old implementation kept one active job in process memory and rejected every
other request.  This module stores the queue in the database and runs exactly
one Apify backfill at a time, so queued accounts survive a page reload and can
resume after a web-process restart.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from . import db
from .apify_sync import run_backfill
from . import ingestion_jobs

logger = logging.getLogger(__name__)

_WORKER_LOCK = threading.Lock()
_WORKER_STARTED = False
_WAKE = threading.Event()
_CLAIM_LOCK_KEY = 7042198361
_STALE_HEARTBEAT_SECONDS = 300


def _now() -> str:
    # Queue order must distinguish two account additions made in the same
    # second; the shared db.utc_now() intentionally has second precision.
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _json(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _ensure_schema(conn: Any) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS account_backfill_jobs (
               job_id TEXT PRIMARY KEY,
               handle TEXT NOT NULL,
               results_limit INTEGER NOT NULL DEFAULT 2000,
               date_from TEXT,
               date_to TEXT,
               status TEXT NOT NULL DEFAULT 'queued',
               progress_json TEXT NOT NULL DEFAULT '{}',
               result_json TEXT NOT NULL DEFAULT '{}',
               error TEXT,
               attempts INTEGER NOT NULL DEFAULT 0,
               next_attempt_at TEXT,
               heartbeat_at TEXT,
               requested_at TEXT NOT NULL,
               started_at TEXT,
               finished_at TEXT
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_account_backfill_jobs_queue "
        "ON account_backfill_jobs(status, requested_at)"
    )
    # The table was introduced before retry/heartbeat support. Keep the queue
    # self-healing when an older database receives the new worker first.
    db._ensure_column(conn, "account_backfill_jobs", "attempts", "attempts INTEGER NOT NULL DEFAULT 0")
    db._ensure_column(conn, "account_backfill_jobs", "next_attempt_at", "next_attempt_at TEXT")
    db._ensure_column(conn, "account_backfill_jobs", "heartbeat_at", "heartbeat_at TEXT")


def _task(row: Any) -> dict[str, Any]:
    return {
        "id": row["job_id"],
        "job_id": row["job_id"],
        "handle": row["handle"],
        "results_limit": int(row["results_limit"] or 2000),
        "date_from": row["date_from"],
        "date_to": row["date_to"],
        "status": row["status"],
        "progress": _json(row["progress_json"], {}),
        "result": _json(row["result_json"], {}),
        "error": row["error"],
        "attempts": int(row["attempts"] or 0),
        "next_attempt_at": row["next_attempt_at"],
        "heartbeat_at": row["heartbeat_at"],
        "requested_at": row["requested_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def _wake_worker() -> None:
    _WAKE.set()


def enqueue(handle: str, results_limit: int = 2000, date_from: str | None = None, date_to: str | None = None) -> dict[str, Any]:
    """Queue one handle, deduplicating an already queued/running job."""
    clean_handle = handle.strip().lstrip("@").lower()
    now = _now()
    with db.connect() as conn:
        _ensure_schema(conn)
        existing = conn.execute(
            """SELECT * FROM account_backfill_jobs
               WHERE handle = ? AND status IN ('queued', 'running')
               ORDER BY requested_at ASC LIMIT 1""",
            (clean_handle,),
        ).fetchone()
        if existing:
            task = _task(existing)
            task["duplicate"] = True
        else:
            job_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO account_backfill_jobs
                   (job_id, handle, results_limit, date_from, date_to, status,
                    progress_json, result_json, requested_at)
                   VALUES (?, ?, ?, ?, ?, 'queued', ?, '{}', ?)""",
                (
                    job_id,
                    clean_handle,
                    max(1, min(int(results_limit or 2000), 5000)),
                    date_from or None,
                    date_to or None,
                    json.dumps({"phase": "queued"}),
                    now,
                ),
            )
            task = {
                "id": job_id,
                "job_id": job_id,
                "handle": clean_handle,
                "results_limit": max(1, min(int(results_limit or 2000), 5000)),
                "date_from": date_from or None,
                "date_to": date_to or None,
                "status": "queued",
                "progress": {"phase": "queued"},
                "result": {},
                "error": None,
                "attempts": 0,
                "next_attempt_at": None,
                "heartbeat_at": None,
                "requested_at": now,
                "started_at": None,
                "finished_at": None,
                "duplicate": False,
            }
    # The dedicated Render worker owns the queue loop. This wake-up still makes
    # local/test workers react immediately; production discovers the durable
    # row on its short polling interval.
    _wake_worker()
    task["position"] = position(task["job_id"])
    return task


def position(job_id: str) -> int:
    with db.connect() as conn:
        _ensure_schema(conn)
        target = conn.execute(
            "SELECT status FROM account_backfill_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if not target or target["status"] != "queued":
            return 0
        row = conn.execute(
            """SELECT COUNT(*) AS ahead FROM account_backfill_jobs queued
               WHERE status = 'queued' AND requested_at <
                 (SELECT requested_at FROM account_backfill_jobs WHERE job_id = ?)""",
            (job_id,),
        ).fetchone()
    # A queued job is position 1 when it is next; a running/done job has no
    # useful queue position and is reported as zero.
    return int(row["ahead"] or 0) + 1 if row else 0


def _claim_next() -> dict[str, Any] | None:
    with db.connect() as conn:
        # Serialize the claim across processes before looking for work. This
        # protects the dedicated worker during restarts and also keeps local
        # operator/test workers from starting two paid Apify runs at once.
        if getattr(conn, "is_postgres", False):
            lock = conn.execute(
                "SELECT pg_try_advisory_xact_lock(?) AS locked",
                (_CLAIM_LOCK_KEY,),
            ).fetchone()
            if not lock or not lock.get("locked"):
                return None
        else:
            # A write transaction is SQLite's cross-process mutex. The commit
            # also makes this work with the shared in-memory connection used
            # by the queue tests.
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
        _ensure_schema(conn)
        running = conn.execute(
            "SELECT 1 FROM account_backfill_jobs WHERE status = 'running' LIMIT 1"
        ).fetchone()
        if running:
            return None
        row = conn.execute(
            """SELECT * FROM account_backfill_jobs
               WHERE status = 'queued'
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
               ORDER BY requested_at ASC LIMIT 1""",
            (_now(),),
        ).fetchone()
        if not row:
            return None
        now = db.utc_now()
        changed = conn.execute(
            """UPDATE account_backfill_jobs
               SET status = 'running', started_at = ?, progress_json = ?,
                   error = NULL, attempts = COALESCE(attempts, 0) + 1,
                   heartbeat_at = ?, next_attempt_at = NULL
               WHERE job_id = ? AND status = 'queued'""",
            (now, json.dumps({"phase": "preparing"}), now, row["job_id"]),
        ).rowcount
        if changed != 1:
            return None
        row = conn.execute("SELECT * FROM account_backfill_jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
    return _task(row)


def _update(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    assignments = []
    params: list[Any] = []
    for key, value in fields.items():
        assignments.append(f"{key} = ?")
        params.append(value)
    params.append(job_id)
    try:
        with db.connect() as conn:
            _ensure_schema(conn)
            conn.execute(f"UPDATE account_backfill_jobs SET {', '.join(assignments)} WHERE job_id = ?", params)
    except Exception:
        # A progress update must never interrupt a paid import. The final
        # status is retried by the next status request if the DB was briefly
        # unavailable.
        logger.exception("Could not update account backfill job %s", job_id)


def _run(task: dict[str, Any]) -> None:
    job_id = task["job_id"]
    heartbeat_stop = threading.Event()

    def heartbeat() -> None:
        while not heartbeat_stop.wait(15):
            _update(job_id, heartbeat_at=db.utc_now())

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True, name=f"backfill-heartbeat-{task['handle']}")
    heartbeat_thread.start()

    def on_progress(progress: dict[str, Any]) -> None:
        _update(job_id, progress_json=json.dumps(progress, default=str))

    try:
        result_box: dict[str, Any] = {}

        def work() -> None:
            result_box["value"] = run_backfill(
                task["handle"],
                results_limit=task["results_limit"],
                date_from=task["date_from"] or None,
                date_to=task["date_to"] or None,
                on_progress=on_progress,
            )

        completed = ingestion_jobs.run(f"account-backfill:{job_id}", "01", work)
        if completed:
            _update(
                job_id,
                status="done",
                result_json=json.dumps(result_box.get("value") or {}, default=str),
                error=None,
                progress_json=json.dumps({"phase": "inserting", "done": 1, "total": 1}),
                heartbeat_at=db.utc_now(),
                finished_at=db.utc_now(),
            )
            return

        # ingestion_jobs keeps the paid Apify run and fetched dataset in its
        # journal. Retry the same queue item after the lease expires instead
        # of launching a second paid scrape. Three attempts cover transient
        # CDN/database failures while still surfacing a real persistent error.
        attempt = int(task.get("attempts") or 1)
        with db.connect() as conn:
            journal = conn.execute(
                "SELECT status, error FROM ingestion_jobs WHERE job_key = ?", (f"account-backfill:{job_id}",)
            ).fetchone()
        if journal and journal["status"] == "done":
            # The paid run and its dataset were already journaled successfully,
            # but the process may have died before the queue row got its final
            # update. Do not scrape or charge again.
            _update(
                job_id,
                status="done",
                result_json=json.dumps({"recovered": True}),
                error=None,
                progress_json=json.dumps({"phase": "inserting", "done": 1, "total": 1}),
                heartbeat_at=db.utc_now(),
                finished_at=db.utc_now(),
            )
            return
        error = (journal["error"] if journal else None) or "Import interrupted; retrying the saved dataset."
        if attempt < 3:
            retry_at = datetime.now(UTC) + timedelta(seconds=ingestion_jobs.RETRY_SECONDS + 5)
            _update(
                job_id,
                status="queued",
                error=str(error)[:2000],
                progress_json=json.dumps({"phase": "retrying", "attempt": attempt}),
                heartbeat_at=db.utc_now(),
                next_attempt_at=retry_at.isoformat(timespec="seconds"),
            )
        else:
            _update(
                job_id,
                status="error",
                error=str(error)[:2000],
                progress_json=json.dumps({"phase": "error", "attempt": attempt}),
                heartbeat_at=db.utc_now(),
                finished_at=db.utc_now(),
            )
    except Exception as exc:  # noqa: BLE001 - retry transient worker failures
        attempt = int(task.get("attempts") or 1)
        if attempt < 3:
            retry_at = datetime.now(UTC) + timedelta(seconds=ingestion_jobs.RETRY_SECONDS + 5)
            _update(
                job_id,
                status="queued",
                error=str(exc)[:2000],
                progress_json=json.dumps({"phase": "retrying", "attempt": attempt}),
                heartbeat_at=db.utc_now(),
                next_attempt_at=retry_at.isoformat(timespec="seconds"),
            )
        else:
            _update(
                job_id,
                status="error",
                error=str(exc)[:2000],
                progress_json=json.dumps({"phase": "error", "attempt": attempt}),
                heartbeat_at=db.utc_now(),
                finished_at=db.utc_now(),
            )
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)


def _recover_stale_jobs() -> int:
    """Return abandoned running rows to the durable queue.

    This runs repeatedly, not only at process boot, because a deployment can
    leave the old worker alive briefly while the API process has already
    restarted. Resetting the attempt counter gives the persisted ingestion
    journal time to let its lease expire and resume the existing Apify run.
    """
    stale_before = (
        datetime.now(UTC) - timedelta(seconds=_STALE_HEARTBEAT_SECONDS)
    ).isoformat(timespec="seconds")
    with db.connect() as conn:
        _ensure_schema(conn)
        return conn.execute(
            """UPDATE account_backfill_jobs
               SET status = 'queued', started_at = NULL,
                   progress_json = ?, error = NULL, next_attempt_at = NULL,
                   attempts = 0
               WHERE status = 'running'
                 AND (heartbeat_at IS NULL OR heartbeat_at < ?)""",
            (json.dumps({"phase": "queued", "recovered": True}), stale_before),
        ).rowcount


def _worker_loop() -> None:
    # A process restart can leave a job marked running. Only recover jobs whose
    # heartbeat is genuinely stale, so a worker restart cannot interrupt a
    # healthy job owned by another worker process.
    try:
        _recover_stale_jobs()
    except Exception:
        logger.exception("Could not recover account backfill queue")

    while True:
        task = None
        try:
            _recover_stale_jobs()
            task = _claim_next()
            if task:
                _run(task)
                continue
        except Exception:
            logger.exception("Account backfill queue worker failed")
        _WAKE.wait(timeout=2.0)
        _WAKE.clear()


def start_worker() -> None:
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True
        threading.Thread(target=_worker_loop, daemon=True, name="account-backfill-queue").start()


def status() -> dict[str, Any]:
    with db.connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """SELECT * FROM account_backfill_jobs
               ORDER BY CASE WHEN status IN ('queued', 'running') THEN 0 ELSE 1 END,
                        requested_at ASC LIMIT 50"""
        ).fetchall()
    tasks = [_task(row) for row in rows]
    active = next((task for task in tasks if task["status"] == "running"), None)
    queue = [task for task in tasks if task["status"] == "queued"]
    recent = next((task for task in tasks if task["status"] in {"done", "error"}), None)
    source = active or recent
    return {
        "running": bool(active),
        "handle": source["handle"] if source else None,
        "result": source["result"] if source else None,
        "error": source["error"] if source else None,
        "progress": source["progress"] if source else None,
        "started_at": source["started_at"] if source else None,
        "active": active,
        "queue": queue,
        "tasks": tasks,
    }
