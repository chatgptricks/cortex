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
    parser = argparse.ArgumentParser(description="Train and evaluate the advanced Post DB prediction model.")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT_DIR / "data" / "models" / "latest_prediction_report.json",
        help="Where to write the model evaluation report.",
    )
    parser.add_argument("--pretty", action="store_true", help="Print formatted JSON.")
    args = parser.parse_args()

    os.chdir(ROOT_DIR)
    _load_dotenv(ROOT_DIR / ".env")
    sys.path.insert(0, str(ROOT_DIR / "backend"))

    from app.db import connect, row_to_post
    from app.prediction_model import fit_advanced_prediction, prediction_payload

    with connect() as conn:
        rows = conn.execute("SELECT * FROM posts ORDER BY created_at DESC").fetchall()
    posts = [row_to_post(row) for row in rows]
    model = fit_advanced_prediction(posts)
    report = prediction_payload(model)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    text = json.dumps(report, indent=2 if args.pretty else None, sort_keys=True)
    print(text)
    print(f"\nWrote report to {args.output}")


if __name__ == "__main__":
    main()
