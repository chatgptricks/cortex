"""Slack alerts for HOT posts.

Runs entirely server-side on Render via an Incoming Webhook -- no local
process, no polling client, nothing to keep running on anyone's machine. The
webhook URL is read from the SLACK_WEBHOOK_URL environment variable; when it
isn't set the whole module no-ops, so the engagement cycle behaves exactly as
before on any environment without Slack configured.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote

logger = logging.getLogger("uvicorn.error")

# Public base used to build links back into the dashboard/cover images.
_PUBLIC_API = os.getenv("PUBLIC_API_BASE", "https://cortex-api-db2e.onrender.com").rstrip("/")
# Where the alert should take you. Overridable so a staging deploy doesn't
# send everyone to production.
_DASHBOARD = os.getenv("DASHBOARD_BASE", "https://sentientdash.app").rstrip("/")


def dashboard_url_for(account: str, shortcode: str) -> str:
    """Deep link that opens this post in the dashboard's detail rail.

    The dashboard keys a post by "account:shortcode" and reads ?post= on load,
    so this lands on the post itself rather than on the grid. Preferred over
    the Instagram permalink because the alert is a prompt to *do* something --
    mark it promo, read the numbers, pull the media -- and all of that lives in
    the dashboard.
    """
    key = quote(f"{account}:{shortcode}", safe="")
    return f"{_DASHBOARD}/?post={key}"


def slack_configured() -> bool:
    return bool(os.getenv("SLACK_WEBHOOK_URL", "").strip())


def _fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def build_hot_message(post: dict[str, Any]) -> dict[str, Any]:
    """Slack Block Kit payload for one HOT post."""
    account = post.get("account") or "?"
    multiplier = post.get("multiplier")
    likes = _fmt_int(post.get("likes"))
    rate = _fmt_int(post.get("rate_per_hour"))
    threshold = _fmt_int(post.get("threshold"))
    age = post.get("age_hours")
    age_txt = f"{age:.1f}h" if isinstance(age, (int, float)) else "—"
    mult_txt = f"{multiplier:.2f}x" if isinstance(multiplier, (int, float)) else "—"

    lines = [
        f"*{likes} likes* in {age_txt}  ·  ~{rate}/hr vs {threshold}/hr threshold",
    ]
    caption = (post.get("caption") or "").strip()
    if caption:
        lines.append(f"_{caption[:180]}{'…' if len(caption) > 180 else ''}_")

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🔥 HOT — @{account} ({mult_txt})", "emoji": True},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]

    cover_url = post.get("cover_url")
    if cover_url:
        blocks.append({"type": "image", "image_url": cover_url, "alt_text": f"Cover for @{account}"})
    # Only the dashboard link -- that's where you'd actually act on a HOT
    # post (mark it promo, pull the media, read the numbers), so the
    # Instagram permalink button was just a second click with nowhere to go.
    shortcode = post.get("shortcode")
    if shortcode:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Open post", "emoji": True},
                        "url": dashboard_url_for(account, str(shortcode)),
                    }
                ],
            }
        )

    return {"text": f"🔥 HOT — @{account} ({mult_txt}, {likes} likes)", "blocks": blocks}


def notify_hot_post(post: dict[str, Any]) -> bool:
    """Posts one HOT alert. Never raises: a Slack outage or a bad webhook must
    not break the engagement cycle that detected the post in the first place.
    """
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    try:
        import httpx

        response = httpx.post(webhook, json=build_hot_message(post), timeout=15.0)
        if response.status_code >= 300:
            logger.error("Slack HOT alert rejected (%s): %s", response.status_code, response.text[:200])
            return False
        return True
    except Exception:
        logger.exception("Slack HOT alert failed for @%s", post.get("account"))
        return False


def cover_url_for(account: str, post_id: int) -> str:
    return f"{_PUBLIC_API}/api/dashboard/covers/{account}/{post_id}"


def notify_custom(message: str, title: str | None = None) -> bool:
    """Posts a free-form alert typed in by hand from the admin panel's
    System tab -- for anything worth pinging Slack about that doesn't fit
    one of the purpose-built alerts above (a heads-up, a reminder, a note
    to the team). Same never-raises contract: a bad webhook here shouldn't
    surface as a 500 for what's already a manual, low-stakes action.
    """
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    clean = (message or "").strip()
    if not clean:
        return False
    heading = (title or "").strip() or "📣 Custom alert"
    payload = {
        "text": f"{heading}: {clean[:150]}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": heading, "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": clean[:2900]}},
        ],
    }
    try:
        import httpx

        response = httpx.post(webhook, json=payload, timeout=15.0)
        if response.status_code >= 300:
            logger.error("Slack custom alert rejected (%s): %s", response.status_code, response.text[:200])
            return False
        return True
    except Exception:
        logger.exception("Slack custom alert failed")
        return False


def notify_disk_warning(pct: float, used_mb: float, total_mb: float, threshold: int) -> bool:
    """Posts one disk-space warning. Same never-raises contract as the HOT
    alert: this is a safety net, and a safety net that can crash the scheduler
    is worse than none.

    Exists because the disk filling up is not a gradual degradation -- SQLite
    can't open its write-ahead log, every DB-backed route 500s, and the service
    stops booting entirely. There is no partial failure to notice first, so the
    only useful warning is one that arrives while there's still room.
    """
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    critical = threshold >= 85
    free_mb = max(total_mb - used_mb, 0)
    try:
        import httpx

        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": ("Disk critical" if critical else "Disk filling up"),
                        "emoji": False,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*cortex-api* disk is at *{pct:.1f}%* "
                            f"({used_mb / 1024:.2f} GB of {total_mb / 1024:.2f} GB, "
                            f"{free_mb / 1024:.2f} GB free).\n"
                            + (
                                "At 100% SQLite stops working and the API goes down completely. "
                                "Free space or resize the disk in Render now."
                                if critical
                                else "Not urgent yet, but worth clearing space or planning a resize."
                            )
                        ),
                    },
                },
            ]
        }
        response = httpx.post(webhook, json=payload, timeout=15.0)
        if response.status_code >= 300:
            logger.error("Slack disk alert rejected (%s): %s", response.status_code, response.text[:200])
            return False
        return True
    except Exception:
        logger.exception("Slack disk alert failed")
        return False


def notify_snapshot_failure(ok_count: int, failed: dict[str, str]) -> bool:
    """Posts one alert when the daily Tracker snapshot job couldn't record
    some (or any) accounts. Same never-raises contract as the alerts above.

    Exists because this job is the Tracker's only data source and it fires
    once a day, so a failure is invisible until someone notices the chart has
    stopped moving. In Aug 2026 an Apify usage limit was hit, every run was
    aborted with a 403, and two days of follower history were lost before
    anyone looked -- and follower counts can't be backfilled after the fact.

    The first failure reason is included verbatim: the useful signal is
    usually the upstream status (403 = usage limit/permissions, 401 = bad
    token), which tells you where to look without opening the logs.
    """
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        return False

    total = ok_count + len(failed)
    total_down = ok_count == 0
    sample = next(iter(failed.values()), "")
    # Reasons embed the Apify request URL, which carries the API token as a
    # query param -- never put that in a Slack message.
    sample = sample.split("for url", 1)[0].strip() or "see logs"

    # Name the accounts that actually failed. The first version of this alert
    # only gave counts, which made a 1-of-22 timeout read like a total outage.
    handles = ", ".join(f"`{h}`" for h in list(failed)[:8])
    if len(failed) > 8:
        handles += f" +{len(failed) - 8} more"

    # Route the advice off the actual error. Telling someone to check their
    # usage limit when the real problem was a one-off socket timeout sends
    # them to the wrong place entirely.
    low = sample.lower()
    if "403" in low or "usage" in low or "limit" in low:
        advice = (
            "Looks like an Apify usage limit -- runs get aborted platform-wide when it's "
            "hit. Check Billing > Limits, then re-run *Snapshot now*."
        )
    elif "401" in low or "token" in low or "forbidden" in low:
        advice = "Looks like an Apify auth problem -- check the API token, then re-run *Snapshot now*."
    elif "timed out" in low or "timeout" in low or "connection" in low:
        advice = (
            "Transient network error -- this is retried automatically, so if you're seeing "
            "this the retries were also exhausted. Re-run *Snapshot now* to capture the "
            "missing accounts before the day rolls over."
        )
    else:
        advice = "Re-run *Snapshot now* in the admin panel to capture the missing accounts before the day rolls over."

    try:
        import httpx

        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": ("Tracker snapshot failed" if total_down else "Tracker snapshot incomplete"),
                        "emoji": False,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            (
                                f"*No accounts were recorded* (0 of {total}). "
                                "Today's follower history is missing for every account "
                                "and cannot be backfilled later.\n"
                                if total_down
                                else f"Recorded *{ok_count} of {total}* accounts. "
                                f"Missing today: {handles}\n"
                            )
                            + f"Error: `{sample}`\n"
                            + advice
                        ),
                    },
                },
            ]
        }
        response = httpx.post(webhook, json=payload, timeout=15.0)
        if response.status_code >= 300:
            logger.error("Slack snapshot alert rejected (%s): %s", response.status_code, response.text[:200])
            return False
        return True
    except Exception:
        logger.exception("Slack snapshot alert failed")
        return False
