"""Persistent seven-day account benchmarks; post-price scenarios stay client-side."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from .account_media_kit import _date, _number

TTL = timedelta(days=7)
SCHEMA_VERSION = 1
_METRICS = ("likes", "comments", "shares", "saves", "impressions", "video_views", "video_plays")
_COUNTS = ("post_count", "undated_post_count", "engagement_post_count", "complete_engagement_post_count")
_NUMBERS = ("average_engagements", "engagement_coverage_pct", "average_complete_engagements",
            "complete_engagement_coverage_pct", "followers", "engagement_rate_pct")
_DATES = ("first_post_at", "last_post_at", "metrics_updated_at", "followers_captured_at")
FIELDS = (*_COUNTS, *_NUMBERS, *_DATES, "metrics")


def _numeric(value: Any) -> bool:
    return value is None or (type(value) in {int, float} and _number(value) is not None)


def ensure_schema(conn: Any) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS promos_engagement_cache (
        account_handle TEXT NOT NULL,
        days INTEGER NOT NULL,
        format TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        calculated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (account_handle, days, format)
    )""")


def _valid_payload(payload: Any) -> bool:
    if not isinstance(payload, dict) or not all(name in payload for name in FIELDS):
        return False
    if any(type(payload[name]) is not int or payload[name] < 0 for name in _COUNTS):
        return False
    if any(not _numeric(payload[name]) for name in _NUMBERS):
        return False
    if any(payload[name] is None or not 0 <= payload[name] <= 100 for name in ("engagement_coverage_pct", "complete_engagement_coverage_pct")):
        return False
    for count, average in (("engagement_post_count", "average_engagements"), ("complete_engagement_post_count", "average_complete_engagements")):
        if bool(payload[count]) != (payload[average] is not None):
            return False
    if any(payload[name] is not None and _date(payload[name]) is None for name in _DATES):
        return False
    population = payload["post_count"]
    if any(payload[name] > population for name in _COUNTS if name != "post_count"):
        return False
    metrics = payload["metrics"]
    if not isinstance(metrics, dict):
        return False
    for name in _METRICS:
        value = metrics.get(name)
        if not isinstance(value, dict) or not all(key in value for key in ("count", "total", "average", "coverage_pct")):
            return False
        if type(value["count"]) is not int or not 0 <= value["count"] <= population:
            return False
        if any(not _numeric(value[key]) for key in ("total", "average", "coverage_pct")):
            return False
        if value["count"] and (value["total"] is None or value["average"] is None):
            return False
        if not value["count"] and (value["total"] is not None or value["average"] is not None):
            return False
        if value["coverage_pct"] is None or not 0 <= value["coverage_pct"] <= 100:
            return False
    return True


def load(conn: Any, handles: list[str], days: int, format: str,
         current: datetime) -> dict[str, tuple[dict[str, Any], datetime]]:
    if not handles:
        return {}
    rows = conn.execute(
        f"SELECT account_handle, schema_version, calculated_at, payload_json FROM promos_engagement_cache "
        f"WHERE days = ? AND format = ? AND account_handle IN ({','.join('?' for _ in handles)})",
        [days, format, *handles],
    ).fetchall()
    result = {}
    for row in rows:
        calculated = _date(row["calculated_at"])
        if calculated is None or calculated > current:
            # Remove only the damaged version observed here. A concurrent
            # replacement must not be deleted or blocked by a future clock.
            conn.execute("DELETE FROM promos_engagement_cache WHERE account_handle = ? AND days = ? AND format = ? AND calculated_at = ? AND payload_json = ?",
                         (row["account_handle"], days, format, row["calculated_at"], row["payload_json"]))
            continue
        if row["schema_version"] != SCHEMA_VERSION or current - calculated > TTL:
            continue
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        if _valid_payload(payload):
            result[row["account_handle"]] = ({name: payload[name] for name in FIELDS}, calculated)
    return result


def save(conn: Any, handle: str, days: int, format: str,
         calculated: datetime, payload: dict[str, Any]) -> None:
    # This allowlist intentionally has no price, currency, account label or
    # access flags. Those values never become cached benchmark measurements.
    measured = {name: payload[name] for name in FIELDS}
    conn.execute("""INSERT INTO promos_engagement_cache
        (account_handle, days, format, schema_version, calculated_at, payload_json)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_handle, days, format) DO UPDATE SET
            schema_version = excluded.schema_version,
            calculated_at = excluded.calculated_at,
            payload_json = excluded.payload_json
        WHERE promos_engagement_cache.calculated_at <= excluded.calculated_at
           OR promos_engagement_cache.schema_version != excluded.schema_version""",
        (handle, days, format, SCHEMA_VERSION, calculated.isoformat(),
         json.dumps(measured, allow_nan=False, separators=(",", ":"))),
    )
