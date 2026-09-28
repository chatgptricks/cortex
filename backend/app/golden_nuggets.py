"""Shared Golden Nugget marks for Research posts.

A Jev golden-nugget review used to live only in the reviewer's browser tab.
Confirmed nuggets are stored here so every signed-in user sees the gold card.
"""
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from .db import connect, utc_now

router = APIRouter(prefix="/api/dashboard/golden-nuggets", tags=["golden-nuggets"])


def ensure_schema(conn: Any) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS post_golden_nuggets (
        account TEXT NOT NULL,
        shortcode TEXT NOT NULL,
        label TEXT NOT NULL,
        target_account TEXT NOT NULL DEFAULT '',
        score DOUBLE PRECISION NOT NULL DEFAULT 0,
        review TEXT NOT NULL DEFAULT '{}',
        reviewed_by TEXT NOT NULL DEFAULT '',
        reviewed_at TEXT NOT NULL,
        PRIMARY KEY (account, shortcode)
    )""")


def _key(account: str, shortcode: str) -> tuple[str, str]:
    clean_account = account.strip().lstrip("@").lower()
    clean_shortcode = shortcode.strip()
    if not clean_account or not clean_shortcode:
        raise HTTPException(status_code=400, detail="Account and shortcode are required.")
    return clean_account, clean_shortcode


def record_review(account: str, shortcode: str, review: dict[str, Any], reviewer: str = "") -> None:
    """Keep confirmed nuggets; a later review that downgrades a post clears it."""
    clean_account, clean_shortcode = _key(account, shortcode)
    with connect() as conn:
        ensure_schema(conn)
        if review.get("label") != "golden_nugget":
            conn.execute(
                "DELETE FROM post_golden_nuggets WHERE account = ? AND shortcode = ?",
                (clean_account, clean_shortcode),
            )
            return
        conn.execute(
            """INSERT INTO post_golden_nuggets
                   (account, shortcode, label, target_account, score, review, reviewed_by, reviewed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account, shortcode) DO UPDATE SET
                   label = excluded.label, target_account = excluded.target_account,
                   score = excluded.score, review = excluded.review,
                   reviewed_by = excluded.reviewed_by, reviewed_at = excluded.reviewed_at""",
            (
                clean_account,
                clean_shortcode,
                "golden_nugget",
                str(review.get("targetAccount") or ""),
                float(review.get("score") or 0),
                json.dumps(review, ensure_ascii=False, default=str)[:20_000],
                reviewer,
                utc_now(),
            ),
        )


def _item(row: Any) -> dict[str, Any]:
    return {
        "account": row["account"],
        "shortcode": row["shortcode"],
        "label": row["label"],
        "targetAccount": row["target_account"] or None,
        "score": row["score"],
        "reviewedAt": row["reviewed_at"],
    }


@router.get("")
def list_golden_nuggets() -> dict[str, Any]:
    """Every signed-in user sees the same marks."""
    with connect() as conn:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM post_golden_nuggets WHERE label = 'golden_nugget' ORDER BY reviewed_at DESC LIMIT 5000"
        ).fetchall()
    return {"items": [_item(row) for row in rows]}


def require_dev(request: Request) -> None:
    if not getattr(request.state, "is_dev", False) or getattr(request.state, "queue_role_preview_active", False):
        raise HTTPException(status_code=403, detail="Only DEV can remove a Golden Nugget.")


@router.delete("/{account}/{shortcode}", dependencies=[Depends(require_dev)])
def remove_golden_nugget(account: str, shortcode: str) -> dict[str, Any]:
    clean_account, clean_shortcode = _key(account, shortcode)
    with connect() as conn:
        ensure_schema(conn)
        conn.execute(
            "DELETE FROM post_golden_nuggets WHERE account = ? AND shortcode = ?",
            (clean_account, clean_shortcode),
        )
    return {"removed": True, "account": clean_account, "shortcode": clean_shortcode}
