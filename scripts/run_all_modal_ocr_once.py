from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Modal OCR pass over every Post DB row missing hook text.")
    parser.add_argument("--limit", type=int, default=100, help="Max files per Modal request. Worker limit is 100.")
    parser.add_argument("--crop-region", default="", help="Defaults to OCR_CROP_REGION.")
    parser.add_argument("--log-dir", default="data/ocr-logs/one-pass")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.limit < 1 or args.limit > 100:
        raise SystemExit("--limit must be between 1 and 100.")

    os.chdir(ROOT_DIR)
    _load_dotenv(ROOT_DIR / ".env")
    sys.path.insert(0, str(ROOT_DIR / "backend"))

    from app.config import OCR_CROP_REGION
    from app.db import connect, init_db, utc_now
    from app.remote_ocr import extract_images_text_remote, remote_ocr_status

    init_db()
    crop_region = args.crop_region or OCR_CROP_REGION
    log_dir = ROOT_DIR / args.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, source_row_number, image_path
            FROM posts
            WHERE section = 'historical'
              AND status = 'completed'
              AND analysis_summary IS NOT NULL
              AND TRIM(COALESCE(hook_text, '')) = ''
            ORDER BY source_row_number, id
            """
        ).fetchall()

    selected = [
        {
            "id": int(row["id"]),
            "source_row_number": int(row["source_row_number"] or 0),
            "image_path": Path(row["image_path"]),
        }
        for row in rows
        if row["image_path"] and Path(row["image_path"]).exists()
    ]
    missing_files = len(rows) - len(selected)
    status = remote_ocr_status()
    print(
        json.dumps(
            {
                "remote_ocr": status,
                "pending_snapshot": len(rows),
                "selected": len(selected),
                "missing_files": missing_files,
                "limit": args.limit,
                "crop_region": crop_region,
            },
            indent=2,
        )
    )
    if args.dry_run:
        return

    total_processed = 0
    total_updated = 0
    total_blank = 0
    for batch_index, start in enumerate(range(0, len(selected), args.limit), start=1):
        batch = selected[start : start + args.limit]
        results = extract_images_text_remote([row["image_path"] for row in batch], crop_region=crop_region)
        updated_rows: list[dict[str, Any]] = []
        with connect() as conn:
            for row, result in zip(batch, results, strict=False):
                text = _clean_text(result.get("text") if isinstance(result, dict) else None)
                updated = False
                if text:
                    conn.execute(
                        """
                        UPDATE posts
                        SET hook_text = ?, updated_at = ?
                        WHERE id = ?
                          AND TRIM(COALESCE(hook_text, '')) = ''
                        """,
                        (text, utc_now(), row["id"]),
                    )
                    updated = True
                updated_rows.append({**_row_summary(row), "ocr_text": text or None, "updated": updated})

        processed = len(results)
        updated_count = sum(1 for item in updated_rows if item["updated"])
        blank_count = processed - updated_count
        total_processed += processed
        total_updated += updated_count
        total_blank += blank_count
        log_path = log_dir / f"modal_ocr_one_pass_{batch_index:03d}.json"
        log_path.write_text(
            json.dumps(
                {
                    "batch": batch_index,
                    "processed": processed,
                    "updated": updated_count,
                    "blank": blank_count,
                    "rows": updated_rows,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "batch": batch_index,
                    "processed": processed,
                    "updated": updated_count,
                    "blank": blank_count,
                    "total_processed": total_processed,
                    "total_updated": total_updated,
                    "total_blank": total_blank,
                    "log": str(log_path),
                }
            )
        )

    with connect() as conn:
        missing_after = conn.execute(
            """
            SELECT COUNT(*)
            FROM posts
            WHERE section = 'historical'
              AND status = 'completed'
              AND analysis_summary IS NOT NULL
              AND TRIM(COALESCE(hook_text, '')) = ''
            """
        ).fetchone()[0]
    print(
        json.dumps(
            {
                "done": True,
                "total_processed": total_processed,
                "total_updated": total_updated,
                "total_blank": total_blank,
                "missing_after": missing_after,
            },
            indent=2,
        )
    )


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _row_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source_row_number": row["source_row_number"],
        "file": row["image_path"].name,
    }


if __name__ == "__main__":
    main()
