from __future__ import annotations

import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

APIFY_ACTOR_ID = "apify~instagram-scraper"

# Progress callback used by run_backfill() and everything it calls, so the
# admin UI can show what's actually happening (starting the scrape, waiting
# on Apify, downloading covers...) instead of a single opaque spinner for
# what can be a many-minutes-long import. Optional everywhere and never
# allowed to break the import itself if the callback misbehaves -- it only
# ever updates an in-memory status dict, but a backfill that already paid
# for an Apify run is worth far more than a progress update.
ProgressFn = Callable[..., None] | None


def _emit(on_progress: ProgressFn, **fields: Any) -> None:
    if on_progress is None:
        return
    try:
        on_progress(fields)
    except Exception:
        pass

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


# Cover images dominate the 2GB Render disk: ~180KB each x thousands of posts.
# Instagram's own JPEGs are already well optimized, so re-encoding them as JPEG
# saves almost nothing (measured: ~8% at q75, and q80+ actually grew the file).
# WebP at q82 measured ~31% smaller at the SAME pixel dimensions -- no downscale,
# so the 1080px-wide sidebar preview stays sharp on retina (it renders at
# 531x843 CSS px, i.e. ~1062px wide at DPR2).
_COVER_MAX_WIDTH = 1080
_COVER_QUALITY = 82


def _compress_cover(raw: bytes) -> tuple[bytes, str]:
    """Returns (bytes, suffix). Falls back to the original bytes untouched if
    the image can't be decoded or if re-encoding wouldn't actually shrink it,
    so a codec edge case never loses or bloats a cover.
    """
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as img:
            img = img.convert("RGB")
            # Only ever downscale oversized sources (some are 1600px wide);
            # never upscale, and never shrink below what the UI renders.
            if img.width > _COVER_MAX_WIDTH:
                new_height = int(img.height * _COVER_MAX_WIDTH / img.width)
                img = img.resize((_COVER_MAX_WIDTH, new_height), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="WEBP", quality=_COVER_QUALITY, method=6)
        compressed = buf.getvalue()
    except Exception:
        return raw, ".jpg"
    if len(compressed) >= len(raw):
        return raw, ".jpg"
    return compressed, ".webp"


_OCR_CLAIM_LOCK = __import__("threading").Lock()


def reset_stuck_ocr_claims() -> int:
    """Releases rows left in the 'in flight' state by a thread that died (a
    Render restart mid-batch). Called before a bulk sweep so nothing is
    stranded and skipped forever.
    """
    from .db import connect

    with connect() as conn:
        a = conn.execute("UPDATE dashboard_posts SET ocr_checked = 0 WHERE ocr_checked = 2").rowcount
        b = conn.execute("UPDATE posts SET hook_text = '' WHERE hook_text = '~'").rowcount
    return int(a or 0) + int(b or 0)


def ensure_local_cover(cover_source_url: str | None, dest_stem: str) -> Path | None:
    """Downloads + compresses a cover that was never cached locally (covers are
    fetched lazily, so thousands of rows have a source URL but no file). Returns
    the local path, or None if there's no URL or the CDN link has expired --
    Instagram's URLs are signed and die after a few days, so this fails often
    for older posts and the caller should treat None as "give up on this row".
    """
    if not cover_source_url:
        return None

    import httpx

    from .config import UPLOAD_DIR

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(timeout=30.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = client.get(cover_source_url)
            response.raise_for_status()
        data, suffix = _compress_cover(response.content)
    except Exception:
        return None

    path = UPLOAD_DIR / f"{dest_stem}{suffix}"
    try:
        path.write_bytes(data)
    except OSError:
        return None
    return path


def run_ocr_sweep(limit: int = 30) -> dict[str, Any]:
    """Fills in cover OCR text (hook_text) for posts that don't have it yet.

    Scoped to dashboard_posts only. The canonical `posts` table is frozen -- it
    belongs to Predict now and no longer receives Instagram posts -- so scanning
    it on every tick was work on rows the dashboard never reads.

    Newest first, so freshly-arrived posts become text-searchable right away.
    Every row touched is marked checked, including blank results, so a cover with
    genuinely no text is never re-sent (and re-billed) on a later pass.

    Runs through Sentient Dash's own standalone OCR worker (sentient_ocr.py /
    workers/modal_ocr_worker.py) -- always the full cover image, no crop, no
    GPU, and no dependency on Predict's shared tribev2 worker. See that
    worker's module docstring for why (it replaced a setup that paid for an
    L40S GPU it never used, and a fixed crop that missed text sitting outside
    it on some accounts' cover templates).
    """
    from .db import connect, utc_now

    from .sentient_ocr import extract_images_text_sentient

    summary: dict[str, Any] = {"sent": 0, "with_text": 0, "skipped": 0, "remaining": 0}

    # Claim rows under a process-wide lock so several sweep threads can run in
    # parallel without two of them grabbing the same cover -- that would OCR
    # (and bill) the same image twice. ocr_checked=2 means "in flight"; the
    # slow Modal call happens outside the lock so threads actually overlap.
    with _OCR_CLAIM_LOCK:
        with connect() as conn:
            dash = conn.execute(
                "SELECT id, account, shortcode, cover_image_path, cover_source_url FROM dashboard_posts "
                "WHERE TRIM(COALESCE(hook_text, '')) = '' AND ocr_checked = 0 "
                "ORDER BY published_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            if dash:
                conn.executemany(
                    "UPDATE dashboard_posts SET ocr_checked = 2 WHERE id = ?",
                    [(int(r["id"]),) for r in dash],
                )
            # The frozen `posts` table is intentionally not scanned here.
            canon: list = []

    with connect() as conn:
        summary["remaining"] = conn.execute(
            # Counts only what this sweep will actually work on -- the frozen
            # `posts` table is out of scope, so including it would report a
            # backlog that never drains.
            "SELECT COUNT(*) AS c FROM dashboard_posts "
            "WHERE TRIM(COALESCE(hook_text,''))='' AND ocr_checked=0"
        ).fetchone()["c"]

    jobs: list[tuple[str, int, Path]] = []
    give_up: list[tuple[str, int]] = []

    for row in dash:
        path = Path(str(row["cover_image_path"])) if row["cover_image_path"] else None
        if path is None or not path.is_file():
            stem = f"dash-{row['account']}-{str(row['shortcode'] or row['id']).strip()}"
            path = ensure_local_cover(row["cover_source_url"], stem)
            if path is None:
                give_up.append(("dashboard_posts", int(row["id"])))
                continue
            with connect() as conn:
                conn.execute(
                    "UPDATE dashboard_posts SET cover_image_path = ? WHERE id = ?", (str(path), int(row["id"]))
                )
        jobs.append(("dashboard_posts", int(row["id"]), path))

    for row in canon:
        path = Path(str(row["image_path"]))
        if not path.is_file():
            give_up.append(("posts", int(row["id"])))
            continue
        jobs.append(("posts", int(row["id"]), path))

    # Rows we can never OCR (no file, dead CDN link) get marked so they don't
    # clog the queue forever. posts has no ocr_checked column, so a sentinel
    # keeps it out of the "blank hook_text" set without faking real text.
    if give_up:
        with connect() as conn:
            for table, row_id in give_up:
                if table == "dashboard_posts":
                    conn.execute("UPDATE dashboard_posts SET ocr_checked = 1 WHERE id = ?", (row_id,))
                else:
                    conn.execute("UPDATE posts SET hook_text = '-' WHERE id = ?", (row_id,))
        summary["skipped"] = len(give_up)

    if not jobs:
        return summary

    try:
        results = extract_images_text_sentient([p for _, _, p in jobs])
    except Exception:
        # Release the claims so a later pass retries these instead of leaving
        # them stranded in the in-flight state.
        with connect() as conn:
            for table, row_id, _ in jobs:
                if table == "dashboard_posts":
                    conn.execute("UPDATE dashboard_posts SET ocr_checked = 0 WHERE id = ?", (row_id,))
                else:
                    conn.execute("UPDATE posts SET hook_text = '' WHERE id = ?", (row_id,))
        raise

    now_iso = utc_now()
    with connect() as conn:
        for (table, row_id, _), result in zip(jobs, results, strict=False):
            text = str(result.get("text") or "").strip() if isinstance(result, dict) else ""
            if text:
                summary["with_text"] += 1
            if table == "dashboard_posts":
                conn.execute(
                    "UPDATE dashboard_posts SET hook_text = ?, ocr_checked = 1, updated_at = ? WHERE id = ?",
                    (text or None, now_iso, row_id),
                )
            else:
                conn.execute(
                    "UPDATE posts SET hook_text = ?, updated_at = ? WHERE id = ?",
                    (text or "-", now_iso, row_id),
                )
    summary["sent"] = len(jobs)
    return summary


def _likes_are_known(raw: Any) -> bool:
    """True only when Instagram/Apify gave us a real like count. Mirrors the
    condition in _apply_likes_floor -- anything it would replace with the 500
    baseline is "unknown", not a genuine 500.
    """
    return isinstance(raw, int) and not isinstance(raw, bool) and raw > 3


# Fields promoted out of the Apify payload into their own columns. Everything
# else still survives in raw_json -- this list is only about what needs to be
# queryable/sortable. Shared by the insert path and the retroactive enrichment
# so both produce identical data.
_EXTRACT_COLUMNS = (
    "raw_json",
    "video_views",
    "video_plays",
    "video_duration",
    "product_type",
    "hashtags",
    "mentions",
    "slide_count",
    "music_song",
    "music_artist",
    "music_audio_id",
    "uses_original_audio",
    "paid_partnership",
    "alt_text",
    "first_comment",
    "ig_media_id",
    "owner_full_name",
    "coauthors",
    "tagged_users",
    "dimensions",
)


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _join_list(value: Any, key: str | None = None, limit: int = 40) -> str | None:
    """Flattens a list of strings (or of dicts, pulling `key`) into a compact
    comma-separated string -- searchable and readable without a JSON parse.
    """
    if not isinstance(value, list) or not value:
        return None
    parts: list[str] = []
    for entry in value[:limit]:
        if key and isinstance(entry, dict):
            text = entry.get(key)
        else:
            text = entry
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return ", ".join(parts) or None


def extract_apify_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Maps one raw Apify post into our promoted columns + the full payload.

    Returns a dict keyed exactly by _EXTRACT_COLUMNS so callers can build an
    INSERT/UPDATE without restating the field list.
    """
    import json as _json

    music = item.get("musicInfo") if isinstance(item.get("musicInfo"), dict) else {}
    # Carousels expose their slides as childPosts (preferred) or images.
    children = item.get("childPosts") if isinstance(item.get("childPosts"), list) else None
    images = item.get("images") if isinstance(item.get("images"), list) else None
    slide_count = len(children) if children else (len(images) if images else None)

    width = _as_int(item.get("originalWidth")) or _as_int(item.get("dimensionsWidth"))
    height = _as_int(item.get("originalHeight")) or _as_int(item.get("dimensionsHeight"))

    paid = item.get("paidPartnership")
    original_audio = music.get("uses_original_audio")

    try:
        raw_json = _json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raw_json = None

    return {
        "raw_json": raw_json,
        "video_views": _as_int(item.get("videoViewCount")),
        "video_plays": _as_int(item.get("videoPlayCount")) or _as_int(item.get("igPlayCount")),
        "video_duration": float(item["videoDuration"]) if isinstance(item.get("videoDuration"), (int, float)) else None,
        # 'clips' means Reel, which the coarse type field ("Video") can't tell
        # apart from a plain in-feed video.
        "product_type": (item.get("productType") or None),
        "hashtags": _join_list(item.get("hashtags")),
        "mentions": _join_list(item.get("mentions")),
        "slide_count": slide_count,
        "music_song": music.get("song_name") or None,
        "music_artist": music.get("artist_name") or None,
        # Instagram's own sound page (every reel that used this exact audio),
        # not a Spotify/Apple Music link -- Apify doesn't give us one of those.
        "music_audio_id": str(music.get("audio_id")) if music.get("audio_id") else None,
        "uses_original_audio": int(bool(original_audio)) if original_audio is not None else None,
        "paid_partnership": int(bool(paid)) if paid is not None else None,
        "alt_text": _clean_text(item.get("alt")) or None,
        "first_comment": _clean_text(item.get("firstComment")) or None,
        "ig_media_id": str(item.get("id")) if item.get("id") is not None else None,
        "owner_full_name": _clean_text(item.get("ownerFullName")) or None,
        "coauthors": _join_list(item.get("coauthorProducers"), key="username"),
        "tagged_users": _join_list(item.get("taggedUsers"), key="username"),
        "dimensions": f"{width}x{height}" if width and height else None,
    }


def enrich_from_existing_runs(max_runs: int = 40, per_run_limit: int = 5000) -> dict[str, Any]:
    """Backfills the newly-added columns from Apify datasets we ALREADY paid for.

    Apify retains each run's dataset, so posts scraped before we started keeping
    the full payload can be completed for free -- no re-scrape. Coverage is
    partial by nature: older datasets expire, so anything whose run is gone
    stays un-enriched (and would need a paid re-scrape to fill).

    Matching is by (ownerUsername, shortCode) so a dataset can never write its
    fields onto a different account's post.
    """
    import httpx

    from .db import connect, utc_now

    token = os.getenv("APIFY_TOKEN", "").strip()
    if not token:
        raise ApifySyncError("APIFY_TOKEN is not configured.")

    summary: dict[str, Any] = {"runs_scanned": 0, "runs_with_data": 0, "updated": 0, "expired_runs": 0}

    with httpx.Client(timeout=90.0) as client:
        runs_res = client.get(
            f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/runs",
            params={"token": token, "limit": max_runs, "desc": "true"},
        )
        runs_res.raise_for_status()
        runs = runs_res.json().get("data", {}).get("items", [])

        for run in runs:
            dataset_id = run.get("defaultDatasetId")
            if not dataset_id:
                continue
            summary["runs_scanned"] += 1
            try:
                items_res = client.get(
                    f"https://api.apify.com/v2/datasets/{dataset_id}/items",
                    params={"token": token, "format": "json", "limit": per_run_limit},
                )
                if items_res.status_code == 404:
                    summary["expired_runs"] += 1
                    continue
                items_res.raise_for_status()
                items = items_res.json()
            except httpx.HTTPError:
                summary["expired_runs"] += 1
                continue

            if not isinstance(items, list) or not items:
                continue

            updated_here = 0
            with connect() as conn:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    shortcode = item.get("shortCode")
                    if not shortcode:
                        continue
                    extracted = extract_apify_fields(item)
                    assignments = ", ".join(f"{col} = ?" for col in _EXTRACT_COLUMNS)
                    # Matched on shortcode only -- accounts repost each other, so
                    # the payload's ownerUsername is often the original author,
                    # not the account we filed the post under. Only fills rows
                    # never enriched, so re-running is cheap and non-destructive.
                    cursor = conn.execute(
                        f"UPDATE dashboard_posts SET {assignments}, enriched_at = ? "
                        f"WHERE shortcode = ? AND raw_json IS NULL",
                        (*(extracted[col] for col in _EXTRACT_COLUMNS), utc_now(), shortcode),
                    )
                    updated_here += cursor.rowcount or 0

            if updated_here:
                summary["runs_with_data"] += 1
                summary["updated"] += updated_here

    return summary


def enrich_from_run(run_id: str) -> dict[str, Any]:
    """Enriches posts from one specific finished run's dataset.

    Companion to enrich_from_existing_runs for the case where a scrape we just
    paid for completed on Apify's side but the result never reached us -- pulls
    that exact dataset instead of re-scraping.
    """
    import httpx

    from .db import connect, utc_now

    token = os.getenv("APIFY_TOKEN", "").strip()
    if not token:
        raise ApifySyncError("APIFY_TOKEN is not configured.")

    with httpx.Client(timeout=90.0) as client:
        run = client.get(f"https://api.apify.com/v2/actor-runs/{run_id}", params={"token": token})
        run.raise_for_status()
        data = run.json().get("data", {})
        dataset_id = data.get("defaultDatasetId")
        if not dataset_id:
            raise ApifySyncError("Run has no dataset.")
        res = client.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            params={"token": token, "format": "json", "limit": 5000},
        )
        res.raise_for_status()
        items = res.json()

    if not isinstance(items, list):
        raise ApifySyncError("Unexpected dataset shape.")

    assignments = ", ".join(f"{col} = ?" for col in _EXTRACT_COLUMNS)
    updated = 0
    now_iso = utc_now()
    with connect() as conn:
        for item in items:
            if not isinstance(item, dict):
                continue
            shortcode = item.get("shortCode")
            if not shortcode:
                continue
            # Matched by shortcode alone, NOT by ownerUsername: several tracked
            # accounts repost each other, so the payload's owner is the original
            # author rather than the account we filed it under (costarica reposts
            # traselveloreal, for instance). The payload describes one real
            # Instagram post, so every row holding that shortcode deserves it.
            extracted = extract_apify_fields(item)
            cursor = conn.execute(
                f"UPDATE dashboard_posts SET {assignments}, enriched_at = ? "
                f"WHERE shortcode = ? AND raw_json IS NULL",
                (*(extracted[col] for col in _EXTRACT_COLUMNS), now_iso, shortcode),
            )
            updated += cursor.rowcount or 0

    return {"run_status": data.get("status"), "dataset_items": len(items), "updated": updated}


def enrich_account_via_profile(account: str, results_limit: int = 3000) -> dict[str, Any]:
    """Enriches an account's existing posts with ONE profile scrape.

    Much faster than hitting each missing post's URL individually: a profile
    scrape pages the feed (~12 posts per request), while resultsType='details'
    navigates to every post separately. Measured ~1.9 posts/sec vs ~0.17 --
    roughly 11x. So for a large gap concentrated in one account this is the right
    tool; the per-URL scrape is only worth it for a handful scattered across
    accounts.

    Updates rows that already exist (matched on shortcode) and inserts any post
    the scrape found that we didn't have yet.
    """
    from .db import connect, utc_now

    cfg = get_account_config(account)
    payload: dict[str, Any] = {
        "directUrls": [f"https://www.instagram.com/{cfg['handle']}/"],
        "resultsType": "posts",
        "resultsLimit": results_limit,
    }
    items = _run_apify_actor_and_fetch(payload, max_wait_seconds=2400.0)

    assignments = ", ".join(f"{col} = ?" for col in _EXTRACT_COLUMNS)
    now_iso = utc_now()
    updated = 0
    seen: set[str] = set()

    with connect() as conn:
        for item in items:
            if not isinstance(item, dict):
                continue
            shortcode = item.get("shortCode")
            if not shortcode:
                continue
            seen.add(shortcode)
            extracted = extract_apify_fields(item)
            # Refresh engagement at the same time -- we're already holding the
            # freshest numbers Apify just returned, so not writing them would
            # waste the call.
            cursor = conn.execute(
                f"UPDATE dashboard_posts SET {assignments}, likes = COALESCE(?, likes), "
                f"comments = COALESCE(?, comments), enriched_at = ?, updated_at = ? "
                f"WHERE account = ? AND shortcode = ?",
                (
                    *(extracted[col] for col in _EXTRACT_COLUMNS),
                    _likes_or_none(item.get("likesCount")),
                    item.get("commentsCount"),
                    now_iso,
                    now_iso,
                    account,
                    shortcode,
                ),
            )
            updated += cursor.rowcount or 0

    # Anything the scrape returned that we had no row for gets inserted through
    # the normal path, so covers are downloaded and compressed as usual.
    with connect() as conn:
        existing = {
            r["shortcode"]
            for r in conn.execute(
                "SELECT shortcode FROM dashboard_posts WHERE account = ?", (account,)
            ).fetchall()
        }
    new_items = [i for i in items if i.get("shortCode") and i["shortCode"] not in existing]
    inserted = _insert_new_posts(account, cfg, new_items) if new_items else {"added": 0}

    return {
        "scraped": len(items),
        "updated_existing": updated,
        "inserted_new": inserted.get("added", 0),
    }


def missing_enrichment_breakdown() -> dict[str, Any]:
    """Which posts still lack the full Apify payload, grouped by account, so the
    cost of filling them can be judged before spending anything."""
    from .db import connect

    with connect() as conn:
        rows = conn.execute(
            "SELECT account, COUNT(*) c FROM dashboard_posts "
            "WHERE raw_json IS NULL AND shortcode IS NOT NULL AND shortcode NOT LIKE 'post-%' "
            "GROUP BY account ORDER BY c DESC"
        ).fetchall()
    by_account = {r["account"]: r["c"] for r in rows}
    total = sum(by_account.values())
    return {
        "missing_total": total,
        "by_account": by_account,
        # Measured rate from this month's billing: ~$0.0023 per scraped post.
        "estimated_usd": round(total * 0.0023, 2),
    }


def scrape_missing_enrichment(
    limit: int = 200, account: str | None = None, batch_size: int = 100
) -> dict[str, Any]:
    """Fills in the payload for posts we never captured it for, by scraping their
    specific post URLs with resultsType='details'.

    Far cheaper than re-running a profile backfill: it touches only the exact
    posts that are missing, instead of re-walking the whole account history.
    """
    from .db import connect, utc_now

    where = "raw_json IS NULL AND shortcode IS NOT NULL AND shortcode NOT LIKE 'post-%'"
    params: list[Any] = []
    if account:
        where += " AND account = ?"
        params.append(account)

    with connect() as conn:
        rows = conn.execute(
            f"SELECT id, account, shortcode, permalink FROM dashboard_posts WHERE {where} "
            f"ORDER BY published_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()

    targets = [
        (int(r["id"]), r["account"], r["shortcode"], r["permalink"] or f"https://www.instagram.com/p/{r['shortcode']}/")
        for r in rows
    ]
    if not targets:
        return {"sent": 0, "updated": 0, "unmatched": 0}

    updated = unmatched = 0
    now_iso = utc_now()
    assignments = ", ".join(f"{col} = ?" for col in _EXTRACT_COLUMNS)

    for start in range(0, len(targets), batch_size):
        batch = targets[start : start + batch_size]
        payload = {"directUrls": [t[3] for t in batch], "resultsType": "details"}
        # Start-and-poll, never run-sync-get-dataset-items: scraping ~100 post
        # URLs one by one routinely exceeds Apify's sync-endpoint limit and
        # returns 408, while the run itself completes and still bills. That's
        # exactly the silent-loss failure this project already paid for once.
        items = _run_apify_actor_and_fetch(payload, max_wait_seconds=1800.0)
        by_shortcode = {i.get("shortCode"): i for i in items if isinstance(i, dict) and i.get("shortCode")}

        with connect() as conn:
            for post_id, _acct, shortcode, _url in batch:
                item = by_shortcode.get(shortcode)
                if not item:
                    unmatched += 1
                    continue
                extracted = extract_apify_fields(item)
                conn.execute(
                    f"UPDATE dashboard_posts SET {assignments}, enriched_at = ? WHERE id = ?",
                    (*(extracted[col] for col in _EXTRACT_COLUMNS), now_iso, post_id),
                )
                updated += 1

    return {"sent": len(targets), "updated": updated, "unmatched": unmatched}


def _slack_alerts_cover(group: str | None) -> bool:
    """Which account groups trigger a Slack HOT alert.

    Defaults to competitors only: your own accounts are already being watched
    in the dashboard, while a competitor breaking out is the thing you'd want
    pushed to you. Set SLACK_ALERT_GROUPS to "all" for everything, or to a
    comma-separated list of groups (e.g. "sentient,competitors").
    """
    raw = os.getenv("SLACK_ALERT_GROUPS", "competitors").strip().lower()
    if raw in {"all", "*", ""}:
        return True
    allowed = {part.strip() for part in raw.split(",") if part.strip()}
    return (group or "").lower() in allowed


def _likes_or_none(raw: Any) -> int | None:
    """Real like count, or None when Instagram hid/under-reported it. Replaces
    the old 500 placeholder: inventing a number made unknown posts look like
    they had real (and identical) engagement, and it polluted sorting, totals
    and the HOT rate math. NULL is honest and the UI renders it as a dash.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw > 3 else None


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


def _run_apify_actor_and_fetch(
    payload: dict[str, Any],
    max_wait_seconds: float = 1800.0,
    poll_interval: float = 8.0,
    on_progress: ProgressFn = None,
) -> list[dict[str, Any]]:
    """Starts the actor run and polls for completion with short, separate
    requests instead of holding one long-lived connection open for the
    entire scrape the way run-sync-get-dataset-items does.

    We confirmed via Apify's own run history that large scrapes routinely
    SUCCEED and get billed on Apify's side while our synchronous call never
    receives the response -- something in the long-held connection path
    between Render and Apify drops it silently, so the completed results
    are paid for but lost. Starting the run, polling its status with quick
    (~30s) calls, then fetching the finished dataset separately never holds
    one connection open for more than a few seconds, so it can't be dropped
    mid-scrape like that.
    """
    token = os.getenv("APIFY_TOKEN", "").strip()
    if not token:
        raise ApifySyncError("APIFY_TOKEN is not configured on the server.")
    try:
        import httpx
    except ImportError as exc:
        raise ApifySyncError("httpx is not installed in the backend environment.") from exc
    import time

    _emit(on_progress, phase="starting_apify_run")
    start_url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/runs"
    try:
        with httpx.Client(timeout=30.0) as client:
            start_response = client.post(start_url, params={"token": token}, json=payload)
            start_response.raise_for_status()
            run = start_response.json().get("data", {})
    except httpx.HTTPError as exc:
        raise ApifySyncError(f"Failed to start Apify run: {exc}") from exc

    run_id = run.get("id")
    if not run_id:
        raise ApifySyncError("Apify did not return a run id.")

    poll_started_at = time.monotonic()
    status_url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    deadline = poll_started_at + max_wait_seconds
    dataset_id = run.get("defaultDatasetId")
    status = run.get("status")
    _emit(
        on_progress,
        phase="waiting_apify",
        run_id=run_id,
        run_status=status,
        elapsed_seconds=0,
    )
    while status in ("READY", "RUNNING") and time.monotonic() < deadline:
        time.sleep(poll_interval)
        try:
            with httpx.Client(timeout=30.0) as client:
                status_response = client.get(status_url, params={"token": token})
                status_response.raise_for_status()
                run = status_response.json().get("data", {})
        except httpx.HTTPError as exc:
            raise ApifySyncError(f"Failed to poll Apify run status: {exc}") from exc
        status = run.get("status")
        dataset_id = run.get("defaultDatasetId") or dataset_id
        _emit(
            on_progress,
            phase="waiting_apify",
            run_id=run_id,
            run_status=status,
            elapsed_seconds=round(time.monotonic() - poll_started_at),
        )

    if status != "SUCCEEDED":
        raise ApifySyncError(f"Apify run did not finish successfully (status={status}).")
    if not dataset_id:
        raise ApifySyncError("Apify run succeeded but returned no dataset id.")

    _emit(on_progress, phase="fetching_dataset")
    items_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items"
    try:
        with httpx.Client(timeout=60.0) as client:
            items_response = client.get(items_url, params={"token": token, "format": "json"})
            items_response.raise_for_status()
            items = items_response.json()
    except httpx.HTTPError as exc:
        raise ApifySyncError(f"Failed to fetch Apify dataset items: {exc}") from exc
    if not isinstance(items, list):
        raise ApifySyncError("Apify dataset returned an unexpected response shape.")
    _emit(on_progress, phase="dataset_ready", fetched=len(items))
    return items


# ---------------------------------------------------------------------------
# New-post insertion
# ---------------------------------------------------------------------------


def _insert_new_chatgptricks_posts(
    new_items: list[dict[str, Any]], on_progress: ProgressFn = None
) -> dict[str, Any]:
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
    total = len(new_items)

    def _tick(index: int, shortcode: str) -> None:
        _emit(
            on_progress,
            phase="inserting",
            done=index + 1,
            total=total,
            added=summary["added"],
            failed=summary["failed"],
            shortcode=shortcode,
        )

    with httpx.Client(timeout=60.0, headers={"User-Agent": "Mozilla/5.0"}) as image_client:
        for index, item in enumerate(new_items):
            shortcode = str(item["shortCode"]).strip()
            source_ref = f"instagram:{shortcode}"
            image_url = item.get("displayUrl") or next(iter(item.get("images") or []), None)
            if not image_url:
                summary["failed"] += 1
                summary["items"].append({"shortcode": shortcode, "status": "failed", "error": "no cover image"})
                _tick(index, shortcode)
                continue

            try:
                image_response = image_client.get(image_url)
                image_response.raise_for_status()
                image_bytes = image_response.content
            except httpx.HTTPError as exc:
                summary["failed"] += 1
                summary["items"].append({"shortcode": shortcode, "status": "failed", "error": str(exc)})
                _tick(index, shortcode)
                continue

            image_bytes, suffix = _compress_cover(image_bytes)
            # Keyed by shortcode rather than random bytes: re-importing a post
            # overwrites its own cover instead of orphaning the old file on
            # disk with no way to ever find or clean it up.
            image_path = UPLOAD_DIR / f"cover-{shortcode}{suffix}"
            image_path.write_bytes(image_bytes)

            caption = _clean_text(item.get("caption"))
            title = _title_from_caption(caption) or f"Instagram post {shortcode}"
            post_type_label, is_video = _post_type_label(item)
            likes = _likes_or_none(item.get("likesCount"))
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
            _tick(index, shortcode)

    return summary


def _insert_new_dashboard_posts(
    account: str, new_items: list[dict[str, Any]], on_progress: ProgressFn = None
) -> dict[str, Any]:
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
    total = len(new_items)

    with httpx.Client(timeout=60.0, headers={"User-Agent": "Mozilla/5.0"}) as image_client:
        for index, item in enumerate(new_items):
            shortcode = str(item["shortCode"]).strip()
            image_url = item.get("displayUrl") or next(iter(item.get("images") or []), None)
            cover_path: str | None = None
            if image_url:
                try:
                    image_response = image_client.get(image_url)
                    image_response.raise_for_status()
                    image_bytes, suffix = _compress_cover(image_response.content)
                    # Keyed by account+shortcode rather than random bytes so a
                    # re-import overwrites its own cover instead of orphaning
                    # the previous file on disk forever.
                    image_path = UPLOAD_DIR / f"dash-{account}-{shortcode}{suffix}"
                    image_path.write_bytes(image_bytes)
                    cover_path = str(image_path)
                except httpx.HTTPError:
                    cover_path = None

            caption = _clean_text(item.get("caption"))
            post_type_label, is_video = _post_type_label(item)
            likes = _likes_or_none(item.get("likesCount"))
            comments = item.get("commentsCount") or 0
            permalink = item.get("url") or f"https://www.instagram.com/p/{shortcode}/"
            extracted = extract_apify_fields(item)
            now_iso = utc_now()

            try:
                with connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO dashboard_posts (
                            account, shortcode, published_at, likes, comments, caption,
                            post_type_label, is_animated, permalink,
                            cover_source_url, cover_image_path, created_at, updated_at,
                            enriched_at, {extra_cols}
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {extra_ph})
                        """.format(
                            extra_cols=", ".join(_EXTRACT_COLUMNS),
                            extra_ph=", ".join("?" for _ in _EXTRACT_COLUMNS),
                        ),
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
                            now_iso,
                            *(extracted[col] for col in _EXTRACT_COLUMNS),
                        ),
                    )
            except Exception as exc:  # e.g. UNIQUE constraint on a race re-add
                summary["failed"] += 1
                summary["items"].append({"shortcode": shortcode, "status": "failed", "error": str(exc)})
                _emit(
                    on_progress,
                    phase="inserting",
                    done=index + 1,
                    total=total,
                    added=summary["added"],
                    failed=summary["failed"],
                    shortcode=shortcode,
                )
                continue

            summary["added"] += 1
            summary["items"].append({"shortcode": shortcode, "status": "added", "published_at": _published_at(item)})
            _emit(
                on_progress,
                phase="inserting",
                done=index + 1,
                total=total,
                added=summary["added"],
                failed=summary["failed"],
                shortcode=shortcode,
            )

    return summary


def _insert_new_posts(
    account: str, cfg: dict[str, Any], new_items: list[dict[str, Any]], on_progress: ProgressFn = None
) -> dict[str, Any]:
    if cfg["is_canonical"]:
        return _insert_new_chatgptricks_posts(new_items, on_progress=on_progress)
    return _insert_new_dashboard_posts(account, new_items, on_progress=on_progress)


# ---------------------------------------------------------------------------
# Minimum post age before the one-time HOT decision is made. Matches the
# scheduler's tightest cadence (30 min during posting hours): once half an hour
# of data exists the hourly rate can be extrapolated, so waiting a full hour
# only delays surfacing a post that's already taking off.
_HOT_MIN_AGE_HOURS = 0.5

# How far back the short cycle looks, and how many posts it pulls per account.
# 20 comfortably covers a day's output for every tracked account (the busiest
# post ~5-10/day), so a lower cap just stops paying to re-fetch old posts.
logger = logging.getLogger(__name__)

_SHORT_LOOKBACK_HOURS = 12
# Per-account retries in the daily snapshot job. The job fires once a day and
# its data can't be backfilled, so a transient Apify timeout on one profile
# would otherwise cost that account a permanent hole in its history.
_SNAPSHOT_ATTEMPTS = 3
_SNAPSHOT_RETRY_DELAY_SECONDS = 5.0
_SHORT_RESULTS_LIMIT = 20

# Short-term cycle: every 30min during posting hours, hourly overnight -- posts <=24h old
# ---------------------------------------------------------------------------


def _short_term_payload(handles: list[str], results_limit: int, now: datetime) -> dict[str, Any]:
    return {
        "directUrls": [f"https://www.instagram.com/{handle}/" for handle in handles],
        "resultsType": "posts",
        # Per Apify's own docs this is "results limit PER URL", so batching
        # multiple accounts into one call still fetches up to this many
        # posts for each profile individually -- it doesn't get divided up
        # across accounts.
        "resultsLimit": results_limit,
        "skipPinnedPosts": True,
        # 12h lookback, and this window is the single biggest driver of the
        # Apify bill: the actor is billed per result, and every cycle re-fetches
        # (and re-pays for) every post still inside the window. Widening it by
        # an hour costs an hour's worth of posts on every cycle, forever.
        #
        # 12h is chosen because the two things this window feeds have very
        # different needs: the one-time HOT check only needs a post to be seen
        # once after it turns 0.5h old, and new-post discovery only needs the
        # window to exceed the cycle interval. Both are satisfied many times
        # over -- hourly cycles give 12 chances to catch anything. Only the
        # like/comment refresh wants a long tail, and the daily cycle already
        # records final numbers for posts older than this.
        "onlyPostsNewerThan": (now - timedelta(hours=_SHORT_LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _process_short_term_items(account: str, cfg: dict[str, Any], items: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Shared per-account logic: insert brand-new posts from `items`, then
    refresh likes/comments (and do the one-time HOT check) on every existing
    post <=24h old. `items` is whatever this account's slice of a (possibly
    multi-account, batched) Apify fetch turned out to be.
    """
    table = cfg["table"]
    scope_sql, scope_params = _account_scope(table, account)

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
        # Tied to the scrape window rather than a fixed 24h: a post older than
        # the lookback can't be in `items`, so counting it as eligible would
        # only inflate the "unmatched" tally and make a healthy cycle look
        # broken. Keep the two in lockstep.
        if age_hours is None or age_hours > _SHORT_LOOKBACK_HOURS:
            continue
        eligible[shortcode] = {"id": row["id"], "hot_checked": bool(row["hot_checked"]), "age_hours": age_hours}

    items_by_shortcode = {it.get("shortCode"): it for it in items if it.get("shortCode")}
    now_iso = utc_now()
    engagement_summary: dict[str, Any] = {"checked": len(eligible), "updated": 0, "hot_marked": 0, "unmatched": 0}
    pending_alerts: list[dict[str, Any]] = []

    for shortcode, info in eligible.items():
        item = items_by_shortcode.get(shortcode)
        if not item:
            engagement_summary["unmatched"] += 1
            continue
        likes = _likes_or_none(item.get("likesCount"))
        comments = item.get("commentsCount")
        comments = comments if isinstance(comments, int) and comments >= 0 else None

        set_clauses = ["likes = ?", "updated_at = ?"]
        params: list[Any] = [likes, now_iso]
        if comments is not None:
            set_clauses.append("comments = ?")
            params.append(comments)
        # Only run the one-time HOT check when the like count is real. With an
        # unknown/hidden count, _apply_likes_floor returns the 500 baseline,
        # which at the ~1h mark computes to a rate of ~500/hr and would
        # permanently mark the post HOT for any account whose threshold is
        # <=500 -- purely as an artifact of the placeholder. Leave hot_checked
        # unset instead so a later cycle can decide on real data.
        if (
            not info["hot_checked"]
            and info["age_hours"] >= _HOT_MIN_AGE_HOURS
            and _likes_are_known(item.get("likesCount"))
        ):
            # The rate is always likes / real elapsed hours, so the check is
            # correct whenever the tick happens to land -- at 0.5h it
            # extrapolates (likes x2), at 1.3h it divides down.
            #
            # Caveat worth knowing: early engagement is front-loaded (the most
            # active followers see a post first), so extrapolating from 30
            # minutes tends to OVERSTATE the full-hour rate. Expect this to mark
            # HOT slightly more readily than the 1-hour check did. The observed
            # multiplier is stored on every post, so the bias can be measured
            # against real data later and the thresholds tuned if needed.
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
                # Queue the alert; it's sent only after the row is committed,
                # so a Slack hiccup can never leave us having announced a post
                # we failed to actually mark as HOT.
                #
                # Scoped to competitor accounts by default: a competitor going
                # HOT is news you'd want pushed to you, whereas your own posts
                # are already being watched in the dashboard. Override with
                # SLACK_ALERT_GROUPS (comma-separated, or "all").
                if _slack_alerts_cover(cfg["group"]):
                    pending_alerts.append(
                        {
                            "account": account,
                            "post_id": info["id"],
                            "likes": likes,
                            "multiplier": multiplier,
                            "rate_per_hour": round(rate_per_hour),
                            "threshold": threshold,
                            "age_hours": info["age_hours"],
                            "permalink": item.get("url") or f"https://www.instagram.com/p/{shortcode}/",
                            # Needed to build the dashboard deep link; the
                            # dashboard keys a post by account + shortcode.
                            "shortcode": shortcode,
                            "caption": _clean_text(item.get("caption")),
                        }
                    )
        params.append(info["id"])
        with connect() as conn:
            conn.execute(f"UPDATE {table} SET {', '.join(set_clauses)} WHERE id = ?", params)
        engagement_summary["updated"] += 1

    if pending_alerts:
        from .slack_alerts import cover_url_for, notify_hot_post, slack_configured

        if slack_configured():
            sent = 0
            for alert in pending_alerts:
                alert["cover_url"] = cover_url_for(alert["account"], alert["post_id"])
                if notify_hot_post(alert):
                    sent += 1
            engagement_summary["slack_alerts_sent"] = sent

    return {"new_posts": insert_summary, "engagement": engagement_summary}


def run_short_term_cycle(account: str, results_limit: int = _SHORT_RESULTS_LIMIT) -> dict[str, Any]:
    """Single-account entry point: (1) pulls the last ~30h of posts and
    inserts any brand-new ones, and (2) refreshes likes/comments on all
    existing posts <=24h old, doing a one-time "first hour" HOT check the
    first time each post is observed at >=1h old. Both pieces reuse a single
    Apify fetch to minimize API calls.

    The scheduler doesn't call this directly anymore -- see
    run_short_term_cycle_batch, which does the same thing for every active
    account in one Apify call instead of one call each. Kept for manual/
    single-account use (e.g. testing a specific account in isolation).
    """
    cfg = get_account_config(account)
    now = datetime.now(UTC)
    payload = _short_term_payload([cfg["handle"]], results_limit, now)
    items = _fetch_apify_items(payload)
    return _process_short_term_items(account, cfg, items, now)


def run_short_term_cycle_batch(accounts: list[str], results_limit: int = _SHORT_RESULTS_LIMIT) -> dict[str, dict[str, Any]]:
    """Same job as run_short_term_cycle, but for every account in one Apify
    call: a single actor run scrapes all accounts' profile URLs at once
    (resultsLimit is documented as "per URL", so each account still gets up
    to `results_limit` posts -- it isn't split across accounts), and results
    are matched back to each account via the post's ownerUsername field. This
    avoids paying a separate actor-run overhead per account every cycle.

    Failures are isolated per account, matching the old one-call-per-account
    behavior: a single account with a bad config, a private/renamed profile,
    or a DB error can't take down the whole batch and silently skip HOT
    detection for every other account. Only a failure of the shared Apify
    fetch itself (which no per-account handling could rescue) propagates.
    """
    if not accounts:
        return {}

    now = datetime.now(UTC)

    configs: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    for account in accounts:
        try:
            configs[account] = get_account_config(account)
        except Exception as exc:
            results[account] = {"error": f"config lookup failed: {exc}"}

    if not configs:
        return results

    handles = [cfg["handle"] for cfg in configs.values()]
    payload = _short_term_payload(handles, results_limit, now)
    items = _fetch_apify_items(payload)

    handle_to_account = {cfg["handle"].lower(): account for account, cfg in configs.items()}
    items_by_account: dict[str, list[dict[str, Any]]] = {account: [] for account in configs}
    for item in items:
        owner = (item.get("ownerUsername") or "").lower()
        account = handle_to_account.get(owner)
        if account:
            items_by_account[account].append(item)

    for account, cfg in configs.items():
        try:
            results[account] = _process_short_term_items(account, cfg, items_by_account[account], now)
        except Exception as exc:
            results[account] = {"error": f"processing failed: {exc}"}
    return results


# ---------------------------------------------------------------------------
# Daily cycle: once/day, posts >24h-7 days, plus exactly 30d / 120d
# ---------------------------------------------------------------------------


def run_daily_cycle(account: str) -> dict[str, Any]:
    """Refreshes likes/comments on posts >24h and <=7 days old (every day),
    plus one-time checks at exactly 30 days and exactly 120 days old. Posts
    outside those windows (8-29 days, 31-119 days, 121+ days) are left
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
        in_daily_window = 24 < age_hours <= 168
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
        # Start-and-poll, never the sync endpoint: per-URL detail scrapes run at
        # ~0.17 posts/sec, so a batch anywhere near the 200 cap takes ~20 min --
        # far past the sync endpoint's limit. It survives today only because
        # batches are per-account (~23 URLs); one prolific account would tip it
        # back into the 408-but-still-billed failure this project already paid
        # for twice.
        items.extend(_run_apify_actor_and_fetch(payload, max_wait_seconds=2400.0))

    items_by_shortcode = {it.get("shortCode"): it for it in items if it.get("shortCode")}
    now_iso = utc_now()

    for shortcode, info in eligible.items():
        item = items_by_shortcode.get(shortcode)
        if not item:
            summary["unmatched"] += 1
            continue
        likes = _likes_or_none(item.get("likesCount"))
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
    results_limit: int = 2000,
    date_from: str | None = None,
    date_to: str | None = None,
    on_progress: ProgressFn = None,
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
        #
        # Tried forcing RESIDENTIAL proxies here to fight Instagram's
        # CheerioCrawler BLOCKED responses on large accounts -- confirmed via
        # run logs that it didn't help (still got blocked repeatedly, in one
        # case worse than the datacenter default) and it costs more, so
        # reverted. The block looks tied to the actor's plain-HTTP request
        # fingerprint rather than IP reputation.
    }
    if date_from:
        payload["onlyPostsNewerThan"] = date_from
    # Large/full-history pulls (hundreds to low thousands of posts) can take
    # many minutes to scrape. run-sync-get-dataset-items holds one HTTP
    # connection open for the whole duration, and we confirmed via Apify's
    # own run history that this reliably gets dropped somewhere in the
    # Render<->Apify path on longer scrapes -- the run still SUCCEEDS and
    # gets billed, but we never receive the response. Start-and-poll instead
    # so no single connection needs to stay open for more than ~30s.
    _emit(on_progress, phase="preparing", account=account)
    items = _run_apify_actor_and_fetch(payload, max_wait_seconds=1800.0, on_progress=on_progress)

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

    _emit(on_progress, phase="matching", fetched=len(items))
    from .db import connect

    with connect() as conn:
        existing_rows = conn.execute(
            f"SELECT shortcode FROM {table} WHERE 1=1{scope_sql}", scope_params
        ).fetchall()
    existing_shortcodes = {row["shortcode"] for row in existing_rows if row["shortcode"]}

    new_items = [it for it in items if it.get("shortCode") and it["shortCode"] not in existing_shortcodes]
    new_items.sort(key=lambda it: it.get("timestamp") or "")
    _emit(
        on_progress,
        phase="inserting",
        done=0,
        total=len(new_items),
        already_had=len(existing_shortcodes),
        fetched=len(items),
    )
    return _insert_new_posts(account, cfg, new_items, on_progress=on_progress)


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
        "following_count": item.get("followsCount"),
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


def refresh_single_post(handle: str, shortcode: str) -> dict[str, Any]:
    """Re-scrapes one post and updates its like/comment counts.

    The scheduled cycle only touches posts inside its 12h lookback, so an
    older post's numbers stay frozen at whatever they were when it aged out.
    This is the manual escape hatch behind the card's "Reload" action.

    Scrapes the post URL directly rather than the profile: one result instead
    of a page of them (~$0.002), and it works no matter how old the post is.
    Deliberately does NOT touch is_hot -- that's a one-time judgement made
    from the post's first-hour velocity, and recomputing it from a like count
    weeks later would be meaningless.
    """
    from .db import connect

    cfg = _account_config(handle)
    table = cfg["table"]
    clean = shortcode.strip()
    if not clean:
        raise ApifySyncError("A shortcode is required.")

    where = "shortcode = ?" if table == "posts" else "account = ? AND shortcode = ?"
    where_params: list[Any] = [clean] if table == "posts" else [handle, clean]

    with connect() as conn:
        row = conn.execute(
            f"SELECT id, likes, comments, permalink FROM {table} WHERE {where}"
            if table != "posts"
            else f"SELECT id, likes, comments FROM {table} WHERE {where}",
            where_params,
        ).fetchone()
    if row is None:
        raise ApifySyncError("Post not found.")

    url = (dict(row).get("permalink") or "").strip() or f"https://www.instagram.com/p/{clean}/"
    items = _fetch_apify_items(
        {"directUrls": [url], "resultsType": "posts", "resultsLimit": 1},
        timeout=90.0,
    )
    item = next((it for it in items if (it.get("shortCode") or "") == clean), items[0] if items else None)
    if not item:
        raise ApifySyncError("Instagram returned nothing for that post -- it may have been deleted.")

    likes = _likes_or_none(item.get("likesCount"))
    comments = item.get("commentsCount")
    comments = comments if isinstance(comments, int) and comments >= 0 else None

    set_clauses = ["likes = ?", "updated_at = ?"]
    params: list[Any] = [likes, utc_now()]
    if comments is not None:
        set_clauses.append("comments = ?")
        params.append(comments)

    with connect() as conn:
        conn.execute(f"UPDATE {table} SET {', '.join(set_clauses)} WHERE {where}", [*params, *where_params])

    before = dict(row)
    return {
        "account": handle,
        "shortcode": clean,
        "likes": likes,
        "comments": comments if comments is not None else before.get("comments"),
        "likes_before": before.get("likes"),
        "comments_before": before.get("comments"),
    }


def snapshot_one_account(handle: str) -> dict[str, Any]:
    """Snapshots a single account right now and records it -- the manual
    "refresh" action behind the Tracker page's per-account button, and the
    building block snapshot_all_accounts loops over for the batch version.

    Retries transient failures (Apify read timeouts on individual profiles
    do happen) since a flaky request otherwise costs a full day of follower
    history that can never be recovered. Retrying one profile costs
    ~$0.002, always cheaper than losing the data point.
    """
    from .db import insert_account_snapshot

    clean = handle.strip().lstrip("@").lower()
    if not clean:
        raise ApifySyncError("Handle is required.")

    last_error: Exception | None = None
    for attempt in range(_SNAPSHOT_ATTEMPTS):
        try:
            preview = fetch_profile_preview(clean)
            insert_account_snapshot(
                handle=clean,
                followers_count=preview.get("followers_count"),
                posts_count=preview.get("posts_count"),
                full_name=preview.get("full_name"),
                verified=bool(preview.get("verified")),
                private=bool(preview.get("private")),
                following_count=preview.get("following_count"),
            )
            return preview
        except Exception as exc:  # noqa: BLE001 -- reported back, not raised
            last_error = exc
            # A missing/renamed account fails identically on every attempt,
            # so don't burn retries (or money) on it.
            if isinstance(exc, ApifySyncError) and "Could not find" in str(exc):
                break
            if attempt + 1 < _SNAPSHOT_ATTEMPTS:
                logger.warning(
                    "Snapshot for %s failed (attempt %d/%d): %s -- retrying",
                    clean, attempt + 1, _SNAPSHOT_ATTEMPTS, exc,
                )
                time.sleep(_SNAPSHOT_RETRY_DELAY_SECONDS)
    if isinstance(last_error, ApifySyncError):
        raise last_error
    raise ApifySyncError(str(last_error) if last_error else "Snapshot failed.")


def snapshot_all_accounts() -> dict[str, Any]:
    """Takes one Tracker-page snapshot (followers/posts count) per active
    account and records it. Shared by the scheduler's daily job, the admin
    "snapshot now" button, and the Tracker page's own overview "refresh all"
    action, so a fresh install (or someone impatient for day-one data)
    doesn't have to wait for 7am CST to see the leaderboard populate.
    """
    ok: list[str] = []
    failed: dict[str, str] = {}
    for account in list_accounts(active_only=True):
        handle = account["handle"]
        try:
            snapshot_one_account(handle)
            ok.append(handle)
        except Exception as exc:  # noqa: BLE001 -- reported back, not raised
            failed[handle] = str(exc)
    return {"snapshotted": ok, "failed": failed}


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
