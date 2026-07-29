from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
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


def run_ocr_sweep(limit: int = 30, crop_region: str = "full") -> dict[str, Any]:
    """Fills in cover OCR text (hook_text) for any post still missing it, across
    BOTH tables -- dashboard_posts and the canonical posts table. Downloads the
    cover first when it was never cached locally.

    Sized for the scheduler: processes at most `limit` covers per call so a
    single tick stays bounded, and marks every row it touches as checked so
    blank/failed ones are never retried (and re-billed) on the next pass.
    """
    from .db import connect, utc_now

    from .remote_ocr import extract_images_text_remote

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
            canon = []
            if len(dash) < limit:
                canon = conn.execute(
                    "SELECT id, shortcode, image_path FROM posts "
                    "WHERE TRIM(COALESCE(hook_text, '')) = '' AND image_path IS NOT NULL AND image_path != '' "
                    "ORDER BY id DESC LIMIT ?",
                    (limit - len(dash),),
                ).fetchall()
                if canon:
                    # posts has no ocr_checked column; the in-flight marker is
                    # a placeholder hook_text that _clean_ocr_text hides.
                    conn.executemany(
                        "UPDATE posts SET hook_text = '~' WHERE id = ?", [(int(r["id"]),) for r in canon]
                    )

    with connect() as conn:
        summary["remaining"] = conn.execute(
            "SELECT (SELECT COUNT(*) FROM dashboard_posts "
            "        WHERE TRIM(COALESCE(hook_text,''))='' AND ocr_checked=0) "
            "     + (SELECT COUNT(*) FROM posts "
            "        WHERE TRIM(COALESCE(hook_text,''))='' AND image_path IS NOT NULL AND image_path != '') AS c"
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
        results = extract_images_text_remote([p for _, _, p in jobs], crop_region=crop_region)
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

    status_url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    deadline = time.monotonic() + max_wait_seconds
    dataset_id = run.get("defaultDatasetId")
    status = run.get("status")
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

    if status != "SUCCEEDED":
        raise ApifySyncError(f"Apify run did not finish successfully (status={status}).")
    if not dataset_id:
        raise ApifySyncError("Apify run succeeded but returned no dataset id.")

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
# Short-term cycle: hourly (7:30am-11:30pm fixed CST), posts <=24h old
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
        "onlyPostsNewerThan": (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        if not info["hot_checked"] and info["age_hours"] >= 1.0 and _likes_are_known(item.get("likesCount")):
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


def run_short_term_cycle(account: str, results_limit: int = 80) -> dict[str, Any]:
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


def run_short_term_cycle_batch(accounts: list[str], results_limit: int = 80) -> dict[str, dict[str, Any]]:
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
        items.extend(_fetch_apify_items(payload, timeout=300.0))

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
    # Large/full-history pulls (hundreds to low thousands of posts) can take
    # many minutes to scrape. run-sync-get-dataset-items holds one HTTP
    # connection open for the whole duration, and we confirmed via Apify's
    # own run history that this reliably gets dropped somewhere in the
    # Render<->Apify path on longer scrapes -- the run still SUCCEEDS and
    # gets billed, but we never receive the response. Start-and-poll instead
    # so no single connection needs to stay open for more than ~30s.
    items = _run_apify_actor_and_fetch(payload, max_wait_seconds=1800.0)

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
