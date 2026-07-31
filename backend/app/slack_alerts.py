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

logger = logging.getLogger("uvicorn.error")

# Public base used to build links back into the dashboard/cover images.
_PUBLIC_API = os.getenv("PUBLIC_API_BASE", "https://cortex-api-db2e.onrender.com").rstrip("/")


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
    permalink = post.get("permalink") or ""
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
    if permalink:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open on Instagram", "emoji": True},
                        "url": permalink,
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
