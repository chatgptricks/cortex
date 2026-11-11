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
RETRY_ALERT_KEY = "posts_retry_alert"
PROVIDER_ALERT_KEY = "posts_apify_alert"
_PROVIDER_RETRY_REASONS = {"apify_auth", "apify_unavailable", "apify_pending"}
# A slot retrying the same error this long is almost certainly stuck on a
# deterministic failure; tell the DEVs long before the 2-hour stale alert.
RETRY_ALERT_AFTER_SECONDS = 20 * 60

_started = False
_lock = threading.Lock()


def _discovery_job() -> dict | None:
    from .db import connect

    with connect() as conn:
        row = conn.execute(
            "SELECT slot, status, state, error, updated_at FROM ingestion_jobs WHERE job_key = ?",
            (DISCOVERY_JOB_KEY,),
        ).fetchone()
    if not row:
        return None
    try:
        state = json.loads(row["state"] or "{}")
    except ValueError:
        state = {}
    return {"slot": row["slot"], "status": row["status"], "error": row["error"], "updated_at": row["updated_at"],
            "last_success_at": state.get("last_success_at"), "frozen_now": state.get("now"),
            "retry_reason": state.get("retry_reason"), "retry_started_at": state.get("retry_started_at"),
            "abandoned": state.get("abandoned")}


def _cst(value: datetime) -> str:
    from datetime import timedelta, timezone

    return value.astimezone(timezone(timedelta(hours=-6))).strftime("%b %d, %I:%M %p CST")


def _check_retry_loop(job: dict, now: datetime) -> bool:
    """Report an Apify incident once, or a deterministic retry per slot/error."""
    from .scheduler import _state_get, _state_set
    from .slack_alerts import notify_devs

    if job["status"] != "retry":
        return False
    reason = job.get("retry_reason")
    provider_failure = reason in _PROVIDER_RETRY_REASONS
    started_at = (job.get("retry_started_at") or job.get("frozen_now")) if provider_failure else job.get("frozen_now")
    if not started_at and reason != "apify_auth":
        return False
    started = datetime.fromisoformat(started_at) if started_at else now
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    minutes = (now - started).total_seconds() / 60
    if reason != "apify_auth" and minutes * 60 < RETRY_ALERT_AFTER_SECONDS:
        return False
    if provider_failure:
        # Keep this marker until discovery catches up. A resumed paid run can
        # advance an old frozen watermark while the live window is still stale.
        if _state_get(PROVIDER_ALERT_KEY):
            return False
        marker = job.get("last_success_at") or f"uninitialized:{job['slot']}"
        _state_set(PROVIDER_ALERT_KEY, marker)
        if reason == "apify_auth":
            title = "Apify rejected the ingestion credentials"
            detail = (
                "Apify credentials are missing or were rejected (HTTP 401/403). Check or replace APIFY_TOKEN in Render for both "
                "`cortex-ingestion-worker` and `cortex-api`, then redeploy the affected services. "
                "The saved discovery slot and any existing paid runs are retained for recovery."
            )
        elif reason == "apify_pending":
            title = "New-post discovery is reconciling an Apify run"
            detail = (
                "Apify has not confirmed whether the run started or finished. Automatic retries "
                "reconcile the saved run before starting another, preserving its recovery journal."
            )
        else:
            title = "New-post discovery is waiting for Apify"
            detail = (
                "Apify requests are temporarily failing or being rate-limited. Discovery retries "
                "with backoff and retains the saved slot and any existing paid runs."
            )
        notify_devs(title, f"{detail}\nError: `{(job.get('error') or 'unknown')[:400]}`")
        logger.warning("Discovery waiting on %s; DEVs alerted", reason)
        return True
    marker = f"{job['slot']}|{(job.get('error') or '')[:200]}"
    if _state_get(RETRY_ALERT_KEY) == marker:
        return False
    _state_set(RETRY_ALERT_KEY, marker)
    notify_devs(
        "New-post discovery is stuck retrying",
        f"The discovery slot `{job['slot']}` has been retrying for {minutes:.0f} minutes and blocks every "
        f"later cycle.\nError: `{(job.get('error') or 'unknown')[:400]}`\n"
        "Existing paid Apify runs are retained and reused on retry, but no new posts arrive "
        "until the cause is fixed.",
    )
    logger.warning("Discovery slot %s stuck retrying for %.0f min; DEVs alerted", job["slot"], minutes)
    return True


ABANDON_ALERT_KEY = "posts_abandon_alert"


def _check_abandoned(job: dict) -> None:
    """Tell the DEVs once when a discovery slot was given up after repeated
    failures (ingestion continued; the next slot covers its window)."""
    from .scheduler import _state_get, _state_set
    from .slack_alerts import notify_devs

    abandoned = job.get("abandoned") or {}
    if not abandoned.get("at") or _state_get(ABANDON_ALERT_KEY) == abandoned["at"]:
        return
    _state_set(ABANDON_ALERT_KEY, abandoned["at"])
    notify_devs(
        "A discovery slot was skipped after repeated failures",
        f"After {abandoned.get('attempts')} failed attempts the slot was abandoned so ingestion keeps running; "
        f"the next slot re-covers its window.\nError: `{str(abandoned.get('error') or 'unknown')[:400]}`",
    )


def check_once(now: datetime | None = None) -> str:
    """Return 'alerted', 'recovered' or 'ok' (used by tests and logs)."""
    from .scheduler import _state_get, _state_set
    from .slack_alerts import notify_devs

    job = _discovery_job()
    if not job:
        return "ok"
    now = now or datetime.now(UTC)
    retry_alerted = _check_retry_loop(job, now)
    _check_abandoned(job)
    if not job["last_success_at"]:
        return "alerted" if retry_alerted else "ok"
    last = datetime.fromisoformat(job["last_success_at"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    age_hours = (now - last).total_seconds() / 3600
    alerted_for = _state_get(ALERT_STATE_KEY) or ""
    provider_alerted_for = _state_get(PROVIDER_ALERT_KEY) or ""

    if age_hours * 3600 > STALE_AFTER_SECONDS:
        if provider_alerted_for:
            return "alerted" if retry_alerted else "ok"
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

    if provider_alerted_for:
        # A still-retrying job, or an abandoned slot, is not a recovery even
        # when its last successful reading happens to be under two hours old.
        if job["status"] != "done" or job.get("error") or job["last_success_at"] == provider_alerted_for:
            return "alerted" if retry_alerted else "ok"
        _state_set(PROVIDER_ALERT_KEY, "")
    if alerted_for or provider_alerted_for:
        _state_set(ALERT_STATE_KEY, "")
        notify_devs(
            "New-post discovery recovered",
            f"Discovery succeeded again (data as of *{_cst(last)}*). "
            "Automatic discovery has resumed and its recovery watermark is current.",
        )
        return "recovered"
    return "alerted" if retry_alerted else "ok"


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
