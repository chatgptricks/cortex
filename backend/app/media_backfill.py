"""Resumable, non-destructive R2 backfill for existing Dashboard media."""

from __future__ import annotations

import argparse

from .db import connect, init_db
from .media_storage import r2_enabled, upload_legacy_local_media

_SOURCES = (("dashboard_posts", "cover_image_path"), ("posts", "image_path"), ("accounts", "avatar_path"))


def backfill(limit: int, *, dry_run: bool) -> dict[str, int]:
    if not r2_enabled():
        raise RuntimeError("R2 is not enabled. Configure and enable R2_MEDIA_ENABLED first.")
    init_db()
    result = {"scanned": 0, "uploaded": 0, "skipped": 0, "failed": 0}
    remaining = limit
    for table, column in _SOURCES:
        if remaining <= 0:
            break
        with connect() as conn:
            rows = conn.execute(
                f"SELECT id, {column} AS media_ref FROM {table} WHERE {column} IS NOT NULL "
                f"AND TRIM({column}) != '' AND {column} NOT LIKE 'r2://uploads/%' ORDER BY id LIMIT ?", (remaining,)
            ).fetchall()
        for row in rows:
            reference = str(row["media_ref"])
            result["scanned"] += 1
            remaining -= 1
            if dry_run:
                result["skipped"] += 1
                continue
            remote_ref = upload_legacy_local_media(reference)
            if not remote_ref:
                result["failed"] += 1
                continue
            with connect() as conn:
                conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (remote_ref, int(row["id"])))
            result["uploaded"] += 1
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill existing Dashboard media to R2 safely.")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    print("media R2 backfill:", backfill(args.limit, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
