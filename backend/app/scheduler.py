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
# runs around the clock, but at two cadences: every 30 minutes during posting
# hours, and hourly overnight. Detection speed matters most right after a post
# goes up, and almost nothing is published between midnight and dawn -- so
# paying for 30-min checks then would be spending without upside. Measured at
# ~$0.079/cycle: 40 cycles/day here vs 48 if it were 30-min around the clock.
_ACTIVE_START_HOUR = 7  # 7:00am CST
_ACTIVE_END_HOUR = 23  # 11:00pm CST (exclusive -- 23:xx is already overnight)
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
    """Identifier for the current slot, so a tick loop that wakes every ~30s only
    fires the job once per slot.

    During posting hours the slot is half-hourly ('2026-07-27T14:30'); overnight
    it's hourly ('2026-07-27T03'). The two formats can't collide, so switching
    cadence across the boundary never re-fires or skips a slot.

    HOT detection is unaffected by the cadence: the "first hour" check computes a
    real likes/hour rate from the post's actual age rather than assuming exactly
    1.0h, so it stays accurate wherever the tick lands.
    """
    if _ACTIVE_START_HOUR <= now_cst.hour < _ACTIVE_END_HOUR:
        half = 0 if now_cst.minute < 30 else 30
        return now_cst.strftime("%Y-%m-%dT%H:") + f"{half:02d}"
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


def _run_account_snapshot_job() -> None:
    """One row per active account per day into account_snapshots -- the
    Tracker page's entire follower-growth chart is built from this. A
    separate lightweight Apify 'details' scrape per account (the same call
    fetch_profile_preview already makes for the add-account wizard), not
    bundled into run_daily_cycle above since that scrapes individual posts
    and never touches the profile-level follower count.
    """
    from .apify_sync import snapshot_all_accounts
    from .slack_alerts import notify_snapshot_failure, slack_configured

    try:
        result = snapshot_all_accounts()
        if result["failed"]:
            logger.error("Account snapshot job: %d ok, failed: %s", len(result["snapshotted"]), result["failed"])
            # This job runs once a day and is the Tracker's only data source,
            # so a silent failure costs a full day of follower history that
            # cannot be recovered afterwards. Always shout about it.
            if slack_configured():
                notify_snapshot_failure(len(result["snapshotted"]), result["failed"])
        else:
            logger.info("Account snapshot job: %d accounts snapshotted", len(result["snapshotted"]))
    except Exception:
        logger.exception("Account snapshot job crashed")
        if slack_configured():
            notify_snapshot_failure(0, {"*": "snapshot job crashed before it could run -- see Render logs"})


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


_DISK_THRESHOLDS = (85, 70)  # checked high-to-low; only the highest crossed one fires
_DISK_STATE_KEY = "disk_alert_level"


def _check_disk() -> None:
    """Warns once per threshold crossed, and re-arms when usage drops back below.

    Written after the disk filled at 2GB and took the whole API down: SQLite
    couldn't open its WAL, every DB-backed route returned 500, and the service
    stopped booting. That failure has no gradual phase to catch -- it goes from
    fine to fully down -- so the alert has to fire while space still remains.

    The last-alerted level is persisted like the other scheduler markers, so a
    Render restart doesn't re-send an alert that already went out.
    """
    import shutil

    from .config import DATA_DIR
    from .slack_alerts import notify_disk_warning, slack_configured

    try:
        usage = shutil.disk_usage(str(DATA_DIR))
    except Exception:
        logger.exception("Disk usage check failed")
        return

    pct = usage.used / usage.total * 100
    crossed = next((t for t in _DISK_THRESHOLDS if pct >= t), 0)

    try:
        last = int(_state_get(_DISK_STATE_KEY) or 0)
    except ValueError:
        last = 0

    if crossed > last:
        logger.warning("Disk at %.1f%% -- crossed the %d%% threshold", pct, crossed)
        if slack_configured():
            notify_disk_warning(pct, usage.used / 1e6, usage.total / 1e6, crossed)
        _state_set(_DISK_STATE_KEY, str(crossed))
    elif crossed < last:
        # Dropped back down (space was freed): re-arm so the next crossing alerts again.
        _state_set(_DISK_STATE_KEY, str(crossed))


def _tick() -> None:
    now_cst = datetime.now(_CST)

    # Never pauses: every 30 min during posting hours, hourly overnight. A post
    # published at 2am still gets its first-hour HOT check on time.
    bucket = _bucket_key(now_cst)
    if bucket != _state_get(_SHORT_BUCKET_KEY):
        # Claim the bucket *before* running so a crash mid-job doesn't leave
        # it unclaimed and re-fire on the next 30s tick.
        _state_set(_SHORT_BUCKET_KEY, bucket)
        _check_disk()  # cheap (one statvfs) and runs before the jobs that write
        _run_short_term_jobs()
        _run_ocr_job()

    daily_trigger = now_cst.replace(hour=_DAILY_JOB_AT[0], minute=_DAILY_JOB_AT[1], second=0, microsecond=0)
    today = now_cst.strftime("%Y-%m-%d")
    if now_cst >= daily_trigger and _state_get(_DAILY_DATE_KEY) != today:
        _state_set(_DAILY_DATE_KEY, today)
        _run_daily_jobs()
        _run_account_snapshot_job()


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
        "Engagement scheduler started (short-term: every 30min 7am-11pm CST, hourly overnight; "
        "daily: 7:00am fixed CST)"
    )
