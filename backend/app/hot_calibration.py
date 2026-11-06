"""Stored, bounded HOT calibration evidence for administrator agent connections."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import math
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import db

router = APIRouter(tags=["hot-calibration"])
_KEYS = ("first_hot_check", "8h", "24h", "48h")
_FIELDS = (
    "id", "shortcode", "published_at", "updated_at", "post_type_label", "product_type",
    "is_animated", "likes", "comments", "video_views", "is_hot", "hot_checked",
    "hot_marked_at", "hot_rate_multiplier", "likes_at_1h", "likes_at_8h", "comments_at_8h",
    "likes_at_24h", "comments_at_24h", "likes_at_48h", "comments_at_48h", "hidden", "is_deleted",
)
_TEXT_FIELDS = {"shortcode", "published_at", "updated_at", "post_type_label", "product_type", "hot_marked_at"}
_CP_FIELDS = ("likes", "comments", "views", "observed_at", "evaluated_at", "age_hours", "hot_threshold", "is_hot", "rate_multiplier", "source")
_CURRENT_FIELDS = ("likes", "comments", "video_views")


def _now() -> datetime:
    return datetime.now(UTC)


def _columns(conn: Any, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _number(value: Any) -> int | float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number) if number.is_integer() else number


def _registry(conn: Any, account: str | None) -> list[dict[str, Any]]:
    columns = _columns(conn, "accounts")
    if "handle" not in columns:
        return []
    fields = [field for field in ("handle", "label", "group_name", "is_canonical", "is_active", "hot_threshold") if field in columns]
    rows = [dict(row) for row in conn.execute(f"SELECT {','.join(fields)} FROM accounts ORDER BY handle").fetchall()]
    for row in rows:
        row["handle"] = str(row["handle"]).lower()
    if account is not None:
        rows = [row for row in rows if row["handle"] == account]
        if not rows:
            raise HTTPException(404, "Unknown account.")
    return rows


def _catalogue(conn: Any, registry: list[dict[str, Any]], start: str, end: str) -> tuple[str, list[Any]]:
    """Filter before deduplication; transfer explicit scalar columns only."""
    handles = [row["handle"] for row in registry]
    canonical = next((row["handle"] for row in registry if row.get("is_canonical")), None)
    if canonical is None and "chatgptricks" in handles:
        canonical = "chatgptricks"
    parts, params = [], []
    for table in ("posts", "dashboard_posts"):
        columns = _columns(conn, table)
        if not {"shortcode", "published_at"}.issubset(columns) or not handles or table == "posts" and canonical is None:
            continue
        selected = [field if field in columns else f"CAST(NULL AS {'TEXT' if field in _TEXT_FIELDS else 'REAL'}) AS {field}" for field in _FIELDS]
        selected.extend(f"{int(field in columns)} AS {field}_available" for field in _CURRENT_FIELDS)
        where = ["published_at >= ?", "published_at <= ?", "shortcode IS NOT NULL", "shortcode <> ''"]
        if table == "posts":
            selected.extend(("? AS account", "0 AS source_priority"))
            params.extend((canonical, start, end))
            if "section" in columns:
                where.append("section <> 'ab'")
        else:
            if "account" not in columns:
                continue
            selected.extend(("LOWER(account) AS account", "1 AS source_priority"))
            where.append(f"LOWER(account) IN ({','.join('?' for _ in handles)})")
            params.extend((start, end, *handles))
        parts.append(f"SELECT {','.join(selected)} FROM {table} WHERE {' AND '.join(where)}")
    if not parts:
        selected = [f"CAST(NULL AS {'TEXT' if field in _TEXT_FIELDS else 'REAL'}) AS {field}" for field in _FIELDS]
        selected.extend(f"0 AS {field}_available" for field in _CURRENT_FIELDS)
        selected.extend(("CAST(NULL AS TEXT) AS account", "0 AS source_priority"))
        parts.append(f"SELECT {','.join(selected)} WHERE 1 = 0")
    # Current metrics can be newer in a duplicate generic row; checkpoint and
    # curation authority remain canonical. Availability distinguishes a real
    # newly unknown count from a source schema that has no such column.
    current_metrics = ", ".join(
        f"FIRST_VALUE({field}) OVER (PARTITION BY account, shortcode ORDER BY {field}_available DESC, updated_at DESC, source_priority, id DESC) AS current_{field}"
        for field in _CURRENT_FIELDS)
    return ("WITH candidates AS (" + " UNION ALL ".join(parts) + "), ranked AS (SELECT candidates.*, " + current_metrics + ", "
            "MAX(CASE WHEN likes_available = 1 OR comments_available = 1 OR video_views_available = 1 THEN updated_at END) OVER (PARTITION BY account, shortcode) AS current_updated_at, "
            "ROW_NUMBER() OVER (PARTITION BY account, shortcode ORDER BY source_priority, updated_at DESC, id DESC) AS source_rank "
            "FROM candidates), catalogue AS (SELECT * FROM ranked WHERE source_rank = 1) ", params)


def _checkpoint_schema(conn: Any) -> set[str]:
    columns = _columns(conn, "engagement_checkpoints")
    return columns if {"account", "shortcode", "checkpoint"}.issubset(columns) else set()


def _summaries(conn: Any, cte: str, params: list[Any], registry: list[dict[str, Any]], export_start: str, summary_start: str, cp_columns: set[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    joins, measures = [], []
    legacy = {"first_hot_check": "likes_at_1h", "8h": "likes_at_8h", "24h": "likes_at_24h", "48h": "likes_at_48h"}
    for index, key in enumerate(_KEYS):
        alias = f"cp{index}"
        if cp_columns:
            joins.append(f"LEFT JOIN engagement_checkpoints {alias} ON {alias}.account = c.account AND {alias}.shortcode = c.shortcode AND {alias}.checkpoint = '{key}'")
        likes = f"CASE WHEN {alias}.checkpoint IS NOT NULL THEN {alias}.likes ELSE c.{legacy[key]} END" if "likes" in cp_columns else f"c.{legacy[key]}"
        known = f"({likes}) IS NOT NULL AND ({likes}) >= 0"
        measures.append(f"SUM(CASE WHEN c.published_at >= ? AND {known} THEN 1 ELSE 0 END) AS cp_{index}_known")
        exact = f"{alias}.observed_at IS NOT NULL AND {known}" if "observed_at" in cp_columns else "1 = 0"
        measures.append(f"SUM(CASE WHEN c.published_at >= ? AND {exact} THEN 1 ELSE 0 END) AS cp_{index}_timed")
    query = (cte + "SELECT c.account, COUNT(CASE WHEN c.published_at >= ? THEN 1 END) AS post_count, "
             "COUNT(CASE WHEN c.published_at >= ? THEN 1 END) AS summary_post_count, "
             "AVG(CASE WHEN c.published_at >= ? AND c.hot_checked = 1 AND c.likes_at_1h >= 0 THEN c.likes_at_1h END) AS avg_likes, "
             "COUNT(CASE WHEN c.published_at >= ? AND c.hot_checked = 1 AND c.likes_at_1h >= 0 THEN c.likes_at_1h END) AS sample_size, "
             + ", ".join(measures) + " FROM catalogue c " + " ".join(joins) + " GROUP BY c.account")
    values = [export_start, summary_start, summary_start, summary_start, *([export_start] * (len(_KEYS) * 2))]
    stats = {row["account"]: dict(row) for row in conn.execute(query, [*params, *values]).fetchall()}
    coverage = {"scope": "all_posts_matching_account_and_publication_window", "post_count": 0,
                "checkpoints": {key: {"likes_known": 0, "with_observed_at": 0} for key in _KEYS}}
    accounts = []
    for account in registry:
        stat = stats.get(account["handle"], {})
        average = stat.get("avg_likes")
        sample_size = int(stat.get("sample_size") or 0)
        # Keep the Dashboard's existing suggestion calculation, including its
        # 600 fallback, so this endpoint and Settings show the same value.
        suggestion = int((average + 99) // 100 * 100) if average else 600
        accounts.append({"handle": account["handle"], "label": account.get("label"),
                         "is_active": bool(account.get("is_active", True)), "hot_threshold": _number(account.get("hot_threshold")),
                         "summary_window_days": 30, "posts_in_summary_window": int(stat.get("summary_post_count") or 0),
                         "avg_likes": round(average) if average is not None else None,
                         "avg_likes_sample_size": sample_size, "suggested_hot_threshold": suggestion,
                         "suggestion_source": "stored_first_check_average" if average else "default_600",
                         "has_calibration_sample": sample_size > 0})
        coverage["post_count"] += int(stat.get("post_count") or 0)
        for index, key in enumerate(_KEYS):
            coverage["checkpoints"][key]["likes_known"] += int(stat.get(f"cp_{index}_known") or 0)
            coverage["checkpoints"][key]["with_observed_at"] += int(stat.get(f"cp_{index}_timed") or 0)
    return accounts, coverage


def _legacy_checkpoint(row: dict[str, Any], key: str) -> dict[str, Any] | None:
    suffix = {"first_hot_check": "1h", "8h": "8h", "24h": "24h", "48h": "48h"}[key]
    likes, comments = _number(row.get(f"likes_at_{suffix}")), _number(row.get(f"comments_at_{suffix}"))
    if likes is None and comments is None:
        return None
    value = dict.fromkeys(_CP_FIELDS)
    value.update(likes=likes, comments=comments, source="legacy_post_columns")
    if key == "first_hot_check":
        value["is_hot"] = bool(row.get("is_hot")) if row.get("hot_checked") else None
        value["rate_multiplier"] = _number(row.get("hot_rate_multiplier"))
    return value


def _read_checkpoints(conn: Any, rows: list[dict[str, Any]], columns: set[str]) -> dict[tuple[str, str, str], dict[str, Any]]:
    if not columns or not rows:
        return {}
    # Account/post pairs are selected before reading checkpoints, bounded by
    # the 100-row result page. No provider payload is read.
    where = " OR ".join("(account = ? AND shortcode = ?)" for _ in rows)
    values = [value for row in rows for value in (row["account"], row["shortcode"])]
    fields = [field for field in ("account", "shortcode", "checkpoint", *_CP_FIELDS) if field in columns]
    checkpoints = {}
    for record in conn.execute(f"SELECT {','.join(fields)} FROM engagement_checkpoints WHERE {where}", values).fetchall():
        item = dict(record)
        if item["checkpoint"] not in _KEYS:
            continue
        value = {field: item.get(field) for field in _CP_FIELDS}
        for field in ("likes", "comments", "views", "age_hours", "hot_threshold", "rate_multiplier"):
            value[field] = _number(value[field])
        value["is_hot"] = bool(value["is_hot"]) if value["is_hot"] is not None else None
        checkpoints[(item["account"], item["shortcode"], item["checkpoint"])] = value
    return checkpoints


def _posts(conn: Any, rows: list[dict[str, Any]], cp_columns: set[str], registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checkpoints = _read_checkpoints(conn, rows, cp_columns)
    observations = {}
    if rows and {"shortcode", "observed_at"}.issubset(_columns(conn, "engagement_observations")):
        codes = sorted({row["shortcode"] for row in rows})
        observations = {row["shortcode"]: row["observed_at"] for row in conn.execute(
            f"SELECT shortcode, observed_at FROM engagement_observations WHERE shortcode IN ({','.join('?' for _ in codes)})", codes).fetchall()}
    accounts = {account["handle"]: account for account in registry}
    posts = []
    for row in rows:
        kind = str(row.get("post_type_label") or "Image").split(" (")[0]
        product = str(row.get("product_type") or "").lower()
        kind = "Reel" if product in {"clips", "reels", "reel"} or kind.lower().startswith("reel") else "Carousel" if kind.lower() in {"carousel", "sidecar"} else "Video" if kind.lower().startswith("video") or row.get("is_animated") else "Image"
        checkpoint_values = {key: checkpoints.get((row["account"], row["shortcode"], key), _legacy_checkpoint(row, key)) for key in _KEYS}
        posts.append({"account": row["account"], "shortcode": row["shortcode"],
                      "permalink": f"https://www.instagram.com/p/{row['shortcode']}/", "format": kind,
                      "published_at": row["published_at"], "hidden": bool(row.get("hidden")), "is_deleted": bool(row.get("is_deleted")),
                      "is_active_account": bool(accounts[row["account"]].get("is_active", True)),
                      "hot_checked": bool(row.get("hot_checked")), "is_hot": bool(row.get("is_hot")),
                      "hot_marked_at": row.get("hot_marked_at"), "hot_rate_multiplier": _number(row.get("hot_rate_multiplier")),
                      "current_hot_threshold": _number(accounts[row["account"]].get("hot_threshold")),
                      "likes_at_1h": _number(row.get("likes_at_1h")), "likes_at_8h": _number(row.get("likes_at_8h")),
                      "comments_at_8h": _number(row.get("comments_at_8h")), "checkpoints": checkpoint_values,
                      "current_metrics": {"likes": _number(row.get("current_likes")), "comments": _number(row.get("current_comments")),
                                          "views": _number(row.get("current_video_views")), "updated_at": row.get("current_updated_at"),
                                          "last_observation_at": observations.get(row["shortcode"])}})
    return posts


@router.get("/api/admin/engagement/checkpoints")
def engagement_checkpoints(
    request: Request,
    account: str | None = Query(None, description="One account handle; inactive accounts are included."),
    days: int = Query(30, ge=1, le=90, description="Publication lookback in days; summaries always use the last 30 days."),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    as_of: datetime | None = Query(None, description="Reuse the returned UTC bound across export pages; cannot be in the future. This pins the publication window, not a historical database snapshot."),
) -> JSONResponse:
    """Read individual first-HOT-check and later checkpoint evidence; no refresh or writes.

    Administrator/Dev owner roles are required; read-only agent connections suffice.
    Legacy capture times and historical thresholds remain unknown. observed_at is
    system receipt time, evaluated_at is the ingestion job's evaluation clock.
    """
    if not (getattr(request.state, "is_admin", False) or getattr(request.state, "is_dev", False)):
        raise HTTPException(403, "Admin or Dev access required.")
    if account is not None:
        account = account.strip().removeprefix("@").lower()
        if not re.fullmatch(r"[a-z0-9_.]{1,30}", account):
            raise HTTPException(422, "Provide one valid account handle.")
    current = _now()
    now = (as_of.replace(tzinfo=UTC) if as_of.tzinfo is None else as_of.astimezone(UTC)) if as_of is not None else current
    if now > current:
        raise HTTPException(422, "as_of cannot be in the future.")
    end = now.isoformat(timespec="seconds")
    start = (now - timedelta(days=days)).isoformat(timespec="seconds")
    summary_start = (now - timedelta(days=30)).isoformat(timespec="seconds")
    with db.connect() as conn:
        registry = _registry(conn, account)
        cte, params = _catalogue(conn, registry, min(start, summary_start), end)
        cp_columns = _checkpoint_schema(conn)
        accounts, coverage = _summaries(conn, cte, params, registry, start, summary_start, cp_columns)
        rows = [dict(row) for row in conn.execute(
            cte + "SELECT " + ",".join((*_FIELDS, *(f"current_{field}" for field in _CURRENT_FIELDS), "current_updated_at", "account")) + " FROM catalogue WHERE published_at >= ? "
            "ORDER BY published_at DESC, account, shortcode LIMIT ? OFFSET ?", [*params, start, limit, offset]).fetchall()]
        posts = _posts(conn, rows, cp_columns, registry)
    total = coverage["post_count"]
    page_coverage = {"scope": "returned_page_only", "post_count": len(posts), "checkpoints": {}}
    for key in _KEYS:
        values = [post["checkpoints"][key] for post in posts if post["checkpoints"][key] is not None]
        page_coverage["checkpoints"][key] = {"likes_known": sum(value["likes"] is not None for value in values),
                                               "with_observed_at": sum(value["likes"] is not None and value["observed_at"] is not None for value in values)}
    return JSONResponse({"generated_at": current.isoformat(timespec="seconds"), "as_of": end, "window": {"days": days, "from": start, "to": end, "applies_to": "published_at"},
                         "summary_window": {"days": 30, "from": summary_start, "to": end}, "accounts": accounts, "posts": posts,
                         "pagination": {"limit": limit, "offset": offset, "total": total, "has_more": offset + len(posts) < total,
                                        "next_offset": offset + len(posts) if offset + len(posts) < total else None},
                         "coverage": {"full_filter": coverage, "page": page_coverage},
                         "measurement_notes": {"first_hot_check": "The first HOT evaluation, from 30 minutes onward; likes_at_1h is the observed count, not a normalized hourly rate.",
                                               "legacy": "Missing capture times, historical thresholds and checkpoints stay null; updated_at is never a checkpoint capture time.",
                                               "observed_at": "System receipt time, not an exact provider collection timestamp.",
                                               "evaluated_at": "Evaluation clock used to calculate age_hours; an ingestion job may retain its starting clock.",
                                               "current_metrics": "Newest available stored scalar source per metric; an explicit newer null stays unknown. last_observation_at is independent observation metadata and does not certify which reading supplied each value.",
                                               "later_checkpoints": "Stored observations only; 8h/24h/48h names describe target windows, not exact observation ages.",
                                               "suggestions": "Same stored-first-check average and 600 fallback as Settings; a default is not evidence of calibration."}},
                        headers={"Cache-Control": "private, no-store", "Vary": "Authorization", "X-Content-Type-Options": "nosniff"})
