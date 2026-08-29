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
import threading
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

logger = logging.getLogger("uvicorn.error")

# Public base used to build links back into the dashboard/cover images.
_PUBLIC_API = os.getenv("PUBLIC_API_BASE", "https://cortex-api-db2e.onrender.com").rstrip("/")
# Where the alert should take you. Overridable so a staging deploy doesn't
# send everyone to production.
_DASHBOARD = os.getenv("DASHBOARD_BASE", "https://sentientdash.app").rstrip("/")

# Queue change logs intentionally go to a fixed, shared channel so VCs have a
# durable audit trail independent of the assignment DMs. Keep the channel ID
# here (rather than trusting a request parameter) so a Queue action can never
# be redirected to an arbitrary Slack destination.
SPOC_DASHBOARD_CHANNEL_ID = "C0BTMHMCYUS"

# Queue users are authenticated by their dashboard email. Slack's Incoming
# Webhook cannot deliver a message to an arbitrary person's DM, so assignment
# alerts deliberately use a bot token and this explicit, reviewed mapping.
# Keeping it local also avoids a users.lookupByEmail dependency and its extra
# Slack OAuth scope every time someone assigns a post.
_SLACK_USERS_BY_EMAIL = {
    "esteban@sentientagency.io": "U08UYJMPJ76",
    "louis@sentientagency.io": "U06DZPVNTBR",
    "ivan@sentientagency.io": "U0516SU09J9",
    "sergio@sentientagency.io": "U087U6470M6",
    "victor@sentientagency.io": "U0BAJA1AC6P",
    "egor@sentientagency.io": "U081LU7PVK3",
    "santiagoflhi@gmail.com": "U0AGH0MJ3EH",
    "dsflorezl@gmail.com": "U0BH9R6EE4Q",
    "sara1107giraldo@gmail.com": "U0BGHD1HD0R",
    "sebastianruizurquijo@gmail.com": "U0BG04Q4Z8F",
    "tevi@sentientagency.io": "U05QU9WCR1N",
    "gabo@sentientagency.io": "U0BLJHSUNJG",
}


def slack_user_id_for_email(email: str | None) -> str:
    """Return the reviewed Slack ID for a dashboard user.

    Dashboard rows may predate the Slack-ID field (or may have an empty value
    after a roster migration). Keep the explicit mapping as the source of
    truth for Queue delivery and profile lookups in those cases.
    """
    clean = str(email or "").strip().lower()
    return _SLACK_USERS_BY_EMAIL.get(clean, "")

_SLACK_PROFILE_CACHE: tuple[float, dict[str, str]] | None = None
_SLACK_PROFILE_LOCK = threading.Lock()
_SLACK_PROFILE_TTL = 6 * 60 * 60


def slack_user_profile_images(user_ids: list[str] | tuple[str, ...] | set[str]) -> dict[str, str]:
    """Return Slack CDN avatar URLs for the requested user IDs.

    The roster is tiny but this endpoint is loaded by every Queue viewer, so
    keep a process-local six-hour cache and return stale data during a Slack
    outage. Empty results are safe: the Queue client falls back to initials.
    """
    wanted = {str(value or '').strip().upper() for value in user_ids if str(value or '').strip()}
    if not wanted:
        return {}
    global _SLACK_PROFILE_CACHE
    now = time.monotonic()
    with _SLACK_PROFILE_LOCK:
        cached = _SLACK_PROFILE_CACHE
        if cached and now - cached[0] < _SLACK_PROFILE_TTL:
            return {user_id: cached[1][user_id] for user_id in wanted if user_id in cached[1]}
        token = os.getenv("SLACK_BOT_TOKEN", "").strip()
        if not token:
            return {}
        try:
            import httpx

            profiles: dict[str, str] = {}
            cursor = ""
            while True:
                params = {"limit": 200}
                if cursor:
                    params["cursor"] = cursor
                response = httpx.get(
                    "https://slack.com/api/users.list",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                    timeout=10.0,
                )
                response.raise_for_status()
                payload = response.json()
                if not payload.get("ok"):
                    logger.warning("Slack profile lookup rejected: %s", payload.get("error", "unknown error"))
                    return {user_id: cached[1][user_id] for user_id in wanted if cached and user_id in cached[1]}
                for member in payload.get("members") or []:
                    user_id = str(member.get("id") or "").strip().upper()
                    profile = member.get("profile") or {}
                    avatar = (
                        profile.get("image_192") or profile.get("image_72") or profile.get("image_48")
                        or profile.get("image_32") or profile.get("image_24") or profile.get("image_original") or ""
                    )
                    if user_id and avatar:
                        profiles[user_id] = str(avatar)
                cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "").strip()
                if not cursor or wanted.issubset(profiles.keys()):
                    break
            _SLACK_PROFILE_CACHE = (now, profiles)
            return {user_id: profiles[user_id] for user_id in wanted if user_id in profiles}
        except Exception:
            logger.exception("Slack profile lookup failed")
            return {user_id: cached[1][user_id] for user_id in wanted if cached and user_id in cached[1]}


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


def queue_url_for(task_id: int) -> str:
    """Deep link which opens exactly one Queue task's side panel."""
    return f"{_DASHBOARD}/queue.html?task={quote(str(task_id), safe='')}"


def slack_configured() -> bool:
    return bool(os.getenv("SLACK_WEBHOOK_URL", "").strip())


def slack_assignment_dm_configured() -> bool:
    """Queue DMs need a bot token; webhooks can only address their channel."""
    return bool(os.getenv("SLACK_BOT_TOKEN", "").strip())


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


def build_queue_assignment_message(
    *,
    task_id: int,
    assignee_email: str,
    assigned_by_email: str,
    account: str,
    post_id: int | None,
    note: str | None = None,
    notes: str | None = None,
    references: list[str] | None = None,
    due_date: str | None = None,
    priority: str | None = None,
    tags: list[str] | None = None,
    recommended_accounts: list[str] | None = None,
    recommended_account: str | None = None,
    production_points: int | None = None,
    scheduled_date: str | None = None,
    scheduled_start_minutes: int | None = None,
    update: bool = False,
    assigned_by_slack_id: str | None = None,
) -> dict[str, Any]:
    """A concise private assignment message with only actionable metadata."""
    assigner_id = (assigned_by_slack_id or "").strip() or _SLACK_USERS_BY_EMAIL.get(assigned_by_email.strip().lower())
    assigner = f"<@{assigner_id}>" if assigner_id else assigned_by_email.split("@", 1)[0]
    account_values = recommended_accounts if recommended_accounts is not None else ([recommended_account] if recommended_account else [])
    destinations = [str(account).lstrip("@") for account in account_values if str(account).strip()]
    destination = ", ".join(f"@{account}" for account in destinations)
    metadata: list[str] = []
    if due_date:
        try:
            due = datetime.fromisoformat(due_date.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Costa_Rica")).strftime("%b %-d, %Y · %-I:%M %p")
        except ValueError:
            due = due_date
        metadata.append(f"*Deadline*\n{due}")
    if priority:
        metadata.append(f"*Priority*\n{priority.replace('_', ' ').title()}")
    if tags:
        metadata.append(f"*Tags*\n{', '.join(f'`{tag}`' for tag in tags)}")
    if production_points:
        metadata.append(f"*Production*\n{production_points} PP · {production_points * 10} min")
    if scheduled_date is not None and scheduled_start_minutes is not None:
        hours, minutes = divmod(int(scheduled_start_minutes), 60)
        scheduled = f"{scheduled_date} · {hours % 12 or 12}:{minutes:02d} {'AM' if hours < 12 else 'PM'}"
        metadata.append(f"*Scheduled*\n{scheduled}")

    # Follow-up notifications are intentionally compact. The first DM is the
    # canonical card (with media and all context); subsequent schedule changes
    # should read as a lightweight audit entry and, when possible, live in that
    # original message's thread.
    if update:
        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": "Queue update", "emoji": True}},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{assigner} updated this post{' for *' + destination + '*' if destination else ''}.",
                },
            },
        ]
        if metadata:
            blocks.append({"type": "section", "fields": [{"type": "mrkdwn", "text": value} for value in metadata]})
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Open in Queue", "emoji": True},
                        "url": queue_url_for(task_id),
                    }
                ],
            }
        )
        return {"text": f"{assigner} updated your Queue assignment{f' for {destination}' if destination else ''}.", "blocks": blocks}

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "New Queue assignment", "emoji": True}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{assigner} assigned this post{' for *' + destination + '*' if destination else ''}.",
            },
        },
    ]
    if note:
        brief = note.strip()[:1800].replace("\n", "\n> ")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Brief*\n> {brief}"}})
    if notes:
        extra = notes.strip()[:1600].replace("\n", "\n> ")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Notes*\n> {extra}"}})
    if references:
        clean_references = [str(value).strip() for value in references if str(value).strip()][:8]
        if clean_references:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*References*\n" + "\n".join(f"• <{value}|Open reference>" for value in clean_references)}})
    if metadata:
        blocks.append({"type": "section", "fields": [{"type": "mrkdwn", "text": value} for value in metadata]})
    if post_id is not None:
        blocks.append({"type": "image", "image_url": cover_url_for(account, post_id), "alt_text": f"Post from @{account}"})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Open in Queue", "emoji": True},
                    "url": queue_url_for(task_id),
                }
            ],
        }
    )
    return {"text": f"{assigner} assigned you a post{f' for {destination}' if destination else ''}.", "blocks": blocks}


def notify_queue_assignment_result(**assignment: Any) -> dict[str, Any]:
    """Send a private Queue DM and return delivery metadata.

    The returned Slack channel/message timestamp is persisted for Queue V2 so
    later updates can reply in the original one-to-one DM thread. Delivery is
    still best-effort and never raises into the assignment transaction.
    """
    token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    assignee_email = str(assignment.get("assignee_email") or "").strip().lower()
    recipient = str(assignment.pop("assignee_slack_id", "") or "").strip() or slack_user_id_for_email(assignee_email)
    update = bool(assignment.get("update"))
    stored_channel = str(assignment.pop("slack_channel_id", "") or "").strip()
    stored_thread = str(assignment.pop("slack_message_ts", "") or "").strip()
    if not token:
        logger.warning("Queue assignment DM skipped: SLACK_BOT_TOKEN is not configured")
        return {"sent": False}
    if not recipient:
        logger.warning("Queue assignment DM skipped: no Slack user mapping for %s", assignee_email)
        return {"sent": False}
    try:
        import httpx

        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(timeout=15.0) as client:
            channel_id = stored_channel
            # Reuse the existing DM for updates when it belongs to this
            # recipient. Otherwise open a fresh one-to-one conversation.
            if not channel_id:
                opened = client.post("https://slack.com/api/conversations.open", headers=headers, json={"users": recipient})
                opened.raise_for_status()
                opened_data = opened.json()
                opened_channel = opened_data.get("channel") or {}
                channel_id = opened_channel.get("id")
                if not opened_data.get("ok") or not channel_id:
                    logger.error("Queue assignment DM open rejected: %s", opened_data.get("error", "unknown error"))
                    return {"sent": False}
            # Verify the conversation before posting so an assignment can
            # never be delivered to a group or channel by mistake.
            inspected = client.get(
                "https://slack.com/api/conversations.info",
                headers=headers,
                params={"channel": channel_id},
            )
            inspected.raise_for_status()
            inspected_data = inspected.json()
            channel = inspected_data.get("channel") or {}
            if (
                not inspected_data.get("ok")
                or not channel.get("is_im")
                or channel.get("user") != recipient
            ):
                logger.error("Queue assignment DM verification rejected: %s", inspected_data.get("error", "not a one-to-one IM"))
                return {"sent": False}
            payload = build_queue_assignment_message(**assignment)
            payload["channel"] = channel_id
            if update and stored_thread:
                payload["thread_ts"] = stored_thread
                payload["reply_broadcast"] = False
            sent = client.post("https://slack.com/api/chat.postMessage", headers=headers, json=payload)
            sent.raise_for_status()
            sent_data = sent.json()
            if not sent_data.get("ok"):
                logger.error("Queue assignment DM rejected: %s", sent_data.get("error", "unknown error"))
                return {"sent": False}
            message_ts = str(sent_data.get("ts") or "").strip()
        return {"sent": True, "channelId": channel_id, "messageTs": stored_thread or message_ts}
    except Exception:
        logger.exception("Queue assignment DM failed for %s", assignee_email)
        return {"sent": False}


def notify_queue_assignment(**assignment: Any) -> bool:
    """Backward-compatible bool wrapper used by legacy Queue endpoints."""
    return bool(notify_queue_assignment_result(**assignment).get("sent"))


_QUEUE_CHANGE_LABELS = {
    "pp_revision_requested": "PP revision requested",
    "pp_revision_approved": "PP revision approved",
    "pp_revision_rejected": "PP revision rejected",
    "cancellation_requested": "Cancellation requested",
    "cancellation_approved": "Cancellation approved",
    "cancellation_rejected": "Cancellation rejected",
    "returned_to_pool": "Returned to pool",
    "cancelled": "Queue post cancelled",
    "deleted": "Queue post deleted",
}


def _slack_actor(email: str | None) -> str:
    clean = str(email or "").strip().lower()
    if not clean:
        return "Unknown user"
    user_id = slack_user_id_for_email(clean)
    return f"<@{user_id}>" if user_id else clean.split("@", 1)[0]


def _queue_change_schedule(date: str | None, start_minutes: int | None) -> str | None:
    if not date or start_minutes is None:
        return None
    try:
        hours, minutes = divmod(int(start_minutes), 60)
        return f"{date} · {hours % 12 or 12}:{minutes:02d} {'AM' if hours < 12 else 'PM'}"
    except (TypeError, ValueError):
        return str(date)


def build_queue_change_message(
    *,
    event_type: str,
    task_id: int | None,
    actor_email: str | None,
    account: str | None,
    shortcode: str | None,
    designer_email: str | None = None,
    status: str | None = None,
    production_points: int | None = None,
    requested_production_points: int | None = None,
    priority: str | None = None,
    tags: list[str] | None = None,
    reason: str | None = None,
    brief: str | None = None,
    scheduled_date: str | None = None,
    scheduled_start_minutes: int | None = None,
    final_permalink: str | None = None,
) -> dict[str, Any]:
    """Build a compact Block Kit audit entry for #spoc-dashboard.

    These messages are deliberately channel-safe: no private brief or
    attachment is required to understand the event, while the Queue button
    lands on the request's side view for the people who need to act on it.
    """
    label = _QUEUE_CHANGE_LABELS.get(event_type, event_type.replace("_", " ").title())
    clean_account = str(account or "").strip().lstrip("@") or "unknown account"
    clean_shortcode = str(shortcode or "").strip()
    post_label = f"@{clean_account}" + (f" · `{clean_shortcode}`" if clean_shortcode else "")
    actor = _slack_actor(actor_email)
    designer = _slack_actor(designer_email) if designer_email else "—"
    title = f"Queue · {label}"

    summary = f"*{post_label}*\n{actor}"
    if event_type.endswith("_requested"):
        summary += " requested a Queue change for this post."
    elif event_type.endswith("_approved"):
        summary += " approved this Queue request."
    elif event_type.endswith("_rejected"):
        summary += " rejected this Queue request."
    elif event_type == "returned_to_pool":
        summary += " returned this firm assignment to the pool."
    elif event_type == "cancelled":
        summary += " cancelled this Queue post."
    elif event_type == "deleted":
        summary += " deleted this Queue post."
    else:
        summary += "."

    fields: list[dict[str, str]] = [
        {"type": "mrkdwn", "text": f"*Designer*\n{designer}"},
    ]
    if status:
        fields.append({"type": "mrkdwn", "text": f"*Status*\n`{status.replace('_', ' ')}`"})
    if production_points is not None:
        pp_text = f"{production_points} PP · {int(production_points) * 10} min"
        if requested_production_points is not None and requested_production_points != production_points:
            pp_text += f" → {requested_production_points} PP"
        fields.append({"type": "mrkdwn", "text": f"*Production*\n{pp_text}"})
    if priority:
        fields.append({"type": "mrkdwn", "text": f"*Priority*\n{str(priority).replace('_', ' ').title()}"})
    schedule = _queue_change_schedule(scheduled_date, scheduled_start_minutes)
    if schedule:
        fields.append({"type": "mrkdwn", "text": f"*Scheduled*\n{schedule}"})
    if tags:
        clean_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
        if clean_tags:
            fields.append({"type": "mrkdwn", "text": f"*Tags*\n{', '.join(f'`{tag}`' for tag in clean_tags)}"})

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150], "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary[:2900]}},
    ]
    if fields:
        blocks.append({"type": "section", "fields": fields[:10]})
    if reason:
        clean_reason = str(reason).strip()[:900]
        if clean_reason:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Reason*\n> {clean_reason.replace(chr(10), chr(10) + '> ')}"}})
    if brief and event_type.endswith("_requested"):
        clean_brief = str(brief).strip()[:700]
        if clean_brief:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Brief*\n> {clean_brief.replace(chr(10), chr(10) + '> ')}"}})
    if final_permalink:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Published post*\n<{final_permalink}|Open Instagram post>"}})
    if task_id is not None:
        blocks.append({
            "type": "actions",
            "elements": [{
                "type": "button",
                "style": "primary" if event_type.endswith("_requested") else None,
                "text": {"type": "plain_text", "text": "Review in Queue" if event_type.endswith("_requested") else "Open in Queue", "emoji": True},
                "url": queue_url_for(task_id),
            }],
        })
        # Slack rejects a null style field even though it is optional. Keep
        # the payload minimal for non-request audit entries.
        if blocks[-1]["elements"][0]["style"] is None:
            blocks[-1]["elements"][0].pop("style", None)
    return {"text": f"{title}: {post_label}", "blocks": blocks}


def notify_queue_change(**change: Any) -> bool:
    """Post one Queue audit event to #spoc-dashboard using the bot token.

    Slack delivery is best-effort and intentionally outside the Queue DB
    transaction. A missing token, missing channel membership, or transient
    Slack failure must never turn a successful Queue action into a 500.
    """
    token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    if not token:
        logger.warning("Queue change log skipped: SLACK_BOT_TOKEN is not configured")
        return False
    try:
        import httpx

        payload = build_queue_change_message(**change)
        payload["channel"] = SPOC_DASHBOARD_CHANNEL_ID
        response = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=15.0,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            logger.error("Queue change log rejected: %s", data.get("error", "unknown error"))
            return False
        return True
    except Exception:
        logger.exception("Queue change log failed for task %s", change.get("task_id"))
        return False


def alert_image_url_for(filename: str) -> str:
    """Public URL for an image attached to a custom alert (see
    /api/admin/alert-image/{filename} in main.py) -- Slack fetches this
    itself to render the image inline, so it has to be a URL its servers
    can reach, not a data: URI or anything auth-gated."""
    return f"{_PUBLIC_API}/api/admin/alert-image/{filename}"


def notify_custom(message: str, title: str | None = None, image_url: str | None = None) -> bool:
    """Posts a free-form alert typed in by hand from the admin panel's
    System tab -- for anything worth pinging Slack about that doesn't fit
    one of the purpose-built alerts above (a heads-up, a reminder, a note
    to the team). Same never-raises contract: a bad webhook here shouldn't
    surface as a 500 for what's already a manual, low-stakes action.

    image_url is optional -- a screenshot pasted or uploaded alongside the
    message, already saved server-side and passed in as a public URL.
    """
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    clean = (message or "").strip()
    if not clean:
        return False
    heading = (title or "").strip() or "📣 Custom alert"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": heading, "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": clean[:2900]}},
    ]
    if image_url:
        blocks.append({"type": "image", "image_url": image_url, "alt_text": "Attached image"})
    payload = {
        "text": f"{heading}: {clean[:150]}",
        "blocks": blocks,
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
