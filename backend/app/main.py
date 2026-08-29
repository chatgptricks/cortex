from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
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
    get_dashboard_user_access,
    get_dashboard_user_role,
    get_usage_summary,
    init_db,
    list_account_snapshots,
    list_dashboard_users,
    list_account_lists,
    log_usage_event,
    remove_dashboard_user,
    seed_dashboard_users_from_env,
    seed_queue_role_roster,
    upsert_dashboard_user,
    utc_now,
    upsert_account_list,
)
from .sentient_ocr import sentient_ocr_status
from .scheduler import start_scheduler
from .queue_rules import (
    SCHEDULER_END,
    SCHEDULER_START,
    SCHEDULER_TIMEZONE,
    intervals_conflict,
    next_available_slot,
    schedule_absolute,
)


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
_FIREBASE_OPEN_PREFIXES = ("/api/dashboard/covers/", "/api/dashboard/avatar/", "/api/admin/alert-image/")
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
    access = get_dashboard_user_access(email)
    if access is None:
        return JSONResponse(
            {"detail": "This Google account is not authorized for Sentient Dash."}, status_code=403
        )
    is_admin = bool(access["is_admin"])
    request.state.user_email = email
    request.state.is_admin = is_admin
    request.state.operating_role = access["operating_role"]
    try:
        request.state.operating_roles = json.loads(access.get("operating_roles") or "[]")
    except json.JSONDecodeError:
        request.state.operating_roles = []
    if not request.state.operating_roles:
        request.state.operating_roles = [access["operating_role"]]
    # Dev is a private role for Esteban. Keep this separate from the active
    # operating role so a preview cannot make the role switcher disappear.
    request.state.is_dev = email == "esteban@sentientagency.io" and "dev" in request.state.operating_roles
    # Esteban's Dev role can safely preview a restricted operating role. This
    # is deliberately a reduction of privileges, never an escalation.
    preview_role = request.headers.get("x-queue-role-preview", "").strip().lower()
    if request.state.is_dev and preview_role in {"sales", "pd", "vc", "admin"}:
        request.state.operating_roles = [preview_role]
        request.state.is_admin = preview_role == "admin"
    if path.startswith("/api/admin/") and not request.state.is_admin:
        return JSONResponse({"detail": "Admin access required."}, status_code=403)
    request.state.user_uid = decoded.get("uid")
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
    seed_queue_role_roster()
    repaired = _queue_v2_reflow_all_schedules()
    if repaired:
        logging.getLogger(__name__).info("Queue startup repair reflowed %s scheduled request(s)", repaired)
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


@app.post("/api/auth/custom-token")
def auth_custom_token(request: Request) -> dict[str, Any]:
    """Mints a short-lived Firebase custom token for the caller's own,
    already-verified account.

    Sentient Dash is split across several real subdomains (hot.sentientdash.app,
    tracker.sentientdash.app, this Queue page, etc.) plus tracker.html/insights.html
    served from the root domain. Firebase's own session persistence is scoped
    per *origin*, so signing in on one subdomain does nothing for the others --
    hence "I have to log in again on every page." A custom token lets a page
    that already has no local session silently re-establish one (via
    signInWithCustomToken) for the exact same Firebase user, without a second
    Google prompt. See src/firebase.js's SSO helpers for the client half --
    they stash this in a `.sentientdash.app`-scoped cookie so every subdomain
    can read it.

    Requires the caller to already be signed in on *some* origin (this route
    is not in _FIREBASE_OPEN_PATHS, so the middleware above already verified
    their ID token and populated request.state.user_uid before this runs).
    """
    uid = getattr(request.state, "user_uid", None)
    if not uid:
        raise HTTPException(status_code=401, detail="Sign in required.")
    if FIREBASE_APP is None:
        raise HTTPException(status_code=503, detail="Firebase auth is not configured on this server.")
    token = firebase_auth.create_custom_token(uid, app=FIREBASE_APP)
    return {"customToken": token.decode("utf-8") if isinstance(token, bytes) else token}


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
        "operating_role": getattr(request.state, "operating_role", "sales"),
        "operating_roles": getattr(request.state, "operating_roles", [getattr(request.state, "operating_role", "sales")]),
        "is_dev": bool(getattr(request.state, "is_dev", False)),
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


# Same fixed offset the daily snapshot job runs on (see scheduler.py's
# _CST) -- used purely to decide which calendar day a snapshot belongs to,
# so "N day growth" lines up with the Historical Stats table's day rows
# regardless of what wall-clock time a manual refresh happens to run at.
_TRACKER_TZ = timezone(timedelta(hours=-6))


def _snapshot_local_date(snapshot: dict[str, Any]):
    captured_at = snapshot.get("captured_at")
    if not captured_at:
        return None
    return datetime.fromisoformat(captured_at).astimezone(_TRACKER_TZ).date()


def _collapse_to_last_per_day(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`snapshots` must be sorted oldest -> newest. Collapses to one entry
    per calendar day -- that day's LAST snapshot -- oldest -> newest, the
    same rule the Tracker page's own collapseToLastPerDay applies
    client-side to the Historical Stats table and growth chart. Growth
    figures below are computed from this instead of the raw per-snapshot
    history so a manual refresh mid-day can't inflate "1 day growth" by
    comparing against an earlier-than-final snapshot from the day before."""
    by_day: dict[Any, dict[str, Any]] = {}
    for snap in snapshots:
        day = _snapshot_local_date(snap)
        if day is None:
            continue
        by_day[day] = snap
    return [by_day[day] for day in sorted(by_day)]


def _tracker_delta(day_snapshots: list[dict[str, Any]], days: int) -> dict[str, Any] | None:
    """Growth over `days` calendar days: latest day's snapshot vs. the
    closest day at-or-before (latest day - `days`), both taken from the
    already day-collapsed list. `day_snapshots` must be sorted oldest ->
    newest (see _collapse_to_last_per_day)."""
    if not day_snapshots:
        return None
    latest = day_snapshots[-1]
    if latest.get("followers_count") is None:
        return None
    cutoff_date = _snapshot_local_date(latest) - timedelta(days=days)
    baseline = None
    for snap in day_snapshots:
        if _snapshot_local_date(snap) <= cutoff_date:
            baseline = snap
        else:
            break
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
        day_snaps = _collapse_to_last_per_day(snaps)
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
                "delta_1d": _tracker_delta(day_snaps, 1),
                "delta_7d": _tracker_delta(day_snaps, 7),
                "delta_30d": _tracker_delta(day_snaps, 30),
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


@app.post("/api/tracker/accounts/{handle}/refresh")
def tracker_account_refresh(handle: str) -> dict[str, Any]:
    """Manually re-scrapes one account's Instagram profile right now, for
    the Tracker page's per-account refresh button. Open to any signed-in
    user, same tier as the rest of /api/tracker/* -- one lightweight Apify
    call (~$0.002), so there's no reason to gate it behind admin."""
    from .apify_sync import ApifySyncError, snapshot_one_account

    clean = handle.strip().lstrip("@").lower()
    known = {a["handle"] for a in list_accounts(active_only=True)}
    if clean not in known:
        raise HTTPException(status_code=404, detail=f"Unknown or inactive account '{clean}'.")
    try:
        preview = snapshot_one_account(clean)
    except ApifySyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "ok": True,
        "handle": clean,
        "followers": preview.get("followers_count"),
        "posts_count": preview.get("posts_count"),
        "following_count": preview.get("following_count"),
        "full_name": preview.get("full_name"),
        "verified": bool(preview.get("verified")),
        "private": bool(preview.get("private")),
        "captured_at": utc_now(),
    }


@app.post("/api/tracker/snapshot-now")
def tracker_snapshot_now() -> dict[str, Any]:
    """Manually runs the same per-account Apify profile scrape the daily 7am
    CST job runs, for the Tracker page's own overview "refresh all" button.
    Open to any signed-in user, same tier as the rest of /api/tracker/*.
    Mirrors /api/admin/tracker/snapshot-now (kept as-is for the admin panel's
    System tab) rather than reusing it, so this one stays reachable without
    admin rights. Costs one lightweight Apify call per active account."""
    from .apify_sync import snapshot_all_accounts

    result = snapshot_all_accounts()
    return {"ok": True, **result}


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
        queue_rows = conn.execute(
            """SELECT id, post_account, post_shortcode, status, designer_email, coordinator_email,
                      production_points, actual_started_at, completed_at, final_permalink
               FROM queue_requests"""
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
    # Source-post workflow state is useful while a coordinator researches;
    # attribution is attached to the eventual published post by permalink.
    queue_by_source = {(row["post_account"], row["post_shortcode"]): dict(row) for row in queue_rows}
    def _normal_permalink(value: str | None) -> str:
        return (value or "").strip().rstrip("/").split("?")[0]
    queue_by_final = {_normal_permalink(row["final_permalink"]): dict(row) for row in queue_rows if row["final_permalink"]}
    for post in posts:
        source = queue_by_source.get((post.get("account"), post.get("shortcode")))
        if source:
            post["queueState"] = source["status"]
            post["queueRequestId"] = source["id"]
        closed = queue_by_final.get(_normal_permalink(post.get("permalink")))
        if closed:
            post["queueAttribution"] = {
                "requestId": closed["id"], "designerEmail": closed["designer_email"],
                "coordinatorEmail": closed["coordinator_email"], "productionPoints": closed["production_points"],
                "actualStartedAt": closed["actual_started_at"], "completedAt": closed["completed_at"],
            }
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


# --- Sentient Queue --------------------------------------------------------
#
# A post can be assigned to many people, so a Queue task is an *assignment*,
# not a property of the post itself.  Every task carries its own state and
# metadata; moving Ana's task to Posted must never close Luis's task for the
# same Instagram post.
QUEUE_STATUSES = {"queue", "in_progress", "posted"}
QUEUE_PRIORITIES = {"low", "medium", "high", "urgent"}
QUEUE_TAGS = {"content", "design", "copy", "research", "review", "repurpose"}


def _queue_status(value: str) -> str:
    clean = value.strip().lower()
    if clean not in QUEUE_STATUSES:
        raise HTTPException(status_code=400, detail="Status must be queue, in_progress, or posted.")
    return clean


def _queue_priority(value: str | None) -> str | None:
    clean = (value or "").strip().lower()
    if not clean:
        return None
    if clean not in QUEUE_PRIORITIES:
        raise HTTPException(status_code=400, detail="Invalid queue priority.")
    return clean


def _queue_due_date(value: str | None) -> str | None:
    clean = (value or "").strip()
    if not clean:
        return None
    try:
        datetime.strptime(clean, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Due date must use YYYY-MM-DD.") from exc
    return clean


def _queue_tags(value: str | None) -> list[str]:
    # The dashboard posts a compact comma-separated FormData value.  Tags are
    # deliberately a fixed vocabulary in v1 so board filters remain useful
    # instead of turning into dozens of almost-identical spellings.
    raw = (value or "").split(",")
    tags = list(dict.fromkeys(tag.strip().lower() for tag in raw if tag.strip()))
    invalid = [tag for tag in tags if tag not in QUEUE_TAGS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown queue tag(s): {', '.join(invalid)}")
    return tags


def _queue_assignee_emails(value: str) -> list[str]:
    emails = list(dict.fromkeys(email.strip().lower() for email in value.split(",") if email.strip()))
    if not emails:
        raise HTTPException(status_code=400, detail="Choose at least one person.")
    known = {user["email"] for user in list_dashboard_users()}
    unknown = [email for email in emails if email not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown queue user(s): {', '.join(unknown)}")
    return emails


def _queue_recommended_account(value: str | None) -> str | None:
    """Normalizes the optional destination account for an assignment.

    Only active Sentient accounts are valid targets. The post's source account
    is intentionally not used here: a task can recommend a post found on one
    account for a different Sentient account.
    """
    clean = (value or "").strip().lstrip("@").lower()
    if not clean:
        return None
    targets = {
        account["handle"]
        for account in list_accounts(active_only=True)
        if account["group"] == "sentient"
    }
    if clean not in targets:
        raise HTTPException(status_code=400, detail="Recommended account must be an active Sentient account.")
    return clean


def _queue_post_exists(account: str, shortcode: str) -> tuple[str, str]:
    clean_account = account.strip().lstrip("@").lower()
    clean_shortcode = shortcode.strip()
    if not clean_shortcode:
        raise HTTPException(status_code=400, detail="Post shortcode is required.")
    table = _resolve_post_table(clean_account)
    with connect() as conn:
        if table == "posts":
            row = conn.execute("SELECT id FROM posts WHERE shortcode = ?", (clean_shortcode,)).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM dashboard_posts WHERE account = ? AND shortcode = ?",
                (clean_account, clean_shortcode),
            ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Post not found.")
    return clean_account, clean_shortcode


def _queue_post_id(account: str, shortcode: str) -> int | None:
    """Returns the ID used by the public cover route for a Queue post."""
    table = _resolve_post_table(account)
    with connect() as conn:
        if table == "posts":
            row = conn.execute("SELECT id FROM posts WHERE shortcode = ?", (shortcode,)).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM dashboard_posts WHERE account = ? AND shortcode = ?",
                (account, shortcode),
            ).fetchone()
    return int(row["id"]) if row else None


def _queue_rows(assignee: str | None, include_posted: bool) -> list[dict[str, Any]]:
    """Returns assignments with a lightweight current post projection.

    The joins are intentionally left joins: an assignment remains useful as
    history even if a post was subsequently removed from the live dashboard.
    """
    canonical = next((a for a in list_accounts(active_only=False) if a["is_canonical"]), None)
    canonical_handle = canonical["handle"] if canonical else ""
    clauses: list[str] = []
    params: list[Any] = [canonical_handle]
    if assignee:
        clauses.append("q.assignee_email = ?")
        params.append(assignee)
    if not include_posted:
        # A task freshly marked "posted" is still worth seeing -- it's how
        # you confirm the thing you just published actually shows up. It
        # only drops out of the default view once it's been sitting there a
        # full day; before that, both a full archive query and the default
        # query return it.
        clauses.append("(q.status != 'posted' OR q.updated_at >= ?)")
        params.append((datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="seconds"))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT q.*, u.role AS assignee_role,
                   dp.id AS dashboard_post_id, dp.caption AS dashboard_caption,
                   dp.permalink AS dashboard_permalink, dp.published_at AS dashboard_published_at,
                   dp.likes AS dashboard_likes, dp.comments AS dashboard_comments,
                   dp.post_type_label AS dashboard_post_type,
                   dp.music_song AS dashboard_music_song, dp.music_artist AS dashboard_music_artist,
                   dp.music_audio_id AS dashboard_music_audio_id,
                   dp.uses_original_audio AS dashboard_uses_original_audio,
                   p.id AS canonical_post_id, p.caption AS canonical_caption,
                   p.title AS canonical_title, p.published_at AS canonical_published_at,
                   p.likes AS canonical_likes, p.comments AS canonical_comments,
                   p.post_type_label AS canonical_post_type
            FROM post_assignments q
            LEFT JOIN dashboard_users u ON u.email = q.assignee_email
            LEFT JOIN dashboard_posts dp
              ON dp.account = q.post_account AND dp.shortcode = q.post_shortcode
            LEFT JOIN posts p
              ON q.post_account = ? AND p.shortcode = q.post_shortcode
            {where}
            ORDER BY
              CASE q.status WHEN 'queue' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
              q.position ASC,
              q.updated_at DESC
            """,
            params,
        ).fetchall()

    assignments: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        tags_raw = row.get("tags") or "[]"
        try:
            tags = json.loads(tags_raw)
            tags = tags if isinstance(tags, list) else []
        except json.JSONDecodeError:
            tags = []
        dashboard_id = row.get("dashboard_post_id")
        canonical_id = row.get("canonical_post_id")
        post_id = dashboard_id if dashboard_id is not None else canonical_id
        caption = row.get("dashboard_caption") if dashboard_id is not None else (row.get("canonical_caption") or row.get("canonical_title"))
        permalink = row.get("dashboard_permalink") or f"https://www.instagram.com/p/{row['post_shortcode']}/"
        assignments.append(
            {
                "id": row["id"],
                "assigneeEmail": row["assignee_email"],
                "assigneeRole": row.get("assignee_role"),
                "status": row["status"],
                "note": row["note"],
                "priority": row["priority"],
                "dueDate": row["due_date"],
                "recommendedAccount": row.get("recommended_account"),
                "tags": tags,
                "position": row["position"],
                "createdByEmail": row["created_by_email"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "post": {
                    "account": row["post_account"],
                    "shortcode": row["post_shortcode"],
                    "caption": caption or "",
                    "permalink": permalink,
                    "publishedAt": row.get("dashboard_published_at") if dashboard_id is not None else row.get("canonical_published_at"),
                    "likes": row.get("dashboard_likes") if dashboard_id is not None else row.get("canonical_likes"),
                    "comments": row.get("dashboard_comments") if dashboard_id is not None else row.get("canonical_comments"),
                    "type": row.get("dashboard_post_type") if dashboard_id is not None else row.get("canonical_post_type"),
                    "coverUrl": (
                        f"/api/dashboard/covers/{row['post_account']}/{post_id}" if post_id is not None else None
                    ),
                    "missing": post_id is None,
                    # Music metadata only exists on dashboard_posts (canonical
                    # posts predate that column) -- deep-diving an assignment
                    # from before the dashboard migration just shows no song,
                    # which matches what the main dashboard itself would show.
                    "musicSong": row.get("dashboard_music_song") if dashboard_id is not None else None,
                    "musicArtist": row.get("dashboard_music_artist") if dashboard_id is not None else None,
                    "usesOriginalAudio": bool(row.get("dashboard_uses_original_audio")) if dashboard_id is not None else False,
                    "musicUrl": (
                        f"https://www.instagram.com/reels/audio/{row['dashboard_music_audio_id']}/"
                        if dashboard_id is not None and row.get("dashboard_music_audio_id")
                        else None
                    ),
                },
            }
        )
    return assignments


def _queue_metrics(assignments: list[dict[str, Any]]) -> dict[str, Any]:
    by_status = {status: 0 for status in QUEUE_STATUSES}
    by_user: dict[str, dict[str, Any]] = {}
    for assignment in assignments:
        by_status[assignment["status"]] += 1
        email = assignment["assigneeEmail"]
        row = by_user.setdefault(email, {"email": email, "queue": 0, "inProgress": 0, "posted": 0, "pending": 0})
        if assignment["status"] == "queue":
            row["queue"] += 1
            row["pending"] += 1
        elif assignment["status"] == "in_progress":
            row["inProgress"] += 1
            row["pending"] += 1
        else:
            row["posted"] += 1
    return {
        "total": len(assignments),
        "queue": by_status["queue"],
        "inProgress": by_status["in_progress"],
        "posted": by_status["posted"],
        "pending": by_status["queue"] + by_status["in_progress"],
        "byUser": sorted(by_user.values(), key=lambda row: (-row["pending"], row["email"])),
    }


def _queue_editable_assignment(task_id: int, caller_email: str, is_admin: bool) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM post_assignments WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Queue task not found.")
    assignment = dict(row)
    if not is_admin and assignment["assignee_email"] != caller_email:
        raise HTTPException(status_code=403, detail="You can only update your own Queue tasks.")
    return assignment


def _queue_log_event(conn: Any, assignment_id: int, actor_email: str, event_type: str, details: dict[str, Any] | None = None) -> None:
    conn.execute(
        """
        INSERT INTO post_assignment_events (assignment_id, actor_email, event_type, details, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (assignment_id, actor_email, event_type, json.dumps(details or {}), utc_now()),
    )


def _queue_history(task_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT actor_email, event_type, details, created_at
            FROM post_assignment_events
            WHERE assignment_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (task_id,),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        try:
            details = json.loads(row["details"] or "{}")
        except json.JSONDecodeError:
            details = {}
        events.append({
            "actorEmail": row["actor_email"],
            "type": row["event_type"],
            "details": details if isinstance(details, dict) else {},
            "createdAt": row["created_at"],
        })
    return events


@app.get("/api/dashboard/queue")
def dashboard_queue(
    request: Request,
    assignee: Annotated[str | None, Query()] = None,
    include_posted: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Queue board data. Admins can request everyone or one person; every
    other signed-in user is always scoped to their own independent tasks."""
    caller = _caller_email(request)
    is_admin = bool(getattr(request.state, "is_admin", False))
    requested = (assignee or "").strip().lower()
    if not requested:
        scope = None if is_admin else caller
    elif requested == "all":
        if not is_admin:
            raise HTTPException(status_code=403, detail="Only admins can view the team Queue.")
        scope = None
    else:
        if not is_admin and requested != caller:
            raise HTTPException(status_code=403, detail="You can only view your own Queue.")
        scope = requested

    assignments = _queue_rows(scope, include_posted)
    all_assignments = _queue_rows(None if is_admin else caller, True)
    return {
        "viewer": {"email": caller, "isAdmin": is_admin},
        "scope": "all" if scope is None else scope,
        "users": list_dashboard_users(),
        "assignments": assignments,
        "metrics": _queue_metrics(all_assignments),
        "tagOptions": sorted(QUEUE_TAGS),
        "priorityOptions": ["low", "medium", "high", "urgent"],
        "recommendedAccounts": [
            {"handle": account["handle"], "label": account["label"]}
            for account in list_accounts(active_only=True)
            if account["group"] == "sentient"
        ],
    }


@app.get("/api/dashboard/queue/summary")
def dashboard_queue_summary(request: Request) -> dict[str, Any]:
    """Small count for the Queue badge in the main dashboard header."""
    assignments = _queue_rows(_caller_email(request), False)
    return _queue_metrics(assignments)


@app.get("/api/dashboard/queue/users")
def dashboard_queue_users(request: Request) -> dict[str, Any]:
    """The assignment picker roster. Non-admins receive only themselves so
    the UI never implies they can assign work to another teammate."""
    caller = _caller_email(request)
    is_admin = bool(getattr(request.state, "is_admin", False))
    users = list_dashboard_users()
    if not is_admin:
        users = [user for user in users if user["email"] == caller]
    return {"users": users, "viewer": {"email": caller, "isAdmin": is_admin}}


@app.get("/api/dashboard/queue/tasks/{task_id}/history")
def dashboard_queue_task_history(task_id: int, request: Request) -> dict[str, Any]:
    caller = _caller_email(request)
    _queue_editable_assignment(task_id, caller, bool(getattr(request.state, "is_admin", False)))
    return {"events": _queue_history(task_id)}


@app.post("/api/dashboard/queue/bulk-update")
def dashboard_queue_bulk_update(
    request: Request,
    task_ids: Annotated[str, Form()],
    assignee: Annotated[str | None, Form()] = None,
    priority: Annotated[str | None, Form()] = None,
    due_date: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form()] = None,
    recommended_account: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Applies selected optional fields to many tasks. Admin-only by design."""
    caller = _caller_email(request)
    if not bool(getattr(request.state, "is_admin", False)):
        raise HTTPException(status_code=403, detail="Only admins can bulk-edit Queue tasks.")
    ids = list(dict.fromkeys(int(raw) for raw in task_ids.split(",") if raw.strip().isdigit()))
    if not ids:
        raise HTTPException(status_code=400, detail="Choose at least one Queue task.")
    clean_assignee = None
    if assignee is not None:
        choices = _queue_assignee_emails(assignee)
        if len(choices) != 1:
            raise HTTPException(status_code=400, detail="Choose one assignee for a bulk update.")
        clean_assignee = choices[0]

    updates: list[str] = []
    values: list[Any] = []
    fields: list[str] = []
    if clean_assignee is not None:
        updates.append("assignee_email = ?"); values.append(clean_assignee); fields.append("assignee")
    if priority is not None:
        updates.append("priority = ?"); values.append(_queue_priority(priority)); fields.append("priority")
    if due_date is not None:
        updates.append("due_date = ?"); values.append(_queue_due_date(due_date)); fields.append("due_date")
    if tags is not None:
        updates.append("tags = ?"); values.append(json.dumps(_queue_tags(tags))); fields.append("tags")
    if recommended_account is not None:
        updates.append("recommended_account = ?"); values.append(_queue_recommended_account(recommended_account)); fields.append("recommended_account")
    if not updates:
        raise HTTPException(status_code=400, detail="Choose at least one field to update.")

    now = utc_now()
    with connect() as conn:
        for task_id in ids:
            row = conn.execute("SELECT * FROM post_assignments WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"Queue task {task_id} not found.")
            if clean_assignee and clean_assignee != row["assignee_email"]:
                duplicate = conn.execute(
                    """SELECT id FROM post_assignments
                       WHERE post_account = ? AND post_shortcode = ? AND assignee_email = ? AND id != ?""",
                    (row["post_account"], row["post_shortcode"], clean_assignee, task_id),
                ).fetchone()
                if duplicate:
                    raise HTTPException(status_code=409, detail="One selected post is already assigned to that person.")
            conn.execute(
                f"UPDATE post_assignments SET {', '.join(updates)}, updated_at = ? WHERE id = ?",
                (*values, now, task_id),
            )
            _queue_log_event(conn, task_id, caller, "bulk_updated", {"fields": fields})
    return {"ok": True, "taskIds": ids, "fields": fields}


@app.post("/api/dashboard/queue/assign")
def dashboard_queue_assign(
    request: Request,
    account: Annotated[str, Form()],
    shortcode: Annotated[str, Form()],
    assignees: Annotated[str, Form()],
    status: Annotated[str, Form()] = "queue",
    note: Annotated[str | None, Form()] = None,
    priority: Annotated[str | None, Form()] = None,
    due_date: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form()] = None,
    recommended_account: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Creates or refreshes one independent task for every chosen person."""
    caller = _caller_email(request)
    is_admin = bool(getattr(request.state, "is_admin", False))
    emails = _queue_assignee_emails(assignees)
    if not is_admin and emails != [caller]:
        raise HTTPException(status_code=403, detail="You can only assign a post to yourself.")
    clean_account, clean_shortcode = _queue_post_exists(account, shortcode)
    clean_status = _queue_status(status)
    clean_priority = _queue_priority(priority)
    clean_due_date = _queue_due_date(due_date)
    clean_tags = _queue_tags(tags)
    clean_recommended_account = _queue_recommended_account(recommended_account)
    clean_note = (note or "").strip()
    post_id = _queue_post_id(clean_account, clean_shortcode)
    now = utc_now()
    dm_notifications: list[dict[str, Any]] = []

    with connect() as conn:
        for email in emails:
            existing = conn.execute(
                "SELECT id, position FROM post_assignments WHERE post_account = ? AND post_shortcode = ? AND assignee_email = ?",
                (clean_account, clean_shortcode, email),
            ).fetchone()
            if existing:
                position = existing["position"]
            else:
                row = conn.execute(
                    "SELECT COALESCE(MAX(position), 0) + 100 AS position FROM post_assignments WHERE assignee_email = ? AND status = ?",
                    (email, clean_status),
                ).fetchone()
                position = int(row["position"])
            cursor = conn.execute(
                """
                INSERT INTO post_assignments (
                    post_account, post_shortcode, assignee_email, status, note, priority,
                    due_date, tags, recommended_account, position, created_by_email, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_account, post_shortcode, assignee_email) DO UPDATE SET
                    status = excluded.status,
                    note = excluded.note,
                    priority = excluded.priority,
                    due_date = excluded.due_date,
                    tags = excluded.tags,
                    recommended_account = excluded.recommended_account,
                    updated_at = excluded.updated_at
                """,
                (
                    clean_account, clean_shortcode, email, clean_status, clean_note, clean_priority,
                    clean_due_date, json.dumps(clean_tags), clean_recommended_account, position, caller, now, now,
                ),
            )
            assignment_id = int(existing["id"]) if existing else int(cursor.lastrowid)
            _queue_log_event(
                conn,
                assignment_id,
                caller,
                "assigned" if existing is None else "assignment_refreshed",
                {"status": clean_status, "recommendedAccount": clean_recommended_account},
            )
            # An assignment to someone else always notifies that person. A
            # self-assignment is normally quiet, with the one requested
            # exception for Esteban's own personal Queue reminder.
            is_self_assignment = email == caller
            if not is_self_assignment or caller == "esteban@sentientagency.io":
                dm_notifications.append({
                    "task_id": assignment_id,
                    "assignee_email": email,
                    "assigned_by_email": caller,
                    "account": clean_account,
                    "post_id": post_id,
                    "note": clean_note or None,
                    "due_date": clean_due_date,
                    "priority": clean_priority,
                    "tags": clean_tags,
                    "recommended_account": clean_recommended_account,
                })
    if dm_notifications:
        from .slack_alerts import notify_queue_assignment

        for notification in dm_notifications:
            notify_queue_assignment(**notification)
    return {"ok": True, "assignees": emails}


@app.post("/api/dashboard/queue/tasks/{task_id}")
def dashboard_queue_update_task(
    task_id: int,
    request: Request,
    status: Annotated[str | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
    priority: Annotated[str | None, Form()] = None,
    due_date: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form()] = None,
    recommended_account: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    caller = _caller_email(request)
    _queue_editable_assignment(task_id, caller, bool(getattr(request.state, "is_admin", False)))
    updates: list[str] = []
    params: list[Any] = []
    changed_fields: list[str] = []
    if status is not None:
        updates.append("status = ?")
        params.append(_queue_status(status))
        changed_fields.append("status")
    if note is not None:
        updates.append("note = ?")
        params.append(note.strip())
        changed_fields.append("note")
    if priority is not None:
        updates.append("priority = ?")
        params.append(_queue_priority(priority))
        changed_fields.append("priority")
    if due_date is not None:
        updates.append("due_date = ?")
        params.append(_queue_due_date(due_date))
        changed_fields.append("due_date")
    if tags is not None:
        updates.append("tags = ?")
        params.append(json.dumps(_queue_tags(tags)))
        changed_fields.append("tags")
    if recommended_account is not None:
        updates.append("recommended_account = ?")
        params.append(_queue_recommended_account(recommended_account))
        changed_fields.append("recommended_account")
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update.")
    updates.append("updated_at = ?")
    params.append(utc_now())
    params.append(task_id)
    with connect() as conn:
        conn.execute(f"UPDATE post_assignments SET {', '.join(updates)} WHERE id = ?", params)
        _queue_log_event(conn, task_id, caller, "updated", {"fields": changed_fields})
    return {"ok": True, "id": task_id}


@app.delete("/api/dashboard/queue/tasks/{task_id}")
def dashboard_queue_delete_task(task_id: int, request: Request) -> dict[str, Any]:
    """Permanently removes one Queue task. Same ownership rule as updating
    it: admins can remove anyone's task, everyone else only their own."""
    caller = _caller_email(request)
    _queue_editable_assignment(task_id, caller, bool(getattr(request.state, "is_admin", False)))
    with connect() as conn:
        conn.execute("DELETE FROM post_assignments WHERE id = ?", (task_id,))
    return {"ok": True, "id": task_id}


@app.post("/api/dashboard/queue/reorder")
def dashboard_queue_reorder(
    request: Request,
    status: Annotated[str, Form()],
    task_ids: Annotated[str, Form()],
) -> dict[str, Any]:
    """Persists the full order of one board column after a drag-and-drop."""
    caller = _caller_email(request)
    is_admin = bool(getattr(request.state, "is_admin", False))
    clean_status = _queue_status(status)
    try:
        ids = list(dict.fromkeys(int(value) for value in task_ids.split(",") if value.strip()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Queue task ids.") from exc
    if not ids:
        raise HTTPException(status_code=400, detail="No Queue tasks to reorder.")
    for task_id in ids:
        _queue_editable_assignment(task_id, caller, is_admin)
    now = utc_now()
    with connect() as conn:
        for position, task_id in enumerate(ids, start=1):
            current = conn.execute("SELECT status FROM post_assignments WHERE id = ?", (task_id,)).fetchone()
            conn.execute(
                "UPDATE post_assignments SET status = ?, position = ?, updated_at = ? WHERE id = ?",
                (clean_status, position * 100, now, task_id),
            )
            if current and current["status"] != clean_status:
                _queue_log_event(conn, task_id, caller, "moved", {"status": clean_status})
    return {"ok": True, "status": clean_status, "taskIds": ids}


# --- Queue V2: production scheduler ---------------------------------------
# Kept alongside the original Queue endpoints so old assignments remain a
# read-only historical record while every new request starts from a clean pool.
QUEUE_V2_STATUSES = {"pool", "scheduled", "in_progress", "completed", "closed", "cancelled"}
QUEUE_V2_TAGS = ["content", "design", "copy", "research", "review", "repurpose"]
QUEUE_V2_PRIORITIES = ["low", "medium", "high", "urgent"]
QUEUE_V2_TICKET_TYPES = {"time_block", "pp_revision", "cancellation"}
QUEUE_V2_TIME_CATEGORIES = {"meeting", "break", "promo", "focus", "other"}
# queue_schedule_drafts predates pool return support and keeps its placement
# columns NOT NULL. These private sentinels let a provisional unassignment be
# shared live without changing the existing SQLite table shape.
QUEUE_V2_POOL_DRAFT_DESIGNER = "__queue_pool__"
QUEUE_V2_POOL_DRAFT_DATE = "0000-00-00"


def _queue_v2_access(request: Request, *, coordinator: bool = False) -> tuple[str, bool, list[str]]:
    email = _caller_email(request)
    is_admin = bool(getattr(request.state, "is_admin", False))
    roles = list(getattr(request.state, "operating_roles", [getattr(request.state, "operating_role", "sales")]))
    if coordinator and not (is_admin or "vc" in roles):
        raise HTTPException(status_code=403, detail="Queue coordination access required.")
    # Every authenticated dashboard user has baseline PD access. Explicit
    # roles only add coordinator/admin capabilities; they never remove Queue.
    return email, is_admin, roles


def _queue_v2_json(value: Any, fallback: Any) -> Any:
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, type(fallback)) else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _queue_v2_user_roles(user: dict[str, Any] | Any) -> list[str]:
    """Return effective Queue roles; every dashboard user is PD-capable."""
    roles = _queue_v2_json(user.get("operating_roles"), [user.get("operating_role") or "sales"])
    normalized = list(dict.fromkeys(str(role).strip().lower() for role in roles if str(role).strip()))
    if "pd" not in normalized:
        normalized.append("pd")
    return normalized


def _queue_v2_priority(value: str | None) -> str:
    clean = (value or "medium").strip().lower()
    if clean not in QUEUE_V2_PRIORITIES:
        raise HTTPException(status_code=400, detail="Priority must be low, medium, high, or urgent.")
    return clean


def _queue_v2_tags(value: str | None) -> list[str]:
    raw = _queue_v2_json(value, []) if value and value.lstrip().startswith("[") else (value or "").split(",")
    tags = list(dict.fromkeys(str(tag).strip().lower() for tag in raw if str(tag).strip()))
    invalid = [tag for tag in tags if tag not in QUEUE_V2_TAGS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown Queue tag(s): {', '.join(invalid)}")
    return tags


def _queue_v2_request(request_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM queue_requests WHERE id = ?", (request_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Queue request not found.")
    return dict(row)


def _queue_v2_require_visible(row: dict[str, Any], caller: str, is_admin: bool, roles: list[str]) -> None:
    if is_admin or "vc" in roles or row.get("designer_email") == caller:
        return
    with connect() as conn:
        draft = conn.execute(
            "SELECT 1 FROM queue_schedule_drafts WHERE request_id = ? AND designer_email = ?",
            (row["id"], caller),
        ).fetchone()
    if not draft:
        raise HTTPException(status_code=403, detail="Not allowed to view this request.")


def _queue_v2_post_snapshot(account: str, shortcode: str) -> dict[str, Any]:
    table = _resolve_post_table(account)
    with connect() as conn:
        if table == "posts":
            row = conn.execute(
                """SELECT id, caption, title, post_type_label, published_at, likes, comments
                   FROM posts WHERE shortcode = ?""",
                (shortcode,),
            ).fetchone()
            if not row:
                return {}
            item = dict(row)
            return {
                "id": item["id"], "caption": item.get("caption") or item.get("title") or "",
                "type": item.get("post_type_label") or "Image", "publishedAt": item.get("published_at"),
                "likes": item.get("likes"), "comments": item.get("comments"),
                "permalink": f"https://www.instagram.com/p/{shortcode}/",
            }
        row = conn.execute(
            """SELECT id, caption, post_type_label, published_at, likes, comments, permalink
               FROM dashboard_posts WHERE account = ? AND shortcode = ?""",
            (account, shortcode),
        ).fetchone()
    if not row:
        return {}
    item = dict(row)
    return {
        "id": item["id"], "caption": item.get("caption") or "",
        "type": item.get("post_type_label") or "Image", "publishedAt": item.get("published_at"),
        "likes": item.get("likes"), "comments": item.get("comments"),
        "permalink": item.get("permalink") or f"https://www.instagram.com/p/{shortcode}/",
    }


def _queue_v2_log(conn: Any, request_id: int, actor: str, event_type: str, details: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO queue_request_events (request_id, actor_email, event_type, details, created_at) VALUES (?, ?, ?, ?, ?)",
        (request_id, actor, event_type, json.dumps(details or {}), utc_now()),
    )


def _queue_v2_publish(conn: Any, event_type: str, actor: str, request_ids: list[int] | set[int] | tuple[int, ...] = ()) -> int:
    """Advance Queue's durable live revision inside the caller transaction."""
    now = utc_now()
    clean_ids = sorted({int(item) for item in request_ids})
    conn.execute(
        """UPDATE queue_live_state
           SET revision = revision + 1, event_type = ?, actor_email = ?, request_ids = ?, updated_at = ?
           WHERE id = 1""",
        (event_type, actor, json.dumps(clean_ids), now),
    )
    row = conn.execute("SELECT revision FROM queue_live_state WHERE id = 1").fetchone()
    return int(row["revision"] if row else 0)


def _queue_v2_live_snapshot(conn: Any) -> dict[str, Any]:
    row = conn.execute(
        "SELECT revision, event_type, actor_email, request_ids, updated_at FROM queue_live_state WHERE id = 1"
    ).fetchone()
    if not row:
        return {"revision": 0, "type": "", "actorEmail": "", "requestIds": [], "updatedAt": ""}
    return {
        "revision": int(row["revision"]), "type": row["event_type"], "actorEmail": row["actor_email"],
        "requestIds": _queue_v2_json(row["request_ids"], []), "updatedAt": row["updated_at"],
    }


def _queue_v2_duration(row: dict[str, Any]) -> int:
    planned = max(10, int(row["production_points"]) * 10)
    if row.get("status") in {"completed", "closed"} and row.get("actual_started_at") and row.get("completed_at"):
        try:
            actual = max(10, round((datetime.fromisoformat(row["completed_at"]) - datetime.fromisoformat(row["actual_started_at"])).total_seconds() / 60))
            return min(planned, actual)
        except (TypeError, ValueError):
            pass
    return planned


def _queue_v2_occupied(row: dict[str, Any], duration: int | None = None) -> dict[str, Any]:
    return {
        "date": row.get("scheduled_date"),
        "start": row.get("scheduled_start_minutes"),
        "duration": duration if duration is not None else _queue_v2_duration(row),
    }


def _queue_v2_ticket(row: dict[str, Any]) -> dict[str, Any]:
    request_id = row.get("request_id")
    request_summary = None
    if request_id is not None:
        request_summary = {
            "id": int(request_id),
            "post": {"account": row.get("post_account") or "", "shortcode": row.get("post_shortcode") or ""},
            "designerEmail": row.get("designer_email"),
            "status": row.get("request_status"),
            "productionPoints": row.get("current_production_points"),
        }
    return {
        "id": int(row["id"]), "type": row["ticket_type"], "status": row["status"],
        "requesterEmail": row["requester_email"], "requestId": request_id,
        "category": row.get("block_category") or "", "title": row.get("title") or "",
        "scheduledDate": row.get("scheduled_date"), "scheduledStartMinutes": row.get("scheduled_start_minutes"),
        "durationMinutes": row.get("duration_minutes"), "requestedProductionPoints": row.get("requested_production_points"),
        "reason": row.get("reason") or "", "reviewerEmail": row.get("reviewer_email"),
        "reviewNote": row.get("review_note") or "", "reviewedAt": row.get("reviewed_at"),
        "createdAt": row["created_at"], "updatedAt": row["updated_at"], "request": request_summary,
    }


def _queue_v2_ticket_rows(conn: Any, *, requester_email: str | None = None, pending_only: bool = False) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if requester_email:
        clauses.append("t.requester_email = ?")
        params.append(requester_email)
    if pending_only:
        clauses.append("t.status = 'pending'")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""SELECT t.*, r.post_account, r.post_shortcode, r.designer_email,
                   r.status AS request_status, r.production_points AS current_production_points
            FROM queue_tickets t
            LEFT JOIN queue_requests r ON r.id = t.request_id
            {where}
            ORDER BY CASE t.status WHEN 'pending' THEN 0 ELSE 1 END, t.created_at DESC, t.id DESC
            LIMIT 150""",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _queue_v2_time_occupied(
    conn: Any, user_email: str, scheduled_date: str, *, exclude_ticket_id: int | None = None,
) -> list[dict[str, Any]]:
    drafts = [dict(row) for row in conn.execute(
        """SELECT d.request_id, d.scheduled_date, d.scheduled_start_minutes,
                  COALESCE(d.production_points, r.production_points) AS production_points
           FROM queue_schedule_drafts d
           JOIN queue_requests r ON r.id = d.request_id
           WHERE d.designer_email = ? AND d.scheduled_date = ?""",
        (user_email, scheduled_date),
    ).fetchall()]
    drafted_ids = {int(row["request_id"]) for row in drafts}
    requests = [dict(row) for row in conn.execute(
        """SELECT * FROM queue_requests
           WHERE designer_email = ? AND scheduled_date = ?
             AND status IN ('scheduled','in_progress','completed','closed')""",
        (user_email, scheduled_date),
    ).fetchall() if int(row["id"]) not in drafted_ids]
    ticket_params: list[Any] = [user_email, scheduled_date]
    ticket_scope = ""
    if exclude_ticket_id is not None:
        ticket_scope = " AND id != ?"
        ticket_params.append(exclude_ticket_id)
    blocks = [dict(row) for row in conn.execute(
        f"""SELECT scheduled_date, scheduled_start_minutes, duration_minutes
            FROM queue_tickets
            WHERE ticket_type = 'time_block' AND status IN ('pending','approved')
              AND requester_email = ? AND scheduled_date = ?{ticket_scope}""",
        ticket_params,
    ).fetchall()]
    occupied = [_queue_v2_occupied(row) for row in requests]
    occupied.extend({"date": row["scheduled_date"], "start": int(row["scheduled_start_minutes"]), "duration": int(row["production_points"]) * 10} for row in drafts)
    occupied.extend({"date": row["scheduled_date"], "start": int(row["scheduled_start_minutes"]), "duration": int(row["duration_minutes"])} for row in blocks)
    return occupied


def _queue_v2_assert_time_available(
    conn: Any, user_email: str, scheduled_date: str, start: int, duration: int, *, exclude_ticket_id: int | None = None,
) -> None:
    occupied = _queue_v2_time_occupied(conn, user_email, scheduled_date, exclude_ticket_id=exclude_ticket_id)
    if any(intervals_conflict(start, duration, int(item["start"]), int(item["duration"])) for item in occupied):
        raise HTTPException(status_code=409, detail="That time overlaps another Queue block.")


def _queue_v2_reflow_scheduled(conn: Any, designer: str, actor: str, priority_id: int | None = None) -> int:
    """Remove every scheduled overlap for one designer without rejecting work.

    In-progress and finished blocks stay fixed. Scheduled blocks advance across
    midnight as needed. A priority request is used by Start so the newly
    requested job owns the first slot after active work and later jobs cascade.
    """
    rows = [dict(row) for row in conn.execute(
        """SELECT * FROM queue_requests
           WHERE designer_email = ? AND status IN ('scheduled','in_progress','completed','closed')
             AND scheduled_date IS NOT NULL AND scheduled_start_minutes IS NOT NULL
           ORDER BY scheduled_date, scheduled_start_minutes, id""",
        (designer,),
    ).fetchall()]
    occupied = [_queue_v2_occupied(row) for row in rows if row["status"] != "scheduled"]
    occupied.extend({
        "date": row["scheduled_date"], "start": int(row["scheduled_start_minutes"]), "duration": int(row["duration_minutes"]),
    } for row in conn.execute(
        """SELECT scheduled_date, scheduled_start_minutes, duration_minutes FROM queue_tickets
           WHERE ticket_type = 'time_block' AND status IN ('pending','approved')
             AND requester_email = ? AND scheduled_date IS NOT NULL""",
        (designer,),
    ).fetchall())
    scheduled_rows = [row for row in rows if row["status"] == "scheduled"]
    if priority_id is not None:
        scheduled_rows.sort(key=lambda row: (row["id"] != priority_id, row["scheduled_date"], row["scheduled_start_minutes"], row["id"]))
    moved = 0
    for row in scheduled_rows:
        resolved_date, resolved_start = next_available_slot(
            row["scheduled_date"], int(row["scheduled_start_minutes"]), _queue_v2_duration(row), occupied,
        )
        if resolved_date != row["scheduled_date"] or resolved_start != int(row["scheduled_start_minutes"]):
            conn.execute(
                "UPDATE queue_requests SET scheduled_date = ?, scheduled_start_minutes = ?, updated_at = ? WHERE id = ?",
                (resolved_date, resolved_start, utc_now(), row["id"]),
            )
            _queue_v2_log(conn, row["id"], actor, "auto_reflowed", {
                "fromDate": row["scheduled_date"], "fromStart": row["scheduled_start_minutes"],
                "date": resolved_date, "start": resolved_start,
            })
            row["scheduled_date"], row["scheduled_start_minutes"] = resolved_date, resolved_start
            moved += 1
        occupied.append(_queue_v2_occupied(row))
    return moved


def _queue_v2_reflow_all_schedules() -> int:
    """One startup pass repairs overlaps saved by older Queue releases."""
    with connect() as conn:
        designers = [row["designer_email"] for row in conn.execute(
            """SELECT DISTINCT designer_email FROM queue_requests
               WHERE designer_email IS NOT NULL AND status = 'scheduled'"""
        ).fetchall()]
        return sum(_queue_v2_reflow_scheduled(conn, designer, "queue-system@sentientdash.app") for designer in designers)


def _queue_v2_project(row: dict[str, Any]) -> dict[str, Any]:
    pp = int(row["production_points"])
    snapshot = _queue_v2_post_snapshot(row["post_account"], row["post_shortcode"])
    return {
        "id": row["id"], "post": {
            "account": row["post_account"], "shortcode": row["post_shortcode"],
            "permalink": row["post_permalink"] or snapshot.get("permalink"),
            "caption": row["post_caption"] or snapshot.get("caption") or "",
            "type": row["post_type"] or snapshot.get("type") or "Image", "coverUrl": row["cover_url"],
            "publishedAt": snapshot.get("publishedAt"), "likes": snapshot.get("likes"),
            "comments": snapshot.get("comments"),
        },
        "productionPoints": pp, "durationMinutes": pp * 10, "priority": row.get("priority") or "medium",
        "tags": _queue_v2_json(row["tags"], []), "brief": row["brief"], "notes": row["notes"],
        "references": _queue_v2_json(row["reference_links"], []), "attachments": _queue_v2_json(row["attachments"], []),
        "status": row["status"], "designerEmail": row["designer_email"], "coordinatorEmail": row["coordinator_email"],
        "recommendedAccounts": _queue_v2_json(row["recommended_accounts"], []),
        "scheduledDate": row["scheduled_date"], "scheduledStartMinutes": row["scheduled_start_minutes"],
        "actualStartedAt": row["actual_started_at"], "completedAt": row["completed_at"], "closedAt": row["closed_at"],
        "finalPermalink": row["final_permalink"], "cancellationReason": row["cancellation_reason"],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


def _queue_v2_project_draft(row: dict[str, Any]) -> dict[str, Any]:
    item = _queue_v2_project(row)
    draft_points = row.get("draft_production_points")
    if draft_points is not None:
        item["productionPoints"] = int(draft_points)
        item["durationMinutes"] = int(draft_points) * 10
    if row["draft_designer_email"] == QUEUE_V2_POOL_DRAFT_DESIGNER:
        item.update({
            "committedStatus": item["status"],
            "status": "pool",
            "designerEmail": None,
            "scheduledDate": None,
            "scheduledStartMinutes": None,
            "recommendedAccounts": _queue_v2_json(row["draft_recommended_accounts"], []),
            "isDraft": True,
            "draftCoordinatorEmail": row["draft_coordinator_email"],
            "draftUpdatedAt": row["draft_updated_at"],
        })
        return item
    item.update({
        "committedStatus": item["status"],
        "status": "scheduled",
        "designerEmail": row["draft_designer_email"],
        "scheduledDate": row["draft_scheduled_date"],
        "scheduledStartMinutes": int(row["draft_scheduled_start_minutes"]),
        "recommendedAccounts": _queue_v2_json(row["draft_recommended_accounts"], []),
        "isDraft": True,
        "draftCoordinatorEmail": row["draft_coordinator_email"],
        "draftUpdatedAt": row["draft_updated_at"],
    })
    return item


def _queue_v2_draft_rows(conn: Any, *, designer_email: str | None = None, request_ids: set[int] | None = None) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if designer_email:
        # Pool-return drafts are visible to every Queue participant so the
        # assigned designer immediately sees the old block disappear before
        # the VC submits the change.
        clauses.append("(d.designer_email = ? OR d.designer_email = ?)")
        params.extend([designer_email, QUEUE_V2_POOL_DRAFT_DESIGNER])
    if request_ids is not None:
        if not request_ids:
            return []
        placeholders = ",".join("?" for _ in request_ids)
        clauses.append(f"d.request_id IN ({placeholders})")
        params.extend(sorted(request_ids))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""SELECT r.*,
                   d.coordinator_email AS draft_coordinator_email,
                   d.designer_email AS draft_designer_email,
                   d.scheduled_date AS draft_scheduled_date,
                   d.scheduled_start_minutes AS draft_scheduled_start_minutes,
                   d.recommended_accounts AS draft_recommended_accounts,
                   d.production_points AS draft_production_points,
                   d.updated_at AS draft_updated_at
            FROM queue_schedule_drafts d
            JOIN queue_requests r ON r.id = d.request_id
            {where}
            ORDER BY d.scheduled_date, d.scheduled_start_minutes, d.request_id""",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _queue_v2_designers() -> list[dict[str, Any]]:
    users = list_dashboard_users()
    with connect() as conn:
        accounts = conn.execute("SELECT designer_email, account_handle FROM queue_designer_accounts ORDER BY account_handle").fetchall()
    by_designer: dict[str, list[str]] = {}
    for item in accounts:
        by_designer.setdefault(item["designer_email"], []).append(item["account_handle"])
    return [{"email": user["email"], "isAdmin": bool(user.get("is_admin")), "accounts": by_designer.get(user["email"], [])} for user in users]


def _queue_v2_scheduler_users() -> list[dict[str, Any]]:
    """Return the complete dashboard roster for the coordinator timeline.

    Every dashboard user is Queue-capable as a PD by default. Their explicit
    secondary roles (VC, Sales, or Admin) remain available to the UI, while
    the client suppresses the implicit PD label so the roster stays concise.
    """
    users = list_dashboard_users()
    from .slack_alerts import slack_user_id_for_email, slack_user_profile_images

    slack_ids = [str(user.get("slack_user_id") or "").strip().upper() or slack_user_id_for_email(user.get("email")) for user in users]
    avatar_by_slack_id = slack_user_profile_images(slack_ids)
    with connect() as conn:
        accounts = conn.execute("SELECT designer_email, account_handle FROM queue_designer_accounts ORDER BY account_handle").fetchall()
        account_rows = conn.execute("SELECT handle, avatar_path FROM accounts WHERE is_active = 1").fetchall()
    by_designer: dict[str, list[str]] = {}
    for item in accounts:
        by_designer.setdefault(item["designer_email"], []).append(item["account_handle"])
    result: list[dict[str, Any]] = []
    for user in users:
        roles = _queue_v2_user_roles(user)
        slack_id = str(user.get("slack_user_id") or "").strip().upper() or slack_user_id_for_email(user.get("email"))
        managed_accounts = by_designer.get(user["email"], [])
        account_avatars = {
            str(item["handle"]): f"/api/dashboard/avatar/{item['handle']}"
            for item in account_rows
            if item["avatar_path"] and str(item["handle"]) in managed_accounts
        }
        result.append({
            "email": user["email"],
            "isAdmin": bool(user.get("is_admin")),
            "roles": roles,
            "isQueueDesigner": "pd" in roles,
            "avatarUrl": avatar_by_slack_id.get(slack_id, ""),
            "accounts": managed_accounts,
            "accountAvatars": account_avatars,
        })
    return result


def _queue_v2_prepare_schedule_changes(
    conn: Any, entries: Any, *, ignore_draft_coordinator: str | None = None,
) -> list[dict[str, Any]]:
    """Validate and collision-resolve provisional or committed placements."""
    if not isinstance(entries, list) or not entries:
        raise HTTPException(status_code=400, detail="Add at least one schedule change before submitting.")
    prepared: list[dict[str, Any]] = []
    changed_ids: set[int] = set()
    for entry in entries:
        try:
            request_id = int(entry["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid schedule change.") from exc
        if request_id in changed_ids:
            raise HTTPException(status_code=400, detail="A Queue request can only appear once per change set.")
        changed_ids.add(request_id)
        raw_designer = entry.get("designerEmail")
        is_pool_return = entry.get("status") == "pool" or raw_designer in (None, "")
        row = conn.execute("SELECT * FROM queue_requests WHERE id = ?", (request_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Queue request not found.")
        if row["status"] not in {"pool", "scheduled"}:
            raise HTTPException(status_code=409, detail="Only pooled or scheduled requests can be moved.")
        try:
            production_points = int(entry.get("productionPoints", row["production_points"]))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Production points must be a positive integer.") from exc
        if production_points < 1:
            raise HTTPException(status_code=400, detail="Production points must be a positive integer.")
        accounts = entry.get("recommendedAccounts", [])
        if not isinstance(accounts, list):
            accounts = []
        clean_accounts = [str(value).strip().lstrip("@").lower() for value in accounts if str(value).strip()]
        clean_accounts = list(dict.fromkeys(clean_accounts))
        if is_pool_return:
            prepared.append({
                "id": request_id, "designer": None, "date": None, "start": None,
                "productionPoints": production_points, "duration": production_points * 10,
                "accounts": clean_accounts, "row": dict(row), "assigneeSlackId": "", "pool": True,
            })
            continue
        try:
            designer = str(raw_designer).strip().lower()
            date, start = str(entry["scheduledDate"]), int(entry["scheduledStartMinutes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid schedule change.") from exc
        if start % 10 or start < SCHEDULER_START or start >= SCHEDULER_END:
            raise HTTPException(status_code=400, detail="Tasks must begin in 10-minute scheduler slots.")
        try:
            datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Schedule date must use YYYY-MM-DD.") from exc
        designer_row = conn.execute(
            "SELECT operating_role, operating_roles, slack_user_id FROM dashboard_users WHERE email = ?",
            (designer,),
        ).fetchone()
        designer_roles = _queue_v2_user_roles(dict(designer_row)) if designer_row else []
        if not designer_row or "pd" not in designer_roles:
            raise HTTPException(status_code=400, detail="Choose a Queue designer.")
        allowed = {item["account_handle"] for item in conn.execute(
            "SELECT account_handle FROM queue_designer_accounts WHERE designer_email = ?", (designer,),
        ).fetchall()}
        # A move between designers should never fail merely because the old
        # designer's recommended account is not owned by the new one. Keep
        # only accounts valid for the target; account selection is optional.
        clean_accounts = [value for value in dict.fromkeys(clean_accounts) if value in allowed]
        prepared.append({
            "id": request_id, "designer": designer, "date": date, "start": start,
            "productionPoints": production_points, "duration": production_points * 10,
            "accounts": clean_accounts, "row": dict(row),
            "assigneeSlackId": designer_row["slack_user_id"],
        })

    occupied_by_designer: dict[str, list[dict[str, Any]]] = {}
    for designer in {item["designer"] for item in prepared if item["designer"]}:
        other_drafts = [dict(row) for row in conn.execute(
            """SELECT d.request_id, d.coordinator_email, d.scheduled_date, d.scheduled_start_minutes,
                      COALESCE(d.production_points, r.production_points) AS production_points
               FROM queue_schedule_drafts d
               JOIN queue_requests r ON r.id = d.request_id
               WHERE d.designer_email = ?""",
            (designer,),
        ).fetchall()
            if int(row["request_id"]) not in changed_ids
            and (not ignore_draft_coordinator or row["coordinator_email"] != ignore_draft_coordinator)]
        drafted_ids = {int(row["request_id"]) for row in other_drafts}
        committed = [dict(row) for row in conn.execute(
            """SELECT * FROM queue_requests
               WHERE designer_email = ? AND status IN ('scheduled','in_progress','completed','closed')
                 AND scheduled_date IS NOT NULL AND scheduled_start_minutes IS NOT NULL""",
            (designer,),
        ).fetchall() if int(row["id"]) not in changed_ids and int(row["id"]) not in drafted_ids]
        occupied_by_designer[designer] = [_queue_v2_occupied(row) for row in committed]
        occupied_by_designer[designer].extend({
            "date": row["scheduled_date"], "start": int(row["scheduled_start_minutes"]),
            "duration": int(row["production_points"]) * 10,
        } for row in other_drafts)
        occupied_by_designer[designer].extend({
            "date": row["scheduled_date"], "start": int(row["scheduled_start_minutes"]),
            "duration": int(row["duration_minutes"]),
        } for row in conn.execute(
            """SELECT scheduled_date, scheduled_start_minutes, duration_minutes FROM queue_tickets
               WHERE ticket_type = 'time_block' AND status IN ('pending','approved')
                 AND requester_email = ? AND scheduled_date IS NOT NULL""",
            (designer,),
        ).fetchall())

    for item in sorted((value for value in prepared if value["designer"]), key=lambda value: (value["designer"], schedule_absolute(value["date"], value["start"]), value["id"])):
        item["date"], item["start"] = next_available_slot(
            item["date"], item["start"], item["duration"], occupied_by_designer[item["designer"]],
        )
        occupied_by_designer[item["designer"]].append({
            "date": item["date"], "start": item["start"], "duration": item["duration"],
        })
    return prepared


def _queue_v2_reflow_drafts(conn: Any, designer: str) -> int:
    """Keep shared provisional placements collision-free after firm changes."""
    drafts = [dict(row) for row in conn.execute(
            """SELECT d.request_id, d.scheduled_date, d.scheduled_start_minutes,
                      COALESCE(d.production_points, r.production_points) AS production_points
           FROM queue_schedule_drafts d
           JOIN queue_requests r ON r.id = d.request_id
           WHERE d.designer_email = ?
           ORDER BY d.scheduled_date, d.scheduled_start_minutes, d.request_id""",
        (designer,),
    ).fetchall()]
    draft_ids = {int(row["request_id"]) for row in drafts}
    committed = [dict(row) for row in conn.execute(
        """SELECT * FROM queue_requests
           WHERE designer_email = ? AND status IN ('scheduled','in_progress','completed','closed')
             AND scheduled_date IS NOT NULL AND scheduled_start_minutes IS NOT NULL""",
        (designer,),
    ).fetchall() if int(row["id"]) not in draft_ids]
    occupied = [_queue_v2_occupied(row) for row in committed]
    occupied.extend({
        "date": row["scheduled_date"], "start": int(row["scheduled_start_minutes"]), "duration": int(row["duration_minutes"]),
    } for row in conn.execute(
        """SELECT scheduled_date, scheduled_start_minutes, duration_minutes FROM queue_tickets
           WHERE ticket_type = 'time_block' AND status IN ('pending','approved')
             AND requester_email = ? AND scheduled_date IS NOT NULL""",
        (designer,),
    ).fetchall())
    moved = 0
    for row in drafts:
        resolved_date, resolved_start = next_available_slot(
            row["scheduled_date"], int(row["scheduled_start_minutes"]), int(row["production_points"]) * 10, occupied,
        )
        if resolved_date != row["scheduled_date"] or resolved_start != int(row["scheduled_start_minutes"]):
            conn.execute(
                "UPDATE queue_schedule_drafts SET scheduled_date = ?, scheduled_start_minutes = ?, updated_at = ? WHERE request_id = ?",
                (resolved_date, resolved_start, utc_now(), row["request_id"]),
            )
            moved += 1
        occupied.append({"date": resolved_date, "start": resolved_start, "duration": int(row["production_points"]) * 10})
    return moved


@app.get("/api/dashboard/queue/v2")
def dashboard_queue_v2(request: Request, date: str | None = None, archive: bool = False) -> dict[str, Any]:
    caller, is_admin, roles = _queue_v2_access(request)
    selected_date = date or datetime.now(SCHEDULER_TIMEZONE).date().isoformat()
    try:
        datetime.strptime(selected_date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Date must use YYYY-MM-DD.") from exc
    clauses = ["(status = 'pool' OR scheduled_date = ? OR status = 'cancelled')" if archive else "(status = 'pool' OR scheduled_date = ?)"]
    params: list[Any] = [selected_date]
    if not archive:
        clauses.append("status != 'cancelled'")
    if not (is_admin or "vc" in roles):
        clauses.append("designer_email = ?")
        params.append(caller)
    with connect() as conn:
        pool_rows = conn.execute(
            """SELECT * FROM queue_requests
               WHERE status = 'pool'
               ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                        created_at, id"""
        ).fetchall()
        rows = conn.execute(
            f"""SELECT * FROM queue_requests WHERE {' AND '.join(clauses)}
                 ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, id""",
            params,
        ).fetchall()
        planning_params: list[Any] = []
        planning_scope = ""
        if not (is_admin or "vc" in roles):
            planning_scope = " AND designer_email = ?"
            planning_params.append(caller)
        planning_rows = conn.execute(
            f"""SELECT * FROM queue_requests
                WHERE status IN ('scheduled','in_progress','completed','closed')
                  AND scheduled_date IS NOT NULL AND scheduled_start_minutes IS NOT NULL{planning_scope}
                ORDER BY scheduled_date, scheduled_start_minutes, id""",
            planning_params,
        ).fetchall()
        assigned_rows = []
        if not (is_admin or "vc" in roles):
            assigned_rows = conn.execute(
                """SELECT * FROM queue_requests
                   WHERE designer_email = ? AND status NOT IN ('pool', 'closed', 'cancelled')
                   ORDER BY scheduled_date, scheduled_start_minutes, id""",
                (caller,),
            ).fetchall()
        draft_rows = _queue_v2_draft_rows(
            conn,
            designer_email=None if (is_admin or "vc" in roles) else caller,
        )
        block_scope = "" if (is_admin or "vc" in roles) else " AND requester_email = ?"
        block_params: list[Any] = [selected_date]
        if block_scope:
            block_params.append(caller)
        time_block_rows = conn.execute(
            f"""SELECT * FROM queue_tickets
                WHERE ticket_type = 'time_block' AND status IN ('pending','approved')
                  AND scheduled_date = ?{block_scope}
                ORDER BY scheduled_start_minutes, id""",
            block_params,
        ).fetchall()
        pending_ticket_count = int(conn.execute(
            "SELECT COUNT(*) AS c FROM queue_tickets WHERE status = 'pending'" + ("" if (is_admin or "vc" in roles) else " AND requester_email = ?"),
            [] if (is_admin or "vc" in roles) else [caller],
        ).fetchone()["c"])
        live_state = _queue_v2_live_snapshot(conn)
    requests = [_queue_v2_project(dict(row)) for row in rows]
    # Pool work is intentionally excluded from the designer's normal schedule
    # response, but PDs need a safe, read-only candidate list for Pick. Keep it
    # separate so the existing pool visibility rules and coordinator UI remain
    # unchanged.
    pick_requests = [_queue_v2_project(dict(row)) for row in pool_rows]
    planning_requests = [_queue_v2_project(dict(row)) for row in planning_rows]
    assigned_requests = [_queue_v2_project(dict(row)) for row in assigned_rows]
    live_drafts = [_queue_v2_project_draft(row) for row in draft_rows]
    if not (is_admin or "vc" in roles):
        requests = [item for item in requests if item["status"] != "pool"]
    scheduler_users = _queue_v2_scheduler_users() if (is_admin or "vc" in roles) else [
        user for user in _queue_v2_scheduler_users() if user["email"] == caller
    ]
    return {
        "viewer": {"email": caller, "isAdmin": is_admin, "isDev": bool(getattr(request.state, "is_dev", False)), "operatingRole": roles[0] if roles else "sales", "operatingRoles": roles},
        "date": selected_date, "requests": requests, "planningRequests": planning_requests,
        "pickRequests": pick_requests,
        "assignedRequests": assigned_requests, "liveDrafts": live_drafts, "liveRevision": live_state["revision"],
        "timeBlocks": [_queue_v2_ticket(dict(row)) for row in time_block_rows], "pendingTicketCount": pending_ticket_count,
        "designers": _queue_v2_designers() if (is_admin or "vc" in roles) else [d for d in _queue_v2_designers() if d["email"] == caller],
        "schedulerUsers": scheduler_users,
        "tags": QUEUE_V2_TAGS, "priorities": QUEUE_V2_PRIORITIES,
        "hours": {"start": SCHEDULER_START, "end": SCHEDULER_END},
    }


@app.post("/api/dashboard/queue/v2/pick")
def dashboard_queue_v2_pick(
    request: Request,
    request_id: Annotated[int, Form()],
    scheduled_date: Annotated[str | None, Form()] = None,
    scheduled_start_minutes: Annotated[int | None, Form()] = None,
) -> dict[str, Any]:
    """Let a PD claim one pooled request for their own next available slot."""
    caller, _, _ = _queue_v2_access(request)
    local_now = datetime.now(SCHEDULER_TIMEZONE)
    target_date = scheduled_date or local_now.date().isoformat()
    target_start = scheduled_start_minutes
    if target_start is None:
        target_start = ((local_now.hour * 60 + local_now.minute + 9) // 10) * 10
        if target_start >= SCHEDULER_END:
            target_date = (local_now.date() + timedelta(days=1)).isoformat()
            target_start = SCHEDULER_START
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Schedule date must use YYYY-MM-DD.") from exc
    with connect() as conn:
        row = conn.execute("SELECT * FROM queue_requests WHERE id = ?", (request_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Queue request not found.")
        if row["status"] != "pool":
            raise HTTPException(status_code=409, detail="That request is no longer available in the pool.")
        prepared = _queue_v2_prepare_schedule_changes(conn, [{
            "id": request_id, "designerEmail": caller, "scheduledDate": target_date,
            "scheduledStartMinutes": int(target_start), "productionPoints": int(row["production_points"]),
            "recommendedAccounts": _queue_v2_json(row["recommended_accounts"], []),
        }])
        item = prepared[0]
        now = utc_now()
        conn.execute(
            """UPDATE queue_requests SET designer_email = ?, coordinator_email = ?, scheduled_date = ?,
               scheduled_start_minutes = ?, recommended_accounts = ?, status = 'scheduled', updated_at = ?
               WHERE id = ?""",
            (caller, caller, item["date"], item["start"], json.dumps(item["accounts"]), now, request_id),
        )
        _queue_v2_log(conn, request_id, caller, "picked", {"date": item["date"], "start": item["start"]})
        _queue_v2_publish(conn, "request_picked", caller, [request_id])
        result = dict(conn.execute("SELECT * FROM queue_requests WHERE id = ?", (request_id,)).fetchone())
    return {"ok": True, "request": _queue_v2_project(result)}


@app.get("/api/dashboard/queue/v2/live")
async def dashboard_queue_v2_live(request: Request, after: int = 0) -> StreamingResponse:
    """Authenticated SSE stream backed by Queue's durable SQLite revision."""
    _queue_v2_access(request)
    last_revision = max(0, int(after))

    async def events():
        nonlocal last_revision
        keepalive_at = time.monotonic()
        while not await request.is_disconnected():
            with connect() as conn:
                state = _queue_v2_live_snapshot(conn)
            if state["revision"] > last_revision:
                last_revision = state["revision"]
                payload = json.dumps(state, separators=(",", ":"))
                yield f"id: {last_revision}\nevent: queue\ndata: {payload}\n\n"
                keepalive_at = time.monotonic()
            elif time.monotonic() - keepalive_at >= 15:
                yield ": keepalive\n\n"
                keepalive_at = time.monotonic()
            await asyncio.sleep(0.75)

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/dashboard/queue/v2/requests/{request_id}")
def dashboard_queue_v2_request_detail(request_id: int, request: Request) -> dict[str, Any]:
    caller, is_admin, roles = _queue_v2_access(request)
    row = _queue_v2_request(request_id)
    _queue_v2_require_visible(row, caller, is_admin, roles)
    with connect() as conn:
        draft_rows = _queue_v2_draft_rows(conn, request_ids={request_id})
    if draft_rows and (
        is_admin or "vc" in roles or draft_rows[0]["draft_designer_email"] == caller
        or (draft_rows[0]["draft_designer_email"] == QUEUE_V2_POOL_DRAFT_DESIGNER and row.get("designer_email") == caller)
    ):
        return {"request": _queue_v2_project_draft(draft_rows[0])}
    return {"request": _queue_v2_project(row)}


@app.get("/api/dashboard/queue/v2/summary")
def dashboard_queue_v2_summary(request: Request) -> dict[str, Any]:
    caller = _caller_email(request)
    roles = list(getattr(request.state, "operating_roles", [getattr(request.state, "operating_role", "sales")]))
    is_admin = bool(getattr(request.state, "is_admin", False))
    with connect() as conn:
        if is_admin or "vc" in roles:
            row = conn.execute("SELECT COUNT(*) AS c FROM queue_requests WHERE status IN ('pool','scheduled','in_progress','completed')").fetchone()
            draft_count = 0
        else:
            row = conn.execute("SELECT COUNT(*) AS c FROM queue_requests WHERE designer_email = ? AND status IN ('scheduled','in_progress','completed')", (caller,)).fetchone()
            draft_count = int(conn.execute(
                """SELECT COUNT(*) AS c FROM queue_schedule_drafts d
                   WHERE d.designer_email = ?
                     AND NOT EXISTS (
                       SELECT 1 FROM queue_requests r
                       WHERE r.id = d.request_id AND r.designer_email = ?
                         AND r.status IN ('scheduled','in_progress','completed')
                     )""",
                (caller, caller),
            ).fetchone()["c"])
    return {"pending": int(row["c"] if row else 0) + draft_count}


@app.get("/api/dashboard/queue/v2/admin-report")
def dashboard_queue_v2_admin_report(request: Request) -> dict[str, Any]:
    """Small operational snapshot for Queue admins, intentionally separate
    from the scheduler payload so designers never receive team-wide totals."""
    _, is_admin, _ = _queue_v2_access(request)
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    with connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS count, COALESCE(SUM(production_points), 0) AS points FROM queue_requests GROUP BY status").fetchall()
        request_rows = [dict(row) for row in conn.execute("SELECT * FROM queue_requests").fetchall()]
        draft_rows = _queue_v2_draft_rows(conn)
    totals = {status: {"count": 0, "points": 0} for status in QUEUE_V2_STATUSES}
    for row in rows:
        totals[row["status"]] = {"count": int(row["count"]), "points": int(row["points"])}
    designer_reports: list[dict[str, Any]] = []
    for designer in _queue_v2_designers():
        items = [row for row in request_rows if row["designer_email"] == designer["email"]]
        active = [row for row in items if row["status"] in {"scheduled", "in_progress", "completed"}]
        closed = [row for row in items if row["status"] == "closed"]
        actual_minutes = [
            max(0, round((datetime.fromisoformat(row["completed_at"]) - datetime.fromisoformat(row["actual_started_at"])).total_seconds() / 60))
            for row in closed if row["actual_started_at"] and row["completed_at"]
        ]
        designer_reports.append({
            "email": designer["email"], "activeRequests": len(active),
            "productionPoints": sum(int(row["production_points"]) for row in active),
            "closedRequests": len(closed),
            "urgentRequests": sum(1 for row in active if (row.get("priority") or "medium") == "urgent"),
            "highPriorityRequests": sum(1 for row in active if (row.get("priority") or "medium") == "high"),
            "averageActualMinutes": round(sum(actual_minutes) / len(actual_minutes)) if actual_minutes else None,
        })
    priority_totals = {priority: {"count": 0, "points": 0} for priority in QUEUE_V2_PRIORITIES}
    for row in request_rows:
        priority = row.get("priority") or "medium"
        priority_totals[priority]["count"] += 1
        priority_totals[priority]["points"] += int(row["production_points"])
    # Admins need a durable, cross-day view of every request that has been
    # assigned to a designer. The scheduler payload is intentionally scoped to
    # one day, so keep this list on the report endpoint instead of forcing the
    # UI to make one request per date. Include closed/cancelled assignments as
    # historical records; unassigned pool items are the only rows omitted.
    assigned_by_id = {row["id"]: _queue_v2_project(row) for row in request_rows if row["designer_email"]}
    for row in draft_rows:
        assigned_by_id[row["id"]] = _queue_v2_project_draft(row)
    assigned_posts = list(assigned_by_id.values())
    assigned_posts.sort(key=lambda item: (
        item.get("scheduledDate") or "9999-99-99",
        item.get("scheduledStartMinutes") if item.get("scheduledStartMinutes") is not None else 9999,
        item.get("id", 0),
    ))
    return {
        "totals": totals,
        "priorities": priority_totals,
        "designers": sorted(designer_reports, key=lambda item: (-item["activeRequests"], item["email"])),
        "assignedPosts": assigned_posts,
    }


@app.post("/api/dashboard/queue/v2/pool")
def dashboard_queue_v2_pool(
    request: Request, account: Annotated[str, Form()], shortcode: Annotated[str, Form()],
    production_points: Annotated[int, Form()], priority: Annotated[str, Form()] = "medium",
    tags: Annotated[str | None, Form()] = None, brief: Annotated[str | None, Form()] = None,
    notes: Annotated[str | None, Form()] = None, references: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    caller, _, _ = _queue_v2_access(request, coordinator=True)
    if production_points < 1:
        raise HTTPException(status_code=400, detail="Production points must be at least 1.")
    clean_account, clean_shortcode = _queue_post_exists(account, shortcode)
    snapshot = _queue_v2_post_snapshot(clean_account, clean_shortcode)
    post_id = snapshot.get("id")
    clean_priority = _queue_v2_priority(priority)
    refs = _queue_v2_json(references, [])
    now = utc_now()
    with connect() as conn:
        existing = conn.execute("SELECT id, status FROM queue_requests WHERE post_account = ? AND post_shortcode = ?", (clean_account, clean_shortcode)).fetchone()
        if existing and existing["status"] != "cancelled":
            raise HTTPException(status_code=409, detail="This post is already in the production Queue.")
        if existing:
            conn.execute("DELETE FROM queue_requests WHERE id = ?", (existing["id"],))
        cursor = conn.execute(
            """INSERT INTO queue_requests (post_account, post_shortcode, post_permalink, post_caption, post_type, cover_url,
                production_points, priority, deadline_at, tags, brief, notes, reference_links, coordinator_email, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (clean_account, clean_shortcode, snapshot.get("permalink") or f"https://www.instagram.com/p/{clean_shortcode}/",
             snapshot.get("caption") or "", snapshot.get("type") or "Image",
             f"/api/dashboard/covers/{clean_account}/{post_id}" if post_id is not None else "", production_points, clean_priority, now,
             json.dumps(_queue_v2_tags(tags)), (brief or "").strip(), (notes or "").strip(), json.dumps(refs if isinstance(refs, list) else []), caller, now, now),
        )
        request_id = int(cursor.lastrowid)
        _queue_v2_log(conn, request_id, caller, "pooled", {"productionPoints": production_points, "priority": clean_priority})
        _queue_v2_publish(conn, "pooled", caller, [request_id])
        row = dict(conn.execute("SELECT * FROM queue_requests WHERE id = ?", (request_id,)).fetchone())
    return {"ok": True, "request": _queue_v2_project(row)}


@app.post("/api/dashboard/queue/v2/drafts")
def dashboard_queue_v2_drafts(request: Request, changes: Annotated[str, Form()]) -> dict[str, Any]:
    caller, _, _ = _queue_v2_access(request, coordinator=True)
    entries = _queue_v2_json(changes, [])
    now = utc_now()
    with connect() as conn:
        prepared = _queue_v2_prepare_schedule_changes(conn, entries, ignore_draft_coordinator=caller)
        incoming_ids = {item["id"] for item in prepared}
        previous_ids = {int(row["request_id"]) for row in conn.execute(
            "SELECT request_id FROM queue_schedule_drafts WHERE coordinator_email = ?", (caller,),
        ).fetchall()}
        if incoming_ids:
            placeholders = ",".join("?" for _ in incoming_ids)
            conn.execute(
                f"DELETE FROM queue_schedule_drafts WHERE coordinator_email = ? AND request_id NOT IN ({placeholders})",
                [caller, *sorted(incoming_ids)],
            )
        for item in prepared:
            conn.execute(
                """INSERT INTO queue_schedule_drafts
                   (request_id, coordinator_email, designer_email, scheduled_date, scheduled_start_minutes,
                    recommended_accounts, production_points, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(request_id) DO UPDATE SET
                     coordinator_email = excluded.coordinator_email,
                     designer_email = excluded.designer_email,
                     scheduled_date = excluded.scheduled_date,
                     scheduled_start_minutes = excluded.scheduled_start_minutes,
                     recommended_accounts = excluded.recommended_accounts,
                     production_points = excluded.production_points,
                     updated_at = excluded.updated_at""",
                (item["id"], caller, item["designer"] or QUEUE_V2_POOL_DRAFT_DESIGNER,
                 item["date"] or QUEUE_V2_POOL_DRAFT_DATE, item["start"] if item["start"] is not None else 0,
                 json.dumps(item["accounts"]), item["productionPoints"], now),
            )
        changed_ids = previous_ids | incoming_ids
        revision = _queue_v2_publish(conn, "draft_updated", caller, changed_ids)
        rows = _queue_v2_draft_rows(conn, request_ids=incoming_ids)
    return {"ok": True, "drafts": [_queue_v2_project_draft(row) for row in rows], "liveRevision": revision}


@app.post("/api/dashboard/queue/v2/drafts/clear")
def dashboard_queue_v2_clear_drafts(
    request: Request, request_ids: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    caller, _, _ = _queue_v2_access(request, coordinator=True)
    parsed = _queue_v2_json(request_ids, []) if request_ids else []
    clean_ids = {int(value) for value in parsed if str(value).isdigit()} if isinstance(parsed, list) else set()
    with connect() as conn:
        if clean_ids:
            placeholders = ",".join("?" for _ in clean_ids)
            owned = {int(row["request_id"]) for row in conn.execute(
                f"SELECT request_id FROM queue_schedule_drafts WHERE coordinator_email = ? AND request_id IN ({placeholders})",
                [caller, *sorted(clean_ids)],
            ).fetchall()}
            conn.execute(
                f"DELETE FROM queue_schedule_drafts WHERE coordinator_email = ? AND request_id IN ({placeholders})",
                [caller, *sorted(clean_ids)],
            )
        else:
            owned = {int(row["request_id"]) for row in conn.execute(
                "SELECT request_id FROM queue_schedule_drafts WHERE coordinator_email = ?", (caller,),
            ).fetchall()}
            conn.execute("DELETE FROM queue_schedule_drafts WHERE coordinator_email = ?", (caller,))
        revision = _queue_v2_publish(conn, "draft_cleared", caller, owned)
    return {"ok": True, "cleared": len(owned), "liveRevision": revision}


@app.post("/api/dashboard/queue/v2/submit")
def dashboard_queue_v2_submit(request: Request, changes: Annotated[str, Form()]) -> dict[str, Any]:
    caller, _, _ = _queue_v2_access(request, coordinator=True)
    entries = _queue_v2_json(changes, [])
    if not isinstance(entries, list) or not entries:
        raise HTTPException(status_code=400, detail="Add at least one schedule change before submitting.")
    notifications: list[dict[str, Any]] = []
    adjustments: list[dict[str, Any]] = []
    same_assignees: dict[int, bool] = {}
    now = utc_now()
    with connect() as conn:
        prepared = _queue_v2_prepare_schedule_changes(conn, entries)
        assigner_row = conn.execute("SELECT slack_user_id FROM dashboard_users WHERE email = ?", (caller,)).fetchone()
        assigner_slack_id = assigner_row["slack_user_id"] if assigner_row else ""
        for item in prepared:
            row = item["row"]
            if item.get("pool"):
                conn.execute(
                    """UPDATE queue_requests SET designer_email = NULL, coordinator_email = ?,
                       scheduled_date = NULL, scheduled_start_minutes = NULL,
                       recommended_accounts = ?, production_points = ?, status = 'pool',
                       slack_channel_id = NULL, slack_message_ts = NULL, updated_at = ? WHERE id = ?""",
                    (caller, json.dumps(item["accounts"]), item["productionPoints"], now, item["id"]),
                )
                _queue_v2_log(conn, item["id"], caller, "returned_to_pool", {"productionPoints": item["productionPoints"]})
                continue
            was_scheduled = row["status"] != "pool"
            same_assignee = str(row.get("designer_email") or "").strip().lower() == item["designer"]
            same_assignees[item["id"]] = same_assignee
            conn.execute(
                """UPDATE queue_requests SET designer_email = ?, coordinator_email = ?, scheduled_date = ?, scheduled_start_minutes = ?,
                   recommended_accounts = ?, production_points = ?, status = 'scheduled',
                   slack_channel_id = ?, slack_message_ts = ?, updated_at = ? WHERE id = ?""",
                (item["designer"], caller, item["date"], item["start"], json.dumps(item["accounts"]), item["productionPoints"],
                 row.get("slack_channel_id") if same_assignee else None,
                 row.get("slack_message_ts") if same_assignee else None,
                 now, item["id"]),
            )
            _queue_v2_log(conn, item["id"], caller, "resubmitted" if was_scheduled else "scheduled", {"designer": item["designer"], "date": item["date"], "start": item["start"], "accounts": item["accounts"]})
        for designer in {item["designer"] for item in prepared if item["designer"]}:
            _queue_v2_reflow_scheduled(conn, designer, caller)
        submitted_ids = {item["id"] for item in prepared}
        placeholders = ",".join("?" for _ in submitted_ids)
        conn.execute(f"DELETE FROM queue_schedule_drafts WHERE request_id IN ({placeholders})", sorted(submitted_ids))
        _queue_v2_publish(conn, "schedule_submitted", caller, submitted_ids)
        for item in prepared:
            row = dict(conn.execute("SELECT * FROM queue_requests WHERE id = ?", (item["id"],)).fetchone())
            if item.get("pool"):
                adjustments.append({"id": item["id"], "designerEmail": None, "scheduledDate": None, "scheduledStartMinutes": None, "status": "pool"})
                continue
            final_date, final_start = row["scheduled_date"], int(row["scheduled_start_minutes"])
            adjustments.append({"id": item["id"], "designerEmail": item["designer"], "scheduledDate": final_date, "scheduledStartMinutes": final_start})
            notifications.append({"task_id": item["id"], "assignee_email": item["designer"], "assigned_by_email": caller,
                                  "account": row["post_account"], "post_id": _queue_post_id(row["post_account"], row["post_shortcode"]),
                                  "note": row["brief"], "notes": row["notes"], "references": _queue_v2_json(row["reference_links"], []),
                                  "priority": row["priority"], "tags": _queue_v2_json(row["tags"], []),
                                  "recommended_accounts": item["accounts"], "production_points": item["productionPoints"],
                                  "scheduled_date": final_date, "scheduled_start_minutes": final_start,
                                  "update": item["row"]["status"] != "pool" and same_assignees.get(item["id"], False),
                                  "slack_channel_id": row.get("slack_channel_id") if same_assignees.get(item["id"], False) else "",
                                  "slack_message_ts": row.get("slack_message_ts") if same_assignees.get(item["id"], False) else "",
                                  "assignee_slack_id": item["assigneeSlackId"], "assigned_by_slack_id": assigner_slack_id})
    sent = 0
    for item in notifications:
        from .slack_alerts import notify_queue_assignment_result
        delivery = notify_queue_assignment_result(**item)
        delivered = bool(delivery.get("sent"))
        sent += int(delivered)
        with connect() as conn:
            if delivered and delivery.get("channelId") and delivery.get("messageTs"):
                conn.execute(
                    "UPDATE queue_requests SET slack_channel_id = ?, slack_message_ts = ?, updated_at = ? WHERE id = ?",
                    (delivery["channelId"], delivery["messageTs"], utc_now(), item["task_id"]),
                )
            _queue_v2_log(conn, item["task_id"], caller, "slack_sent" if delivered else "slack_failed")
            _queue_v2_publish(conn, "notification_updated", caller, [item["task_id"]])
    return {"ok": True, "submitted": len(entries), "adjustments": adjustments, "notifications": {"sent": sent, "failed": len(notifications) - sent}}


@app.get("/api/dashboard/queue/v2/tickets")
def dashboard_queue_v2_tickets(request: Request) -> dict[str, Any]:
    caller, is_admin, roles = _queue_v2_access(request)
    coordinator = is_admin or "vc" in roles
    with connect() as conn:
        rows = _queue_v2_ticket_rows(conn, requester_email=None if coordinator else caller)
    return {"tickets": [_queue_v2_ticket(row) for row in rows]}


@app.post("/api/dashboard/queue/v2/tickets/time-block")
def dashboard_queue_v2_create_time_block(
    request: Request,
    category: Annotated[str, Form()],
    scheduled_date: Annotated[str, Form()],
    scheduled_start_minutes: Annotated[int, Form()],
    duration_minutes: Annotated[int, Form()],
    title: Annotated[str | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    caller, _, _ = _queue_v2_access(request)
    clean_category = category.strip().lower()
    if clean_category not in QUEUE_V2_TIME_CATEGORIES:
        raise HTTPException(status_code=400, detail="Choose meeting, break, promo, focus, or other.")
    try:
        datetime.strptime(scheduled_date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Schedule date must use YYYY-MM-DD.") from exc
    start, duration = int(scheduled_start_minutes), int(duration_minutes)
    if start % 10 or start < SCHEDULER_START or start >= SCHEDULER_END:
        raise HTTPException(status_code=400, detail="Time blocks must begin in 10-minute scheduler slots.")
    if duration < 10 or duration % 10 or start + duration > SCHEDULER_END:
        raise HTTPException(status_code=400, detail="Time blocks must use 10-minute increments and stay within one day.")
    clean_title = (title or "").strip()[:80] or clean_category.replace("_", " ").title()
    clean_note = (note or "").strip()[:500]
    now = utc_now()
    with connect() as conn:
        _queue_v2_assert_time_available(conn, caller, scheduled_date, start, duration)
        cursor = conn.execute(
            """INSERT INTO queue_tickets
               (ticket_type, requester_email, status, block_category, title, scheduled_date,
                scheduled_start_minutes, duration_minutes, reason, created_at, updated_at)
               VALUES ('time_block', ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (caller, clean_category, clean_title, scheduled_date, start, duration, clean_note, now, now),
        )
        ticket_id = int(cursor.lastrowid)
        _queue_v2_publish(conn, "ticket_created", caller)
        row = dict(conn.execute("SELECT * FROM queue_tickets WHERE id = ?", (ticket_id,)).fetchone())
    return {"ok": True, "ticket": _queue_v2_ticket(row)}


@app.post("/api/dashboard/queue/v2/tickets/pp-revision")
def dashboard_queue_v2_request_pp_revision(
    request: Request,
    request_id: Annotated[int, Form()],
    production_points: Annotated[int, Form()],
    reason: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    caller, _, _ = _queue_v2_access(request)
    row = _queue_v2_request(request_id)
    if row.get("designer_email") != caller:
        raise HTTPException(status_code=403, detail="Only the assigned designer can request a PP revision.")
    if row["status"] not in {"scheduled", "in_progress"}:
        raise HTTPException(status_code=409, detail="PP revisions are available for scheduled or in-progress work.")
    if production_points < 1 or production_points == int(row["production_points"]):
        raise HTTPException(status_code=400, detail="Choose a different positive PP value.")
    now = utc_now()
    with connect() as conn:
        pending = conn.execute(
            "SELECT 1 FROM queue_tickets WHERE ticket_type = 'pp_revision' AND request_id = ? AND status = 'pending'",
            (request_id,),
        ).fetchone()
        if pending:
            raise HTTPException(status_code=409, detail="A PP revision is already pending for this request.")
        cursor = conn.execute(
            """INSERT INTO queue_tickets
               (ticket_type, requester_email, request_id, status, requested_production_points,
                reason, created_at, updated_at)
               VALUES ('pp_revision', ?, ?, 'pending', ?, ?, ?, ?)""",
            (caller, request_id, production_points, (reason or "").strip()[:500], now, now),
        )
        ticket_id = int(cursor.lastrowid)
        _queue_v2_log(conn, request_id, caller, "pp_revision_requested", {"productionPoints": production_points, "reason": (reason or "").strip()})
        _queue_v2_publish(conn, "ticket_created", caller, [request_id])
        ticket_row = dict(conn.execute("SELECT * FROM queue_tickets WHERE id = ?", (ticket_id,)).fetchone())
    return {"ok": True, "ticket": _queue_v2_ticket(ticket_row)}


@app.post("/api/dashboard/queue/v2/tickets/cancellation")
def dashboard_queue_v2_request_cancellation(
    request: Request,
    request_id: Annotated[int, Form()],
    reason: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    caller, _, _ = _queue_v2_access(request)
    row = _queue_v2_request(request_id)
    if row.get("designer_email") != caller:
        raise HTTPException(status_code=403, detail="Only the assigned designer can request cancellation.")
    if row["status"] not in {"scheduled", "in_progress", "completed"}:
        raise HTTPException(status_code=409, detail="This request cannot be cancelled through a ticket.")
    clean_reason = (reason or "").strip()[:500]
    now = utc_now()
    with connect() as conn:
        pending = conn.execute(
            "SELECT 1 FROM queue_tickets WHERE ticket_type = 'cancellation' AND request_id = ? AND status = 'pending'",
            (request_id,),
        ).fetchone()
        if pending:
            raise HTTPException(status_code=409, detail="A cancellation request is already pending.")
        cursor = conn.execute(
            """INSERT INTO queue_tickets
               (ticket_type, requester_email, request_id, status, reason, created_at, updated_at)
               VALUES ('cancellation', ?, ?, 'pending', ?, ?, ?)""",
            (caller, request_id, clean_reason, now, now),
        )
        ticket_id = int(cursor.lastrowid)
        _queue_v2_log(conn, request_id, caller, "cancellation_requested", {"reason": clean_reason})
        _queue_v2_publish(conn, "ticket_created", caller, [request_id])
        ticket_row = dict(conn.execute("SELECT * FROM queue_tickets WHERE id = ?", (ticket_id,)).fetchone())
    return {"ok": True, "ticket": _queue_v2_ticket(ticket_row)}


@app.post("/api/dashboard/queue/v2/tickets/{ticket_id}/review")
def dashboard_queue_v2_review_ticket(
    ticket_id: int,
    request: Request,
    action: Annotated[str, Form()],
    review_note: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    caller, _, _ = _queue_v2_access(request, coordinator=True)
    clean_action = action.strip().lower()
    if clean_action not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="Ticket action must be approve or reject.")
    new_status = "approved" if clean_action == "approve" else "rejected"
    now = utc_now()
    affected_ids: set[int] = set()
    with connect() as conn:
        ticket_row = conn.execute("SELECT * FROM queue_tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not ticket_row:
            raise HTTPException(status_code=404, detail="Queue ticket not found.")
        ticket = dict(ticket_row)
        if ticket["status"] != "pending":
            raise HTTPException(status_code=409, detail="This ticket has already been reviewed.")
        if clean_action == "approve" and ticket["ticket_type"] == "time_block":
            _queue_v2_assert_time_available(
                conn, ticket["requester_email"], ticket["scheduled_date"],
                int(ticket["scheduled_start_minutes"]), int(ticket["duration_minutes"]), exclude_ticket_id=ticket_id,
            )
        elif clean_action == "approve" and ticket["ticket_type"] == "pp_revision":
            queue_row = conn.execute("SELECT * FROM queue_requests WHERE id = ?", (ticket["request_id"],)).fetchone()
            if not queue_row or queue_row["status"] not in {"scheduled", "in_progress"}:
                raise HTTPException(status_code=409, detail="The Queue request can no longer receive a PP revision.")
            queue_item = dict(queue_row)
            new_points = int(ticket["requested_production_points"])
            if queue_item["status"] == "in_progress":
                conflicts = _queue_v2_time_occupied(conn, queue_item["designer_email"], queue_item["scheduled_date"])
                conflicts = [item for item in conflicts if not (
                    item["date"] == queue_item["scheduled_date"]
                    and int(item["start"]) == int(queue_item["scheduled_start_minutes"])
                    and int(item["duration"]) == _queue_v2_duration(queue_item)
                )]
                if any(intervals_conflict(int(queue_item["scheduled_start_minutes"]), new_points * 10, int(item["start"]), int(item["duration"])) for item in conflicts):
                    raise HTTPException(status_code=409, detail="The revised in-progress block would overlap another firm block.")
            conn.execute("UPDATE queue_requests SET production_points = ?, updated_at = ? WHERE id = ?", (new_points, now, ticket["request_id"]))
            _queue_v2_log(conn, ticket["request_id"], caller, "pp_revision_approved", {"from": queue_item["production_points"], "to": new_points})
            if queue_item.get("designer_email"):
                _queue_v2_reflow_scheduled(conn, queue_item["designer_email"], caller, ticket["request_id"] if queue_item["status"] == "scheduled" else None)
            affected_ids.add(int(ticket["request_id"]))
        elif clean_action == "approve" and ticket["ticket_type"] == "cancellation":
            queue_row = conn.execute("SELECT * FROM queue_requests WHERE id = ?", (ticket["request_id"],)).fetchone()
            if not queue_row or queue_row["status"] not in {"scheduled", "in_progress", "completed"}:
                raise HTTPException(status_code=409, detail="The Queue request can no longer be cancelled.")
            conn.execute("DELETE FROM queue_schedule_drafts WHERE request_id = ?", (ticket["request_id"],))
            conn.execute(
                "UPDATE queue_requests SET status = 'cancelled', cancellation_reason = ?, updated_at = ? WHERE id = ?",
                (ticket["reason"], now, ticket["request_id"]),
            )
            _queue_v2_log(conn, ticket["request_id"], caller, "cancellation_approved", {"reason": ticket["reason"]})
            affected_ids.add(int(ticket["request_id"]))
        if ticket.get("request_id") and clean_action == "reject":
            _queue_v2_log(conn, ticket["request_id"], caller, f"{ticket['ticket_type']}_rejected", {"reviewNote": (review_note or "").strip()})
            affected_ids.add(int(ticket["request_id"]))
        conn.execute(
            """UPDATE queue_tickets SET status = ?, reviewer_email = ?, review_note = ?, reviewed_at = ?, updated_at = ?
               WHERE id = ?""",
            (new_status, caller, (review_note or "").strip()[:500], now, now, ticket_id),
        )
        _queue_v2_publish(conn, "ticket_reviewed", caller, affected_ids)
        reviewed = dict(conn.execute(
            """SELECT t.*, r.post_account, r.post_shortcode, r.designer_email,
                      r.status AS request_status, r.production_points AS current_production_points
               FROM queue_tickets t LEFT JOIN queue_requests r ON r.id = t.request_id WHERE t.id = ?""",
            (ticket_id,),
        ).fetchone())
    return {"ok": True, "ticket": _queue_v2_ticket(reviewed)}


@app.post("/api/dashboard/queue/v2/requests/{request_id}/start")
def dashboard_queue_v2_start(request_id: int, request: Request) -> dict[str, Any]:
    caller, is_admin, _ = _queue_v2_access(request)
    row = _queue_v2_request(request_id)
    if not is_admin and row["designer_email"] != caller:
        raise HTTPException(status_code=403, detail="Only the assigned designer can start this request.")
    if row["status"] not in {"scheduled", "completed"}:
        raise HTTPException(status_code=409, detail="Only scheduled or completed work can be started.")
    now = utc_now()
    local_now = datetime.now(SCHEDULER_TIMEZONE)
    current_slot = (local_now.hour * 60 + local_now.minute) // 10 * 10
    current_date = local_now.date().isoformat()
    designer = str(row["designer_email"] or "")
    with connect() as conn:
        conn.execute("DELETE FROM queue_schedule_drafts WHERE request_id = ?", (request_id,))
        active_rows = [dict(item) for item in conn.execute(
            """SELECT * FROM queue_requests
               WHERE designer_email = ? AND status = 'in_progress' AND id != ?
                 AND scheduled_date IS NOT NULL AND scheduled_start_minutes IS NOT NULL
               ORDER BY scheduled_date, scheduled_start_minutes, id""",
            (designer, request_id),
        ).fetchall()]
        if active_rows:
            current_absolute = schedule_absolute(current_date, current_slot)
            active_occupied = []
            for active in active_rows:
                active_absolute = schedule_absolute(active["scheduled_date"], int(active["scheduled_start_minutes"]))
                active_duration = max(_queue_v2_duration(active), current_absolute + 10 - active_absolute)
                active_occupied.append(_queue_v2_occupied(active, active_duration))
            resolved_date, resolved_start = next_available_slot(
                current_date, current_slot, _queue_v2_duration(row), active_occupied,
            )
            conn.execute(
                """UPDATE queue_requests SET status = 'scheduled', scheduled_start_minutes = ?, scheduled_date = ?,
                   actual_started_at = NULL, completed_at = NULL, updated_at = ? WHERE id = ?""",
                (resolved_start, resolved_date, now, request_id),
            )
            _queue_v2_reflow_scheduled(conn, designer, caller, priority_id=request_id)
            updated = dict(conn.execute("SELECT * FROM queue_requests WHERE id = ?", (request_id,)).fetchone())
            _queue_v2_log(conn, request_id, caller, "deferred_after_in_progress", {
                "date": updated["scheduled_date"], "start": updated["scheduled_start_minutes"],
                "activeRequestIds": [item["id"] for item in active_rows],
            })
            _queue_v2_reflow_drafts(conn, designer)
            _queue_v2_publish(conn, "request_deferred", caller, [request_id])
            return {"ok": True, "deferred": True, "scheduledDate": updated["scheduled_date"], "scheduledStartMinutes": updated["scheduled_start_minutes"]}
        conn.execute(
            """UPDATE queue_requests SET status = 'in_progress', scheduled_start_minutes = ?, scheduled_date = ?,
               actual_started_at = ?, completed_at = NULL, updated_at = ? WHERE id = ?""",
            (current_slot, current_date, now, now, request_id),
        )
        _queue_v2_reflow_scheduled(conn, designer, caller)
        _queue_v2_log(conn, request_id, caller, "started", {"date": current_date, "actualStartMinutes": current_slot})
        _queue_v2_reflow_drafts(conn, designer)
        _queue_v2_publish(conn, "request_started", caller, [request_id])
    return {"ok": True, "deferred": False, "scheduledDate": current_date, "scheduledStartMinutes": current_slot}


@app.post("/api/dashboard/queue/v2/requests/{request_id}/edit")
def dashboard_queue_v2_edit(
    request_id: int, request: Request,
    production_points: Annotated[int, Form()], priority: Annotated[str, Form()] = "medium",
    tags: Annotated[str | None, Form()] = None, brief: Annotated[str | None, Form()] = None,
    notes: Annotated[str | None, Form()] = None, references: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Coordinator-owned edits to a production request's specification.

    Scheduling is intentionally still controlled through Submit so assignment
    notifications remain a deliberate action. Changing PP immediately
    cascades scheduled work forward when the larger block needs more room.
    """
    caller, _, _ = _queue_v2_access(request, coordinator=True)
    if production_points < 1:
        raise HTTPException(status_code=400, detail="Production points must be at least 1.")
    row = _queue_v2_request(request_id)
    clean_priority = _queue_v2_priority(priority)
    clean_tags = _queue_v2_tags(tags)
    refs = _queue_v2_json(references, [])
    if not isinstance(refs, list):
        raise HTTPException(status_code=400, detail="Reference links must be a list.")
    clean_refs = [str(item).strip() for item in refs if str(item).strip()]
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """UPDATE queue_requests SET production_points = ?, priority = ?, tags = ?, brief = ?, notes = ?,
               reference_links = ?, updated_at = ? WHERE id = ?""",
            (production_points, clean_priority, json.dumps(clean_tags), (brief or "").strip(), (notes or "").strip(),
             json.dumps(clean_refs), now, request_id),
        )
        # Keep an existing collaborative placement's provisional duration in
        # sync with the edited request so its live block and PP count change
        # immediately, before the next Submit.
        conn.execute(
            "UPDATE queue_schedule_drafts SET production_points = ?, updated_at = ? WHERE request_id = ?",
            (production_points, now, request_id),
        )
        _queue_v2_log(conn, request_id, caller, "edited", {
            "productionPoints": production_points, "priority": clean_priority, "tags": clean_tags,
        })
        if row.get("designer_email") and row.get("status") in {"scheduled", "in_progress", "completed", "closed"}:
            _queue_v2_reflow_scheduled(conn, row["designer_email"], caller, priority_id=request_id if row["status"] == "scheduled" else None)
        draft_designers = [item["designer_email"] for item in conn.execute(
            "SELECT designer_email FROM queue_schedule_drafts WHERE request_id = ?", (request_id,),
        ).fetchall()]
        for designer in draft_designers:
            _queue_v2_reflow_drafts(conn, designer)
        _queue_v2_publish(conn, "request_edited", caller, [request_id])
        updated = dict(conn.execute("SELECT * FROM queue_requests WHERE id = ?", (request_id,)).fetchone())
    return {"ok": True, "request": _queue_v2_project(updated)}


@app.post("/api/dashboard/queue/v2/requests/{request_id}/attachments")
def dashboard_queue_v2_add_attachment(request_id: int, request: Request, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    caller, is_admin, roles = _queue_v2_access(request)
    row = _queue_v2_request(request_id)
    _queue_v2_require_visible(row, caller, is_admin, roles)
    clean_name = Path(file.filename or "attachment").name
    suffix = Path(clean_name).suffix[:12]
    attachment_id = secrets.token_hex(8)
    folder = DATA_DIR / "queue_attachments" / str(request_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{attachment_id}{suffix}"
    payload = file.file.read(20 * 1024 * 1024 + 1)
    if len(payload) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Queue attachments must be 20 MB or smaller.")
    target.write_bytes(payload)
    attachments = _queue_v2_json(row["attachments"], [])
    attachments.append({
        "id": attachment_id, "name": clean_name, "size": len(payload),
        "contentType": file.content_type or "application/octet-stream", "uploadedAt": utc_now(),
    })
    now = utc_now()
    with connect() as conn:
        conn.execute("UPDATE queue_requests SET attachments = ?, updated_at = ? WHERE id = ?", (json.dumps(attachments), now, request_id))
        _queue_v2_log(conn, request_id, caller, "attachment_added", {"id": attachment_id, "name": clean_name, "size": len(payload)})
        _queue_v2_publish(conn, "attachment_added", caller, [request_id])
        updated = dict(conn.execute("SELECT * FROM queue_requests WHERE id = ?", (request_id,)).fetchone())
    return {"ok": True, "request": _queue_v2_project(updated)}


@app.get("/api/dashboard/queue/v2/requests/{request_id}/attachments/{attachment_id}")
def dashboard_queue_v2_attachment(request_id: int, attachment_id: str, request: Request) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{16}", attachment_id):
        raise HTTPException(status_code=404, detail="Queue attachment not found.")
    caller, is_admin, roles = _queue_v2_access(request)
    row = _queue_v2_request(request_id)
    _queue_v2_require_visible(row, caller, is_admin, roles)
    attachment = next((item for item in _queue_v2_json(row["attachments"], []) if item.get("id") == attachment_id), None)
    if not attachment:
        raise HTTPException(status_code=404, detail="Queue attachment not found.")
    folder = DATA_DIR / "queue_attachments" / str(request_id)
    matches = list(folder.glob(f"{attachment_id}.*")) + ([folder / attachment_id] if (folder / attachment_id).exists() else [])
    if not matches or not matches[0].is_file():
        raise HTTPException(status_code=404, detail="Queue attachment file not found.")
    return FileResponse(matches[0], media_type=attachment.get("contentType") or "application/octet-stream", filename=attachment.get("name") or "attachment")


@app.post("/api/dashboard/queue/v2/requests/{request_id}/complete")
def dashboard_queue_v2_complete(request_id: int, request: Request) -> dict[str, Any]:
    caller, is_admin, _ = _queue_v2_access(request)
    row = _queue_v2_request(request_id)
    if not is_admin and row["designer_email"] != caller:
        raise HTTPException(status_code=403, detail="Only the assigned designer can complete this request.")
    if row["status"] != "in_progress":
        raise HTTPException(status_code=409, detail="Start the request before completing it.")
    now = utc_now()
    with connect() as conn:
        conn.execute("UPDATE queue_requests SET status = 'completed', completed_at = ?, updated_at = ? WHERE id = ?", (now, now, request_id))
        _queue_v2_log(conn, request_id, caller, "completed")
        _queue_v2_publish(conn, "request_completed", caller, [request_id])
    return {"ok": True}


@app.post("/api/dashboard/queue/v2/requests/{request_id}/close")
def dashboard_queue_v2_close(request_id: int, request: Request, final_permalink: Annotated[str, Form()]) -> dict[str, Any]:
    caller, is_admin, _ = _queue_v2_access(request)
    row = _queue_v2_request(request_id)
    if not is_admin and row["designer_email"] != caller:
        raise HTTPException(status_code=403, detail="Only the assigned designer can close this request.")
    link = final_permalink.strip()
    if not re.match(r"^https?://(www\.)?instagram\.com/(p|reel)/[^/?#]+/?", link):
        raise HTTPException(status_code=400, detail="Add a valid Instagram post or Reel permalink.")
    if row["status"] != "completed":
        raise HTTPException(status_code=409, detail="Complete the request before closing it.")
    now = utc_now()
    with connect() as conn:
        conn.execute("UPDATE queue_requests SET status = 'closed', final_permalink = ?, closed_at = ?, updated_at = ? WHERE id = ?", (link, now, now, request_id))
        _queue_v2_log(conn, request_id, caller, "closed", {"finalPermalink": link})
        _queue_v2_publish(conn, "request_closed", caller, [request_id])
    return {"ok": True}


@app.post("/api/dashboard/queue/v2/requests/{request_id}/cancel")
def dashboard_queue_v2_cancel(request_id: int, request: Request, reason: Annotated[str | None, Form()] = None) -> dict[str, Any]:
    caller, _, _ = _queue_v2_access(request, coordinator=True)
    row = _queue_v2_request(request_id)
    now = utc_now()
    with connect() as conn:
        conn.execute("DELETE FROM queue_schedule_drafts WHERE request_id = ?", (request_id,))
        conn.execute("UPDATE queue_requests SET status = 'cancelled', cancellation_reason = ?, updated_at = ? WHERE id = ?", ((reason or "").strip(), now, request_id))
        _queue_v2_log(conn, request_id, caller, "cancelled", {"reason": (reason or "").strip()})
        _queue_v2_publish(conn, "request_cancelled", caller, [request_id])
    return {"ok": True}


@app.post("/api/dashboard/queue/v2/requests/{request_id}/notify")
def dashboard_queue_v2_notify(request_id: int, request: Request) -> dict[str, Any]:
    caller, _, _ = _queue_v2_access(request, coordinator=True)
    row = _queue_v2_request(request_id)
    if not row["designer_email"]:
        raise HTTPException(status_code=409, detail="Schedule this request before sending a notification.")
    with connect() as conn:
        recipient = conn.execute("SELECT slack_user_id FROM dashboard_users WHERE email = ?", (row["designer_email"],)).fetchone()
        assigner = conn.execute("SELECT slack_user_id FROM dashboard_users WHERE email = ?", (caller,)).fetchone()
    from .slack_alerts import notify_queue_assignment_result, slack_user_id_for_email
    sent_result = notify_queue_assignment_result(
        task_id=request_id, assignee_email=row["designer_email"], assigned_by_email=caller,
        assignee_slack_id=(recipient["slack_user_id"] if recipient else "") or slack_user_id_for_email(row["designer_email"]),
        assigned_by_slack_id=(assigner["slack_user_id"] if assigner else "") or slack_user_id_for_email(caller),
        account=row["post_account"], post_id=_queue_post_id(row["post_account"], row["post_shortcode"]),
        note=row["brief"], notes=row["notes"], references=_queue_v2_json(row["reference_links"], []),
        priority=row["priority"], tags=_queue_v2_json(row["tags"], []),
        recommended_accounts=_queue_v2_json(row["recommended_accounts"], []), production_points=row["production_points"],
        scheduled_date=row["scheduled_date"], scheduled_start_minutes=row["scheduled_start_minutes"], update=True,
        slack_channel_id=row.get("slack_channel_id") or "", slack_message_ts=row.get("slack_message_ts") or "",
    )
    sent = bool(sent_result.get("sent"))
    with connect() as conn:
        if sent and sent_result.get("channelId") and sent_result.get("messageTs"):
            conn.execute(
                "UPDATE queue_requests SET slack_channel_id = ?, slack_message_ts = ?, updated_at = ? WHERE id = ?",
                (sent_result["channelId"], sent_result["messageTs"], utc_now(), request_id),
            )
        _queue_v2_log(conn, request_id, caller, "slack_sent" if sent else "slack_failed", {"manualRetry": True})
        _queue_v2_publish(conn, "notification_updated", caller, [request_id])
    return {"ok": True, "sent": sent}


@app.get("/api/dashboard/queue/v2/requests/{request_id}/history")
def dashboard_queue_v2_history(request_id: int, request: Request) -> dict[str, Any]:
    caller, is_admin, roles = _queue_v2_access(request)
    row = _queue_v2_request(request_id)
    _queue_v2_require_visible(row, caller, is_admin, roles)
    with connect() as conn:
        rows = conn.execute("SELECT actor_email, event_type, details, created_at FROM queue_request_events WHERE request_id = ? ORDER BY id DESC", (request_id,)).fetchall()
    return {"events": [{"actorEmail": item["actor_email"], "type": item["event_type"], "details": _queue_v2_json(item["details"], {}), "createdAt": item["created_at"]} for item in rows]}


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


_ALERT_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
# Screenshots and phone photos land well under this; the real limit is
# httpx's own request timeout on the Slack post, not disk space.
_ALERT_IMAGE_MAX_BYTES = 8 * 1024 * 1024
_ALERT_IMAGE_NAME_RE = re.compile(r"^alert-[0-9a-f]{32}\.(jpg|png|gif|webp)$")


@app.post("/api/admin/slack-custom")
def admin_slack_custom(
    password: Annotated[str, Form()],
    message: Annotated[str, Form()],
    title: Annotated[str | None, Form()] = None,
    image: Annotated[UploadFile | None, File()] = None,
) -> dict[str, Any]:
    """Free-form Slack alert typed in from the admin panel's System tab --
    for anything worth flagging that doesn't have its own purpose-built
    alert (HOT posts, disk, snapshot failures). image is optional: a
    screenshot pasted or uploaded alongside the message, saved here and
    referenced by URL in the Slack message (Slack's Block Kit image block
    needs a URL it can fetch, not raw bytes)."""
    if not TRICKS_DASH_REFRESH_PASSWORD or not secrets.compare_digest(
        password.strip(), TRICKS_DASH_REFRESH_PASSWORD
    ):
        raise HTTPException(status_code=401, detail="Incorrect refresh password.")

    from .slack_alerts import alert_image_url_for, notify_custom, slack_configured

    if not slack_configured():
        raise HTTPException(status_code=503, detail="SLACK_WEBHOOK_URL is not set on the server.")

    clean_message = message.strip()
    if not clean_message:
        raise HTTPException(status_code=400, detail="Message is required.")

    image_url: str | None = None
    if image is not None and image.filename:
        suffix = _ALERT_IMAGE_EXTENSIONS.get((image.content_type or "").lower())
        if not suffix:
            raise HTTPException(status_code=400, detail="Only JPEG, PNG, GIF, or WebP images are supported.")
        data = image.file.read(_ALERT_IMAGE_MAX_BYTES + 1)
        if len(data) > _ALERT_IMAGE_MAX_BYTES:
            raise HTTPException(status_code=400, detail="Image is too large (8 MB max).")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"alert-{secrets.token_hex(16)}{suffix}"
        (UPLOAD_DIR / filename).write_bytes(data)
        image_url = alert_image_url_for(filename)

    sent = notify_custom(clean_message, title=(title or "").strip() or None, image_url=image_url)
    return {"sent": sent}


@app.get("/api/admin/alert-image/{filename}")
def admin_alert_image(filename: str) -> FileResponse:
    """Serves an image attached to a custom alert. Unauthenticated on
    purpose -- Slack's own servers fetch this URL to render the image
    inline in the message, and can't send a Bearer token when they do.
    Filenames are a random 32-hex-char token generated server-side (see
    admin_slack_custom above), not user input, so the regex here is just a
    path-traversal guard, not a real access check."""
    if not _ALERT_IMAGE_NAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="Not found.")
    path = UPLOAD_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(path)


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

    The average itself only looks at the last 30 days of posts, so a recent
    slump or surge in engagement moves the suggestion instead of it being
    dragged down by a post's whole history. An account tracked for less
    than 30 days just averages whatever it has so far -- the window is a
    ceiling on how far back to look, never a floor on how much data is
    required.
    """
    accounts = list_accounts(active_only=False)
    cutoff_30 = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")

    with connect() as conn:
        dash_rows = conn.execute(
            """
            SELECT account,
                   AVG(CASE WHEN hot_checked = 1 AND published_at >= ? THEN likes_at_1h END) AS avg_likes,
                   COUNT(CASE WHEN hot_checked = 1 AND published_at >= ? THEN likes_at_1h END) AS n_posts,
                   COUNT(*) AS total_posts,
                   MIN(published_at) AS oldest_post_at
            FROM dashboard_posts
            GROUP BY account
            """,
            (cutoff_30, cutoff_30),
        ).fetchall()
        canonical_row = conn.execute(
            """
            SELECT AVG(CASE WHEN hot_checked = 1 AND published_at >= ? THEN likes_at_1h END) AS avg_likes,
                   COUNT(CASE WHEN hot_checked = 1 AND published_at >= ? THEN likes_at_1h END) AS n_posts,
                   COUNT(*) AS total_posts,
                   MIN(published_at) AS oldest_post_at
            FROM posts
            """,
            (cutoff_30, cutoff_30),
        ).fetchone()

    stats_by_handle = {row["account"]: dict(row) for row in dash_rows}
    snapshots_by_handle = all_account_snapshots()

    for account in accounts:
        stat = dict(canonical_row) if account["is_canonical"] and canonical_row else stats_by_handle.get(account["handle"])
        avg_likes = stat["avg_likes"] if stat else None
        n_posts = stat["n_posts"] if stat else 0
        account["avg_likes"] = round(avg_likes) if avg_likes is not None else None
        account["avg_likes_sample_size"] = n_posts
        # Real post count on file, unlike n_posts above (which is scoped to
        # hot_checked rows for the avg-likes sample) -- this is what the
        # admin table shows so it's actually meaningful for backfilled
        # accounts too, which never go through the HOT check at all.
        account["total_posts"] = stat["total_posts"] if stat else 0
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
    operating_role: Annotated[str | None, Form()] = None,
    is_admin: Annotated[bool | None, Form()] = None,
    slack_user_id: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Add a new allowed email, or change an existing one's role. Admins can
    add other admins or plain viewers; there's no further gate here because
    /api/admin/* already requires an admin session to reach this at all."""
    clean_email = email.strip().lower()
    if not clean_email or "@" not in clean_email:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if role not in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'viewer'.")
    if operating_role is not None and operating_role not in {"vc", "pd", "sales"}:
        raise HTTPException(status_code=400, detail="Operating role must be VC, PD, or Sales.")
    clean_slack_id = None if slack_user_id is None else slack_user_id.strip().upper()
    if clean_slack_id and not re.fullmatch(r"U[A-Z0-9]{8,20}", clean_slack_id):
        raise HTTPException(status_code=400, detail="Slack user ID must start with U.")
    upsert_dashboard_user(clean_email, role, operating_role, is_admin, clean_slack_id)
    with connect() as conn:
        _queue_v2_publish(conn, "roster_updated", _caller_email(request))
    return {"ok": True, "users": list_dashboard_users()}


@app.get("/api/admin/queue/designer-accounts")
def admin_queue_designer_accounts() -> dict[str, Any]:
    return {"designers": _queue_v2_designers()}


@app.post("/api/admin/queue/designer-accounts")
def admin_queue_add_designer_account(
    request: Request, designer_email: Annotated[str, Form()], account_handle: Annotated[str, Form()]
) -> dict[str, Any]:
    designer = designer_email.strip().lower()
    handle = account_handle.strip().lstrip("@").lower()
    if not handle:
        raise HTTPException(status_code=400, detail="Choose an account.")
    with connect() as conn:
        user = conn.execute("SELECT operating_role, operating_roles FROM dashboard_users WHERE email = ?", (designer,)).fetchone()
        user_roles = _queue_v2_user_roles(dict(user)) if user else []
        if not user or "pd" not in user_roles:
            raise HTTPException(status_code=400, detail="Choose a designer.")
        account = conn.execute("SELECT handle FROM accounts WHERE handle = ? AND group_name = 'sentient'", (handle,)).fetchone()
        if not account:
            raise HTTPException(status_code=400, detail="Choose an active Sentient account.")
        conn.execute("INSERT OR IGNORE INTO queue_designer_accounts (designer_email, account_handle, created_at) VALUES (?, ?, ?)", (designer, handle, utc_now()))
        _queue_v2_publish(conn, "designer_accounts_updated", _caller_email(request))
    return {"ok": True, "designers": _queue_v2_designers()}


@app.delete("/api/admin/queue/designer-accounts")
def admin_queue_remove_designer_account(request: Request, designer_email: Annotated[str, Query()], account_handle: Annotated[str, Query()]) -> dict[str, Any]:
    with connect() as conn:
        conn.execute("DELETE FROM queue_designer_accounts WHERE designer_email = ? AND account_handle = ?", (designer_email.strip().lower(), account_handle.strip().lstrip("@").lower()))
        _queue_v2_publish(conn, "designer_accounts_updated", _caller_email(request))
    return {"ok": True, "designers": _queue_v2_designers()}


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
    with connect() as conn:
        _queue_v2_publish(conn, "roster_updated", _caller_email(request))
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


_BACKFILL_RUN: dict[str, Any] = {
    "running": False,
    "handle": None,
    "result": None,
    "error": None,
    "progress": None,
    "started_at": None,
}


def _backfill_worker(
    handle: str, results_limit: int, date_from: str | None = None, date_to: str | None = None
) -> None:
    # Fine-grained phase updates (starting the Apify run, waiting on it,
    # downloading the dataset, saving each post's cover) so the admin UI can
    # show what's actually happening during what's otherwise a multi-minute
    # black box -- see run_backfill()'s on_progress parameter.
    def on_progress(progress: dict[str, Any]) -> None:
        _BACKFILL_RUN["progress"] = progress

    try:
        _BACKFILL_RUN["result"] = run_backfill(
            handle,
            results_limit=results_limit,
            date_from=date_from,
            date_to=date_to,
            on_progress=on_progress,
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
    _BACKFILL_RUN.update(
        {
            "running": True,
            "handle": handle,
            "result": None,
            "error": None,
            "progress": {"phase": "queued"},
            "started_at": time.time(),
        }
    )
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
