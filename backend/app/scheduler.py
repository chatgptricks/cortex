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

# The short-term job (new posts + <=24h engagement + the one-time HOT check)
# now runs every hour around the clock. It used to pause overnight, which meant
# anything published between 11:30pm and 7:30am got its first look hours late --
# by then the post is no longer "new" and the chance to surface it early is gone.
_DAILY_JOB_AT = (7, 0)  # 7:00am CST

_started = False
_lock = threading.Lock()

_SHORT_BUCKET_KEY = "last_short_bucket"
_DAILY_DATE_KEY = "last_daily_date"

# Covers OCR'd per hourly tick. New posts arrive at a few per account per day,
# so this keeps up easily while also chipping away at any backlog without
# making one tick run long.
_OCR_PER_TICK = 30


def _state_get(key: str) -> str | None:
    """Scheduler run markers live in the DB, not memory: Render restarts the
    process on every deploy, and in-memory markers meant both jobs re-fired
    immediately on boot -- re-running the full daily engagement cycle (real
    Apify credits) once per deploy.
    """
    from .db import connect

    try:
        with connect() as conn:
            row = conn.execute("SELECT value FROM scheduler_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    except Exception:
        logger.exception("Failed to read scheduler state %s", key)
        return None


def _state_set(key: str, value: str) -> None:
    from .db import connect, utc_now

    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO scheduler_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, utc_now()),
            )
    except Exception:
        logger.exception("Failed to persist scheduler state %s", key)




def _bucket_key(now_cst: datetime) -> str:
    """Hourly bucket identifier, e.g. '2026-07-27T07' -- used so a tick loop
    that wakes every ~30s only fires each job once per bucket. HOT detection
    still works fine on this coarser cadence: the "first hour" check already
    computes an actual likes/hour rate from the post's real age rather than
    assuming exactly 1.0h, so it stays accurate however the tick lands."""
    return now_cst.strftime("%Y-%m-%dT%H")


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
    from .apify_sync import ApifySyncError, run_short_term_cycle_batch

    accounts = _active_account_handles()
    if not accounts:
        return
    # One Apify call for every active account (each still gets its own
    # up-to-results_limit posts -- resultsLimit is a per-URL cap, not a
    # shared total) instead of one call per account, cutting per-run
    # overhead N-fold.
    try:
        results = run_short_term_cycle_batch(accounts)
        logger.info("Short-term engagement cycle (batched, %d accounts): %s", len(accounts), results)
    except ApifySyncError as exc:
        logger.error("Short-term engagement cycle (batched) failed: %s", exc)
    except Exception:
        logger.exception("Short-term engagement cycle (batched) crashed")


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


def _run_ocr_job() -> None:
    """Keeps cover OCR current without anyone running it by hand: each hourly
    tick tops up a bounded number of covers still missing hook_text, so posts
    that arrived since the last tick become text-searchable on their own.
    """
    from .apify_sync import run_ocr_sweep

    try:
        result = run_ocr_sweep(limit=_OCR_PER_TICK)
        if result.get("sent") or result.get("skipped"):
            logger.info("Cover OCR sweep: %s", result)
    except Exception:
        logger.exception("Cover OCR sweep crashed")


def _tick() -> None:
    now_cst = datetime.now(_CST)

    # Runs every hour, 24/7 -- no overnight pause, so a post published at 2am
    # still gets its first-hour HOT check on time.
    bucket = _bucket_key(now_cst)
    if bucket != _state_get(_SHORT_BUCKET_KEY):
        # Claim the bucket *before* running so a crash mid-job doesn't leave
        # it unclaimed and re-fire on the next 30s tick.
        _state_set(_SHORT_BUCKET_KEY, bucket)
        _run_short_term_jobs()
        _run_ocr_job()

    daily_trigger = now_cst.replace(hour=_DAILY_JOB_AT[0], minute=_DAILY_JOB_AT[1], second=0, microsecond=0)
    today = now_cst.strftime("%Y-%m-%d")
    if now_cst >= daily_trigger and _state_get(_DAILY_DATE_KEY) != today:
        _state_set(_DAILY_DATE_KEY, today)
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
        "Engagement scheduler started (short-term: hourly, 24/7; "
        "daily: 7:00am fixed CST)"
    )
