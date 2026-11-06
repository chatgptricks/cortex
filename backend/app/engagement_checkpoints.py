"""Immutable account-relative measurements retained from already-paid reads.

``observed_at`` is when Cortex received the reading, not an Instagram-provided
capture timestamp. ``evaluated_at`` and ``age_hours`` preserve the actual HOT
evaluation clock, which durable ingestion jobs may freeze across retries.
"""
from __future__ import annotations

from typing import Any


def ensure_schema(conn: Any) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS engagement_checkpoints (
        account TEXT NOT NULL,
        shortcode TEXT NOT NULL,
        checkpoint TEXT NOT NULL,
        likes INTEGER,
        comments INTEGER,
        views INTEGER,
        observed_at TEXT NOT NULL,
        evaluated_at TEXT NOT NULL,
        age_hours REAL NOT NULL,
        hot_threshold INTEGER,
        is_hot INTEGER,
        rate_multiplier REAL,
        source TEXT NOT NULL,
        PRIMARY KEY (account, shortcode, checkpoint)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_engagement_checkpoints_observed
        ON engagement_checkpoints(observed_at, account, shortcode)""")


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def record(conn: Any, *, account: str, shortcode: str, checkpoint: str,
           item: dict[str, Any], observed_at: str, evaluated_at: str,
           age_hours: float, hot_threshold: int | None = None,
           is_hot: int | None = None, rate_multiplier: float | None = None,
           source: str = "apify_ingestion") -> bool:
    """Keep the first real reading; the caller owns the enclosing transaction.

    Hidden likes never become a checkpoint sample, but a known zero does.
    Startup/batch setup owns schema creation, not this per-post write.
    """
    if checkpoint not in {"first_hot_check", "8h", "24h", "48h"}:
        raise ValueError("Unknown engagement checkpoint")
    likes = _count(item.get("likesCount"))
    if likes is None or age_hours < 0:
        return False
    views = _count(item.get("videoViewCount"))
    if views is None:
        views = _count(item.get("videoPlayCount"))
    first = checkpoint == "first_hot_check"
    changed = conn.execute("""INSERT INTO engagement_checkpoints
        (account, shortcode, checkpoint, likes, comments, views, observed_at,
         evaluated_at, age_hours, hot_threshold, is_hot, rate_multiplier, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account, shortcode, checkpoint) DO NOTHING""",
        (account.strip().removeprefix("@").lower(), shortcode, checkpoint,
         likes, _count(item.get("commentsCount")), views, observed_at,
         evaluated_at, age_hours, hot_threshold if first else None,
         is_hot if first else None, rate_multiplier if first else None, source)).rowcount
    return changed == 1


def record_later(conn: Any, *, account: str, shortcode: str, item: dict[str, Any],
                 observed_at: str, evaluated_at: str, age_hours: float,
                 source: str = "apify_ingestion") -> bool:
    """Use existing Apify readings within six-hour windows, without scrapes.

    The ingestion pipeline treats Apify's 0..3 readings as hidden or
    under-reported. Preserve that policy here; ``record`` can still retain
    an explicitly known zero supplied by another trusted measurement path.
    """
    likes = _count(item.get("likesCount"))
    if likes is None or likes <= 3:
        return False
    checkpoint = "24h" if 24 <= age_hours < 30 else "48h" if 48 <= age_hours < 54 else None
    if checkpoint is None:
        return False
    return record(conn, account=account, shortcode=shortcode, checkpoint=checkpoint,
                  item=item, observed_at=observed_at, evaluated_at=evaluated_at,
                  age_hours=age_hours, source=source)
