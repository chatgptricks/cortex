from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Modal OCR for completed Post DB covers missing hook text.")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0, help="Defaults to OCR_BATCH_SIZE.")
    parser.add_argument("--min-ready", type=int, default=0, help="Defaults to OCR_BATCH_MIN_READY.")
    parser.add_argument("--crop-region", default="", help="Defaults to OCR_CROP_REGION.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    os.chdir(ROOT_DIR)
    _load_dotenv(ROOT_DIR / ".env")
    sys.path.insert(0, str(ROOT_DIR / "backend"))

    from app.config import OCR_BATCH_MIN_READY, OCR_BATCH_SIZE, OCR_CROP_REGION
    from app.db import connect, init_db, utc_now
    from app.remote_ocr import extract_images_text_remote, remote_ocr_status

    init_db()
    batch_size = args.limit or OCR_BATCH_SIZE
    min_ready = args.min_ready or OCR_BATCH_MIN_READY
    crop_region = args.crop_region or OCR_CROP_REGION
    if batch_size < min_ready:
        raise SystemExit(f"OCR batch size must be at least {min_ready}.")

    with connect() as conn:
        eligible_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM posts
            WHERE section = 'historical'
              AND status = 'completed'
              AND analysis_summary IS NOT NULL
              AND TRIM(COALESCE(hook_text, '')) = ''
              AND source_row_number >= ?
            """,
            (args.start,),
        ).fetchone()[0]

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
            (args.start, batch_size),
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

    status = remote_ocr_status()
    preview = {
        "remote_ocr": status,
        "eligible_count": eligible_count,
        "required_min_ready": min_ready,
        "selected_count": len(selected),
        "batch_size": batch_size,
        "crop_region": crop_region,
        "selected": [
            {"id": row["id"], "source_row_number": row["source_row_number"], "file": row["image_path"].name}
            for row in selected
        ],
    }
    if args.dry_run:
        print(json.dumps(preview, indent=2))
        return
    if eligible_count < min_ready:
        raise SystemExit(json.dumps({**preview, "error": "Not enough completed Post DB posts missing OCR."}, indent=2))
    if len(selected) < min(batch_size, min_ready):
        raise SystemExit(json.dumps({**preview, "error": "Some eligible image files are missing locally."}, indent=2))

    results = extract_images_text_remote([row["image_path"] for row in selected], crop_region=crop_region)
    updated: list[dict[str, Any]] = []
    with connect() as conn:
        for row, result in zip(selected, results, strict=False):
            text = _clean_text(result.get("text") if isinstance(result, dict) else None)
            if not text:
                updated.append({**_row_summary(row), "ocr_text": None, "updated": False})
                continue
            conn.execute(
                """
                UPDATE posts
                SET hook_text = ?, updated_at = ?
                WHERE id = ?
                  AND TRIM(COALESCE(hook_text, '')) = ''
                """,
                (text, utc_now(), row["id"]),
            )
            updated.append({**_row_summary(row), "ocr_text": text, "updated": True})

    print(
        json.dumps(
            {
                "remote_ocr": status,
                "eligible_count": eligible_count,
                "processed": len(results),
                "updated": updated,
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
