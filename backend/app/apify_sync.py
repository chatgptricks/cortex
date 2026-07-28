from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

APIFY_ACTOR_ID = "apify~instagram-scraper"

VALID_GROUPS = ("sentient", "competitors")

# The only canonical account is chatgptricks -- it lives in `posts`, the
# table shared with Predict's prediction model. Every other account (self-
# serve or seeded) lives in the generic `dashboard_posts` table, keyed by
# `account`, and must never be merged into `posts` (explicit prior product
# decision -- see README).
_CANONICAL_HANDLE = "chatgptricks"


class ApifySyncError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Account registry (DB-driven, self-serve -- see /api/admin/accounts)
# ---------------------------------------------------------------------------


def get_account_config(handle: str) -> dict[str, Any]:
    from .db import connect

    with connect() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE handle = ?", (handle,)).fetchone()
    if row is None:
        raise ApifySyncError(f"Unknown account '{handle}'.")
    is_canonical = bool(row["is_canonical"])
    return {
        "handle": row["handle"],
        "label": row["label"],
        "group": row["group_name"],
        "hot_threshold": row["hot_threshold"],
        "is_canonical": is_canonical,
        "is_active": bool(row["is_active"]),
        "table": "posts" if is_canonical else "dashboard_posts",
    }


def list_accounts(active_only: bool = False) -> list[dict[str, Any]]:
    from .db import connect

    query = "SELECT * FROM accounts"
    if active_only:
        query += " WHERE is_active = 1"
    query += " ORDER BY group_name, handle"
    with connect() as conn:
        rows = conn.execute(query).fetchall()
    return [
        {
            "handle": row["handle"],
            "label": row["label"],
            "group": row["group_name"],
            "hot_threshold": row["hot_threshold"],
            "is_canonical": bool(row["is_canonical"]),
            "is_active": bool(row["is_active"]),
            "has_avatar": bool(row["avatar_path"]),
        }
        for row in rows
    ]


def create_account(handle: str, label: str, group: str, hot_threshold: int) -> dict[str, Any]:
    """Self-serve account creation. Always non-canonical -- new accounts
    always write into the generic dashboard_posts table, never `posts`.
    """
    from .db import connect, utc_now

    handle = handle.strip().lstrip("@").lower()
    if not handle:
        raise ApifySyncError("Account handle is required.")
    if group not in VALID_GROUPS:
        raise ApifySyncError(f"Group must be one of {VALID_GROUPS}.")
    label = (label or handle).strip()
    now_iso = utc_now()

    with connect() as conn:
        existing = conn.execute("SELECT handle FROM accounts WHERE handle = ?", (handle,)).fetchone()
        if existing:
            raise ApifySyncError(f"Account '{handle}' already exists.")
        conn.execute(
            """
            INSERT INTO accounts (handle, label, group_name, hot_threshold, is_canonical, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, 1, ?, ?)
            """,
            (handle, label, group, hot_threshold, now_iso, now_iso),
        )

    return get_account_config(handle)


def _account_scope(table: str, account: str) -> tuple[str, tuple]:
    """Extra WHERE-clause SQL + params to scope a query to one account when
    reading/writing the shared dashboard_posts table. The canonical `posts`
    table has no account column (chatgptricks only), so no scoping needed.
    """
    if table == "dashboard_posts":
        return " AND account = ?", (account,)
    return "", ()


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _title_from_caption(caption: str) -> str:
    for line in caption.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:120]
    return ""


def _post_type_label(item: dict[str, Any]) -> tuple[str, bool]:
    kind = item.get("type", "")
    if kind == "Sidecar":
        return "Carousel", False
    if kind == "Video":
        return "Video", True
    return "Image", False


def _published_at(item: dict[str, Any]) -> str | None:
    ts = item.get("timestamp")
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(str(ts).replace("Z", ""), fmt)
            return dt.replace(tzinfo=UTC).isoformat(timespec="seconds")
        except ValueError:
            continue
    return str(ts)


def _apply_likes_floor(raw: Any) -> int:
    """Instagram/Apify sometimes reports a very low or sentinel (-1, hidden)
    like count for a post. Per product decision, any value of 3 or below
    (including missing/negative/non-numeric) is treated as "unknown" and
    reported as a baseline of 500 instead of the tiny/inaccurate number.
    """
    if isinstance(raw, bool):
        return 500
    if isinstance(raw, int) and raw > 3:
        return raw
    return 500


def _post_age_hours(published_at: str | None, now: datetime) -> float | None:
    if not published_at:
        return None
    text = str(published_at).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (now - dt).total_seconds() / 3600.0


def _fetch_apify_items(payload: dict[str, Any], timeout: float = 180.0) -> list[dict[str, Any]]:
    token = os.getenv("APIFY_TOKEN", "").strip()
    if not token:
        raise ApifySyncError("APIFY_TOKEN is not configured on the server.")
    try:
        import httpx
    except ImportError as exc:
        raise ApifySyncError("httpx is not installed in the backend environment.") from exc

    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, params={"token": token}, json=payload)
            response.raise_for_status()
            items = response.json()
    except httpx.HTTPError as exc:
        raise ApifySyncError(f"Apify request failed: {exc}") from exc
    if not isinstance(items, list):
        raise ApifySyncError("Apify returned an unexpected response shape.")
    return items


# ---------------------------------------------------------------------------
# New-post insertion
# ---------------------------------------------------------------------------


def _insert_new_chatgptricks_posts(new_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Mirrors the insert shape sync_instagram_profile_posts() uses for
    IG-sourced posts (section="single", source_ref="instagram:<shortcode>",
    shortcode set), so these stay properly deduplicated against any future
    sync, unlike rows created through the plain POST /api/posts endpoint.
    """
    import httpx

    from .config import UPLOAD_DIR
    from .db import connect, utc_now

    summary: dict[str, Any] = {"added": 0, "failed": 0, "items": []}
    if not new_items:
        return summary

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=60.0, headers={"User-Agent": "Mozilla/5.0"}) as image_client:
        for item in new_items:
            shortcode = str(item["shortCode"]).strip()
            source_ref = f"instagram:{shortcode}"
            image_url = item.get("displayUrl") or next(iter(item.get("images") or []), None)
            if not image_url:
                summary["failed"] += 1
                summary["items"].append({"shortcode": shortcode, "status": "failed", "error": "no cover image"})
                continue

            try:
                image_response = image_client.get(image_url)
                image_response.raise_for_status()
                image_bytes = image_response.content
            except httpx.HTTPError as exc:
                summary["failed"] += 1
                summary["items"].append({"shortcode": shortcode, "status": "failed", "error": str(exc)})
                continue

            suffix = ".jpg"
            content_type = image_response.headers.get("content-type", "")
            if "png" in content_type:
                suffix = ".png"
            elif "webp" in content_type:
                suffix = ".webp"

            image_path = UPLOAD_DIR / f"{os.urandom(16).hex()}{suffix}"
            image_path.write_bytes(image_bytes)

            caption = _clean_text(item.get("caption"))
            title = _title_from_caption(caption) or f"Instagram post {shortcode}"
            post_type_label, is_video = _post_type_label(item)
            likes = _apply_likes_floor(item.get("likesCount"))
            comments = item.get("commentsCount") or 0
            now_iso = utc_now()

            with connect() as conn:
                conn.execute(
                    """
                    INSERT INTO posts (
                        section, title, caption, published_at, likes, comments,
                        post_type_label, source_ref, shortcode, image_path,
                        original_filename, status, progress_percent,
                        progress_message, is_animated, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "single",
                        title,
                        caption,
                        _published_at(item),
                        likes,
                        comments,
                        post_type_label,
                        source_ref,
                        shortcode,
                        str(image_path),
                        f"instagram-{shortcode}{suffix}",
                        "queued",
                        5,
                        "Queued",
                        int(is_video),
                        now_iso,
                        now_iso,
                    ),
                )

            summary["added"] += 1
            summary["items"].append({"shortcode": shortcode, "status": "added", "published_at": _published_at(item)})

    return summary


def _insert_new_dashboard_posts(account: str, new_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Generic insert used by every non-canonical account (Sentient or
    Competitors) into the shared dashboard_posts table, scoped by `account`.
    """
    import httpx

    from .config import UPLOAD_DIR
    from .db import connect, utc_now

    summary: dict[str, Any] = {"added": 0, "failed": 0, "items": []}
    if not new_items:
        return summary

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=60.0, headers={"User-Agent": "Mozilla/5.0"}) as image_client:
        for item in new_items:
            shortcode = str(item["shortCode"]).strip()
            image_url = item.get("displayUrl") or next(iter(item.get("images") or []), None)
            cover_path: str | None = None
            if image_url:
                try:
                    image_response = image_client.get(image_url)
                    image_response.raise_for_status()
                    suffix = ".jpg"
                    content_type = image_response.headers.get("content-type", "")
                    if "png" in content_type:
                        suffix = ".png"
                    elif "webp" in content_type:
                        suffix = ".webp"
                    image_path = UPLOAD_DIR / f"dash-{account}-{os.urandom(16).hex()}{suffix}"
                    image_path.write_bytes(image_response.content)
                    cover_path = str(image_path)
                except httpx.HTTPError:
                    cover_path = None

            caption = _clean_text(item.get("caption"))
            post_type_label, is_video = _post_type_label(item)
            likes = _apply_likes_floor(item.get("likesCount"))
            comments = item.get("commentsCount") or 0
            permalink = item.get("url") or f"https://www.instagram.com/p/{shortcode}/"
            now_iso = utc_now()

            try:
                with connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO dashboard_posts (
                            account, shortcode, published_at, likes, comments, caption,
                            post_type_label, is_animated, permalink,
                            cover_source_url, cover_image_path, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            account,
                            shortcode,
                            _published_at(item),
                            likes,
                            comments,
                            caption,
                            post_type_label,
                            int(is_video),
                            permalink,
                            image_url,
                            cover_path,
                            now_iso,
                            now_iso,
                        ),
                    )
            except Exception as exc:  # e.g. UNIQUE constraint on a race re-add
                summary["failed"] += 1
                summary["items"].append({"shortcode": shortcode, "status": "failed", "error": str(exc)})
                continue

            summary["added"] += 1
            summary["items"].append({"shortcode": shortcode, "status": "added", "published_at": _published_at(item)})

    return summary


def _insert_new_posts(account: str, cfg: dict[str, Any], new_items: list[dict[str, Any]]) -> dict[str, Any]:
    if cfg["is_canonical"]:
        return _insert_new_chatgptricks_posts(new_items)
    return _insert_new_dashboard_posts(account, new_items)


# ---------------------------------------------------------------------------
# Short-term cycle: every 30 min (7:30am-11:30pm fixed CST), posts <=24h old
# ---------------------------------------------------------------------------


def run_short_term_cycle(account: str, results_limit: int = 80) -> dict[str, Any]:
    """(1) Pulls the last ~30h of posts and inserts any brand-new ones, and
    (2) refreshes likes/comments on all existing posts <=24h old, doing a
    one-time "first hour" HOT check the first time each post is observed at
    >=1h old: is_hot=1 if likes >= the account's per-hour threshold at that
    point. Both pieces reuse a single Apify fetch to minimize API calls.
    """
    cfg = get_account_config(account)
    table = cfg["table"]
    scope_sql, scope_params = _account_scope(table, account)
    now = datetime.now(UTC)

    payload: dict[str, Any] = {
        "directUrls": [f"https://www.instagram.com/{cfg['handle']}/"],
        "resultsType": "posts",
        "resultsLimit": results_limit,
        "skipPinnedPosts": True,
        "onlyPostsNewerThan": (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    items = _fetch_apify_items(payload)

    from .db import connect, utc_now

    with connect() as conn:
        existing_rows = conn.execute(
            f"SELECT shortcode FROM {table} WHERE 1=1{scope_sql}", scope_params
        ).fetchall()
    existing_shortcodes = {row["shortcode"] for row in existing_rows if row["shortcode"]}

    new_items = [it for it in items if it.get("shortCode") and it["shortCode"] not in existing_shortcodes]
    new_items.sort(key=lambda it: it.get("timestamp") or "")
    insert_summary = _insert_new_posts(account, cfg, new_items)

    # Re-read so freshly-inserted posts are also eligible for the engagement
    # pass below (a post that's brand new is, by definition, well within the
    # <=24h window).
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id, shortcode, published_at, hot_checked FROM {table} WHERE 1=1{scope_sql}", scope_params
        ).fetchall()

    eligible: dict[str, dict[str, Any]] = {}
    for row in rows:
        shortcode = row["shortcode"]
        if not shortcode or shortcode.startswith("post-"):
            continue
        age_hours = _post_age_hours(row["published_at"], now)
        if age_hours is None or age_hours > 24:
            continue
        eligible[shortcode] = {"id": row["id"], "hot_checked": bool(row["hot_checked"]), "age_hours": age_hours}

    items_by_shortcode = {it.get("shortCode"): it for it in items if it.get("shortCode")}
    now_iso = utc_now()
    engagement_summary: dict[str, Any] = {"checked": len(eligible), "updated": 0, "hot_marked": 0, "unmatched": 0}

    for shortcode, info in eligible.items():
        item = items_by_shortcode.get(shortcode)
        if not item:
            engagement_summary["unmatched"] += 1
            continue
        likes = _apply_likes_floor(item.get("likesCount"))
        comments = item.get("commentsCount")
        comments = comments if isinstance(comments, int) and comments >= 0 else None

        set_clauses = ["likes = ?", "updated_at = ?"]
        params: list[Any] = [likes, now_iso]
        if comments is not None:
            set_clauses.append("comments = ?")
            params.append(comments)
        if not info["hot_checked"] and info["age_hours"] >= 1.0:
            # Checks land on a fixed 30-min grid, so a post is rarely
            # observed at exactly 1.0h old (could be 1.0-1.5h, or much more
            # overnight). Rather than compare the raw like count, compute
            # the actual accumulation rate (likes / real elapsed hours) and
            # compare THAT against the account's per-hour threshold -- this
            # stays accurate regardless of exactly when the check happens to
            # land relative to the post's real publish time.
            rate_per_hour = likes / info["age_hours"]
            threshold = cfg["hot_threshold"]
            multiplier = round(rate_per_hour / threshold, 3) if threshold else 0.0
            is_hot = 1 if rate_per_hour >= threshold else 0
            set_clauses += ["is_hot = ?", "likes_at_1h = ?", "hot_checked = 1", "hot_rate_multiplier = ?"]
            params += [is_hot, likes, multiplier]
            if is_hot:
                set_clauses.append("hot_marked_at = ?")
                params.append(now_iso)
                engagement_summary["hot_marked"] += 1
        params.append(info["id"])
        with connect() as conn:
            conn.execute(f"UPDATE {table} SET {', '.join(set_clauses)} WHERE id = ?", params)
        engagement_summary["updated"] += 1

    return {"new_posts": insert_summary, "engagement": engagement_summary}


# ---------------------------------------------------------------------------
# Daily cycle: once/day, posts >24h-10 days, plus exactly 30d / 120d
# ---------------------------------------------------------------------------


def run_daily_cycle(account: str) -> dict[str, Any]:
    """Refreshes likes/comments on posts >24h and <=10 days old (every day),
    plus one-time checks at exactly 30 days and exactly 120 days old. Posts
    outside those windows (11-29 days, 31-119 days, 121+ days) are left
    untouched. <=24h posts are excluded here -- they're handled by
    run_short_term_cycle instead.

    Looks up the exact set of eligible post IDs in our own database first,
    then asks Apify to scrape *only those specific post URLs*
    (resultsType="details" against directUrls of individual posts). Cost
    scales with how many posts actually need a refresh today (typically a
    few dozen), not with a fixed lookback window re-walked daily.
    """
    cfg = get_account_config(account)
    table = cfg["table"]
    scope_sql, scope_params = _account_scope(table, account)
    now = datetime.now(UTC)
    has_permalink_column = table == "dashboard_posts"

    from .db import connect, utc_now

    select_cols = "id, shortcode, published_at, refreshed_30d, refreshed_120d"
    if has_permalink_column:
        select_cols += ", permalink"

    with connect() as conn:
        rows = conn.execute(
            f"SELECT {select_cols} FROM {table} "
            f"WHERE shortcode IS NOT NULL AND shortcode != ''{scope_sql}",
            scope_params,
        ).fetchall()

    eligible: dict[str, dict[str, Any]] = {}
    for row in rows:
        shortcode = row["shortcode"]
        if not shortcode or shortcode.startswith("post-"):
            continue
        age_hours = _post_age_hours(row["published_at"], now)
        if age_hours is None:
            continue
        age_days = int(age_hours // 24)
        in_daily_window = 24 < age_hours <= 240
        mark_30 = age_days == 30 and not row["refreshed_30d"]
        mark_120 = age_days == 120 and not row["refreshed_120d"]
        if not (in_daily_window or mark_30 or mark_120):
            continue
        permalink = (row["permalink"] if has_permalink_column else None) or f"https://www.instagram.com/p/{shortcode}/"
        eligible[shortcode] = {"id": row["id"], "mark_30": mark_30, "mark_120": mark_120, "permalink": permalink}

    summary: dict[str, Any] = {"checked": len(eligible), "updated": 0, "unmatched": 0}
    if not eligible:
        return summary

    # Batch the specific post URLs in chunks so one oversized day (unlikely
    # at current posting volume, but not impossible) doesn't blow past any
    # per-run item cap the actor enforces.
    all_urls = [info["permalink"] for info in eligible.values()]
    items: list[dict[str, Any]] = []
    for i in range(0, len(all_urls), 200):
        batch = all_urls[i : i + 200]
        payload: dict[str, Any] = {
            "directUrls": batch,
            "resultsType": "details",
        }
        items.extend(_fetch_apify_items(payload, timeout=300.0))

    items_by_shortcode = {it.get("shortCode"): it for it in items if it.get("shortCode")}
    now_iso = utc_now()

    for shortcode, info in eligible.items():
        item = items_by_shortcode.get(shortcode)
        if not item:
            summary["unmatched"] += 1
            continue
        likes = _apply_likes_floor(item.get("likesCount"))
        comments = item.get("commentsCount")
        comments = comments if isinstance(comments, int) and comments >= 0 else None

        set_clauses = ["likes = ?", "updated_at = ?"]
        params: list[Any] = [likes, now_iso]
        if comments is not None:
            set_clauses.append("comments = ?")
            params.append(comments)
        if info["mark_30"]:
            set_clauses.append("refreshed_30d = 1")
        if info["mark_120"]:
            set_clauses.append("refreshed_120d = 1")
        params.append(info["id"])
        with connect() as conn:
            conn.execute(f"UPDATE {table} SET {', '.join(set_clauses)} WHERE id = ?", params)
        summary["updated"] += 1

    return summary


def run_manual_refresh(account: str) -> dict[str, Any]:
    """Manual 'Refresh' button override: runs both cycles immediately."""
    short_term = run_short_term_cycle(account)
    daily = run_daily_cycle(account)
    return {"short_term": short_term, "daily": daily}


def run_backfill(
    account: str,
    results_limit: int = 5000,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """One-time initial history import for a freshly self-serve-added
    account: pulls up to `results_limit` recent posts and inserts any not
    already known. Meant to be triggered once right after an account is
    created via /api/admin/accounts, so it has a real post history before
    the normal scheduler starts incrementally refreshing it.

    date_from/date_to (YYYY-MM-DD) let the wizard offer "all posts" (both
    omitted) vs a specific date range. Apify's actor only supports a lower
    bound natively (onlyPostsNewerThan); the upper bound, when given, is
    applied as a post-fetch filter here since the actor has no "older than"
    input.
    """
    cfg = get_account_config(account)
    table = cfg["table"]
    scope_sql, scope_params = _account_scope(table, account)

    payload: dict[str, Any] = {
        "directUrls": [f"https://www.instagram.com/{cfg['handle']}/"],
        "resultsType": "posts",
        "resultsLimit": results_limit,
        # Unlike the scheduler's incremental refresh, this is a one-time
        # historical import -- we want manually pinned posts included too
        # (they still count toward the account's real post history), so
        # don't skip them here.
    }
    if date_from:
        payload["onlyPostsNewerThan"] = date_from
    # Large/full-history pulls (hundreds to low thousands of posts) can
    # legitimately take well past 5 minutes to scrape. The old 300s timeout
    # here meant our own HTTP client gave up on Apify's response before big
    # accounts finished -- the actor run kept going (and billing) on Apify's
    # side regardless, but we never received or stored its results because
    # we'd already disconnected. Give it real headroom instead.
    items = _fetch_apify_items(payload, timeout=1800.0)

    if date_to:
        try:
            upper_bound = datetime.fromisoformat(date_to).replace(tzinfo=UTC)
        except ValueError:
            upper_bound = None
        if upper_bound is not None:
            def _within_upper_bound(it: dict[str, Any]) -> bool:
                ts = it.get("timestamp")
                if not ts:
                    return True
                try:
                    posted_at = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                except ValueError:
                    return True
                return posted_at <= upper_bound + timedelta(days=1)

            items = [it for it in items if _within_upper_bound(it)]

    from .db import connect

    with connect() as conn:
        existing_rows = conn.execute(
            f"SELECT shortcode FROM {table} WHERE 1=1{scope_sql}", scope_params
        ).fetchall()
    existing_shortcodes = {row["shortcode"] for row in existing_rows if row["shortcode"]}

    new_items = [it for it in items if it.get("shortCode") and it["shortCode"] not in existing_shortcodes]
    new_items.sort(key=lambda it: it.get("timestamp") or "")
    return _insert_new_posts(account, cfg, new_items)


def fetch_profile_preview(handle: str) -> dict[str, Any]:
    """Lightweight lookup for the add-account wizard: a single Apify
    'details' scrape of the profile URL itself (one result, not paginated
    posts) so the wizard can show the real profile picture, display name,
    and follower count before the account is actually created. Read-only --
    doesn't touch the database at all.
    """
    clean = handle.strip().lstrip("@")
    if not clean:
        raise ApifySyncError("Handle is required.")

    payload: dict[str, Any] = {
        "directUrls": [f"https://www.instagram.com/{clean}/"],
        "resultsType": "details",
    }
    items = _fetch_apify_items(payload, timeout=60.0)
    if not items:
        raise ApifySyncError("Could not find that Instagram account.")

    item = items[0]
    if item.get("error"):
        raise ApifySyncError(item.get("errorDescription") or "Could not find that Instagram account.")

    return {
        "handle": item.get("username") or clean,
        "full_name": item.get("fullName"),
        "profile_pic_url": item.get("profilePicUrlHD") or item.get("profilePicUrl"),
        "followers_count": item.get("followersCount"),
        "posts_count": item.get("postsCount"),
        "verified": bool(item.get("verified")),
        "private": bool(item.get("private")),
    }


def store_avatar_from_url(handle: str, image_url: str) -> str:
    """Downloads a profile picture from a known URL into UPLOAD_DIR and
    records it on the account. Instagram's CDN URLs (via Apify) are signed
    and expire within a day or two, so we keep our own copy and serve it
    through /api/dashboard/avatar/{handle} instead of the raw CDN URL.
    """
    import httpx

    from .config import UPLOAD_DIR
    from .db import connect, utc_now

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    clean = handle.strip().lstrip("@").lower()
    suffix = ".jpg"
    try:
        with httpx.Client(timeout=30.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = client.get(image_url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "png" in content_type:
                suffix = ".png"
            elif "webp" in content_type:
                suffix = ".webp"
            avatar_path = UPLOAD_DIR / f"avatar-{clean}{suffix}"
            avatar_path.write_bytes(response.content)
    except httpx.HTTPError as exc:
        raise ApifySyncError(f"Could not download the profile picture: {exc}") from exc

    with connect() as conn:
        conn.execute(
            "UPDATE accounts SET avatar_path = ?, updated_at = ? WHERE handle = ?",
            (str(avatar_path), utc_now(), clean),
        )
    return str(avatar_path)


def fetch_and_store_avatar(handle: str) -> str:
    """Looks up the account's current profile picture via Apify (one
    lightweight 'details' scrape) and caches it locally. Use
    store_avatar_from_url() directly instead when the URL is already known
    (e.g. the add-account wizard's own preview fetch) to avoid a second,
    redundant Apify call.
    """
    preview = fetch_profile_preview(handle)
    image_url = preview.get("profile_pic_url")
    if not image_url:
        raise ApifySyncError(f"No profile picture available for '{handle}'.")
    return store_avatar_from_url(handle, image_url)
