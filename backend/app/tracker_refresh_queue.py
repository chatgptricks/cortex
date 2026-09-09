"""Durable, deduplicated Tracker refresh jobs owned by the worker process."""
from __future__ import annotations

import json
import logging
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


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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


def _item(row: Any) -> dict[str, Any]:
    item = dict(row)
    try:
        item["result"] = json.loads(item.pop("result_json") or "{}")
    except (TypeError, ValueError):
        item["result"] = {}
    return item


def enqueue(*, kind: str, handle: str | None, requested_by: str) -> dict[str, Any]:
    if kind not in {"account", "all"}:
        raise ValueError("Unknown Tracker refresh kind")
    clean_handle = (handle or "").strip().lstrip("@").lower() or None
    if kind == "account" and not clean_handle:
        raise ValueError("Account refresh requires a handle")
    # One paid refresh per scope/day. Repeated clicks return the same durable
    # job rather than launching another Apify run.
    day = datetime.now(UTC).date().isoformat()
    job_key = f"tracker:{kind}:{clean_handle or 'all'}:{day}"
    now = _now()
    with db.connect() as conn:
        initialize(conn)
        conn.execute(
            """INSERT INTO tracker_refresh_jobs(job_id, job_key, kind, handle, requested_by, status, requested_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
               ON CONFLICT(job_key) DO NOTHING""",
            (uuid.uuid4().hex, job_key, kind, clean_handle, requested_by, now, now),
        )
        row = conn.execute("SELECT * FROM tracker_refresh_jobs WHERE job_key = ?", (job_key,)).fetchone()
    _WAKE.set()
    return _item(row)


def get(job_id: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        initialize(conn)
        row = conn.execute("SELECT * FROM tracker_refresh_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _item(row) if row else None


def _recover_stale() -> None:
    threshold = (datetime.now(UTC) - timedelta(seconds=_STALE_SECONDS)).isoformat(timespec="seconds")
    with db.connect() as conn:
        initialize(conn)
        conn.execute(
            """UPDATE tracker_refresh_jobs SET status = 'queued', started_at = NULL,
                   heartbeat_at = NULL, updated_at = ?
               WHERE status = 'running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)""",
            (_now(), threshold),
        )


def _claim() -> dict[str, Any] | None:
    with db.connect() as conn:
        initialize(conn)
        row = conn.execute("SELECT * FROM tracker_refresh_jobs WHERE status = 'queued' ORDER BY requested_at LIMIT 1").fetchone()
        if not row:
            return None
        now = _now()
        changed = conn.execute(
            """UPDATE tracker_refresh_jobs SET status = 'running', started_at = ?, heartbeat_at = ?, updated_at = ?
               WHERE job_id = ? AND status = 'queued'""",
            (now, now, now, row["job_id"]),
        ).rowcount
        if changed != 1:
            return None
        row = conn.execute("SELECT * FROM tracker_refresh_jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
    return _item(row)


def _run(task: dict[str, Any]) -> None:
    job_id = task["job_id"]
    stop = threading.Event()
    def heartbeat() -> None:
        while not stop.wait(15):
            with db.connect() as conn:
                conn.execute("UPDATE tracker_refresh_jobs SET heartbeat_at = ?, updated_at = ? WHERE job_id = ? AND status = 'running'", (_now(), _now(), job_id))
    thread = threading.Thread(target=heartbeat, daemon=True, name=f"tracker-refresh-{job_id[:8]}")
    thread.start()
    try:
        result_box: dict[str, Any] = {}
        def work() -> None:
            from .apify_sync import snapshot_all_accounts, snapshot_one_account
            result_box["value"] = snapshot_all_accounts() if task["kind"] == "all" else snapshot_one_account(task["handle"])
        complete = ingestion_jobs.run(f"tracker-refresh:{task['job_key']}", datetime.now(UTC).date().isoformat(), work)
        now = _now()
        with db.connect() as conn:
            if complete:
                conn.execute("UPDATE tracker_refresh_jobs SET status = 'done', result_json = ?, error = NULL, finished_at = ?, updated_at = ? WHERE job_id = ?", (json.dumps(result_box.get("value") or {}, default=str), now, now, job_id))
            else:
                conn.execute("UPDATE tracker_refresh_jobs SET status = 'error', error = 'Refresh interrupted; retry tomorrow or inspect ingestion job.', finished_at = ?, updated_at = ? WHERE job_id = ?", (now, now, job_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Tracker refresh %s failed", job_id)
        with db.connect() as conn:
            conn.execute("UPDATE tracker_refresh_jobs SET status = 'error', error = ?, finished_at = ?, updated_at = ? WHERE job_id = ?", (str(exc)[:1000], _now(), _now(), job_id))
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
