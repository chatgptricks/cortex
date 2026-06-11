#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

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
    parser = argparse.ArgumentParser(description="Process pending/failed historical posts concurrently.")
    parser.add_argument(
        "--concurrency", "-c",
        type=int,
        default=4,
        help="Number of concurrent worker threads. Adjust based on your Modal worker settings."
    )
    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=0,
        help="Maximum number of posts to process in this run. 0 processes all."
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=2,
        help="Duration of the generated static video."
    )
    args = parser.parse_args()

    os.chdir(ROOT_DIR)
    _load_dotenv(ROOT_DIR / ".env")
    sys.path.insert(0, str(ROOT_DIR / "backend"))

    from app.config import ANALYSIS_DIR, DATA_DIR, VIDEO_DIR
    from app.db import connect, utc_now
    from app.remote_tribe import RemoteTribeUnavailable, analyze_video_remote, remote_tribe_status
    from app.tribe_adapter import TribeUnavailable, analyze_video, write_analysis
    from app.video import create_static_video

    # Check remote worker configuration
    remote_status = remote_tribe_status()
    is_remote = remote_status.get("configured", False)
    print(f"TRIBE v2 backend mode: {'Remote GPU (' + remote_status.get('url') + ')' if is_remote else 'Local CPU/GPU'}")

    # Fetch queued and failed posts
    with connect() as conn:
        query = """
            SELECT id, title, source_row_number, image_path 
            FROM posts 
            WHERE section = 'historical' 
              AND status IN ('queued', 'failed')
            ORDER BY source_row_number, id
        """
        if args.limit > 0:
            query += f" LIMIT {args.limit}"
        
        pending_posts = [dict(row) for row in conn.execute(query).fetchall()]

    if not pending_posts:
        print("No pending or failed historical posts to process.")
        return

    print(f"Found {len(pending_posts)} posts to process with concurrency={args.concurrency}.")
    
    start_time = time.time()
    successful = 0
    failed = 0

    def process_single_post(post: dict[str, Any]) -> tuple[int, bool, float, str | None]:
        post_id = int(post["id"])
        source_num = post.get("source_row_number")
        title = post.get("title")
        image_path = Path(post["image_path"])
        
        prefix = f"[Row {source_num:04d} | ID {post_id}]"
        print(f"{prefix} Starting: '{title[:40]}'")
        
        item_start = time.time()
        
        def _set_progress(percent: int, message: str, status: str = "running") -> None:
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE posts
                    SET status = ?, error = NULL, progress_percent = ?,
                        progress_message = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, percent, message, utc_now(), post_id),
                )

        try:
            _set_progress(10, "Preparing cover")
            video_path = VIDEO_DIR / f"{post_id}-{uuid.uuid4().hex}.mp4"
            
            _set_progress(22, "Converting image to video")
            create_static_video(image_path, video_path, duration_seconds=args.duration_seconds)
            
            if is_remote:
                _set_progress(38, "Video ready; sending to remote GPU")
                summary = analyze_video_remote(video_path, duration_seconds=args.duration_seconds)
            else:
                _set_progress(38, "Video ready; loading TRIBE v2")
                summary = analyze_video(video_path, duration_seconds=args.duration_seconds)
                
            _set_progress(84, "Summarizing brain activations")
            analysis_path = ANALYSIS_DIR / f"{post_id}-{uuid.uuid4().hex}.json"
            write_analysis(analysis_path, summary)
            
            _set_progress(94, "Saving results")
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE posts
                    SET video_path = ?, analysis_path = ?, analysis_summary = ?,
                        status = ?, error = NULL, progress_percent = ?,
                        progress_message = ?, llm_report = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(video_path),
                        str(analysis_path),
                        json.dumps(summary),
                        "completed",
                        100,
                        "Complete",
                        utc_now(),
                        post_id,
                    ),
                )
            
            elapsed = time.time() - item_start
            print(f"{prefix} SUCCESS in {elapsed:.2f}s")
            return post_id, True, elapsed, None
            
        except Exception as exc:
            elapsed = time.time() - item_start
            err_msg = str(exc)
            print(f"{prefix} FAILED in {elapsed:.2f}s: {err_msg}")
            
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE posts
                    SET status = ?, error = ?, progress_message = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    ("failed", err_msg, "Analysis failed", utc_now(), post_id),
                )
            return post_id, False, elapsed, err_msg

    # Execute batch concurrently using ThreadPoolExecutor
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {executor.submit(process_single_post, post): post for post in pending_posts}
        for future in as_completed(futures):
            post_id, ok, elapsed, err = future.result()
            results.append((post_id, ok, elapsed, err))
            if ok:
                successful += 1
            else:
                failed += 1

    total_elapsed = time.time() - start_time
    print("\n" + "=" * 50)
    print("BATCH RUN COMPLETED")
    print("=" * 50)
    print(f"Total time elapsed: {total_elapsed:.2f} seconds")
    print(f"Successful:         {successful}")
    print(f"Failed:             {failed}")
    if successful > 0:
        avg_time = sum(res[2] for res in results if res[1]) / successful
        print(f"Average time/post:  {avg_time:.2f} seconds")
    print("=" * 50)

if __name__ == "__main__":
    main()
