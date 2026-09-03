"""Restore legacy post covers through Apify and store them in R2.

This is intentionally a one-off job rather than a web-process background
thread: large historical Apify runs must survive browser/shell disconnects.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from typing import Any

from app.apify_sync import (
    APIFY_REEL_ACTOR_ID,
    _refresh_cover_from_item,
    _run_apify_actor_and_fetch,
    store_avatar_from_url,
)
from app.db import connect
from app.media_storage import upload_legacy_local_media


def _targets(source_prefix: str) -> list[Any]:
    with connect() as conn:
        return conn.execute(
            "SELECT id, shortcode FROM posts WHERE image_path IS NOT NULL "
            "AND TRIM(image_path) != '' AND image_path NOT LIKE ? "
            "AND source_ref LIKE ? AND shortcode IS NOT NULL "
            "AND TRIM(shortcode) != '' ORDER BY id",
            ("r2://uploads/%", f"{source_prefix}:%"),
        ).fetchall()


def _all_post_targets() -> list[Any]:
    """Every recoverable post still pointing at legacy local media."""
    with connect() as conn:
        return conn.execute(
            "SELECT id, shortcode FROM posts WHERE image_path IS NOT NULL "
            "AND TRIM(image_path) != '' AND image_path NOT LIKE ? "
            "AND shortcode IS NOT NULL AND TRIM(shortcode) != '' ORDER BY id",
            ("r2://uploads/%",),
        ).fetchall()


def _fallback_targets(source_prefix: str) -> list[Any]:
    """Posts whose lost legacy path was normalized to the native fallback."""
    with connect() as conn:
        return conn.execute(
            "SELECT id, shortcode FROM posts WHERE image_path = ? "
            "AND source_ref LIKE ? AND shortcode IS NOT NULL "
            "AND TRIM(shortcode) != '' ORDER BY id",
            ("", f"{source_prefix}:%"),
        ).fetchall()


def _avatar_targets() -> list[Any]:
    with connect() as conn:
        return conn.execute(
            "SELECT handle FROM accounts WHERE avatar_path IS NOT NULL "
            "AND TRIM(avatar_path) != '' AND avatar_path NOT LIKE ? ORDER BY handle",
            ("r2://uploads/%",),
        ).fetchall()


def _local_only_targets() -> list[Any]:
    """Legacy images that predate source references and cannot be scraped."""
    with connect() as conn:
        return conn.execute(
            "SELECT id, image_path FROM posts WHERE image_path IS NOT NULL "
            "AND TRIM(image_path) != '' AND image_path NOT LIKE ? "
            "AND (shortcode IS NULL OR TRIM(shortcode) = '') ORDER BY id",
            ("r2://uploads/%",),
        ).fetchall()


def _apply(rows: Iterable[Any], items: list[dict[str, Any]]) -> dict[str, int]:
    by_shortcode = {
        str(item["shortCode"]): item
        for item in items
        if isinstance(item, dict) and item.get("shortCode")
    }
    result = {"targets": 0, "matched": 0, "uploaded": 0, "unmatched": 0}
    for row in rows:
        result["targets"] += 1
        shortcode = str(row["shortcode"])
        item = by_shortcode.get(shortcode)
        if item is None:
            result["unmatched"] += 1
            continue
        result["matched"] += 1
        result["uploaded"] += int(
            _refresh_cover_from_item(
                "posts",
                "",
                shortcode,
                row,
                item,
                image_timeout_seconds=15.0,
            )
        )
    return result


def normalize_chatgptricks(results_limit: int) -> dict[str, int]:
    rows = _targets("chatgptricks")
    if not rows:
        return {"targets": 0, "matched": 0, "uploaded": 0, "unmatched": 0}
    items = _run_apify_actor_and_fetch(
        {
            "directUrls": ["https://www.instagram.com/chatgptricks/"],
            "resultsType": "posts",
            "resultsLimit": results_limit,
        },
        max_wait_seconds=2400.0,
    )
    result = _apply(rows, items)
    print({"source": "chatgptricks", "actor_items": len(items), **result}, flush=True)
    return result


def normalize_chatgptricks_reels(results_limit: int) -> dict[str, int]:
    """Recover historical Reels that are absent from the profile-post feed."""
    rows = _fallback_targets("chatgptricks")
    if not rows:
        return {"targets": 0, "matched": 0, "uploaded": 0, "unmatched": 0}
    items = _run_apify_actor_and_fetch(
        {
            "username": ["chatgptricks"],
            "resultsLimit": results_limit,
            "includeTranscript": True,
        },
        max_wait_seconds=2400.0,
        actor_id=APIFY_REEL_ACTOR_ID,
    )
    result = _apply(rows, items)
    print({"source": "chatgptricks_reels", "actor_items": len(items), **result}, flush=True)
    return result


def normalize_direct(rows: list[Any], batch_size: int, source: str) -> dict[str, int]:
    total = {"targets": 0, "matched": 0, "uploaded": 0, "unmatched": 0}
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        items = _run_apify_actor_and_fetch(
            {
                "directUrls": [
                    f"https://www.instagram.com/p/{row['shortcode']}/" for row in batch
                ],
                "resultsType": "details",
            },
            max_wait_seconds=1800.0,
        )
        result = _apply(batch, items)
        for key, value in result.items():
            total[key] += value
        print({"source": source, "batch": start // batch_size + 1, **total}, flush=True)
    return total


def normalize_avatars(batch_size: int) -> dict[str, int]:
    rows = _avatar_targets()
    total = {"targets": 0, "matched": 0, "uploaded": 0, "unmatched": 0}
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        items = _run_apify_actor_and_fetch(
            {
                "directUrls": [
                    f"https://www.instagram.com/{str(row['handle']).strip().lstrip('@')}/"
                    for row in batch
                ],
                "resultsType": "details",
            },
            max_wait_seconds=1800.0,
        )
        by_handle = {
            str(item.get("username") or "").strip().lstrip("@").lower(): item
            for item in items
            if isinstance(item, dict)
        }
        for row in batch:
            total["targets"] += 1
            handle = str(row["handle"]).strip().lstrip("@").lower()
            item = by_handle.get(handle)
            image_url = item and (item.get("profilePicUrlHD") or item.get("profilePicUrl"))
            if not image_url:
                total["unmatched"] += 1
                continue
            total["matched"] += 1
            try:
                store_avatar_from_url(handle, str(image_url))
            except Exception:  # per-account recovery must not abandon the batch
                continue
            total["uploaded"] += 1
        print({"source": "avatars", "batch": start // batch_size + 1, **total}, flush=True)
    return total


def normalize_local_only() -> dict[str, int]:
    result = {"targets": 0, "uploaded": 0, "failed": 0}
    for row in _local_only_targets():
        result["targets"] += 1
        remote_ref = upload_legacy_local_media(str(row["image_path"]))
        if not remote_ref:
            result["failed"] += 1
            continue
        with connect() as conn:
            conn.execute("UPDATE posts SET image_path = ? WHERE id = ?", (remote_ref, int(row["id"])))
        result["uploaded"] += 1
    print({"source": "local_only", **result}, flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize legacy post covers with Apify.")
    parser.add_argument(
        "--source",
        choices=("chatgptricks", "chatgptricks_reels", "instagram", "all", "retry"),
        default="all",
    )
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--profile-limit", type=int, default=3000)
    args = parser.parse_args()
    if args.batch_size < 1 or args.profile_limit < 1:
        parser.error("batch-size and profile-limit must be positive")
    if args.source in {"chatgptricks", "all"}:
        normalize_chatgptricks(args.profile_limit)
    if args.source == "chatgptricks_reels":
        normalize_chatgptricks_reels(args.profile_limit)
    if args.source in {"instagram", "all"}:
        print(
            {
                "source": "instagram",
                **normalize_direct(_targets("instagram"), args.batch_size, "instagram"),
            },
            flush=True,
        )
    if args.source == "retry":
        print(
            {
                "source": "post_retry",
                **normalize_direct(_all_post_targets(), args.batch_size, "post_retry"),
            },
            flush=True,
        )
        print({"source": "avatars", **normalize_avatars(args.batch_size)}, flush=True)
        normalize_local_only()


if __name__ == "__main__":
    main()
