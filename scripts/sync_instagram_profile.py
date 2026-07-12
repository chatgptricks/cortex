#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync the newest Instagram posts from a profile into Cortex."
    )
    parser.add_argument(
        "--profile",
        required=True,
        help="Instagram profile username or profile URL to scan for new posts.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=12,
        help="Maximum number of recent posts to inspect from the profile page.",
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=2,
        help="Duration of the generated static video used for analysis.",
    )
    parser.add_argument(
        "--analyze",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run TRIBE analysis after each import. Use --no-analyze for import-only syncs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the posts that would be imported without writing anything.",
    )
    parser.add_argument(
        "--prune-missing",
        action="store_true",
        help="Delete Instagram posts already in the DB that are no longer on the current profile timeline.",
    )
    parser.add_argument(
        "--start-after-shortcode",
        default="",
        help="Skip newer posts until this shortcode is seen, then continue importing older posts.",
    )
    parser.add_argument(
        "--stop-on-existing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Stop at the first shortcode already in the DB. Use --no-stop-on-existing to keep scanning older posts.",
    )
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Update existing Instagram rows with the latest metadata while scanning the timeline.",
    )
    args = parser.parse_args()

    os.chdir(ROOT_DIR)
    _load_dotenv(ROOT_DIR / ".env")
    sys.path.insert(0, str(ROOT_DIR / "backend"))

    from app.db import init_db
    from app.instagram_import import InstagramImportError, sync_instagram_profile_posts
    from app.main import run_analysis_job

    init_db()

    try:
        summary = sync_instagram_profile_posts(
            args.profile,
            limit=args.limit,
            dry_run=args.dry_run,
            analyze_now=args.analyze,
            duration_seconds=args.duration_seconds,
            analyze_post=run_analysis_job,
            prune_missing=args.prune_missing,
            stop_on_existing=args.stop_on_existing,
            refresh_existing=args.refresh_existing,
            start_after_shortcode=args.start_after_shortcode or None,
        )
    except InstagramImportError as exc:
        raise SystemExit(str(exc)) from exc

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
