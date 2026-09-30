"""Watch new-post discovery from outside the ingestion worker.

The worker cannot report its own death or a hang in its scheduler loop, so the
public API process checks the durable discovery watermark instead. It only
reads one row and sends at most one DEV alert per stalled watermark, plus one
"recovered" message; it never starts ingestion work in the web process.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime

logger = logging.getLogger("uvicorn.error")

# Discovery runs every 45 minutes by day and hourly overnight, so two hours
# without a successful slot means at least one full cycle was missed.
STALE_AFTER_SECONDS = 2 * 60 * 60
CHECK_EVERY_SECONDS = 10 * 60
DISCOVERY_JOB_KEY = "scheduled-posts"
ALERT_STATE_KEY = "posts_stale_alert"

_started = False
_lock = threading.Lock()


def _discovery_job() -> dict | None:
    from .db import connect

    with connect() as conn:
        row = conn.execute(
            "SELECT status, state, error, updated_at FROM ingestion_jobs WHERE job_key = ?",
            (DISCOVERY_JOB_KEY,),
        ).fetchone()
    if not row:
        return None
    try:
        state = json.loads(row["state"] or "{}")
    except ValueError:
        state = {}
    return {"status": row["status"], "error": row["error"], "updated_at": row["updated_at"],
            "last_success_at": state.get("last_success_at")}


def _cst(value: datetime) -> str:
    from datetime import timedelta, timezone

    return value.astimezone(timezone(timedelta(hours=-6))).strftime("%b %d, %I:%M %p CST")


def check_once(now: datetime | None = None) -> str:
    """Return 'alerted', 'recovered' or 'ok' (used by tests and logs)."""
    from .scheduler import _state_get, _state_set
    from .slack_alerts import notify_devs

    job = _discovery_job()
    if not job or not job["last_success_at"]:
        return "ok"
    now = now or datetime.now(UTC)
    last = datetime.fromisoformat(job["last_success_at"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    age_hours = (now - last).total_seconds() / 3600
    alerted_for = _state_get(ALERT_STATE_KEY) or ""

    if age_hours * 3600 > STALE_AFTER_SECONDS:
        if alerted_for == job["last_success_at"]:
            return "ok"
        _state_set(ALERT_STATE_KEY, job["last_success_at"])
        detail = f"\nLast error: `{job['error'][:300]}`" if job.get("error") else ""
        notify_devs(
            f"No new posts collected for {age_hours:.1f}h",
            f"New-post discovery last succeeded *{_cst(last)}* ({age_hours:.1f} hours ago).\n"
            f"Job status: `{job['status']}`, updated {job['updated_at']}.{detail}\n"
            "Check `cortex-ingestion-worker` in Render. Paid Apify runs are kept in the "
            "ingestion journal and resume without being charged again.",
        )
        logger.warning("New-post discovery stale for %.1fh; DEVs alerted", age_hours)
        return "alerted"

    if alerted_for:
        _state_set(ALERT_STATE_KEY, "")
        notify_devs(
            "New-post discovery recovered",
            f"Discovery succeeded again (data as of *{_cst(last)}*). "
            "The next cycle fetches everything published during the gap.",
        )
        return "recovered"
    return "ok"


def _loop(stop: threading.Event) -> None:
    while not stop.wait(CHECK_EVERY_SECONDS):
        try:
            check_once()
        except Exception:
            logger.exception("Ingestion monitor check failed")


def start_monitor(stop: threading.Event | None = None) -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, args=(stop or threading.Event(),), daemon=True,
                     name="ingestion-monitor").start()
