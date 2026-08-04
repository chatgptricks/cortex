from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from bisect import bisect_right
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .apify_sync import (
    VALID_GROUPS,
    ApifySyncError,
    create_account,
    fetch_and_store_avatar,
    fetch_profile_preview,
    get_account_config,
    list_accounts,
    run_backfill,
    run_manual_refresh,
    store_avatar_from_url,
)
from .calibration import fit_calibration, predict_likes
from .config import (
    ALLOWED_IMAGE_SUFFIXES,
    ANALYSIS_DIR,
    DATA_DIR,
    DEFAULT_VIDEO_SECONDS,
    EXTRA_CORS_ORIGINS,
    OCR_BATCH_MIN_READY,
    OCR_BATCH_SIZE,
    OCR_CROP_REGION,
    PREDICT_API_KEY,
    PREDICT_PASSWORD,
    PREDICT_USERNAME,
    TRICKS_DASH_REFRESH_PASSWORD,
    UPLOAD_DIR,
    VIDEO_DIR,
    ensure_directories,
)
from .db import connect, init_db, row_to_post, utc_now
from .instagram_import import (
    InstagramImportError,
    fetch_instagram_post,
    sync_instagram_profile_posts,
)
from .llm_report import LlmReportUnavailable, generate_llm_report, llm_report_status
from .prediction_model import fit_advanced_prediction, predict_performance, prediction_payload
from .prediction_v2 import fit_multi_signal, multi_signal_payload, predict_multi_signal
from .remote_ocr import RemoteOcrUnavailable, extract_images_text_remote, remote_ocr_status
from .remote_tribe import RemoteTribeUnavailable, analyze_video_remote, remote_tribe_status
from .scheduler import start_scheduler
from .tribe_adapter import TribeUnavailable, analyze_video, tribe_status, write_analysis
from .video import create_static_video


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


@app.middleware("http")
async def _require_api_key(request, call_next):  # type: ignore[no-untyped-def]
    if PREDICT_API_KEY and request.method != "OPTIONS":
        path = request.url.path
        # /api/health stays open (Render health checks); /api/auth/check is the
        # login probe and validates the key itself.
        if (
            (path.startswith("/api") or path.startswith("/media"))
            and path not in {"/api/health", "/api/auth/check", "/api/auth/login"}
            # Sentient Dash's unified account/post routes are a read-only
            # public explorer (plus password-gated refresh, checked inside
            # the handlers), same pattern as the old tricks-dash/
            # traselveloreal/ routes it replaced.
            and not path.startswith("/api/dashboard/")
            # Same read-only public surface, aggregate projection for the
            # Insights site. Carries no captions, no cover URLs and no
            # credentials -- strictly the numbers already visible on the
            # public dashboard, reshaped for analysis.
            and not path.startswith("/api/insights/")
            # /api/admin/* routes are individually password-gated inside
            # their own handlers (TRICKS_DASH_REFRESH_PASSWORD), same as
            # tricks-dash/traselveloreal -- no need to also require the
            # main admin API key.
            and not path.startswith("/api/admin/")
        ):
            provided = request.headers.get("x-api-key") or request.query_params.get("token")
            if provided != PREDICT_API_KEY:
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "Invalid or missing API key."}, status_code=401)
    return await call_next(request)


@app.on_event("startup")
def startup() -> None:
    init_db()
    _seed_default_metadata_options()
    start_scheduler()


ensure_directories()
app.mount("/media", StaticFiles(directory=DATA_DIR), name="media")

_CALIBRATION_CACHE: dict[str, Any] = {}
_PREDICTION_MODEL_CACHE: dict[str, Any] = {}
_PREDICTION_V2_CACHE: dict[str, Any] = {}
_MODEL_LOG = logging.getLogger("uvicorn.error")
_FIT_LOCK = threading.Lock()


@app.get("/api/auth/check")
def auth_check(request: Request) -> dict[str, Any]:
    if not PREDICT_API_KEY:
        return {"auth_required": False, "ok": True}
    provided = request.headers.get("x-api-key") or request.query_params.get("token")
    return {"auth_required": True, "ok": provided == PREDICT_API_KEY}


@app.post("/api/auth/login")
def auth_login(
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> dict[str, Any]:
    if not PREDICT_API_KEY:
        return {"ok": True, "token": ""}
    if secrets.compare_digest(username.strip(), PREDICT_USERNAME) and secrets.compare_digest(
        password, PREDICT_PASSWORD or ""
    ):
        return {"ok": True, "token": PREDICT_API_KEY}
    time.sleep(0.8)  # slow down brute-force attempts
    raise HTTPException(status_code=401, detail="Invalid username or password.")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "deployment": {
            "commit": os.getenv("RENDER_GIT_COMMIT"),
            "service": os.getenv("RENDER_SERVICE_NAME"),
        },
        "tribev2": tribe_status(),
        "llm_report": llm_report_status(),
        "remote_tribe": remote_tribe_status(),
        "remote_ocr": remote_ocr_status(),
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
        {"handle": a["handle"], "group": a["group"], "threshold": thresholds.get(a["handle"])}
        for a in accounts
    ]}


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
                       source_row_number, created_at, section, is_hot, hot_rate_multiplier
                FROM posts
                """
            ).fetchall()
        else:
            canonical_rows = []
        dashboard_rows = conn.execute(
            """
            SELECT id, account, shortcode, published_at, likes, comments, caption,
                   post_type_label, is_animated, permalink, is_hot, hot_rate_multiplier,
                   hook_text, music_song, music_artist, music_audio_id, uses_original_audio
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
    """Full roster including inactive accounts, for the admin UI."""
    return {"accounts": list_accounts(active_only=False)}


_OCR_RUN: dict[str, Any] = {"running": False, "done": 0, "with_text": 0, "batches": 0, "error": None, "started": None}
_OCR_RUN_LOCK = threading.Lock()


def _ocr_worker(crop_region: str, batch_size: int, max_batches: int) -> None:
    """Drains the OCR backlog by calling the exact same run_ocr_sweep() the
    hourly scheduler uses -- just in a tight loop instead of once per tick, so
    the one-time backlog clears in hours rather than days. Running the real
    production path (not a parallel copy) means this also exercises the cover
    re-download and canonical-posts handling.
    """
    from .apify_sync import run_ocr_sweep

    try:
        for _ in range(max_batches):
            result = run_ocr_sweep(limit=batch_size, crop_region=crop_region)
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


def _backfill_worker(handle: str, results_limit: int) -> None:
    try:
        _BACKFILL_RUN["result"] = run_backfill(handle, results_limit=results_limit)
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
def temp_backfill_bg(handle: str, password: Annotated[str, Form()], results_limit: int = 2000) -> dict[str, Any]:
    """runs a backfill in a background thread so a client
    disconnect (or a slow scrape) can't abort it."""
    _require_admin(password)
    if _BACKFILL_RUN["running"]:
        return {"already_running": True, **_BACKFILL_RUN}
    _BACKFILL_RUN.update({"running": True, "handle": handle, "result": None, "error": None})
    threading.Thread(
        target=_backfill_worker, args=(handle, results_limit), daemon=True, name=f"backfill-{handle}"
    ).start()
    return {"started": True, "handle": handle, "results_limit": results_limit}


@app.get("/api/admin/accounts/backfill-status")
def temp_backfill_status() -> dict[str, Any]:
    """progress of the background backfill."""
    return dict(_BACKFILL_RUN)


@app.post("/api/admin/ocr/start")
def temp_ocr_start(
    password: Annotated[str, Form()],
    crop_region: str = "full",
    batch_size: int = 100,
    max_batches: int = 200,
    workers: int = 3,
) -> dict[str, Any]:
    """kick off the background OCR sweep. `workers` threads run in
    parallel; row claiming is serialized so they never process the same cover
    twice. Remove after use.
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
        threading.Thread(
            target=_ocr_worker, args=(crop_region, size, max_batches), daemon=True, name=f"ocr-sweep-{i}"
        ).start()
    return {"started": True, "crop_region": crop_region, "batch_size": size, "workers": workers, "released": released}


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
_PREVIEW_MAX_PER_MIN = 10
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


@app.get("/api/admin/_temp-music-test")
def temp_music_test(password: str, shortcode: str) -> dict[str, Any]:
    """One-off, read-only: re-scrapes a single already-known post via
    resultsType='details' (the expensive per-URL mode) to check whether it
    returns musicInfo for carousels, which resultsType='posts' (what the
    scheduler/backfills actually use) never does. Writes nothing to the DB.
    Remove after use.
    """
    _require_admin(password)
    from .apify_sync import _run_apify_actor_and_fetch

    with connect() as conn:
        row = conn.execute(
            "SELECT permalink, post_type_label FROM dashboard_posts WHERE shortcode = ?", (shortcode,)
        ).fetchone()
    if not row or not row["permalink"]:
        raise HTTPException(status_code=404, detail="Post not found or has no permalink.")

    payload = {"directUrls": [row["permalink"]], "resultsType": "details"}
    items = _run_apify_actor_and_fetch(payload, max_wait_seconds=180.0)
    item = items[0] if items else {}
    return {
        "shortcode": shortcode,
        "post_type_label": row["post_type_label"],
        "musicInfo": item.get("musicInfo"),
        "keys": sorted(item.keys()) if item else [],
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


_TEST_HOT_SHORTCODE_PREFIX = "TESTHOT-"
_TEST_HOT_SCENARIOS = [
    ("TESTHOT-1x", 1.2, "Tier 1 - just over the line, base badge"),
    ("TESTHOT-2x", 2.4, "Tier 2 - bigger + brighter badge"),
    ("TESTHOT-3x", 3.3, "Tier 3 - fire creeping up the card"),
    ("TESTHOT-5x", 5.6, "Tier 4 - blazing: glare sweep + outer glow"),
    ("TESTHOT-8x", 8.0, "Tier 4 - extreme case, sanity check"),
]


@app.get("/api/calibration")
def calibration() -> dict[str, Any]:
    posts = _all_posts()
    model = _fit_calibration_cached(posts)
    return _calibration_payload(model)


@app.get("/api/prediction-model")
def prediction_model() -> dict[str, Any]:
    posts = _all_posts()
    model = _fit_prediction_model_cached(posts)
    payload = prediction_payload(model)
    payload["multi_signal_v2"] = multi_signal_payload(_fit_prediction_v2_cached(posts))
    return payload


@app.post("/api/post-db/ocr/modal-batch")
def run_post_db_modal_ocr_batch(
    start: int = Query(default=1),
    limit: int = Query(default=OCR_BATCH_SIZE),
    min_ready: int = Query(default=OCR_BATCH_MIN_READY),
    crop_region: str = Query(default=OCR_CROP_REGION),
) -> dict[str, Any]:
    if limit < min_ready:
        raise HTTPException(status_code=400, detail=f"Modal OCR batch size must be at least {min_ready}.")
    eligible_count, rows = _eligible_modal_ocr_posts(start=start, limit=limit)
    if eligible_count < min_ready:
        raise HTTPException(
            status_code=400,
            detail=f"Modal OCR needs at least {min_ready} completed Post DB posts with blank OCR; {eligible_count} are ready.",
        )
    if len(rows) < min(limit, min_ready):
        raise HTTPException(status_code=400, detail="Some eligible image files are missing locally.")
    try:
        results = extract_images_text_remote([Path(row["image_path"]) for row in rows], crop_region=crop_region)
    except RemoteOcrUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    updated_ids: list[int] = []
    with connect() as conn:
        for row, result in zip(rows, results, strict=False):
            text = str(result.get("text") or "").strip() if isinstance(result, dict) else ""
            if not text:
                continue
            conn.execute(
                """
                UPDATE posts
                SET hook_text = ?, updated_at = ?
                WHERE id = ?
                  AND TRIM(COALESCE(hook_text, '')) = ''
                """,
                (text, utc_now(), int(row["id"])),
            )
            updated_ids.append(int(row["id"]))
    return {
        "eligible_count": eligible_count,
        "processed_count": len(results),
        "updated_count": len(updated_ids),
        "crop_region": crop_region,
        "posts": [decorate_post(_get_post_or_404(post_id)) for post_id in updated_ids],
    }


@app.post("/api/post-db/ocr/missing")
def run_post_db_missing_ocr_batch(
    limit: int = Query(default=OCR_BATCH_SIZE),
    crop_region: str = Query(default="full"),
) -> dict[str, Any]:
    eligible_count, rows = _eligible_missing_ocr_posts(limit=limit)
    if not rows:
        return {
            "eligible_count": eligible_count,
            "processed_count": 0,
            "updated_count": 0,
            "crop_region": crop_region,
            "posts": [],
        }
    try:
        results = extract_images_text_remote([Path(row["image_path"]) for row in rows], crop_region=crop_region)
    except RemoteOcrUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    updated_ids: list[int] = []
    with connect() as conn:
        for row, result in zip(rows, results, strict=False):
            text = str(result.get("text") or "").strip() if isinstance(result, dict) else ""
            if not text:
                continue
            conn.execute(
                """
                UPDATE posts
                SET hook_text = ?, updated_at = ?
                WHERE id = ?
                  AND TRIM(COALESCE(hook_text, '')) = ''
                """,
                (text, utc_now(), int(row["id"])),
            )
            updated_ids.append(int(row["id"]))

    return {
        "eligible_count": eligible_count,
        "processed_count": len(results),
        "updated_count": len(updated_ids),
        "crop_region": crop_region,
        "posts": [decorate_post(_get_post_or_404(post_id)) for post_id in updated_ids],
    }


@app.get("/api/posts")
def list_posts(section: str | None = Query(default=None)) -> dict[str, Any]:
    if section == "historical":
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT id, section, title, published_at, likes,
                       person_label, company_label, post_type_label,
                       source_ref, source_row_number, shortcode,
                       image_path, original_filename, status, error,
                       progress_percent, progress_message, tags, hook_text,
                       is_animated, comments, created_at, updated_at,
                       analysis_path IS NOT NULL AS has_analysis_summary,
                       brain_global_mean_abs,
                       brain_global_peak_abs,
                       virality_potential
                FROM posts
                WHERE section = ?
                ORDER BY created_at DESC
                """,
                ("historical",),
            ).fetchall()
        posts = [_lightweight_historical_post(row_to_post(row)) for row in rows]
        return {"posts": posts, "calibration": {"ready": False, "sample_count": 0, "feature_order": []}}

    all_p = _all_posts()
    calib = _fit_calibration_cached(all_p)
    with connect() as conn:
        if section:
            rows = conn.execute(
                "SELECT * FROM posts WHERE section = ? ORDER BY created_at DESC",
                (section,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM posts ORDER BY created_at DESC").fetchall()
    if section == "historical":
        posts = [_lightweight_historical_post(row_to_post(row)) for row in rows]
    else:
        percentile_values = _tribe_percentile_reference(all_p)
        posts = [
            decorate_post(row_to_post(row), all_p, calib, None, percentile_values)
            for row in rows
        ]
    return {"posts": posts, "calibration": _calibration_payload(calib)}


@app.get("/api/posts/{post_id}")
def get_post(post_id: int) -> dict[str, Any]:
    post = _get_post_or_404(post_id)
    return {"post": decorate_post(post)}


@app.get("/api/metadata-options")
def metadata_options() -> dict[str, list[str]]:
    with connect() as conn:
        post_rows = conn.execute(
            """
            SELECT person_label, company_label, post_type_label, tags
            FROM posts
            """
        ).fetchall()
        option_rows = conn.execute("SELECT kind, label FROM metadata_options").fetchall()

    all_people = []
    all_companies = []
    all_tags = []
    
    def _parse_or_wrap(val: str | None) -> list[str]:
        if not val:
            return []
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return [str(i) for i in parsed]
        except Exception:
            pass
        return [val]

    for row in post_rows:
        all_people.extend(_parse_or_wrap(row["person_label"]))
        all_companies.extend(_parse_or_wrap(row["company_label"]))
        all_tags.extend(_parse_or_wrap(row["tags"]))

    return {
        "people": _merged_options(
            DEFAULT_PERSON_OPTIONS,
            [row["label"] for row in option_rows if row["kind"] == "person"] + all_people,
        ),
        "companies": _merged_options(
            DEFAULT_COMPANY_OPTIONS,
            [row["label"] for row in option_rows if row["kind"] == "company"] + all_companies,
        ),
        "post_types": _merged_options(
            DEFAULT_POST_TYPE_OPTIONS,
            [row["label"] for row in option_rows if row["kind"] == "post_type"]
            + [row["post_type_label"] for row in post_rows],
        ),
        "tags": _merged_options([], all_tags),
    }


@app.post("/api/posts")
def create_post(
    background_tasks: BackgroundTasks,
    title: Annotated[str, Form()],
    section: Annotated[str, Form()] = "single",
    caption: Annotated[str | None, Form()] = None,
    published_at: Annotated[str | None, Form()] = None,
    likes: Annotated[str | None, Form()] = None,
    person_label: Annotated[str | None, Form()] = None,
    company_label: Annotated[str | None, Form()] = None,
    post_type_label: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form()] = None,
    hook_text: Annotated[str | None, Form()] = None,
    is_animated: Annotated[bool, Form()] = False,
    comments: Annotated[int | None, Form()] = None,
    duration_seconds: Annotated[int, Form()] = DEFAULT_VIDEO_SECONDS,
    analyze_now: Annotated[bool, Form()] = True,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    if section not in {"single", "historical", "ab"}:
        raise HTTPException(status_code=400, detail="section must be single, historical, or ab")
    image_path = _save_upload(file)
    now = utc_now()
    clean_person = person_label
    clean_company = company_label
    clean_post_type = _clean_metadata_label(post_type_label)
    clean_hook_text = hook_text
    post_likes = _normalized_likes(likes, section)
    status, progress_percent, progress_message = _initial_post_state(section, analyze_now)
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO posts (
                section, title, caption, published_at, likes,
                person_label, company_label, post_type_label,
                tags, hook_text, is_animated, comments,
                image_path, original_filename, status, progress_percent, progress_message,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                section,
                title.strip() or "Untitled cover",
                caption,
                published_at,
                post_likes,
                clean_person,
                clean_company,
                clean_post_type,
                tags,
                clean_hook_text,
                int(is_animated),
                comments,
                str(image_path),
                file.filename,
                status,
                progress_percent,
                progress_message,
                now,
                now,
            ),
        )
        post_id = int(cursor.lastrowid)
        _save_metadata_options(conn, clean_person, clean_company, clean_post_type)
    if analyze_now:
        background_tasks.add_task(run_analysis_job, post_id, duration_seconds)
    post = _get_post_or_404(post_id)
    return {"post": decorate_post(post)}


@app.post("/api/posts/instagram-link")
def create_post_from_instagram_link(
    background_tasks: BackgroundTasks,
    instagram_url: Annotated[str, Form()],
    cover_image_url: Annotated[str | None, Form()] = None,
    duration_seconds: Annotated[int, Form()] = DEFAULT_VIDEO_SECONDS,
    analyze_now: Annotated[bool, Form()] = True,
) -> dict[str, Any]:
    try:
        imported = fetch_instagram_post(instagram_url, cover_image_url=cover_image_url)
    except InstagramImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    source_ref = f"instagram:{imported.shortcode}"
    with connect() as conn:
        existing = conn.execute("SELECT * FROM posts WHERE source_ref = ?", (source_ref,)).fetchone()
    if existing:
        existing_post = row_to_post(existing)
        update_values: dict[str, Any] = {"updated_at": utc_now()}
        if imported.caption and not existing_post.get("caption"):
            update_values["caption"] = imported.caption
        if imported.title and (
            not existing_post.get("title")
            or str(existing_post.get("title")).startswith("Instagram post ")
        ):
            update_values["title"] = imported.title
        existing_image_path = Path(str(existing_post.get("image_path") or ""))
        if not existing_image_path.exists():
            image_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{imported.image_suffix}"
            image_path.write_bytes(imported.image_bytes)
            update_values["image_path"] = str(image_path)
            update_values["original_filename"] = f"instagram-{imported.shortcode}{imported.image_suffix}"
        if analyze_now and existing_post.get("status") not in {"queued", "running", "completed"}:
            update_values["status"] = "queued"
            update_values["error"] = None
            update_values["progress_percent"] = 5
            update_values["progress_message"] = "Queued"
        if len(update_values) > 1:
            assignments = ", ".join(f"{key} = ?" for key in update_values)
            with connect() as conn:
                conn.execute(
                    f"UPDATE posts SET {assignments} WHERE id = ?",
                    (*update_values.values(), int(existing_post["id"])),
                )
        if analyze_now and update_values.get("status") == "queued":
            background_tasks.add_task(run_analysis_job, int(existing_post["id"]), duration_seconds)
        return {"post": decorate_post(_get_post_or_404(int(existing_post["id"]))), "created": False}

    image_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{imported.image_suffix}"
    image_path.write_bytes(imported.image_bytes)
    now = utc_now()
    status, progress_percent, progress_message = _initial_post_state("single", analyze_now)
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO posts (
                section, title, caption, source_ref, shortcode,
                image_path, original_filename, status, progress_percent,
                progress_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "single",
                imported.title,
                imported.caption,
                source_ref,
                imported.shortcode,
                str(image_path),
                f"instagram-{imported.shortcode}{imported.image_suffix}",
                status,
                progress_percent,
                progress_message,
                now,
                now,
            ),
        )
        post_id = int(cursor.lastrowid)

    if analyze_now:
        background_tasks.add_task(run_analysis_job, post_id, duration_seconds)
    return {"post": decorate_post(_get_post_or_404(post_id)), "created": True}


@app.post("/api/posts/instagram-profile-sync")
def sync_posts_from_instagram_profile(
    profile: Annotated[str, Form()],
    limit: Annotated[int, Form()] = 12,
    duration_seconds: Annotated[int, Form()] = DEFAULT_VIDEO_SECONDS,
    analyze_now: Annotated[bool, Form()] = True,
    dry_run: Annotated[bool, Form()] = False,
    stop_on_existing: Annotated[bool, Form()] = True,
    refresh_existing: Annotated[bool, Form()] = False,
) -> dict[str, Any]:
    try:
        return sync_instagram_profile_posts(
            profile,
            limit=limit,
            dry_run=dry_run,
            analyze_now=analyze_now,
            duration_seconds=duration_seconds,
            analyze_post=run_analysis_job,
            stop_on_existing=stop_on_existing,
            refresh_existing=refresh_existing,
        )
    except InstagramImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/posts/batch")
def create_posts_batch(
    background_tasks: BackgroundTasks,
    section: Annotated[str, Form()] = "historical",
    titles: Annotated[str, Form()] = "[]",
    captions: Annotated[str, Form()] = "[]",
    published_ats: Annotated[str, Form()] = "[]",
    likes: Annotated[str, Form()] = "[]",
    person_labels: Annotated[str, Form()] = "[]",
    company_labels: Annotated[str, Form()] = "[]",
    post_type_labels: Annotated[str, Form()] = "[]",
    duration_seconds: Annotated[int, Form()] = DEFAULT_VIDEO_SECONDS,
    analyze_now: Annotated[bool, Form()] = False,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    if section != "historical":
        raise HTTPException(status_code=400, detail="Batch upload is currently limited to Post DB posts.")
    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one Post DB cover image.")

    title_values = _parse_metadata_list(titles, len(files))
    caption_values = _parse_metadata_list(captions, len(files))
    published_values = _parse_metadata_list(published_ats, len(files))
    like_values = _parse_metadata_list(likes, len(files))
    person_values = _parse_metadata_list(person_labels, len(files))
    company_values = _parse_metadata_list(company_labels, len(files))
    post_type_values = _parse_metadata_list(post_type_labels, len(files))

    now = utc_now()
    post_ids: list[int] = []
    with connect() as conn:
        for index, upload in enumerate(files):
            image_path = _save_upload(upload)
            post_title = title_values[index] or _title_from_filename(upload.filename) or f"Post DB cover {index + 1}"
            post_likes = _normalized_likes(like_values[index], "historical")
            clean_person = person_values[index]
            clean_company = company_values[index]
            clean_post_type = _clean_metadata_label(post_type_values[index])
            hook_text = None
            status, progress_percent, progress_message = _initial_post_state("historical", analyze_now)
            cursor = conn.execute(
                """
                INSERT INTO posts (
                    section, title, caption, published_at, likes,
                    person_label, company_label, post_type_label, hook_text,
                    image_path, original_filename, status, progress_percent,
                    progress_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "historical",
                    post_title,
                    caption_values[index] or None,
                    published_values[index] or None,
                    post_likes,
                    clean_person,
                    clean_company,
                    clean_post_type,
                    hook_text,
                    str(image_path),
                    upload.filename,
                    status,
                    progress_percent,
                    progress_message,
                    now,
                    now,
                ),
            )
            post_ids.append(int(cursor.lastrowid))
            _save_metadata_options(conn, clean_person, clean_company, clean_post_type)

    if analyze_now:
        background_tasks.add_task(run_batch_analysis_job, post_ids, duration_seconds)

    posts = [decorate_post(_get_post_or_404(post_id)) for post_id in post_ids]
    return {"posts": posts}


@app.patch("/api/posts/{post_id}")
def update_post(
    post_id: int,
    title: Annotated[str | None, Form()] = None,
    caption: Annotated[str | None, Form()] = None,
    published_at: Annotated[str | None, Form()] = None,
    likes: Annotated[str | None, Form()] = None,
    person_label: Annotated[str | None, Form()] = None,
    company_label: Annotated[str | None, Form()] = None,
    post_type_label: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form()] = None,
    hook_text: Annotated[str | None, Form()] = None,
    is_animated: Annotated[bool | None, Form()] = None,
    comments: Annotated[str | None, Form()] = None,
    shortcode: Annotated[str | None, Form()] = None,
    source_ref: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    post = _get_post_or_404(post_id)
    fields = {
        "title": title,
        "caption": caption,
        "published_at": published_at,
        "person_label": person_label,
        "company_label": company_label,
        "post_type_label": _clean_metadata_label(post_type_label),
        "tags": tags,
        "hook_text": hook_text,
        "is_animated": int(is_animated) if is_animated is not None else None,
        "shortcode": shortcode,
        "source_ref": source_ref,
    }
    if likes is not None:
        fields["likes"] = _normalized_likes(likes, post.get("section") or "")
        if post.get("section") == "single":
            fields["section"] = "historical"
    if comments is not None and comments != "":
        fields["comments"] = _optional_int(comments)
    fields = {key: value for key, value in fields.items() if value is not None}
    fields["updated_at"] = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE posts SET {assignments} WHERE id = ?",
            (*fields.values(), post_id),
        )
        _save_metadata_options(
            conn,
            fields.get("person_label"),
            fields.get("company_label"),
            fields.get("post_type_label"),
        )
    return {"post": decorate_post(_get_post_or_404(post_id))}


@app.delete("/api/posts/{post_id}")
def delete_post(post_id: int) -> dict[str, Any]:
    post = _get_post_or_404(post_id)
    file_paths = _post_file_paths(post)
    affected_ab_test_ids = _ab_test_ids_for_post(post_id)
    with connect() as conn:
        conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        _delete_empty_ab_tests(conn)
    for test_id in affected_ab_test_ids:
        _sync_ab_test_decision(test_id)
    deleted_files = _delete_owned_files(file_paths)
    return {"ok": True, "deleted_post_id": post_id, "deleted_files": deleted_files}


@app.post("/api/posts/{post_id}/analyze")
def analyze_post(
    post_id: int,
    background_tasks: BackgroundTasks,
    duration_seconds: Annotated[int, Form()] = DEFAULT_VIDEO_SECONDS,
) -> dict[str, Any]:
    _get_post_or_404(post_id)
    with connect() as conn:
        conn.execute(
            """
            UPDATE posts
            SET status = ?, error = NULL, progress_percent = ?, progress_message = ?,
                llm_report = NULL, updated_at = ?
            WHERE id = ?
            """,
            ("queued", 5, "Queued", utc_now(), post_id),
        )
    _sync_ab_tests_for_post(post_id)
    background_tasks.add_task(run_analysis_job, post_id, duration_seconds)
    return {"post": decorate_post(_get_post_or_404(post_id))}


@app.post("/api/posts/{post_id}/report")
def generate_post_report(
    post_id: int,
    force: bool = Query(default=False),
) -> dict[str, Any]:
    post = decorate_post(_get_post_or_404(post_id))
    if post.get("section") == "historical":
        raise HTTPException(
            status_code=400,
            detail="Post DB stores structured brain data and real likes; LLM text reports are disabled for this section.",
        )
    if post.get("status") != "completed" or not post.get("analysis_summary"):
        raise HTTPException(
            status_code=400,
            detail="Generate a completed TRIBE v2 analysis before requesting an LLM report.",
        )
    if post.get("llm_report") and not force:
        return {"report": post["llm_report"]}

    try:
        report = generate_llm_report(post, _calibration_payload(_fit_calibration_cached(_all_posts())))
    except LlmReportUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    with connect() as conn:
        conn.execute(
            "UPDATE posts SET llm_report = ?, updated_at = ? WHERE id = ?",
            (json.dumps(report), utc_now(), post_id),
        )
    return {"report": report}


@app.get("/api/ab-tests")
def list_ab_tests() -> dict[str, Any]:
    _sync_all_ab_test_decisions()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM ab_tests ORDER BY created_at DESC").fetchall()
    return {"tests": [dict(row) for row in rows]}


@app.post("/api/ab-tests")
def create_ab_test(
    background_tasks: BackgroundTasks,
    name: Annotated[str, Form()],
    duration_seconds: Annotated[int, Form()] = DEFAULT_VIDEO_SECONDS,
    candidate_titles: Annotated[str, Form()] = "[]",
    person_label: Annotated[str | None, Form()] = None,
    company_label: Annotated[str | None, Form()] = None,
    post_type_label: Annotated[str | None, Form()] = None,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="Upload at least two covers for A/B testing")
    labels = _parse_candidate_titles(candidate_titles, len(files))
    clean_person = _clean_metadata_label(person_label)
    clean_company = _clean_metadata_label(company_label)
    clean_post_type = _clean_metadata_label(post_type_label)
    now = utc_now()
    with connect() as conn:
        _save_metadata_options(conn, clean_person, clean_company, clean_post_type)
        test_cursor = conn.execute(
            "INSERT INTO ab_tests (name, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name.strip() or "A/B test", "running", now, now),
        )
        test_id = int(test_cursor.lastrowid)
        post_ids = []
        for index, upload in enumerate(files):
            image_path = _save_upload(upload)
            label = labels[index] or f"Cover {index + 1}"
            post_cursor = conn.execute(
                """
                INSERT INTO posts (
                    section, title, person_label, company_label, post_type_label,
                    image_path, original_filename, status,
                    created_at, updated_at, progress_percent, progress_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ab",
                    label,
                    clean_person,
                    clean_company,
                    clean_post_type,
                    str(image_path),
                    upload.filename,
                    "queued",
                    now,
                    now,
                    5,
                    "Queued",
                ),
            )
            post_id = int(post_cursor.lastrowid)
            post_ids.append(post_id)
            conn.execute(
                "INSERT INTO ab_candidates (ab_test_id, post_id, label, created_at) VALUES (?, ?, ?, ?)",
                (test_id, post_id, label, now),
            )
    for post_id in post_ids:
        background_tasks.add_task(run_analysis_job, post_id, duration_seconds)
    return get_ab_test(test_id)


@app.get("/api/ab-tests/{test_id}")
def get_ab_test(test_id: int) -> dict[str, Any]:
    _sync_ab_test_decision(test_id)
    with connect() as conn:
        test = conn.execute("SELECT * FROM ab_tests WHERE id = ?", (test_id,)).fetchone()
        if not test:
            raise HTTPException(status_code=404, detail="A/B test not found")
        rows = conn.execute(
            """
            SELECT c.id AS candidate_id, c.label, p.*
            FROM ab_candidates c
            JOIN posts p ON p.id = c.post_id
            WHERE c.ab_test_id = ?
            ORDER BY c.id ASC
            """,
            (test_id,),
        ).fetchall()

    test_data = dict(test)
    all_p = _all_posts()
    candidates = [decorate_post(row_to_post(row), all_p) for row in rows]
    ranked = rank_candidates(
        candidates,
        winner_post_id=test_data.get("winner_post_id"),
        all_posts=all_p,
    )
    return {"test": test_data, "candidates": ranked}


@app.delete("/api/ab-tests/{test_id}")
def delete_ab_test(test_id: int) -> dict[str, Any]:
    with connect() as conn:
        test = conn.execute("SELECT * FROM ab_tests WHERE id = ?", (test_id,)).fetchone()
        if not test:
            raise HTTPException(status_code=404, detail="A/B test not found")
        rows = conn.execute(
            """
            SELECT p.*
            FROM ab_candidates c
            JOIN posts p ON p.id = c.post_id
            WHERE c.ab_test_id = ?
            """,
            (test_id,),
        ).fetchall()
        posts = [row_to_post(row) for row in rows]
        file_paths = [path for post in posts for path in _post_file_paths(post)]
        post_ids = [int(post["id"]) for post in posts]
        if post_ids:
            placeholders = ", ".join("?" for _ in post_ids)
            conn.execute(f"DELETE FROM posts WHERE id IN ({placeholders})", post_ids)
        conn.execute("DELETE FROM ab_tests WHERE id = ?", (test_id,))
    deleted_files = _delete_owned_files(file_paths)
    return {"ok": True, "deleted_test_id": test_id, "deleted_post_ids": post_ids, "deleted_files": deleted_files}


def run_batch_analysis_job(post_ids: list[int], duration_seconds: int = DEFAULT_VIDEO_SECONDS) -> None:
    for post_id in post_ids:
        run_analysis_job(post_id, duration_seconds)


def run_analysis_job(post_id: int, duration_seconds: int = DEFAULT_VIDEO_SECONDS) -> None:
    try:
        _set_post_progress(post_id, 10, "Preparing cover", status="running")
        post = _get_post_or_404(post_id)
        image_path = Path(post["image_path"])
        video_path = VIDEO_DIR / f"{post_id}-{uuid.uuid4().hex}.mp4"
        _set_post_progress(post_id, 22, "Converting image to video")
        create_static_video(image_path, video_path, duration_seconds=duration_seconds)
        if remote_tribe_status()["configured"]:
            _set_post_progress(post_id, 38, "Video ready; sending to remote GPU")
            summary = analyze_video_remote(video_path, duration_seconds=duration_seconds)
        else:
            _set_post_progress(post_id, 38, "Video ready; loading TRIBE v2")
            summary = analyze_video(video_path, duration_seconds=duration_seconds)
        _set_post_progress(post_id, 84, "Summarizing brain activations")
        analysis_path = ANALYSIS_DIR / f"{post_id}-{uuid.uuid4().hex}.json"
        write_analysis(analysis_path, summary)
        _set_post_progress(post_id, 94, "Saving results")
        with connect() as conn:
            metrics = summary.get("metrics") or {}
            conn.execute(
                """
                UPDATE posts
                SET video_path = ?, analysis_path = ?, analysis_summary = ?,
                    brain_global_mean_abs = ?, brain_global_peak_abs = ?,
                    virality_potential = ?,
                    status = ?, error = NULL, progress_percent = ?,
                    progress_message = ?, llm_report = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(video_path),
                    str(analysis_path),
                    json.dumps(summary),
                    metrics.get("global_mean_abs"),
                    metrics.get("global_peak_abs"),
                    summary.get("virality_potential"),
                    "completed",
                    100,
                    "Complete",
                    utc_now(),
                    post_id,
                ),
            )
        _sync_ab_tests_for_post(post_id)
    except (RemoteTribeUnavailable, TribeUnavailable, Exception) as exc:
        with connect() as conn:
            conn.execute(
                """
                UPDATE posts
                SET status = ?, error = ?, progress_message = ?, updated_at = ?
                WHERE id = ?
                """,
                ("failed", str(exc), "Analysis failed", utc_now(), post_id),
            )
        _sync_ab_tests_for_post(post_id)


def _set_post_progress(
    post_id: int,
    percent: int,
    message: str,
    status: str | None = None,
) -> None:
    fields: dict[str, Any] = {
        "progress_percent": max(0, min(100, int(percent))),
        "progress_message": message,
        "updated_at": utc_now(),
    }
    if status is not None:
        fields["status"] = status
        fields["error"] = None
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE posts SET {assignments} WHERE id = ?",
            (*fields.values(), post_id),
        )


def decorate_post(
    post: dict[str, Any],
    all_posts: list[dict[str, Any]] | None = None,
    calibration_model: Any = None,
    prediction_model: Any = None,
    percentile_values: list[float] | None = None,
    prediction_v2_model: Any = None,
) -> dict[str, Any]:
    if all_posts is None:
        all_posts = _all_posts()
    post["image_url"] = _media_url(post.get("image_path"))
    post["video_url"] = _media_url(post.get("video_path"))
    post["analysis_url"] = _media_url(post.get("analysis_path"))
    if post.get("section") == "historical":
        post["llm_report"] = None
        if post.get("analysis_summary") and "surface" in post["analysis_summary"]:
            del post["analysis_summary"]["surface"]
    if post.get("section") == "historical" or not post.get("analysis_summary"):
        # No prediction possible without an analysis — skip model fitting entirely
        # so imports/uploads return immediately.
        post["calibrated_prediction"] = None
    else:
        # Memory-lean chain: only fall back to the legacy models when v2 is not
        # ready. Fitting all three at once OOMs small instances.
        if prediction_v2_model is None:
            prediction_v2_model = _fit_prediction_v2_cached(all_posts)
        prediction = predict_multi_signal(post, prediction_v2_model)
        if prediction is None:
            if prediction_model is None:
                prediction_model = _fit_prediction_model_cached(all_posts)
            prediction = predict_performance(post, prediction_model)
        if prediction is None:
            if calibration_model is None:
                calibration_model = _fit_calibration_cached(all_posts)
            prediction = predict_likes(post, calibration_model, all_posts=all_posts)
        post["calibrated_prediction"] = prediction
    post["tribe_percentile"] = _tribe_percentile(post, all_posts, percentile_values)
    return post


def _lightweight_historical_post(post: dict[str, Any]) -> dict[str, Any]:
    post["image_url"] = _media_url(post.get("image_path"))
    post["video_url"] = _media_url(post.get("video_path"))
    post["analysis_url"] = _media_url(post.get("analysis_path"))
    post["has_analysis_summary"] = bool(post.get("analysis_summary"))
    post["analysis_summary"] = None
    post["llm_report"] = None
    post["calibrated_prediction"] = None
    post["tribe_percentile"] = None
    return post


def _initial_post_state(section: str, analyze_now: bool) -> tuple[str, int, str | None]:
    if analyze_now:
        return "queued", 5, "Queued"
    if section == "historical":
        return "queued", 0, "Stored in Post DB; analysis pending"
    return "failed", 0, None


def rank_candidates(
    candidates: list[dict[str, Any]],
    winner_post_id: int | None = None,
    all_posts: list[dict[str, Any]] | None = None,
    calibration_model: Any = None,
    prediction_model: Any = None,
    prediction_v2_model: Any = None,
) -> list[dict[str, Any]]:
    if all_posts is None:
        all_posts = _all_posts()
    if prediction_v2_model is None:
        prediction_v2_model = _fit_prediction_v2_cached(all_posts)
    decorated = []
    for candidate in candidates:
        prediction = predict_multi_signal(candidate, prediction_v2_model)
        if prediction is None:
            if prediction_model is None:
                prediction_model = _fit_prediction_model_cached(all_posts)
            prediction = predict_performance(candidate, prediction_model)
        if prediction is None:
            if calibration_model is None:
                calibration_model = _fit_calibration_cached(all_posts)
            prediction = predict_likes(candidate, calibration_model, all_posts=all_posts)
        summary = candidate.get("analysis_summary") or {}
        metric = ((summary.get("metrics") or {}).get("global_mean_abs") or 0.0)
        ranking_value = prediction.get("ranking_value", prediction["predicted_likes"]) if prediction else metric
        candidate["ranking_value"] = ranking_value
        candidate["ranking_basis"] = "advanced_prediction" if prediction and prediction.get("model_version") else (
            "calibrated_likes" if prediction else "tribev2_global_activation"
        )
        decorated.append(candidate)
    decorated.sort(key=lambda item: item.get("ranking_value") or 0, reverse=True)
    for index, candidate in enumerate(decorated, start=1):
        candidate["rank"] = index
        candidate["is_winner"] = candidate.get("id") == winner_post_id
    return decorated


def _sync_all_ab_test_decisions() -> None:
    with connect() as conn:
        rows = conn.execute("SELECT id FROM ab_tests").fetchall()
    for row in rows:
        _sync_ab_test_decision(int(row["id"]))


def _sync_ab_tests_for_post(post_id: int) -> None:
    for test_id in _ab_test_ids_for_post(post_id):
        _sync_ab_test_decision(test_id)


def _ab_test_ids_for_post(post_id: int) -> list[int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT ab_test_id FROM ab_candidates WHERE post_id = ?",
            (post_id,),
        ).fetchall()
    return [int(row["ab_test_id"]) for row in rows]


def _sync_ab_test_decision(test_id: int) -> None:
    with connect() as conn:
        test = conn.execute("SELECT * FROM ab_tests WHERE id = ?", (test_id,)).fetchone()
        if not test:
            return
        rows = conn.execute(
            """
            SELECT p.*
            FROM ab_candidates c
            JOIN posts p ON p.id = c.post_id
            WHERE c.ab_test_id = ?
            ORDER BY c.id ASC
            """,
            (test_id,),
        ).fetchall()

    all_p = _all_posts()
    candidates = [decorate_post(row_to_post(row), all_p) for row in rows]
    if not candidates:
        with connect() as conn:
            conn.execute(
                "UPDATE ab_tests SET status = ?, winner_post_id = NULL, updated_at = ? WHERE id = ?",
                ("failed", utc_now(), test_id),
            )
        return

    if any(candidate.get("status") in {"queued", "running"} for candidate in candidates):
        with connect() as conn:
            conn.execute(
                "UPDATE ab_tests SET status = ?, winner_post_id = NULL, updated_at = ? WHERE id = ?",
                ("running", utc_now(), test_id),
            )
        return

    completed = [candidate for candidate in candidates if candidate.get("status") == "completed"]
    if not completed:
        with connect() as conn:
            conn.execute(
                "UPDATE ab_tests SET status = ?, winner_post_id = NULL, updated_at = ? WHERE id = ?",
                ("failed", utc_now(), test_id),
            )
        return

    winner = rank_candidates(
        completed,
        all_posts=all_p,
    )[0]
    with connect() as conn:
        conn.execute(
            "UPDATE ab_tests SET status = ?, winner_post_id = ?, updated_at = ? WHERE id = ?",
            ("completed", int(winner["id"]), utc_now(), test_id),
        )


def _all_posts() -> list[dict[str, Any]]:
    """Load all posts with heavy surface arrays stripped from historical rows.

    Surface data (20k+ vertices per post) is only needed by the 3D viewer for
    single/ab posts; loading it for 2k+ historical rows costs ~600MB of RAM.
    """
    with connect() as conn:
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(posts)")]
        select_columns = ", ".join(
            (
                "CASE WHEN section = 'historical' "
                "THEN json_remove(analysis_summary, '$.surface') "
                "ELSE analysis_summary END AS analysis_summary"
            )
            if column == "analysis_summary"
            else column
            for column in columns
        )
        rows = conn.execute(f"SELECT {select_columns} FROM posts ORDER BY created_at DESC").fetchall()
    return [row_to_post(row) for row in rows]


def _fit_cached(
    posts: list[dict[str, Any]],
    cache: dict[str, Any],
    fitter: Any,
    label: str,
) -> Any:
    signature = _posts_signature(posts)
    cached = cache.get(signature)
    if cached is not None:
        return cached
    with _FIT_LOCK:
        cached = cache.get(signature)
        if cached is not None:
            return cached
        _MODEL_LOG.info("%s starting (n=%d)", label, len(posts))
        started = time.monotonic()
        model = fitter(posts)
        _MODEL_LOG.info("%s done in %.1fs", label, time.monotonic() - started)
        cache.clear()
        cache[signature] = model
        return model


def _fit_calibration_cached(posts: list[dict[str, Any]]) -> Any:
    return _fit_cached(posts, _CALIBRATION_CACHE, fit_calibration, "fit_calibration")


def _fit_prediction_model_cached(posts: list[dict[str, Any]]) -> Any:
    return _fit_cached(posts, _PREDICTION_MODEL_CACHE, fit_advanced_prediction, "fit_advanced_prediction")


def _fit_prediction_v2_cached(posts: list[dict[str, Any]]) -> Any:
    return _fit_cached(posts, _PREDICTION_V2_CACHE, fit_multi_signal, "fit_multi_signal")


def _posts_signature(posts: list[dict[str, Any]]) -> str:
    latest = max((str(post.get("updated_at") or "") for post in posts), default="")
    return f"{len(posts)}:{latest}"


def _calibration_payload(model: Any) -> dict[str, Any]:
    data = model.__dict__.copy()
    for key in [
        "vocab_tags",
        "vocab_person",
        "vocab_company",
        "vocab_post_type",
        "vocab_hook_tokens",
        "means",
        "scales",
        "coefficients",
        "intercept",
        "train_vectors_scaled",
        "train_likes",
    ]:
        data.pop(key, None)
    return data


def _eligible_modal_ocr_posts(start: int, limit: int) -> tuple[int, list[dict[str, Any]]]:
    with connect() as conn:
        eligible_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM posts
                WHERE section = 'historical'
                  AND status = 'completed'
                  AND analysis_summary IS NOT NULL
                  AND TRIM(COALESCE(hook_text, '')) = ''
                  AND source_row_number >= ?
                """,
                (start,),
            ).fetchone()[0]
        )
        rows = conn.execute(
            """
            SELECT id, source_row_number, image_path
            FROM posts
            WHERE section = 'historical'
              AND status = 'completed'
              AND analysis_summary IS NOT NULL
              AND TRIM(COALESCE(hook_text, '')) = ''
              AND source_row_number >= ?
            ORDER BY source_row_number, id
            LIMIT ?
            """,
            (start, max(1, limit)),
        ).fetchall()

    records = []
    for row in rows:
        image_path = row["image_path"]
        if image_path and Path(image_path).exists():
            records.append(
                {
                    "id": int(row["id"]),
                    "source_row_number": int(row["source_row_number"] or 0),
                    "image_path": image_path,
                }
            )
    return eligible_count, records


def _eligible_missing_ocr_posts(limit: int) -> tuple[int, list[dict[str, Any]]]:
    with connect() as conn:
        eligible_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM posts
                WHERE TRIM(COALESCE(hook_text, '')) = ''
                  AND image_path IS NOT NULL
                  AND TRIM(COALESCE(image_path, '')) <> ''
                """
            ).fetchone()[0]
        )
        rows = conn.execute(
            """
            SELECT id, image_path
            FROM posts
            WHERE TRIM(COALESCE(hook_text, '')) = ''
              AND image_path IS NOT NULL
              AND TRIM(COALESCE(image_path, '')) <> ''
            ORDER BY created_at, id
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()

    records = []
    for row in rows:
        image_path = row["image_path"]
        if image_path and Path(image_path).exists():
            records.append(
                {
                    "id": int(row["id"]),
                    "image_path": image_path,
                }
            )
    return eligible_count, records


def _get_post_or_404(post_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Post not found")
    return row_to_post(row)


def _save_upload(file: UploadFile) -> Path:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"Upload a cover image with one of: {', '.join(sorted(ALLOWED_IMAGE_SUFFIXES))}",
        )
    output = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    with output.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)
    return output


def _post_file_paths(post: dict[str, Any]) -> list[Path]:
    paths = []
    for key in ["image_path", "video_path", "analysis_path"]:
        value = post.get(key)
        if value:
            paths.append(Path(value))
    return paths


def _delete_owned_files(paths: list[Path]) -> int:
    data_root = DATA_DIR.resolve()
    deleted = 0
    for path in {item.expanduser() for item in paths}:
        try:
            resolved = path.resolve()
            resolved.relative_to(data_root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            resolved.unlink()
            deleted += 1
    return deleted


def _delete_empty_ab_tests(conn: Any) -> None:
    conn.execute(
        """
        DELETE FROM ab_tests
        WHERE id NOT IN (SELECT DISTINCT ab_test_id FROM ab_candidates)
        """
    )


def _media_url(path_value: str | None) -> str | None:
    if not path_value:
        return None
    path = Path(path_value)
    try:
        relative = path.relative_to(DATA_DIR)
    except ValueError:
        return None
    return f"/media/{relative.as_posix()}"


def _parse_candidate_titles(raw: str, expected: int) -> list[str]:
    try:
        titles = json.loads(raw)
    except json.JSONDecodeError:
        titles = []
    if not isinstance(titles, list):
        titles = []
    titles = [str(item) for item in titles][:expected]
    while len(titles) < expected:
        titles.append("")
    return titles


def _parse_metadata_list(raw: str, expected: int) -> list[str]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        values = []
    if not isinstance(values, list):
        values = []
    normalized = ["" if item is None else str(item).strip() for item in values[:expected]]
    while len(normalized) < expected:
        normalized.append("")
    return normalized


def _clean_metadata_label(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        return None
    return cleaned[:80]


def _seed_default_metadata_options() -> None:
    with connect() as conn:
        for label in DEFAULT_PERSON_OPTIONS:
            _insert_metadata_option(conn, "person", label)
        for label in DEFAULT_COMPANY_OPTIONS:
            _insert_metadata_option(conn, "company", label)
        for label in DEFAULT_POST_TYPE_OPTIONS:
            _insert_metadata_option(conn, "post_type", label)


def _save_metadata_options(
    conn: Any,
    person_label: Any = None,
    company_label: Any = None,
    post_type_label: Any = None,
) -> None:
    def _insert_multiple(kind: str, val: Any) -> None:
        if not val:
            return
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                for item in parsed:
                    _insert_metadata_option(conn, kind, str(item))
                return
        except Exception:
            pass
        _insert_metadata_option(conn, kind, val)

    _insert_multiple("person", person_label)
    _insert_multiple("company", company_label)
    _insert_metadata_option(conn, "post_type", post_type_label)


def _insert_metadata_option(conn: Any, kind: str, label: Any) -> None:
    cleaned = _clean_metadata_label(str(label)) if label is not None else None
    if not cleaned:
        return
    now = utc_now()
    conn.execute(
        """
        INSERT INTO metadata_options (kind, label, slug, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(kind, slug) DO UPDATE SET
            label = excluded.label,
            updated_at = excluded.updated_at
        """,
        (kind, cleaned, _metadata_slug(cleaned), now, now),
    )


def _metadata_slug(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")
    return slug or uuid.uuid4().hex


def _merged_options(defaults: list[str], values: list[str | None]) -> list[str]:
    options: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        cleaned = _clean_metadata_label(value)
        if not cleaned:
            return
        key = cleaned.casefold()
        if key in seen:
            return
        seen.add(key)
        options.append(cleaned)

    for default in defaults:
        add(default)
    custom = sorted(
        [item for item in (_clean_metadata_label(value) for value in values) if item],
        key=str.casefold,
    )
    for value in custom:
        add(value)
    return options


FLOP_LIKES_BASELINE = 850


def _normalized_likes(raw: Any, section: str) -> int | None:
    likes = _optional_int(raw)
    if section == "historical" and likes is None:
        return FLOP_LIKES_BASELINE
    return likes


def _optional_int(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _title_from_filename(filename: str | None) -> str:
    if not filename:
        return ""
    return Path(filename).stem.replace("_", " ").replace("-", " ").strip().title()


def _tribe_percentile_reference(all_posts: list[dict[str, Any]]) -> list[float]:
    values = []
    for row in all_posts:
        if (
            row.get("section") == "historical"
            and row.get("status") == "completed"
            and row.get("analysis_summary")
        ):
            value = ((row["analysis_summary"].get("metrics") or {}).get("global_mean_abs") or 0.0)
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                numeric = 0.0
            if numeric == numeric and numeric not in {float("inf"), float("-inf")}:
                values.append(numeric)
    values.sort()
    return values


def _tribe_percentile(
    post: dict[str, Any],
    all_posts: list[dict[str, Any]] | None = None,
    percentile_values: list[float] | None = None,
) -> float | None:
    summary = post.get("analysis_summary")
    if not summary:
        return None
    value = (summary.get("metrics") or {}).get("global_mean_abs")
    if value is None:
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None
    if numeric_value != numeric_value or numeric_value in {float("inf"), float("-inf")}:
        return None
    if percentile_values is None and all_posts is None:
        all_posts = _all_posts()
    if percentile_values is None:
        percentile_values = _tribe_percentile_reference(all_posts or [])
    if len(percentile_values) < 2:
        return None
    below_or_equal = bisect_right(percentile_values, numeric_value)
    return round(100 * below_or_equal / len(percentile_values), 1)
