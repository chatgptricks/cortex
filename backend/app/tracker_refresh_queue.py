"""Durable, deduplicated Tracker refresh jobs owned by the worker process."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from . import db, ingestion_jobs

logger = logging.getLogger(__name__)
_WAKE = threading.Event()
_START_LOCK = threading.Lock()
_STARTED = False
_STALE_SECONDS = 300
_ENQUEUE_LOCK_KEY = 7042198374
_CLAIM_LOCK_KEY = 7042198375
_MAX_ATTEMPTS = 4


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def initialize(conn: Any) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tracker_refresh_jobs (
               job_id TEXT PRIMARY KEY, job_key TEXT NOT NULL UNIQUE,
               kind TEXT NOT NULL CHECK(kind IN ('account', 'all')),
               handle TEXT, requested_by TEXT NOT NULL, status TEXT NOT NULL,
               result_json TEXT NOT NULL DEFAULT '{}', error TEXT,
               requested_at TEXT NOT NULL, started_at TEXT, heartbeat_at TEXT,
               finished_at TEXT, updated_at TEXT NOT NULL
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tracker_refresh_jobs_status ON tracker_refresh_jobs(status, requested_at)")
    if getattr(conn, "is_postgres", False):
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracker_refresh_jobs)").fetchall()}
        if "attempts" not in columns:
            conn.execute("ALTER TABLE tracker_refresh_jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0")
        if "available_at" not in columns:
            conn.execute("ALTER TABLE tracker_refresh_jobs ADD COLUMN IF NOT EXISTS available_at TEXT")
    else:
        db._ensure_column(conn, "tracker_refresh_jobs", "attempts", "attempts INTEGER NOT NULL DEFAULT 0")
        db._ensure_column(conn, "tracker_refresh_jobs", "available_at", "available_at TEXT")


def _item(row: Any) -> dict[str, Any]:
    item = dict(row)
    try:
        item["result"] = json.loads(item.pop("result_json") or "{}")
    except (TypeError, ValueError):
        item["result"] = {}
    item["progress"] = item["result"].get("progress", {})
    return item


def enqueue(*, kind: str, handle: str | None, requested_by: str) -> dict[str, Any]:
    if kind not in {"account", "all"}:
        raise ValueError("Unknown Tracker refresh kind")
    clean_handle = (handle or "").strip().lstrip("@").lower() or None
    if kind == "account" and not clean_handle:
        raise ValueError("Account refresh requires a handle")
    # Share in-flight work only. Returning a completed 15-minute bucket made
    # an explicit reload appear successful without ever reading Instagram.
    job_id = uuid.uuid4().hex
    job_key = f"tracker:{kind}:{clean_handle or 'all'}:{job_id}"
    now = _now()
    with db.connect() as conn:
        initialize(conn)
        if getattr(conn, "is_postgres", False):
            conn.execute("SELECT pg_advisory_xact_lock(?)", (_ENQUEUE_LOCK_KEY,))
        else:
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT * FROM tracker_refresh_jobs
               WHERE status IN ('queued', 'running')
                 AND kind = ? AND COALESCE(handle, '') = ?
               ORDER BY requested_at LIMIT 1""",
            (kind, clean_handle or ""),
        ).fetchone()
        if not row:
            conn.execute(
                """INSERT INTO tracker_refresh_jobs(job_id, job_key, kind, handle, requested_by, status, requested_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (job_id, job_key, kind, clean_handle, requested_by, now, now),
            )
            row = conn.execute("SELECT * FROM tracker_refresh_jobs WHERE job_id = ?", (job_id,)).fetchone()
    _WAKE.set()
    return _item(row)


def last_requested_at(requested_by: str) -> datetime | None:
    """When this person last created a manual refresh (for per-user limits)."""
    with db.connect() as conn:
        initialize(conn)
        row = conn.execute(
            "SELECT requested_at FROM tracker_refresh_jobs WHERE requested_by = ? ORDER BY requested_at DESC LIMIT 1",
            (requested_by,),
        ).fetchone()
    if not row:
        return None
    value = datetime.fromisoformat(str(row["requested_at"]))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def get(job_id: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        initialize(conn)
        row = conn.execute("SELECT * FROM tracker_refresh_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _item(row) if row else None


def _recover_stale() -> None:
    threshold = (datetime.now(UTC) - timedelta(seconds=_STALE_SECONDS)).isoformat(timespec="microseconds")
    with db.connect() as conn:
        initialize(conn)
        conn.execute(
            """UPDATE tracker_refresh_jobs SET status = 'queued', started_at = NULL,
                   heartbeat_at = NULL, available_at = NULL, updated_at = ?
               WHERE status = 'running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)""",
            (_now(), threshold),
        )


def _claim() -> dict[str, Any] | None:
    with db.connect() as conn:
        initialize(conn)
        if getattr(conn, "is_postgres", False):
            lock = conn.execute("SELECT pg_try_advisory_xact_lock(?) AS locked", (_CLAIM_LOCK_KEY,)).fetchone()
            if not lock or not lock["locked"]:
                return None
        else:
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM tracker_refresh_jobs WHERE status = 'queued' "
            "AND (available_at IS NULL OR available_at <= ?) ORDER BY requested_at LIMIT 1", (_now(),)
        ).fetchone()
        if not row:
            return None
        now = _now()
        changed = conn.execute(
            """UPDATE tracker_refresh_jobs SET status = 'running', started_at = ?, heartbeat_at = ?, updated_at = ?, attempts = attempts + 1
               WHERE job_id = ? AND status = 'queued'""",
            (now, now, now, row["job_id"]),
        ).rowcount
        if changed != 1:
            return None
        row = conn.execute("SELECT * FROM tracker_refresh_jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
    return _item(row)


def _safe_error(exc: Exception) -> str:
    return re.sub(r"(?i)(token|api_key|authorization)=[^&\s]+", r"\1=REDACTED", str(exc))[:1000]


def _run(task: dict[str, Any]) -> None:
    job_id = task["job_id"]
    stop = threading.Event()
    def heartbeat() -> None:
        while not stop.wait(15):
            try:
                with db.connect() as conn:
                    conn.execute("UPDATE tracker_refresh_jobs SET heartbeat_at = ?, updated_at = ? WHERE job_id = ? AND status = 'running'", (_now(), _now(), job_id))
            except Exception:
                logger.exception("Could not update Tracker refresh heartbeat")
    thread = threading.Thread(target=heartbeat, daemon=True, name=f"tracker-refresh-{job_id[:8]}")
    thread.start()
    try:
        def progress(fields: dict[str, Any]) -> None:
            with db.connect() as conn:
                conn.execute("UPDATE tracker_refresh_jobs SET result_json = ?, updated_at = ? WHERE job_id = ? AND status = 'running'",
                             (json.dumps({"progress": fields}), _now(), job_id))

        def work() -> dict[str, Any]:
            from .apify_sync import ProfileSnapshotError, snapshot_all_accounts, snapshot_one_account
            if task["kind"] == "all":
                return snapshot_all_accounts(force=True, on_progress=progress)
            progress({"total": 1, "completed": 0, "succeeded": 0, "failed": 0, "handle": task["handle"]})
            try:
                return snapshot_one_account(task["handle"], force=True)
            except ProfileSnapshotError as exc:
                return {"error": _safe_error(exc)}
        # Retain the completed result: a crash after persisting snapshots but
        # before marking this queue row done must not erase success on replay.
        ingestion_key = f"tracker-refresh:{task['job_key']}"
        slot = task["requested_at"]
        with db.connect() as conn:
            ingestion_jobs.initialize(conn)
            previous = conn.execute("SELECT slot, status, state FROM ingestion_jobs WHERE job_key = ?", (ingestion_key,)).fetchone()
        if previous and (previous["status"] != "done" or "result" in json.loads(previous["state"])):
            # An old worker could claim this queue row during a rolling deploy
            # using its UTC-day slot. Keep that slot through retry/redelivery;
            # this queue job has its own unique ingestion key.
            slot = previous["slot"]
        result = ingestion_jobs.call(ingestion_key, work, slot=slot)
        if result.get("error"):
            with db.connect() as conn:
                conn.execute("UPDATE tracker_refresh_jobs SET status = 'error', error = ?, finished_at = ?, updated_at = ? WHERE job_id = ?",
                             (result["error"], _now(), _now(), job_id))
            return
        if task["kind"] == "all":
            succeeded, failed = len(result.get("snapshotted", [])), len(result.get("failed", {}))
        else:
            succeeded, failed = 1, 0
        result["progress"] = {"total": succeeded + failed, "completed": succeeded + failed, "succeeded": succeeded, "failed": failed}
        now = _now()
        with db.connect() as conn:
            conn.execute("UPDATE tracker_refresh_jobs SET status = 'done', result_json = ?, error = NULL, finished_at = ?, updated_at = ? WHERE job_id = ?", (json.dumps(result, default=str), now, now, job_id))
    except ingestion_jobs.IngestionPending as exc:
        # Resume the journal's paid run automatically after its retry lease.
        # A transient provider/DB error must not require another user click.
        retry = int(task.get("attempts") or 1) < _MAX_ATTEMPTS
        now = _now()
        available = (datetime.now(UTC) + timedelta(seconds=ingestion_jobs.RETRY_SECONDS + 1)).isoformat(timespec="microseconds")
        with db.connect() as conn:
            conn.execute("UPDATE tracker_refresh_jobs SET status = ?, error = ?, available_at = ?, finished_at = ?, updated_at = ? WHERE job_id = ?",
                         ("queued" if retry else "error", _safe_error(exc), available if retry else None, None if retry else now, now, job_id))
    except Exception as exc:  # noqa: BLE001
        logger.error("Tracker refresh %s failed: %s", job_id, _safe_error(exc))
        with db.connect() as conn:
            conn.execute("UPDATE tracker_refresh_jobs SET status = 'error', error = ?, finished_at = ?, updated_at = ? WHERE job_id = ?", (_safe_error(exc), _now(), _now(), job_id))
    finally:
        stop.set()
        thread.join(timeout=1)


def _loop() -> None:
    while True:
        try:
            _recover_stale()
            task = _claim()
            if task:
                _run(task)
                continue
        except Exception:  # noqa: BLE001
            logger.exception("Tracker refresh worker failed")
        _WAKE.wait(2)
        _WAKE.clear()


def start_worker() -> None:
    global _STARTED
    with _START_LOCK:
        if _STARTED:
            return
        _STARTED = True
        threading.Thread(target=_loop, daemon=True, name="tracker-refresh-worker").start()
