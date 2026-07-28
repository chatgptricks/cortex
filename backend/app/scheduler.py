from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("uvicorn.error")

# Fixed Central Standard Time (UTC-6), not DST-aware -- explicit product
# decision so the automated window stays pinned to the same clock times
# year-round rather than shifting with daylight saving.
_CST = timezone(timedelta(hours=-6))

_SHORT_JOB_START = (7, 30)  # 7:30am CST
_SHORT_JOB_END = (23, 30)  # 11:30pm CST
_DAILY_JOB_AT = (7, 0)  # 7:00am CST, right before the short-term window opens

_last_short_bucket: str | None = None
_last_daily_date: str | None = None
_started = False
_lock = threading.Lock()


def _in_short_job_window(now_cst: datetime) -> bool:
    start = now_cst.replace(hour=_SHORT_JOB_START[0], minute=_SHORT_JOB_START[1], second=0, microsecond=0)
    end = now_cst.replace(hour=_SHORT_JOB_END[0], minute=_SHORT_JOB_END[1], second=0, microsecond=0)
    return start <= now_cst <= end


def _bucket_key(now_cst: datetime) -> str:
    """30-minute bucket identifier, e.g. '2026-07-27T07:30' -- used so a
    tick loop that wakes every ~30s only fires each job once per bucket."""
    bucket_minute = 0 if now_cst.minute < 30 else 30
    return now_cst.strftime("%Y-%m-%dT%H:") + f"{bucket_minute:02d}"


def _active_account_handles() -> list[str]:
    """Pulled fresh from the DB on every run (not cached) so a new account
    added via the self-serve /api/admin/accounts endpoint is automatically
    picked up on the very next tick, with no redeploy needed.
    """
    from .apify_sync import list_accounts

    try:
        return [account["handle"] for account in list_accounts(active_only=True)]
    except Exception:
        logger.exception("Failed to load active accounts for scheduler tick")
        return []


def _run_short_term_jobs() -> None:
    from .apify_sync import ApifySyncError, run_short_term_cycle

    for account in _active_account_handles():
        try:
            result = run_short_term_cycle(account)
            logger.info("Short-term engagement cycle (%s): %s", account, result)
        except ApifySyncError as exc:
            logger.error("Short-term engagement cycle (%s) failed: %s", account, exc)
        except Exception:
            logger.exception("Short-term engagement cycle (%s) crashed", account)


def _run_daily_jobs() -> None:
    from .apify_sync import ApifySyncError, run_daily_cycle

    for account in _active_account_handles():
        try:
            result = run_daily_cycle(account)
            logger.info("Daily engagement cycle (%s): %s", account, result)
        except ApifySyncError as exc:
            logger.error("Daily engagement cycle (%s) failed: %s", account, exc)
        except Exception:
            logger.exception("Daily engagement cycle (%s) crashed", account)


def _tick() -> None:
    global _last_short_bucket, _last_daily_date
    now_cst = datetime.now(_CST)

    if _in_short_job_window(now_cst):
        bucket = _bucket_key(now_cst)
        if bucket != _last_short_bucket:
            _last_short_bucket = bucket
            _run_short_term_jobs()

    daily_trigger = now_cst.replace(hour=_DAILY_JOB_AT[0], minute=_DAILY_JOB_AT[1], second=0, microsecond=0)
    today = now_cst.strftime("%Y-%m-%d")
    if now_cst >= daily_trigger and _last_daily_date != today:
        _last_daily_date = today
        _run_daily_jobs()


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception:
            logger.exception("Engagement scheduler tick failed")
        time.sleep(30)


def start_scheduler() -> None:
    """Starts a single in-process background thread that drives both
    automated jobs. Safe to call multiple times (no-ops after the first).
    Relies on the web service running as a single always-on instance
    (Render 'standard' plan) -- if the service is ever scaled to multiple
    instances, each would run its own scheduler and jobs would fire once
    per instance.
    """
    global _started
    with _lock:
        if _started:
            return
        _started = True
    thread = threading.Thread(target=_loop, daemon=True, name="engagement-scheduler")
    thread.start()
    logger.info(
        "Engagement scheduler started (short-term: every 30min, 7:30am-11:30pm fixed CST; "
        "daily: 7:00am fixed CST)"
    )
