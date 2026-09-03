"""Restore legacy post covers through Apify and store them in R2.

This is intentionally a one-off job rather than a web-process background
thread: large historical Apify runs must survive browser/shell disconnects.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from typing import Any

from app.apify_sync import _refresh_cover_from_item, _run_apify_actor_and_fetch
from app.db import connect


def _targets(source_prefix: str) -> list[Any]:
    with connect() as conn:
        return conn.execute(
            "SELECT id, shortcode FROM posts WHERE image_path IS NOT NULL "
            "AND TRIM(image_path) != '' AND image_path NOT LIKE ? "
            "AND source_ref LIKE ? AND shortcode IS NOT NULL "
            "AND TRIM(shortcode) != '' ORDER BY id",
            ("r2://uploads/%", f"{source_prefix}:%"),
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
            _refresh_cover_from_item("posts", "", shortcode, row, item)
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


def normalize_direct(batch_size: int) -> dict[str, int]:
    rows = _targets("instagram")
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
        print({"source": "instagram", "batch": start // batch_size + 1, **total}, flush=True)
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize legacy post covers with Apify.")
    parser.add_argument("--source", choices=("chatgptricks", "instagram", "all"), default="all")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--profile-limit", type=int, default=3000)
    args = parser.parse_args()
    if args.batch_size < 1 or args.profile_limit < 1:
        parser.error("batch-size and profile-limit must be positive")
    if args.source in {"chatgptricks", "all"}:
        normalize_chatgptricks(args.profile_limit)
    if args.source in {"instagram", "all"}:
        print({"source": "instagram", **normalize_direct(args.batch_size)}, flush=True)


if __name__ == "__main__":
    main()
