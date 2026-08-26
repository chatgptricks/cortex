from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from .apify_sync import (
    VALID_GROUPS,
    ApifySyncError,
    create_account,
    fetch_and_store_avatar,
    fetch_profile_preview,
    get_account_config,
    list_accounts,
    refresh_single_post,
    run_backfill,
    run_manual_refresh,
    store_avatar_from_url,
)
from .post_media import PostMediaError, build_zip, collect_media, fetch_one
from .config import (
    DATA_DIR,
    EXTRA_CORS_ORIGINS,
    TRICKS_DASH_REFRESH_PASSWORD,
    UPLOAD_DIR,
    ensure_directories,
)
from .db import (
    delete_account_list,
    all_account_snapshots,
    connect,
    count_dashboard_admins,
    get_dashboard_user_role,
    get_usage_summary,
    init_db,
    list_account_snapshots,
    list_dashboard_users,
    list_account_lists,
    log_usage_event,
    remove_dashboard_user,
    seed_dashboard_users_from_env,
    upsert_dashboard_user,
    utc_now,
    upsert_account_list,
)
from .sentient_ocr import sentient_ocr_status
from .scheduler import start_scheduler


app = FastAPI(title="Cortex API", version="1.0.0")

DEFAULT_PERSON_OPTIONS = [
    "Elon Musk",
    "Sam Altman",
    "Jensen Huang",
    "Dario Amodei",
    "Donald Trump",
    "Xi Jinping",
]
DEFAULT_COMPANY_OPTIONS = [
    "ChatGPT / OpenAI",
    "Claude / Anthropic",
    "Gemini / Google",
    "Grok / xAI",
]
DEFAULT_POST_TYPE_OPTIONS = ["Tricks", "News", "Promo", "Reel", "Meme"]



# --- Google login (Firebase Auth) -----------------------------------------
# Sentient Dash used to be public-read with a single shared password gating
# admin writes. That's replaced entirely by Google sign-in: every request
# now needs a valid Firebase ID token for a Google account in the
# dashboard_users table, checked by the middleware below. The old per-endpoint
# TRICKS_DASH_REFRESH_PASSWORD checks scattered through this file are left
# untouched (the frontend still sends that same fixed value automatically,
# with no UI for it anymore) -- they're redundant now that this middleware
# is the real gate, since nobody without a valid, allowlisted Google session
# can reach those endpoints to submit that password in the first place.
import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials as firebase_credentials

FIREBASE_APP = None
for _cred_path in (
    "/etc/secrets/firebase-adminsdk.json",
    str(Path(__file__).resolve().parent.parent / "firebase-adminsdk.json"),
):
    if os.path.exists(_cred_path):
        try:
            FIREBASE_APP = firebase_admin.initialize_app(firebase_credentials.Certificate(_cred_path))
        except ValueError:
            FIREBASE_APP = firebase_admin.get_app()
        break

# ALLOWED_EMAILS/ADMIN_EMAILS are now only a one-time seed for the
# dashboard_users table (see seed_dashboard_users_from_env at startup) --
# the table itself is the live source of truth, editable from the Users tab
# in Settings without touching Render. Kept here so a from-scratch deploy
# with an empty DB still boots with the right people able to sign in.
_SEED_ALLOWED_EMAILS = {e.strip().lower() for e in os.getenv("ALLOWED_EMAILS", "").split(",") if e.strip()}
_SEED_ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}

# Cover/avatar images load via plain <img src="..."> tags, which can't carry
# an Authorization header -- excluded here for that technical reason only,
# not because they're meant to stay public by design. Everything else (post
# data, search, every admin action) requires a signed-in, allowlisted
# Google account once FIREBASE_APP is configured.
_FIREBASE_OPEN_PREFIXES = ("/api/dashboard/covers/", "/api/dashboard/avatar/")
_FIREBASE_OPEN_PATHS = {"/api/health", "/", "/docs", "/openapi.json", "/redoc"}


@app.middleware("http")
async def _require_firebase_user(request, call_next):  # type: ignore[no-untyped-def]
    if FIREBASE_APP is None:
        # No credentials configured (e.g. local dev without the secret
        # file) -- stay open rather than lock everyone out.
        return await call_next(request)
    path = request.url.path
    if request.method == "OPTIONS" or path in _FIREBASE_OPEN_PATHS or path.startswith(_FIREBASE_OPEN_PREFIXES):
        return await call_next(request)

    from fastapi.responses import JSONResponse

    header = request.headers.get("authorization") or ""
    if not header.startswith("Bearer "):
        return JSONResponse({"detail": "Sign in required."}, status_code=401)
    token = header[len("Bearer ") :].strip()
    try:
        decoded = firebase_auth.verify_id_token(token)
    except Exception:
        return JSONResponse({"detail": "Your session expired -- please sign in again."}, status_code=401)
    email = (decoded.get("email") or "").strip().lower()
    role = get_dashboard_user_role(email)
    if role is None:
        return JSONResponse(
            {"detail": "This Google account is not authorized for Sentient Dash."}, status_code=403
        )
    is_admin = role == "admin"
    if path.startswith("/api/admin/") and not is_admin:
        return JSONResponse({"detail": "Admin access required."}, status_code=403)
    request.state.user_email = email
    request.state.is_admin = is_admin
    try:
        # Feeds the Users tab's usage heatmap. Best-effort: a failed insert
        # here should never turn into a failed request for the user.
        log_usage_event(email, path, request.method)
    except Exception:
        logging.getLogger(__name__).warning("usage log insert failed", exc_info=True)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", *EXTRA_CORS_ORIGINS],
    # Local Vite projects in this workspace use different ports (including the
    # standalone Tricks Dash on 4175). Production origins remain explicit.
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1):\d+$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registered last on purpose. Starlette runs the most recently added
# middleware outermost, so this wraps the two auth middlewares below rather
# than sitting inside them. When an auth middleware short-circuits with a 401
# or 403 it returns a response directly -- if CORS were inner, that response
# would carry no Access-Control-Allow-Origin, the browser would refuse to let
# the page read it, and a simple "your session expired" would surface as an
# opaque network failure. That is exactly what made a signed-out visit to the
# dashboard show "Could not load the shared Post DB" instead of the sign-in
# gate.

@app.on_event("startup")
def startup() -> None:
    init_db()
    seed_dashboard_users_from_env(_SEED_ALLOWED_EMAILS, _SEED_ADMIN_EMAILS)
    start_scheduler()


ensure_directories()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "deployment": {
            "commit": os.getenv("RENDER_GIT_COMMIT"),
            "service": os.getenv("RENDER_SERVICE_NAME"),
        },
        # Sentient Dash's own cover-image OCR -- standalone, GPU-free worker.
        # Predict (tribev2, LLM report, Post DB) is archived and fully
        # disconnected -- nothing here reports on it anymore.
        "sentient_ocr": sentient_ocr_status(),
    }


@app.get("/api/dashboard/me")
def dashboard_me(request: Request) -> dict[str, Any]:
    """Tells the frontend who's signed in and whether they can see Settings.
    Purely informational -- /api/admin/* is what actually enforces the
    admin-only boundary, this just lets the UI hide the button for everyone
    else instead of showing it and then bouncing them with a 403.
    """
    return {
        "email": getattr(request.state, "user_email", None),
        "is_admin": bool(getattr(request.state, "is_admin", False)),
    }


@app.get("/api/dashboard/accounts")
def dashboard_accounts() -> dict[str, Any]:
    """Public roster of every account (Sentient + Competitors) driving
    Sentient Dash. New accounts are added via POST /api/admin/accounts --
    no code changes or redeploys needed to add the next one.
    """
    return {"accounts": list_accounts(active_only=True)}


def _likes_or_null(raw: Any) -> int | None:
    """Surfaces "we don't know" as null instead of a number. Covers both the
    legacy 500 placeholder rows and Instagram's hidden/under-reported counts
    (0-3), so the UI can render a dash rather than invent engagement.
    """
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 3 else None


def _clean_ocr_text(raw: Any) -> str:
    """hook_text carries a '-' sentinel for covers we can never OCR (file gone,
    expired CDN link). It exists only to keep those rows out of the retry
    queue, so it must never surface as real cover text or pollute search.
    """
    text = str(raw or "").strip()
    return "" if text in {"-", "~"} else text


@app.get("/api/insights/posts")
def insights_posts() -> dict[str, Any]:
    """Compact projection built for analysis rather than browsing.

    Separate from /api/dashboard/posts on purpose: that one carries captions and
    cover URLs for rendering cards, while this one drops the heavy text and
    exposes the enrichment columns (reel views, slide counts, hashtags, audio,
    duration) that make aggregate questions answerable. Arrays are short keys to
    keep ~11.5k rows light enough to analyse entirely client-side.
    """
    accounts = list_accounts(active_only=True)
    group_by_handle = {a["handle"]: a["group"] for a in accounts}
    thresholds = {a["handle"]: a["hot_threshold"] for a in accounts}

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT account, shortcode, published_at, likes, comments,
                   post_type_label, product_type, video_views, video_plays,
                   video_duration, slide_count, hashtags, music_song,
                   music_artist, uses_original_audio, paid_partnership,
                   is_hot, hot_rate_multiplier, hook_text, permalink
            FROM dashboard_posts
            WHERE published_at IS NOT NULL AND published_at != ''
            """
        ).fetchall()

    posts = []
    for row in rows:
        p = dict(row)
        hook = _clean_ocr_text(p.get("hook_text"))
        # post_type_label carries a productType suffix for some rows
        # ("Carousel (carousel_container)"), which would split the same format
        # into several buckets and quietly halve every per-format average.
        # The suffix is preserved separately in `pt`.
        raw_label = p.get("post_type_label") or "Image"
        base_type = raw_label.split(" (")[0].strip() or "Image"
        posts.append(
            {
                "a": p["account"],
                "g": group_by_handle.get(p["account"], "sentient"),
                "sc": p["shortcode"],
                "d": p["published_at"],
                "l": _likes_or_null(p.get("likes")),
                "c": p.get("comments") or 0,
                "t": base_type,
                "pt": p.get("product_type"),
                "v": p.get("video_views"),
                "pl": p.get("video_plays"),
                "dur": p.get("video_duration"),
                "sl": p.get("slide_count"),
                "h": p.get("hashtags"),
                "ms": p.get("music_song"),
                "oa": p.get("uses_original_audio"),
                "pp": p.get("paid_partnership"),
                "hot": 1 if p.get("is_hot") else 0,
                "hm": p.get("hot_rate_multiplier"),
                # Truncated: enough for word-frequency mining, not for reading.
                "ocr": hook[:220] if hook else "",
                "u": p.get("permalink"),
            }
        )

    return {"posts": posts, "accounts": [
        {
            "handle": a["handle"],
            "group": a["group"],
            "threshold": thresholds.get(a["handle"]),
            # Insights' word cloud mines OCR'd cover text; without the label a
            # page's own name/watermark (e.g. "Get Into AI") shows up as its
            # own top "topic" on every one of its posts. Sent so the frontend
            # can strip both the handle and each word of the label.
            "label": a.get("label"),
        }
        for a in accounts
    ]}


def _closest_snapshot_at_or_before(snapshots: list[dict[str, Any]], cutoff: str) -> dict[str, Any] | None:
    """`snapshots` must be sorted oldest -> newest (captured_at ascending).
    Returns the most recent one at or before `cutoff`, or None if the
    account's recorded history doesn't reach back that far yet."""
    candidate = None
    for snap in snapshots:
        if snap["captured_at"] <= cutoff:
            candidate = snap
        else:
            break
    return candidate


def _tracker_delta(snapshots: list[dict[str, Any]], latest: dict[str, Any] | None, days: int) -> dict[str, Any] | None:
    if not latest or latest.get("followers_count") is None:
        return None
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    baseline = _closest_snapshot_at_or_before(snapshots, cutoff)
    if not baseline or baseline is latest or baseline.get("followers_count") is None:
        return None
    delta = latest["followers_count"] - baseline["followers_count"]
    pct = (delta / baseline["followers_count"] * 100) if baseline["followers_count"] else None
    return {"delta": delta, "pct": pct, "from": baseline["captured_at"]}


@app.get("/api/tracker/summary")
def tracker_summary() -> dict[str, Any]:
    """Social-Blade-style leaderboard: every tracked account's current
    follower count, 1d/7d/30d growth (grows more meaningful as
    account_snapshots accumulates day over day -- there's no way to
    backfill Instagram's past follower counts), and an engagement trend
    pulled from the post history already on hand, which goes back months
    rather than starting from zero today. Open to any signed-in user, not
    admin-gated -- same tier as /api/insights/*.
    """
    accounts = list_accounts(active_only=True)
    snapshots_by_handle = all_account_snapshots()

    now = datetime.now(UTC)
    cutoff_30 = (now - timedelta(days=30)).isoformat(timespec="seconds")
    cutoff_60 = (now - timedelta(days=60)).isoformat(timespec="seconds")

    with connect() as conn:
        engagement_rows = conn.execute(
            """
            SELECT account,
                   AVG(CASE WHEN published_at >= ? THEN likes END) AS avg_likes_30d,
                   COUNT(CASE WHEN published_at >= ? THEN 1 END) AS n_30d,
                   AVG(CASE WHEN published_at >= ? AND published_at < ? THEN likes END) AS avg_likes_prev_30d
            FROM dashboard_posts
            WHERE published_at IS NOT NULL AND published_at != ''
            GROUP BY account
            """,
            (cutoff_30, cutoff_30, cutoff_60, cutoff_30),
        ).fetchall()
    engagement_by_account = {row["account"]: dict(row) for row in engagement_rows}

    rows: list[dict[str, Any]] = []
    for account in accounts:
        handle = account["handle"]
        snaps = snapshots_by_handle.get(handle, [])
        latest = snaps[-1] if snaps else None
        eng = engagement_by_account.get(handle, {})
        avg30 = eng.get("avg_likes_30d")
        avgprev = eng.get("avg_likes_prev_30d")
        engagement_trend_pct = (avg30 - avgprev) / avgprev * 100 if avg30 is not None and avgprev else None

        rows.append(
            {
                "handle": handle,
                "label": account.get("label"),
                "group": account.get("group"),
                "followers": latest.get("followers_count") if latest else None,
                "posts_count": latest.get("posts_count") if latest else None,
                "captured_at": latest.get("captured_at") if latest else None,
                "full_name": latest.get("full_name") if latest else None,
                "verified": bool(latest.get("verified")) if latest else False,
                "private": bool(latest.get("private")) if latest else False,
                "history_days": len(snaps),
                "delta_1d": _tracker_delta(snaps, latest, 1),
                "delta_7d": _tracker_delta(snaps, latest, 7),
                "delta_30d": _tracker_delta(snaps, latest, 30),
                "avg_likes_30d": avg30,
                "posts_30d": eng.get("n_30d") or 0,
                "engagement_trend_pct": engagement_trend_pct,
            }
        )

    # Followers is the primary Social-Blade-style ranking. Growth is ranked
    # separately (by 7d delta, the shortest window likely to be populated
    # soon after this ships) and only among accounts that actually have
    # one yet, so a brand-new snapshot history doesn't rank via a missing
    # value sorting first or last by accident.
    by_followers = sorted((r for r in rows if r["followers"] is not None), key=lambda r: r["followers"], reverse=True)
    for i, r in enumerate(by_followers, start=1):
        r["rank_by_followers"] = i
    by_growth = sorted(
        (r for r in rows if r["delta_7d"] is not None), key=lambda r: r["delta_7d"]["delta"], reverse=True
    )
    for i, r in enumerate(by_growth, start=1):
        r["rank_by_growth_7d"] = i
    for r in rows:
        r.setdefault("rank_by_followers", None)
        r.setdefault("rank_by_growth_7d", None)

    rows.sort(key=lambda r: (r["followers"] is None, -(r["followers"] or 0)))

    earliest = min((snaps[0]["captured_at"] for snaps in snapshots_by_handle.values() if snaps), default=None)
    return {"accounts": rows, "tracking_since": earliest, "generated_at": utc_now()}


@app.get("/api/tracker/accounts/{handle}")
def tracker_account_detail(handle: str) -> dict[str, Any]:
    """Growth history for one account: the full follower/posts_count
    snapshot series recorded so far, plus a weekly engagement trend computed
    from the post history we already have (months of real data, unlike the
    follower series which starts from whenever tracking began)."""
    clean = handle.strip().lstrip("@").lower()
    accounts = {a["handle"]: a for a in list_accounts(active_only=False)}
    account = accounts.get(clean)
    if not account:
        raise HTTPException(status_code=404, detail="Unknown account.")

    snapshots = list_account_snapshots(clean)

    with connect() as conn:
        post_rows = conn.execute(
            "SELECT published_at, likes, video_views FROM dashboard_posts "
            "WHERE account = ? AND published_at IS NOT NULL AND published_at != ''",
            (clean,),
        ).fetchall()

    weekly: dict[str, dict[str, Any]] = {}
    daily: dict[str, list[int]] = {}
    for row in post_rows:
        try:
            dt = datetime.fromisoformat(row["published_at"])
        except ValueError:
            continue
        # ISO week start (Monday) as a stable bucket key -- the chart plots
        # calendar weeks, not an exact rolling 7-day window.
        week_start = (dt - timedelta(days=dt.weekday())).date().isoformat()
        bucket = weekly.setdefault(week_start, {"likes": [], "views": [], "count": 0})
        bucket["likes"].append(row["likes"] or 0)
        if row["video_views"] is not None:
            bucket["views"].append(row["video_views"])
        bucket["count"] += 1
        # Per calendar day too -- feeds the historical-stats table's
        # "engagement rate" column (that day's avg likes / that day's
        # follower count), independent of the weekly chart above.
        daily.setdefault(dt.date().isoformat(), []).append(row["likes"] or 0)

    engagement_weekly = [
        {
            "week_start": week,
            "post_count": b["count"],
            "avg_likes": sum(b["likes"]) / len(b["likes"]) if b["likes"] else None,
            "avg_views": sum(b["views"]) / len(b["views"]) if b["views"] else None,
        }
        for week, b in sorted(weekly.items())
    ]

    def _day_likes(captured_at: str) -> list[int]:
        try:
            day = datetime.fromisoformat(captured_at).date().isoformat()
        except ValueError:
            return []
        return daily.get(day, [])

    return {
        "handle": clean,
        "label": account.get("label"),
        "group": account.get("group"),
        "followers_history": [
            {
                "date": s["captured_at"],
                "followers": s["followers_count"],
                "posts_count": s["posts_count"],
                "following_count": s["following_count"],
                "full_name": s["full_name"],
                "verified": bool(s["verified"]),
                "private": bool(s["private"]),
                # That calendar day's posts (if any), for the historical-stats
                # table's engagement-rate column -- independent of the weekly
                # chart, which buckets by ISO week instead.
                "avg_likes_that_day": (lambda vals: sum(vals) / len(vals) if vals else None)(_day_likes(s["captured_at"])),
                "posts_that_day": len(_day_likes(s["captured_at"])),
            }
            for s in snapshots
        ],
        "engagement_weekly": engagement_weekly,
    }


@app.get("/api/dashboard/posts")
def dashboard_posts() -> dict[str, Any]:
    """Unified, public, read-only projection across every account. Each
    post is tagged with `account` and `group` (sentient/competitors) so the
    frontend can build the All/Sentient/Competitors tabs and per-tab account
    filter from a single fetch.
    """
    accounts = list_accounts(active_only=True)
    group_by_handle = {a["handle"]: a["group"] for a in accounts}
    canonical = next((a for a in accounts if a["is_canonical"]), None)

    posts: list[dict[str, Any]] = []

    with connect() as conn:
        if canonical:
            canonical_rows = conn.execute(
                """
                SELECT id, title, caption, hook_text, published_at, likes, comments,
                       post_type_label, shortcode, image_path, is_animated,
                       source_row_number, created_at, section, is_hot, hot_rate_multiplier,
                       is_promo, hidden
                FROM posts
                """
            ).fetchall()
        else:
            canonical_rows = []
        dashboard_rows = conn.execute(
            """
            SELECT id, account, shortcode, published_at, likes, comments, caption,
                   post_type_label, is_animated, permalink, is_hot, hot_rate_multiplier,
                   hook_text, music_song, music_artist, music_audio_id, uses_original_audio,
                   is_promo, hidden
            FROM dashboard_posts
            """
        ).fetchall()

    if canonical:
        handle = canonical["handle"]
        for row in canonical_rows:
            post = dict(row)
            shortcode = str(post.get("shortcode") or "").strip()
            post_type = str(post.get("post_type_label") or "").strip() or "Image"
            has_video = post_type.lower().startswith("video") or bool(post.get("is_animated"))
            posts.append(
                {
                    "rank": post.get("source_row_number") or post["id"],
                    "postDate": post.get("published_at"),
                    "likes": _likes_or_null(post.get("likes")),
                    "comments": int(post.get("comments") or 0),
                    "type": post_type,
                    "video": "Yes" if has_video else "No",
                    "shortcode": shortcode or f"post-{post['id']}",
                    "permalink": f"https://www.instagram.com/p/{shortcode}/" if shortcode else "",
                    "caption": post.get("caption") or post.get("title") or "",
                    "excerpt": post.get("title") or "",
                    "section": post.get("section") or "",
                    # hook_text is the normalized OCR result for the cover
                    # image; already indexed in the frontend's search field.
                    "ocrText": _clean_ocr_text(post.get("hook_text")),
                    "coverUrl": f"/api/dashboard/covers/{handle}/{post['id']}",
                    "isHot": bool(post.get("is_hot")),
                    "hotMultiplier": post.get("hot_rate_multiplier"),
                    "isPromo": bool(post.get("is_promo")),
                    "hidden": bool(post.get("hidden")),
                    "account": handle,
                    "group": group_by_handle.get(handle, "sentient"),
                    # The canonical `posts` table predates the music columns
                    # (they were added to dashboard_posts only) -- always null
                    # here rather than missing, so the frontend can treat the
                    # two account types the same way.
                    "musicSong": None,
                    "musicArtist": None,
                    "usesOriginalAudio": None,
                    "musicUrl": None,
                }
            )

    for row in dashboard_rows:
        post = dict(row)
        account = post.get("account")
        shortcode = str(post.get("shortcode") or "").strip()
        post_type = str(post.get("post_type_label") or "").strip() or "Image"
        has_video = post_type.lower().startswith("video") or bool(post.get("is_animated"))
        posts.append(
            {
                "rank": post["id"],
                "postDate": post.get("published_at"),
                "likes": _likes_or_null(post.get("likes")),
                "comments": int(post.get("comments") or 0),
                "type": post_type,
                "video": "Yes" if has_video else "No",
                "shortcode": shortcode,
                "permalink": post.get("permalink") or (f"https://www.instagram.com/p/{shortcode}/" if shortcode else ""),
                "caption": post.get("caption") or "",
                "excerpt": post.get("caption") or "",
                "section": "single",
                "ocrText": _clean_ocr_text(post.get("hook_text")),
                "coverUrl": f"/api/dashboard/covers/{account}/{post['id']}",
                "isHot": bool(post.get("is_hot")),
                "hotMultiplier": post.get("hot_rate_multiplier"),
                "isPromo": bool(post.get("is_promo")),
                "hidden": bool(post.get("hidden")),
                "account": account,
                "group": group_by_handle.get(account, "competitors"),
                "musicSong": post.get("music_song"),
                "musicArtist": post.get("music_artist"),
                "usesOriginalAudio": bool(post.get("uses_original_audio")),
                # Instagram's own sound page -- not Spotify/Apple Music, Apify
                # doesn't supply a link to those. Links to every reel that used
                # this exact audio, including original-audio "songs".
                "musicUrl": (
                    f"https://www.instagram.com/reels/audio/{post['music_audio_id']}/"
                    if post.get("music_audio_id")
                    else None
                ),
            }
        )

    posts.sort(key=lambda p: p.get("postDate") or "", reverse=True)
    # Posts with an unknown like count are null now, so they're excluded from
    # both the total and the average -- averaging them in as 0 would drag the
    # figure down with data we simply don't have.
    known_likes = [post["likes"] for post in posts if post["likes"] is not None]
    total_likes = sum(known_likes)
    return {
        "posts": posts,
        "summary": {
            "Exported posts": len(posts),
            "Total likes": total_likes,
            "Average likes": round(total_likes / len(known_likes)) if known_likes else 0,
        },
    }


@app.get("/api/dashboard/covers/{account}/{post_id}")
def dashboard_cover(account: str, post_id: int) -> FileResponse:
    """Serve a post cover for any account. Non-canonical accounts lazily
    download and cache from the original CDN URL on first request (avoids
    eagerly downloading covers for every post during import/backfill).
    """
    try:
        cfg = get_account_config(account)
    except ApifySyncError as exc:
        raise HTTPException(status_code=404, detail="Unknown account.") from exc

    if cfg["is_canonical"]:
        with connect() as conn:
            row = conn.execute("SELECT image_path FROM posts WHERE id = ?", (post_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Post cover not found.")
        image_path = Path(str(row["image_path"]))
        if not image_path.is_file():
            raise HTTPException(status_code=404, detail="Post cover file is unavailable.")
        return FileResponse(image_path)

    with connect() as conn:
        row = conn.execute(
            "SELECT cover_image_path, cover_source_url FROM dashboard_posts WHERE id = ? AND account = ?",
            (post_id, account),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Post cover not found.")

    image_path = Path(str(row["cover_image_path"])) if row["cover_image_path"] else None
    if image_path and image_path.is_file():
        return FileResponse(image_path)

    cover_source_url = row["cover_source_url"]
    if not cover_source_url:
        raise HTTPException(status_code=404, detail="Post cover is unavailable.")

    import httpx

    try:
        response = httpx.get(cover_source_url, timeout=20.0, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch cover: {exc}") from exc

    suffix = ".jpg"
    content_type = response.headers.get("content-type", "")
    if "png" in content_type:
        suffix = ".png"
    elif "webp" in content_type:
        suffix = ".webp"

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    new_path = UPLOAD_DIR / f"dash-{account}-{os.urandom(16).hex()}{suffix}"
    new_path.write_bytes(response.content)

    with connect() as conn:
        conn.execute(
            "UPDATE dashboard_posts SET cover_image_path = ? WHERE id = ?",
            (str(new_path), post_id),
        )

    return FileResponse(new_path)


def _caller_email(request: Request) -> str:
    """Email of the signed-in user, set by the Firebase middleware.

    Falls back to a sentinel when Firebase isn't configured (local dev runs
    with the gate open) so lists still work there instead of crashing.
    """
    return (getattr(request.state, "user_email", "") or "local@dev").strip().lower()


@app.get("/api/dashboard/lists")
def dashboard_lists(request: Request) -> dict[str, Any]:
    """Custom account lists this user can see: their own plus shared ones."""
    return {"lists": list_account_lists(_caller_email(request))}


@app.post("/api/dashboard/lists")
def dashboard_lists_save(
    request: Request,
    name: Annotated[str, Form()],
    handles: Annotated[str, Form()],
    list_id: Annotated[int | None, Form()] = None,
) -> dict[str, Any]:
    """Creates or updates one of the caller's lists.

    `handles` is a comma-separated string rather than a repeated field so the
    frontend can send it from a plain FormData without special-casing arrays.
    Ownership is enforced in the DB layer, so passing someone else's list_id
    fails rather than editing their list.
    """
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="A list needs a name.")
    # Lowercased because handles are stored that way: Instagram treats them
    # case-insensitively, so "@ChatGPTricks" must resolve to the same account
    # as "chatgptricks" rather than being rejected as unknown.
    wanted = [h.strip().lstrip("@").lower() for h in handles.split(",") if h.strip()]
    if not wanted:
        raise HTTPException(status_code=400, detail="Pick at least one account.")

    # Drop handles that aren't real accounts: a list pointing at a deleted or
    # mistyped handle would silently render an empty tab.
    known = {a["handle"] for a in list_accounts(active_only=False)}
    unknown = [h for h in wanted if h not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown account(s): {', '.join(unknown)}")

    try:
        saved = upsert_account_list(_caller_email(request), clean_name, wanted, list_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"list": saved}


@app.post("/api/dashboard/lists/delete")
def dashboard_lists_delete(request: Request, list_id: Annotated[int, Form()]) -> dict[str, Any]:
    if not delete_account_list(_caller_email(request), list_id):
        raise HTTPException(status_code=404, detail="List not found.")
    return {"deleted": list_id}


def _resolve_post_table(account: str) -> str:
    """Which table holds this account's posts. The canonical account lives in
    the legacy `posts` table and everyone else in `dashboard_posts`; the card
    menu has to work on either, so every post-level write goes through here.
    """
    row = next((a for a in list_accounts(active_only=False) if a["handle"] == account), None)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown account: {account}")
    return "posts" if row["is_canonical"] else "dashboard_posts"


@app.post("/api/dashboard/posts/flags")
def dashboard_post_flags(
    account: Annotated[str, Form()],
    shortcode: Annotated[str, Form()],
    is_promo: Annotated[bool | None, Form()] = None,
    hidden: Annotated[bool | None, Form()] = None,
) -> dict[str, Any]:
    """Sets curation flags from the post card's ... menu.

    Both flags are optional so the caller can toggle one without having to
    know (or accidentally overwrite) the other. Neither destroys anything:
    `hidden` filters the post out of the grid but leaves the row, its cover
    and its numbers intact, so it still counts in totals and can be undone.
    Deliberately not a delete -- an irreversible action on a single click,
    against data that costs money to re-scrape, isn't worth the convenience.
    """
    table = _resolve_post_table(account)
    updates: list[str] = []
    params: list[Any] = []
    if is_promo is not None:
        updates.append("is_promo = ?")
        params.append(1 if is_promo else 0)
    if hidden is not None:
        updates.append("hidden = ?")
        params.append(1 if hidden else 0)
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update.")

    updates.append("updated_at = ?")
    params.append(utc_now())

    # The canonical `posts` table has no `account` column (it only ever holds
    # the one account), so scope by shortcode alone there.
    where = "shortcode = ?" if table == "posts" else "account = ? AND shortcode = ?"
    where_params: list[Any] = [shortcode] if table == "posts" else [account, shortcode]

    with connect() as conn:
        cursor = conn.execute(
            f"UPDATE {table} SET {', '.join(updates)} WHERE {where}", [*params, *where_params]
        )
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="Post not found.")
        row = conn.execute(f"SELECT is_promo, hidden FROM {table} WHERE {where}", where_params).fetchone()

    return {"account": account, "shortcode": shortcode, "is_promo": bool(row["is_promo"]), "hidden": bool(row["hidden"])}


@app.post("/api/dashboard/posts/reload")
def dashboard_post_reload(
    account: Annotated[str, Form()],
    shortcode: Annotated[str, Form()],
) -> dict[str, Any]:
    """Re-scrapes one post's like/comment counts on demand.

    The scheduled cycle only looks at posts inside its 12h window, so an older
    post's numbers are frozen at whatever they were when it aged out. This is
    the escape hatch for "that count looks stale". One Apify result, ~$0.002.
    """
    try:
        return refresh_single_post(account, shortcode)
    except ApifySyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/dashboard/posts/media")
def dashboard_post_media(
    account: Annotated[str, Query()],
    shortcode: Annotated[str, Query()],
    list_only: Annotated[bool, Query(alias="list")] = False,
    only: Annotated[str | None, Query()] = None,
) -> Response:
    """Lists a post's media, or returns some of it as a download.

    Three shapes, one resolve step:
      ?list=1        -> JSON, one entry per item, for the picker
      (no args)      -> ZIP of everything
      ?only=2,5      -> ZIP of just those, keeping their original numbering
      ?only=3        -> that single file, unzipped

    A lone file in a ZIP is a wrapper the person then has to undo, so one
    requested item comes back as itself.
    """
    _resolve_post_table(account)  # 404s on an unknown account before any scraping

    wanted: set[int] | None = None
    if only:
        try:
            wanted = {int(part) for part in only.split(",") if part.strip()}
        except ValueError:
            raise HTTPException(status_code=400, detail="`only` must be comma-separated numbers.") from None
        if not wanted:
            raise HTTPException(status_code=400, detail="`only` was empty.")

    try:
        if list_only:
            items, source = collect_media(shortcode)
            return JSONResponse(
                {
                    "source": source,
                    # The signed CDN url is deliberately not echoed back: the
                    # browser never fetches the media itself, the server does.
                    # `poster` is returned because the picker has to show a
                    # thumbnail, and for an image it is the image.
                    "items": [
                        {
                            "index": it["index"],
                            "kind": it["kind"],
                            "filename": it["filename"],
                            "poster": it.get("poster"),
                        }
                        for it in items
                    ],
                }
            )

        if wanted and len(wanted) == 1:
            items, _ = collect_media(shortcode)
            index = next(iter(wanted))
            item = next((i for i in items if i["index"] == index), None)
            if item is None:
                raise HTTPException(status_code=404, detail=f"This post has no item {index}.")
            payload, suffix = fetch_one(item["url"])
            name = f"{account}-{shortcode}-{index:02d}{suffix}"
            # "image/jpg" isn't a registered type -- browsers tolerate it, but
            # the correct one is image/jpeg.
            media_type = {
                ".mp4": "video/mp4",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
            }.get(suffix, "application/octet-stream")
            return Response(
                content=payload,
                media_type=media_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{name}"',
                    "Access-Control-Expose-Headers": "Content-Disposition, X-Slide-Count, X-Media-Source",
                    "X-Slide-Count": "1",
                    "X-Media-Source": "direct",
                },
            )

        payload, filename, meta = build_zip(account, shortcode, wanted)
    except PostMediaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # A browser can only read headers it is told to expose, and this
            # is a cross-origin request. Content-Disposition matters as much as
            # the counts: the client names the saved file from it, so without
            # it a single JPEG gets saved with the ZIP's name and extension.
            "Access-Control-Expose-Headers": "Content-Disposition, X-Slide-Count, X-Media-Source",
            "X-Slide-Count": str(meta["slides"]),
            "X-Media-Source": str(meta["source"]),
        },
    )


@app.post("/api/dashboard/refresh")
def dashboard_refresh(password: Annotated[str, Form()]) -> dict[str, Any]:
    """Password-gated manual override: runs the short-term (<=24h + HOT
    check) and daily (>24h-10day, 30d, 120d) engagement cycles for every
    active account -- Sentient and Competitors alike. Gated because each
    call costs Apify credits and writes to the live database.
    """
    if not TRICKS_DASH_REFRESH_PASSWORD or not secrets.compare_digest(
        password.strip(), TRICKS_DASH_REFRESH_PASSWORD
    ):
        raise HTTPException(status_code=401, detail="Incorrect refresh password.")

    results: dict[str, Any] = {}
    for account in list_accounts(active_only=True):
        handle = account["handle"]
        try:
            results[handle] = run_manual_refresh(handle)
        except ApifySyncError as exc:
            results[handle] = {"error": str(exc)}
    return results


@app.post("/api/admin/slack-test")
def admin_slack_test(password: Annotated[str, Form()]) -> dict[str, Any]:
    """Sends a sample HOT alert so the Slack webhook can be verified without
    waiting for a real post to cross its threshold."""
    if not TRICKS_DASH_REFRESH_PASSWORD or not secrets.compare_digest(
        password.strip(), TRICKS_DASH_REFRESH_PASSWORD
    ):
        raise HTTPException(status_code=401, detail="Incorrect refresh password.")

    from .slack_alerts import notify_hot_post, slack_configured

    if not slack_configured():
        raise HTTPException(status_code=503, detail="SLACK_WEBHOOK_URL is not set on the server.")

    sent = notify_hot_post(
        {
            "account": "evolving.ai",
            "likes": 4200,
            "multiplier": 2.35,
            "rate_per_hour": 1410,
            "threshold": 600,
            "age_hours": 3.0,
            "permalink": "https://www.instagram.com/chatgptricks/",
            "shortcode": "DcXEyj_nZud",
            "caption": "Test alert from Sentient Dash — if you can read this, HOT alerts are wired up.",
        }
    )
    return {"sent": sent}


@app.get("/api/admin/slack-status")
def admin_slack_status() -> dict[str, Any]:
    """Whether the server has a Slack webhook configured (never exposes it)."""
    from .slack_alerts import slack_configured

    return {
        "configured": slack_configured(),
        "alert_groups": os.getenv("SLACK_ALERT_GROUPS", "competitors"),
    }


@app.get("/api/admin/accounts")
def admin_list_accounts() -> dict[str, Any]:
    """Full roster including inactive accounts, for the admin UI.

    Enriched with a suggested HOT threshold, current follower count from the
    tracker snapshots, and the published date of the oldest post we have on
    file (how far back a "extract history" backfill has actually reached,
    which otherwise isn't visible anywhere).

    The suggestion is built from `likes_at_1h` -- the like count the
    engagement pass actually captured around the ~1h mark for each post it
    ran the one-time HOT check against (see the `is_hot` calc further down
    this file: `rate_per_hour = likes / age_hours`, compared to
    `hot_threshold`). That's a materially different number from a post's
    lifetime `likes`, which keeps climbing for days after the HOT check has
    already happened -- averaging final totals would suggest a threshold
    calibrated to multi-day likes, not the first-hour pace HOT is actually
    measuring. Backfilled posts never went through that live check (no
    `likes_at_1h`), so they're correctly excluded rather than diluting the
    average with stale totals.
    """
    accounts = list_accounts(active_only=False)

    with connect() as conn:
        dash_rows = conn.execute(
            """
            SELECT account,
                   AVG(CASE WHEN hot_checked = 1 THEN likes_at_1h END) AS avg_likes,
                   COUNT(CASE WHEN hot_checked = 1 THEN likes_at_1h END) AS n_posts,
                   MIN(published_at) AS oldest_post_at
            FROM dashboard_posts
            GROUP BY account
            """
        ).fetchall()
        canonical_row = conn.execute(
            """
            SELECT AVG(CASE WHEN hot_checked = 1 THEN likes_at_1h END) AS avg_likes,
                   COUNT(CASE WHEN hot_checked = 1 THEN likes_at_1h END) AS n_posts,
                   MIN(published_at) AS oldest_post_at
            FROM posts
            """
        ).fetchone()

    stats_by_handle = {row["account"]: dict(row) for row in dash_rows}
    snapshots_by_handle = all_account_snapshots()

    for account in accounts:
        stat = dict(canonical_row) if account["is_canonical"] and canonical_row else stats_by_handle.get(account["handle"])
        avg_likes = stat["avg_likes"] if stat else None
        n_posts = stat["n_posts"] if stat else 0
        account["avg_likes"] = round(avg_likes) if avg_likes is not None else None
        account["avg_likes_sample_size"] = n_posts
        # New accounts with no post history yet fall back to the same 600
        # default the add-account wizard already uses.
        account["suggested_hot_threshold"] = (
            int((avg_likes + 99) // 100 * 100) if avg_likes else 600
        )
        snaps = snapshots_by_handle.get(account["handle"]) or []
        latest = snaps[-1] if snaps else None
        account["followers"] = latest.get("followers_count") if latest else None
        account["oldest_post_at"] = stat["oldest_post_at"] if stat else None

    return {"accounts": accounts}


@app.get("/api/admin/users")
def admin_list_users() -> dict[str, Any]:
    """Who can sign in to Sentient Dash and who's an admin. Backs the Users
    tab in Settings -- this table is the live source of truth (see
    seed_dashboard_users_from_env for the one-time env-var migration)."""
    return {"users": list_dashboard_users()}


@app.post("/api/admin/users")
def admin_upsert_user(
    request: Request,
    email: Annotated[str, Form()],
    role: Annotated[str, Form()] = "viewer",
) -> dict[str, Any]:
    """Add a new allowed email, or change an existing one's role. Admins can
    add other admins or plain viewers; there's no further gate here because
    /api/admin/* already requires an admin session to reach this at all."""
    clean_email = email.strip().lower()
    if not clean_email or "@" not in clean_email:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if role not in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'viewer'.")
    upsert_dashboard_user(clean_email, role)
    return {"ok": True, "users": list_dashboard_users()}


@app.post("/api/admin/users/remove")
def admin_remove_user(request: Request, email: Annotated[str, Form()]) -> dict[str, Any]:
    """Revoke someone's access entirely. Guarded against two lockout
    scenarios: removing your own session's admin account, and removing the
    last remaining admin -- either would leave nobody able to manage users or
    accounts, with no way back in short of hand-editing the database."""
    clean_email = email.strip().lower()
    current_email = getattr(request.state, "user_email", None)
    if current_email and clean_email == current_email:
        raise HTTPException(status_code=400, detail="You can't remove your own access.")
    role = get_dashboard_user_role(clean_email)
    if role == "admin" and count_dashboard_admins() <= 1:
        raise HTTPException(status_code=400, detail="Can't remove the last remaining admin.")
    remove_dashboard_user(clean_email)
    return {"ok": True, "users": list_dashboard_users()}


@app.get("/api/admin/usage")
def admin_usage(days: Annotated[int, Query(ge=1, le=90)] = 30) -> dict[str, Any]:
    """Usage analytics behind the Users tab's heatmap: who actually opens
    Sentient Dash, how often, when, and in which section (dashboard vs.
    insights vs. this admin panel). Sourced from usage_log, populated by the
    Firebase auth middleware on every authenticated request."""
    return get_usage_summary(days=days)


@app.post("/api/admin/tracker/snapshot-now")
def admin_tracker_snapshot_now() -> dict[str, Any]:
    """Manually runs the same per-account Apify profile scrape the daily
    7am CST job runs (see scheduler._run_account_snapshot_job), so day-one
    -- or re-adding an account -- doesn't have to wait for the next
    scheduled tick to show up on the Tracker page. Costs one lightweight
    Apify call per active account."""
    from .apify_sync import snapshot_all_accounts

    result = snapshot_all_accounts()
    return {"ok": True, **result}


_OCR_RUN: dict[str, Any] = {"running": False, "done": 0, "with_text": 0, "batches": 0, "error": None, "started": None}
_OCR_RUN_LOCK = threading.Lock()


def _ocr_worker(batch_size: int, max_batches: int) -> None:
    """Drains the OCR backlog by calling the exact same run_ocr_sweep() the
    hourly scheduler uses -- just in a tight loop instead of once per tick, so
    the one-time backlog clears in hours rather than days. Running the real
    production path (not a parallel copy) means this also exercises the cover
    re-download and canonical-posts handling.
    """
    from .apify_sync import run_ocr_sweep

    try:
        for _ in range(max_batches):
            result = run_ocr_sweep(limit=batch_size)
            _OCR_RUN["done"] += int(result.get("sent") or 0)
            _OCR_RUN["with_text"] += int(result.get("with_text") or 0)
            _OCR_RUN["skipped"] = _OCR_RUN.get("skipped", 0) + int(result.get("skipped") or 0)
            _OCR_RUN["batches"] += 1
            if not result.get("sent") and not result.get("skipped"):
                break
    except Exception as exc:
        _OCR_RUN["error"] = str(exc)
    finally:
        with _OCR_RUN_LOCK:
            _OCR_RUN["workers_live"] = max(0, int(_OCR_RUN.get("workers_live", 1)) - 1)
            if _OCR_RUN["workers_live"] == 0:
                _OCR_RUN["running"] = False


_BACKFILL_RUN: dict[str, Any] = {"running": False, "handle": None, "result": None, "error": None}


def _backfill_worker(
    handle: str, results_limit: int, date_from: str | None = None, date_to: str | None = None
) -> None:
    try:
        _BACKFILL_RUN["result"] = run_backfill(
            handle, results_limit=results_limit, date_from=date_from, date_to=date_to
        )
    except Exception as exc:
        _BACKFILL_RUN["error"] = str(exc)
    finally:
        _BACKFILL_RUN["running"] = False


def _require_admin(password: str) -> None:
    """Shared gate for the admin operations tools. These either spend Apify
    credits or write to the live database, so they all require the same refresh
    password the rest of /api/admin/* uses. The read-only *status* endpoints are
    deliberately left open: they expose only counters, cost nothing to call, and
    are polled while a long job runs.
    """
    if not TRICKS_DASH_REFRESH_PASSWORD or not secrets.compare_digest(
        (password or "").strip(), TRICKS_DASH_REFRESH_PASSWORD
    ):
        raise HTTPException(status_code=401, detail="Incorrect refresh password.")


@app.get("/api/admin/apify/runs")
def temp_runs(password: str, limit: int = 15) -> dict[str, Any]:
    """recent Apify runs so a finished-but-unsaved one can be
    reused instead of paying to scrape the same profile again."""
    _require_admin(password)
    import httpx

    from .apify_sync import APIFY_ACTOR_ID

    token = os.getenv("APIFY_TOKEN", "").strip()
    with httpx.Client(timeout=30.0) as client:
        r = client.get(
            f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/runs",
            params={"token": token, "limit": limit, "desc": "true"},
        )
        r.raise_for_status()
        items = r.json().get("data", {}).get("items", [])
    return {
        "runs": [
            {
                "id": i.get("id"),
                "status": i.get("status"),
                "startedAt": i.get("startedAt"),
                "finishedAt": i.get("finishedAt"),
                "usd": i.get("usageTotalUsd"),
                "datasetId": i.get("defaultDatasetId"),
            }
            for i in items
        ]
    }


@app.get("/api/admin/apify/run-log-summary/{run_id}")
def temp_run_log_summary(run_id: str, password: str) -> dict[str, Any]:
    """Diagnoses why a scrape came back short (rate-limited? blocked
    session? actor decided it was done?) without handing back the raw log,
    which routinely contains request URLs/cookies from the actor's own HTTP
    calls that shouldn't leave this server. Read-only, same admin gate as
    the rest of this module.
    """
    _require_admin(password)
    import re
    import httpx

    token = os.getenv("APIFY_TOKEN", "").strip()
    with httpx.Client(timeout=30.0) as client:
        r = client.get(f"https://api.apify.com/v2/actor-runs/{run_id}/log", params={"token": token})
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail="No log for this run (too old, or bad id).")
        r.raise_for_status()
        text = r.text

    # Strip anything cookie/token/session-shaped before a single character of
    # this leaves the server -- the actor's own log lines routinely embed its
    # HTTP request headers verbatim.
    def _redact(line: str) -> str:
        line = re.sub(r"(?i)(cookie|set-cookie|authorization|sessionid|csrftoken|ds_user_id)\s*[:=]\s*\S+", r"\1=[redacted]", line)
        line = re.sub(r"token=\S+", "token=[redacted]", line)
        return line

    lines = text.splitlines()
    keywords = [
        "rate limit", "rate-limit", "429", "checkpoint", "challenge_required",
        "temporarily blocked", "please wait", "login_required", "captcha",
        "not authorized", "403", "please try again", "unusual activity",
        "no more items", "reached the end", "finished", "session pool",
        "retiring session", "blocked", "error", "warn",
    ]
    hits: dict[str, int] = {}
    samples: list[str] = []
    for line in lines:
        low = line.lower()
        for kw in keywords:
            if kw in low:
                hits[kw] = hits.get(kw, 0) + 1
                if len(samples) < 25:
                    samples.append(_redact(line)[:300])
                break

    return {
        "run_id": run_id,
        "total_lines": len(lines),
        "keyword_counts": hits,
        "sample_lines": samples,
        "first_line": _redact(lines[0])[:300] if lines else None,
        "last_line": _redact(lines[-1])[:300] if lines else None,
    }


@app.post("/api/admin/apify/test-alt-actor/{handle}")
def temp_test_alt_actor(
    handle: str,
    password: Annotated[str, Form()],
    results_limit: int = 60,
) -> dict[str, Any]:
    """One-off, cheap (small results_limit) test of a different Instagram
    scraper Actor (apify/instagram-post-scraper instead of our usual
    apify~instagram-scraper), to see empirically whether it also gets
    blocked on accounts where the current one does. Does NOT insert
    anything into the database -- purely diagnostic. Costs whatever this
    actor charges for results_limit results (~$1-2.70 per 1000).
    """
    _require_admin(password)
    import httpx

    # Starts the run and returns immediately -- does NOT poll to completion.
    # A 60-item Instagram scrape still takes a couple of minutes, and a
    # client held open that long through Render's proxy is exactly the bug
    # this whole investigation started from. The run/dataset status is
    # publicly readable from Apify without a token, so the caller can poll
    # it directly instead of this endpoint holding a connection open.
    token = os.getenv("APIFY_TOKEN", "").strip()
    payload = {"username": [handle], "resultsLimit": results_limit}
    with httpx.Client(timeout=30.0) as client:
        start = client.post(
            "https://api.apify.com/v2/acts/apify~instagram-post-scraper/runs",
            params={"token": token},
            json=payload,
        )
        start.raise_for_status()
        run = start.json().get("data", {})
    run_id = run.get("id")
    if not run_id:
        raise HTTPException(status_code=502, detail="Alt actor did not return a run id.")

    return {"started": True, "run_id": run_id, "status": run.get("status")}


@app.post("/api/admin/apify/abort-run/{run_id}")
def temp_abort_run(run_id: str, password: Annotated[str, Form()]) -> dict[str, Any]:
    """stop an in-flight Apify run so it stops billing."""
    _require_admin(password)
    import httpx

    token = os.getenv("APIFY_TOKEN", "").strip()
    with httpx.Client(timeout=30.0) as client:
        r = client.post(f"https://api.apify.com/v2/actor-runs/{run_id}/abort", params={"token": token})
    return {"status_code": r.status_code, "body": r.text[:300]}


_ENRICH_RUN: dict[str, Any] = {"running": False, "result": None, "error": None}


def _enrich_worker(max_runs: int, per_run_limit: int) -> None:
    from .apify_sync import enrich_from_existing_runs

    try:
        _ENRICH_RUN["result"] = enrich_from_existing_runs(max_runs=max_runs, per_run_limit=per_run_limit)
    except Exception as exc:
        _ENRICH_RUN["error"] = str(exc)
    finally:
        _ENRICH_RUN["running"] = False


@app.post("/api/admin/apify/enrich")
def temp_enrich(password: Annotated[str, Form()], max_runs: int = 40, per_run_limit: int = 5000) -> dict[str, Any]:
    """backfill the new columns from already-paid Apify datasets.
    Runs in a background thread so a client disconnect can't abort it."""
    _require_admin(password)
    if _ENRICH_RUN["running"]:
        return {"already_running": True, **_ENRICH_RUN}
    _ENRICH_RUN.update({"running": True, "result": None, "error": None})
    threading.Thread(target=_enrich_worker, args=(max_runs, per_run_limit), daemon=True, name="enrich").start()
    return {"started": True, "max_runs": max_runs}


@app.post("/api/admin/apify/enrich-from-run/{run_id}")
def admin_enrich_from_run(run_id: str, password: Annotated[str, Form()]) -> dict[str, Any]:
    """Recovers a specific paid run's dataset into the enrichment columns."""
    _require_admin(password)
    from .apify_sync import enrich_from_run

    try:
        return enrich_from_run(run_id)
    except ApifySyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/admin/sample-enriched")
def admin_sample_enriched(
    password: str,
    limit: int = 10,
    account: str | None = None,
    only_video: bool = False,
    include_raw: bool = False,
) -> dict[str, Any]:
    """Inspection helper: returns fully-enriched rows so the captured data can be
    reviewed without a DB client. raw_json is omitted by default (it's several KB
    per post) but its key list is summarised so you can see everything retained.
    """
    _require_admin(password)

    where = "raw_json IS NOT NULL"
    params: list[Any] = []
    if account:
        where += " AND account = ?"
        params.append(account)
    if only_video:
        where += " AND video_views IS NOT NULL"

    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM dashboard_posts WHERE {where} ORDER BY published_at DESC LIMIT ?",
            (*params, max(1, min(limit, 50))),
        ).fetchall()

    import json as _json

    out: list[dict[str, Any]] = []
    for row in rows:
        post = dict(row)
        raw = post.pop("raw_json", None)
        try:
            parsed = _json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            parsed = {}
        post["_raw_json_keys"] = sorted(parsed)
        post["_raw_json_bytes"] = len(raw or "")
        if include_raw:
            post["_raw_json"] = parsed
        # Long text would drown the output; the point here is field coverage.
        for key in ("caption", "hook_text", "alt_text", "first_comment"):
            if isinstance(post.get(key), str) and len(post[key]) > 160:
                post[key] = post[key][:160] + "…"
        out.append(post)

    return {"count": len(out), "posts": out}


_PROFILE_ENRICH: dict[str, Any] = {"running": False, "account": None, "result": None, "error": None}


def _profile_enrich_worker(account: str, results_limit: int) -> None:
    from .apify_sync import enrich_account_via_profile

    try:
        _PROFILE_ENRICH["result"] = enrich_account_via_profile(account, results_limit=results_limit)
    except Exception as exc:
        _PROFILE_ENRICH["error"] = str(exc)
    finally:
        _PROFILE_ENRICH["running"] = False


@app.post("/api/admin/apify/enrich-profile/{handle}")
def admin_enrich_profile(
    handle: str,
    password: Annotated[str, Form()],
    results_limit: int = 3000,
) -> dict[str, Any]:
    """Enriches one account's back catalogue with a single profile scrape --
    ~11x faster per post than scraping each missing URL individually."""
    _require_admin(password)
    if _PROFILE_ENRICH["running"]:
        return {"already_running": True, **_PROFILE_ENRICH}
    _PROFILE_ENRICH.update({"running": True, "account": handle, "result": None, "error": None})
    threading.Thread(
        target=_profile_enrich_worker, args=(handle, results_limit), daemon=True, name=f"enrich-profile-{handle}"
    ).start()
    return {"started": True, "account": handle, "results_limit": results_limit}


@app.get("/api/admin/apify/enrich-profile-status")
def admin_enrich_profile_status() -> dict[str, Any]:
    return dict(_PROFILE_ENRICH)


@app.get("/api/admin/apify/missing")
def admin_missing_enrichment() -> dict[str, Any]:
    """What still lacks the full Apify payload, and what it would cost to fill."""
    from .apify_sync import missing_enrichment_breakdown

    return missing_enrichment_breakdown()


_SCRAPE_RUN: dict[str, Any] = {"running": False, "result": None, "error": None}


def _scrape_missing_worker(limit: int, account: str | None) -> None:
    from .apify_sync import scrape_missing_enrichment

    try:
        _SCRAPE_RUN["result"] = scrape_missing_enrichment(limit=limit, account=account)
    except Exception as exc:
        _SCRAPE_RUN["error"] = str(exc)
    finally:
        _SCRAPE_RUN["running"] = False


@app.post("/api/admin/apify/scrape-missing")
def admin_scrape_missing(
    password: Annotated[str, Form()],
    limit: int = 200,
    account: str | None = None,
) -> dict[str, Any]:
    """Scrapes ONLY the posts still missing their payload (by exact post URL).
    This spends Apify credits -- roughly $0.0023 per post. Background thread so a
    disconnect can't abort it."""
    _require_admin(password)
    if _SCRAPE_RUN["running"]:
        return {"already_running": True, **_SCRAPE_RUN}
    _SCRAPE_RUN.update({"running": True, "result": None, "error": None})
    threading.Thread(
        target=_scrape_missing_worker, args=(limit, account), daemon=True, name="scrape-missing"
    ).start()
    return {"started": True, "limit": limit, "account": account, "estimated_usd": round(limit * 0.0023, 2)}


@app.get("/api/admin/apify/scrape-missing-status")
def admin_scrape_missing_status() -> dict[str, Any]:
    return dict(_SCRAPE_RUN)


@app.get("/api/admin/apify/enrich-status")
def temp_enrich_status() -> dict[str, Any]:
    """enrichment progress + how much of the DB now has full data."""
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM dashboard_posts").fetchone()["c"]
        enriched = conn.execute("SELECT COUNT(*) c FROM dashboard_posts WHERE raw_json IS NOT NULL").fetchone()["c"]
        with_views = conn.execute(
            "SELECT COUNT(*) c FROM dashboard_posts WHERE video_views IS NOT NULL"
        ).fetchone()["c"]
        with_slides = conn.execute(
            "SELECT COUNT(*) c FROM dashboard_posts WHERE slide_count IS NOT NULL"
        ).fetchone()["c"]
        with_tags = conn.execute("SELECT COUNT(*) c FROM dashboard_posts WHERE hashtags IS NOT NULL").fetchone()["c"]
        with_music = conn.execute("SELECT COUNT(*) c FROM dashboard_posts WHERE music_song IS NOT NULL").fetchone()["c"]
    return {
        "total_posts": total,
        "with_raw_json": enriched,
        "coverage_pct": round(100 * enriched / total, 1) if total else 0,
        "with_video_views": with_views,
        "with_slide_count": with_slides,
        "with_hashtags": with_tags,
        "with_music": with_music,
        **_ENRICH_RUN,
    }


@app.post("/api/admin/apify/import-run/{handle}")
def temp_import_run(handle: str, run_id: str, password: Annotated[str, Form()]) -> dict[str, Any]:
    """import posts from an ALREADY-COMPLETED Apify run instead of
    re-scraping. Apify keeps each run's dataset, so when a scrape succeeded but
    our side never stored the results (a deploy restart killed the request),
    this recovers the data for free rather than paying for the same work twice.
    """
    _require_admin(password)
    import httpx

    from .apify_sync import _account_scope, _insert_new_posts, get_account_config

    token = os.getenv("APIFY_TOKEN", "").strip()
    with httpx.Client(timeout=60.0) as client:
        run = client.get(f"https://api.apify.com/v2/actor-runs/{run_id}", params={"token": token})
        run.raise_for_status()
        data = run.json().get("data", {})
        dataset_id = data.get("defaultDatasetId")
        if not dataset_id:
            raise HTTPException(status_code=404, detail="Run has no dataset.")
        items_res = client.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items", params={"token": token, "format": "json"}
        )
        items_res.raise_for_status()
        items = items_res.json()

    if not isinstance(items, list):
        raise HTTPException(status_code=502, detail="Unexpected dataset shape.")

    cfg = get_account_config(handle)
    table = cfg["table"]
    scope_sql, scope_params = _account_scope(table, handle)
    with connect() as conn:
        existing = {
            r["shortcode"]
            for r in conn.execute(f"SELECT shortcode FROM {table} WHERE 1=1{scope_sql}", scope_params).fetchall()
            if r["shortcode"]
        }

    # Guard against importing the wrong run: everything gets stored under
    # `handle`, so a dataset belonging to another profile would silently
    # corrupt this account's history. Only accept items whose ownerUsername
    # matches (items without the field are kept -- some payloads omit it).
    target = cfg["handle"].lower()
    owners = {str(i.get("ownerUsername") or "").lower() for i in items if i.get("ownerUsername")}
    foreign = {o for o in owners if o != target}
    if owners and target not in owners:
        raise HTTPException(
            status_code=409,
            detail=f"Dataset belongs to {sorted(owners)}, not '{target}'. Refusing to import.",
        )
    items = [i for i in items if str(i.get("ownerUsername") or target).lower() == target]

    new_items = [i for i in items if i.get("shortCode") and i["shortCode"] not in existing]
    new_items.sort(key=lambda i: i.get("timestamp") or "")
    result = _insert_new_posts(handle, cfg, new_items)
    return {
        "run_status": data.get("status"),
        "dataset_items": len(items),
        "skipped_foreign": sorted(foreign),
        "already_had": len(existing),
        "new": len(new_items),
        "result": result,
    }


@app.post("/api/admin/accounts/backfill-bg/{handle}")
def temp_backfill_bg(
    handle: str,
    password: Annotated[str, Form()],
    results_limit: Annotated[int, Form()] = 2000,
    date_from: Annotated[str | None, Form()] = None,
    date_to: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Runs a backfill in a background thread so a client disconnect (or a slow
    scrape) can't abort it.

    This is the endpoint the add-account wizard uses. The synchronous one
    cannot survive a full history import: the scrape routinely runs for
    minutes, far longer than the proxy in front of this service will hold an
    idle connection open, so the request dies and the account is left sitting
    at zero posts with nothing to explain why.

    Takes the same date_from/date_to as the synchronous route, otherwise a
    date-ranged import would silently widen to everything once the wizard
    switched over.
    """
    _require_admin(password)
    if _BACKFILL_RUN["running"]:
        return {"already_running": True, **_BACKFILL_RUN}
    _BACKFILL_RUN.update({"running": True, "handle": handle, "result": None, "error": None})
    threading.Thread(
        target=_backfill_worker,
        args=(handle, results_limit, date_from or None, date_to or None),
        daemon=True,
        name=f"backfill-{handle}",
    ).start()
    return {"started": True, "handle": handle, "results_limit": results_limit}


@app.get("/api/admin/accounts/backfill-status")
def temp_backfill_status() -> dict[str, Any]:
    """progress of the background backfill."""
    return dict(_BACKFILL_RUN)


@app.post("/api/admin/ocr/start")
def temp_ocr_start(
    password: Annotated[str, Form()],
    batch_size: int = 100,
    max_batches: int = 200,
    workers: int = 3,
) -> dict[str, Any]:
    """kick off the background OCR sweep. `workers` threads run in
    parallel; row claiming is serialized so they never process the same cover
    twice. Always OCRs the full cover image via Sentient Dash's own worker
    (sentient_ocr.py) -- no crop-region option here anymore. Remove after use.
    """
    _require_admin(password)
    from .apify_sync import reset_stuck_ocr_claims

    with _OCR_RUN_LOCK:
        if _OCR_RUN["running"]:
            return {"already_running": True, **_OCR_RUN}
        released = reset_stuck_ocr_claims()
        workers = max(1, min(workers, 6))
        _OCR_RUN.update(
            {
                "running": True,
                "done": 0,
                "with_text": 0,
                "skipped": 0,
                "batches": 0,
                "error": None,
                "started": utc_now(),
                "workers_live": workers,
            }
        )
    size = max(1, min(batch_size, 100))
    for i in range(workers):
        threading.Thread(target=_ocr_worker, args=(size, max_batches), daemon=True, name=f"ocr-sweep-{i}").start()
    return {"started": True, "batch_size": size, "workers": workers, "released": released}


@app.get("/api/admin/ocr/status")
def temp_ocr_status() -> dict[str, Any]:
    """progress of the background OCR sweep."""
    with connect() as conn:
        # Mirrors run_ocr_sweep's own queue: rows with no local cover file are
        # no longer excluded (the sweep re-downloads them). Scoped to
        # dashboard_posts, matching the sweep: the frozen `posts` table is not
        # processed, so counting it would show a backlog that never drains.
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM dashboard_posts "
            "WHERE TRIM(COALESCE(hook_text,''))='' AND ocr_checked=0"
        ).fetchone()["c"]
        done = conn.execute(
            "SELECT COUNT(*) AS c FROM dashboard_posts "
            "WHERE TRIM(COALESCE(hook_text,'')) NOT IN ('','-','~')"
        ).fetchone()["c"]
    return {"remaining": remaining, "with_text_total": done, **_OCR_RUN}


# The preview endpoint is deliberately unauthenticated -- the add-account wizard
# shows a live profile picture in step 1, before the user has entered the refresh
# password. But every call spends Apify credits, so without a throttle it's an
# open tap anyone with the URL could run up. Cache repeat lookups of the same
# handle and cap the global rate.
_PREVIEW_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_PREVIEW_CACHE_TTL = 600.0  # seconds
_PREVIEW_CALLS: list[float] = []
# The wizard fires one lookup per debounced pause while you type, and a couple
# of retries after a miss burns through a tight budget fast -- at which point
# the 429 was surfaced as "couldn't find that account". Twenty a minute is
# still bounded (~$0.046/min worst case) and no longer trips during normal use.
_PREVIEW_MAX_PER_MIN = 20
_PREVIEW_LOCK = threading.Lock()


@app.get("/api/admin/accounts/preview")
def admin_preview_account(handle: str) -> dict[str, Any]:
    """Read-only lookup used by the add-account wizard's first step to show
    the real Instagram profile picture/name/follower count for a handle
    before the account is actually created. No password required (nothing
    is written), but rate-limited and cached because it costs Apify credits.
    """
    key = handle.strip().lstrip("@").lower()
    if not key:
        raise HTTPException(status_code=400, detail="Handle is required.")

    now = time.monotonic()
    with _PREVIEW_LOCK:
        cached = _PREVIEW_CACHE.get(key)
        if cached and now - cached[0] < _PREVIEW_CACHE_TTL:
            return cached[1]
        # Typing a handle fires several debounced lookups, so allow a small
        # burst but refuse sustained hammering.
        _PREVIEW_CALLS[:] = [t for t in _PREVIEW_CALLS if now - t < 60.0]
        if len(_PREVIEW_CALLS) >= _PREVIEW_MAX_PER_MIN:
            raise HTTPException(status_code=429, detail="Too many lookups. Try again in a moment.")
        _PREVIEW_CALLS.append(now)

    try:
        result = fetch_profile_preview(handle)
    except ApifySyncError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    with _PREVIEW_LOCK:
        _PREVIEW_CACHE[key] = (time.monotonic(), result)
    return result


@app.post("/api/admin/accounts")
def admin_create_account(
    password: Annotated[str, Form()],
    handle: Annotated[str, Form()],
    label: Annotated[str, Form()] = "",
    group: Annotated[str, Form()] = "competitors",
    hot_threshold: Annotated[int, Form()] = 600,
) -> dict[str, Any]:
    """Self-serve account creation: register a new IG handle under Sentient
    or Competitors, no code changes or redeploy required. Always
    non-canonical -- writes into the generic dashboard_posts table, never
    Predict's `posts`. Automatically picked up by the scheduler on its next
    tick; call the backfill endpoint below afterward to seed initial history.
    """
    if not TRICKS_DASH_REFRESH_PASSWORD or not secrets.compare_digest(
        password.strip(), TRICKS_DASH_REFRESH_PASSWORD
    ):
        raise HTTPException(status_code=401, detail="Incorrect refresh password.")
    try:
        account = create_account(handle, label, group, hot_threshold)
    except ApifySyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"account": account}


@app.post("/api/admin/accounts/{handle}/settings")
def admin_update_account_settings(
    handle: str,
    password: Annotated[str, Form()],
    hot_threshold: Annotated[int | None, Form()] = None,
    label: Annotated[str | None, Form()] = None,
    group: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Edit an account's tunables from the dashboard's Settings panel. Only the
    fields actually supplied are changed, so the panel can PATCH one value at a
    time without having to resend the whole account.
    """
    if not TRICKS_DASH_REFRESH_PASSWORD or not secrets.compare_digest(
        password.strip(), TRICKS_DASH_REFRESH_PASSWORD
    ):
        raise HTTPException(status_code=401, detail="Incorrect refresh password.")

    try:
        get_account_config(handle)
    except ApifySyncError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    updates: list[str] = []
    params: list[Any] = []
    if hot_threshold is not None:
        if hot_threshold < 1:
            raise HTTPException(status_code=400, detail="hot_threshold must be at least 1.")
        updates.append("hot_threshold = ?")
        params.append(int(hot_threshold))
    if label is not None and label.strip():
        updates.append("label = ?")
        params.append(label.strip())
    if group is not None:
        if group not in VALID_GROUPS:
            raise HTTPException(status_code=400, detail=f"group must be one of {VALID_GROUPS}.")
        updates.append("group_name = ?")
        params.append(group)

    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update.")

    updates.append("updated_at = ?")
    params.append(utc_now())
    params.append(handle)
    with connect() as conn:
        conn.execute(f"UPDATE accounts SET {', '.join(updates)} WHERE handle = ?", params)

    return {"ok": True, "account": get_account_config(handle)}


@app.post("/api/admin/accounts/{handle}/deactivate")
def admin_deactivate_account(handle: str, password: Annotated[str, Form()]) -> dict[str, Any]:
    """Soft-disable an account: stops the scheduler from touching it and
    hides it from the public roster, without deleting its post history.
    """
    if not TRICKS_DASH_REFRESH_PASSWORD or not secrets.compare_digest(
        password.strip(), TRICKS_DASH_REFRESH_PASSWORD
    ):
        raise HTTPException(status_code=401, detail="Incorrect refresh password.")
    if handle == "chatgptricks":
        raise HTTPException(status_code=400, detail="Cannot deactivate the canonical account.")
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE accounts SET is_active = 0, updated_at = ? WHERE handle = ?",
            (utc_now(), handle),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Unknown account.")
    return {"ok": True, "handle": handle, "deactivated": True}


@app.post("/api/admin/accounts/{handle}/activate")
def admin_activate_account(handle: str, password: Annotated[str, Form()]) -> dict[str, Any]:
    """Symmetric to /deactivate: brings a soft-disabled account back into the
    scheduler and the public roster. Post history was never touched by
    deactivation, so there's nothing to restore -- just the flag.
    """
    if not TRICKS_DASH_REFRESH_PASSWORD or not secrets.compare_digest(
        password.strip(), TRICKS_DASH_REFRESH_PASSWORD
    ):
        raise HTTPException(status_code=401, detail="Incorrect refresh password.")
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE accounts SET is_active = 1, updated_at = ? WHERE handle = ?",
            (utc_now(), handle),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Unknown account.")
    return {"ok": True, "handle": handle, "activated": True}


@app.get("/api/admin/disk-status")
def admin_disk_status() -> dict[str, Any]:
    """Current usage of the persistent data volume -- the same numbers the
    Slack disk-warning alert (scheduler.py) is based on, surfaced here so the
    admin panel can show the picture without waiting for a threshold crossing.
    """
    import shutil

    usage = shutil.disk_usage(str(DATA_DIR))
    pct = (usage.used / usage.total * 100) if usage.total else 0.0
    return {
        "used_mb": round(usage.used / 1e6, 1),
        "total_mb": round(usage.total / 1e6, 1),
        "free_mb": round(usage.free / 1e6, 1),
        "pct_used": round(pct, 1),
    }


@app.post("/api/admin/accounts/{handle}/backfill")
def admin_backfill_account(
    handle: str,
    password: Annotated[str, Form()],
    results_limit: Annotated[int, Form()] = 2000,
    date_from: Annotated[str | None, Form()] = None,
    date_to: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """One-time initial history import for a freshly self-serve-added
    account, so it has a real post history before the scheduler starts
    incrementally refreshing it. date_from/date_to (YYYY-MM-DD) are optional
    -- omit both for "all posts" (bounded only by results_limit).
    """
    if not TRICKS_DASH_REFRESH_PASSWORD or not secrets.compare_digest(
        password.strip(), TRICKS_DASH_REFRESH_PASSWORD
    ):
        raise HTTPException(status_code=401, detail="Incorrect refresh password.")
    try:
        return run_backfill(handle, results_limit=results_limit, date_from=date_from or None, date_to=date_to or None)
    except ApifySyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/admin/accounts/{handle}/avatar")
def admin_fetch_avatar(
    handle: str,
    password: Annotated[str, Form()],
    image_url: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Caches the account's real Instagram profile picture locally (served
    via GET /api/dashboard/avatar/{handle}). Pass image_url when it's
    already known (e.g. from the add-account wizard's own preview fetch)
    to skip a redundant Apify call; omit it to look the picture up fresh.
    """
    if not TRICKS_DASH_REFRESH_PASSWORD or not secrets.compare_digest(
        password.strip(), TRICKS_DASH_REFRESH_PASSWORD
    ):
        raise HTTPException(status_code=401, detail="Incorrect refresh password.")
    try:
        if image_url:
            store_avatar_from_url(handle, image_url)
        else:
            fetch_and_store_avatar(handle)
        return {"ok": True, "handle": handle}
    except ApifySyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/dashboard/avatar/{handle}")
def dashboard_avatar(handle: str) -> FileResponse:
    """Serves a cached local copy of the account's profile picture."""
    with connect() as conn:
        row = conn.execute("SELECT avatar_path FROM accounts WHERE handle = ?", (handle,)).fetchone()
    if not row or not row["avatar_path"]:
        raise HTTPException(status_code=404, detail="No profile picture cached for this account.")
    avatar_path = Path(str(row["avatar_path"]))
    if not avatar_path.is_file():
        raise HTTPException(status_code=404, detail="Profile picture file is unavailable.")
    return FileResponse(avatar_path)


@app.post("/api/admin/reset-hot-check")
def reset_hot_check(password: Annotated[str, Form()], account: Annotated[str, Form()]) -> dict[str, Any]:
    """One-off utility: clears hot_checked (and the derived is_hot/likes_at_1h/
    hot_rate_multiplier fields) for posts still <=24h old, so the next
    short-term cycle re-evaluates them under the current HOT-detection
    rules/threshold. Useful after tuning the threshold or check formula so
    already-checked recent posts aren't stuck with a stale verdict.
    """
    if not TRICKS_DASH_REFRESH_PASSWORD or not secrets.compare_digest(
        password.strip(), TRICKS_DASH_REFRESH_PASSWORD
    ):
        raise HTTPException(status_code=401, detail="Incorrect refresh password.")
    try:
        cfg = get_account_config(account)
    except ApifySyncError as exc:
        raise HTTPException(status_code=400, detail="Unknown account.") from exc
    table = cfg["table"]
    scope_sql = " AND account = ?" if table == "dashboard_posts" else ""
    scope_params: tuple[Any, ...] = (account,) if table == "dashboard_posts" else ()

    from datetime import UTC, datetime

    with connect() as conn:
        rows = conn.execute(
            f"SELECT id, published_at FROM {table} WHERE hot_checked = 1{scope_sql}", scope_params
        ).fetchall()

    now = datetime.now(UTC)
    reset_ids: list[int] = []
    for row in rows:
        published_at = row["published_at"]
        if not published_at:
            continue
        try:
            dt = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        age_hours = (now - dt).total_seconds() / 3600.0
        if age_hours <= 24:
            reset_ids.append(row["id"])

    if reset_ids:
        placeholders = ", ".join("?" for _ in reset_ids)
        with connect() as conn:
            conn.execute(
                f"UPDATE {table} SET hot_checked = 0, is_hot = 0, likes_at_1h = NULL, "
                f"hot_marked_at = NULL, hot_rate_multiplier = NULL WHERE id IN ({placeholders})",
                reset_ids,
            )

    return {"account": account, "reset": len(reset_ids)}


