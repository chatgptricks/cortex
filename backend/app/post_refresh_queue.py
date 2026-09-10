"""Durable queue for single-post count refreshes.

Reload Counts is user initiated, but the Instagram/Apify request can take
longer than an HTTP request is allowed to stay open.  Keep the request and
the paid work separate so a browser can poll a durable result instead of
waiting on Render's proxy timeout.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import logging
import threading
import uuid
from typing import Any

from . import db

logger = logging.getLogger(__name__)
_WAKE = threading.Event()
_START_LOCK = threading.Lock()
_STARTED = False
_STALE_SECONDS = 600
_CLAIM_LOCK_KEY = 7042198363
_ENQUEUE_LOCK_KEY = 7042198364


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def initialize(conn: Any) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS post_refresh_jobs (
               job_id TEXT PRIMARY KEY,
               job_key TEXT NOT NULL UNIQUE,
               account TEXT NOT NULL,
               shortcode TEXT NOT NULL,
               status TEXT NOT NULL,
               result_json TEXT NOT NULL DEFAULT '{}',
               error TEXT,
               requested_at TEXT NOT NULL,
               started_at TEXT,
               heartbeat_at TEXT,
               finished_at TEXT,
               updated_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_post_refresh_jobs_status "
        "ON post_refresh_jobs(status, requested_at)"
    )


def _item(row: Any) -> dict[str, Any]:
    item = dict(row)
    try:
        item["result"] = json.loads(item.pop("result_json") or "{}")
    except (TypeError, ValueError):
        item["result"] = {}
    return item


def enqueue(*, account: str, shortcode: str) -> dict[str, Any]:
    clean_account = str(account or "").strip().lstrip("@").lower()
    clean_shortcode = str(shortcode or "").strip()
    if not clean_account or not clean_shortcode:
        raise ValueError("Account and shortcode are required.")
    job_key = f"post-refresh:{clean_account}:{clean_shortcode}"
    now = _now()
    with db.connect() as conn:
        initialize(conn)
        if getattr(conn, "is_postgres", False):
            conn.execute("SELECT pg_advisory_xact_lock(?)", (_ENQUEUE_LOCK_KEY,))
        else:
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM post_refresh_jobs WHERE job_key = ?", (job_key,)
        ).fetchone()
        if row and row["status"] in {"queued", "running"}:
            result = _item(row)
        elif row:
            conn.execute(
                """UPDATE post_refresh_jobs
                   SET account = ?, shortcode = ?, status = 'queued',
                       result_json = '{}', error = NULL, requested_at = ?,
                       started_at = NULL, heartbeat_at = NULL, finished_at = NULL,
                       updated_at = ?
                   WHERE job_key = ?""",
                (clean_account, clean_shortcode, now, now, job_key),
            )
            result = _item(conn.execute(
                "SELECT * FROM post_refresh_jobs WHERE job_key = ?", (job_key,)
            ).fetchone())
        else:
            job_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO post_refresh_jobs(
                       job_id, job_key, account, shortcode, status,
                       requested_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
                (job_id, job_key, clean_account, clean_shortcode, now, now),
            )
            result = _item(conn.execute(
                "SELECT * FROM post_refresh_jobs WHERE job_id = ?", (job_id,)
            ).fetchone())
    _WAKE.set()
    return result


def get(job_id: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        initialize(conn)
        row = conn.execute(
            "SELECT * FROM post_refresh_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    return _item(row) if row else None


def _recover_stale() -> None:
    threshold = (datetime.now(UTC) - timedelta(seconds=_STALE_SECONDS)).isoformat(timespec="seconds")
    with db.connect() as conn:
        initialize(conn)
        conn.execute(
            """UPDATE post_refresh_jobs
               SET status = 'queued', started_at = NULL, heartbeat_at = NULL,
                   updated_at = ?, error = 'Refresh worker restarted; retrying.'
               WHERE status = 'running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)""",
            (_now(), threshold),
        )


def _claim() -> dict[str, Any] | None:
    with db.connect() as conn:
        initialize(conn)
        if getattr(conn, "is_postgres", False):
            row = conn.execute(
                "SELECT pg_try_advisory_xact_lock(?) AS locked", (_CLAIM_LOCK_KEY,)
            ).fetchone()
            if not row or not row.get("locked"):
                return None
        else:
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM post_refresh_jobs WHERE status = 'queued' ORDER BY requested_at LIMIT 1"
        ).fetchone()
        if not row:
            return None
        now = _now()
        changed = conn.execute(
            """UPDATE post_refresh_jobs
               SET status = 'running', started_at = ?, heartbeat_at = ?, updated_at = ?
               WHERE job_id = ? AND status = 'queued'""",
            (now, now, now, row["job_id"]),
        ).rowcount
        if changed != 1:
            return None
        return _item(conn.execute(
            "SELECT * FROM post_refresh_jobs WHERE job_id = ?", (row["job_id"],)
        ).fetchone())


def _heartbeat(job_id: str, stop: threading.Event) -> None:
    while not stop.wait(15):
        try:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE post_refresh_jobs SET heartbeat_at = ?, updated_at = ? "
                    "WHERE job_id = ? AND status = 'running'",
                    (_now(), _now(), job_id),
                )
        except Exception:  # pragma: no cover - defensive worker telemetry
            logger.exception("Could not update post refresh heartbeat")


def _run(task: dict[str, Any]) -> None:
    job_id = task["job_id"]
    stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(job_id, stop), daemon=True)
    heartbeat.start()
    try:
        from .apify_sync import refresh_single_post

        result = refresh_single_post(task["account"], task["shortcode"])
        now = _now()
        with db.connect() as conn:
            conn.execute(
                """UPDATE post_refresh_jobs
                   SET status = 'done', result_json = ?, error = NULL,
                       finished_at = ?, updated_at = ?
                   WHERE job_id = ?""",
                (json.dumps(result, default=str), now, now, job_id),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Post refresh %s failed", job_id)
        with db.connect() as conn:
            conn.execute(
                """UPDATE post_refresh_jobs
                   SET status = 'error', error = ?, finished_at = ?, updated_at = ?
                   WHERE job_id = ?""",
                (str(exc)[:1000], _now(), _now(), job_id),
            )
    finally:
        stop.set()
        heartbeat.join(timeout=1)


def _loop() -> None:
    while True:
        try:
            _recover_stale()
            task = _claim()
            if task:
                _run(task)
                continue
        except Exception:  # pragma: no cover - keep worker alive
            logger.exception("Post refresh worker failed")
        _WAKE.wait(2)
        _WAKE.clear()


def start_worker() -> None:
    global _STARTED
    with _START_LOCK:
        if _STARTED:
            return
        _STARTED = True
        threading.Thread(target=_loop, daemon=True, name="post-refresh-worker").start()
