"""Bounded, stored-only Research, HOT, Tracker and Insights exports.

No endpoint initializes a schema, refreshes a profile, classifies a topic, or
calls a provider. Account restrictions are part of every source query, before
ordering and pagination. Large provider payloads are only read for one detail.
"""
from __future__ import annotations

import json
import mimetypes
import re
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from . import account_media_kit as kit, db, external_api as api
from .promo_classification import is_public_promo
from .research_collaboration import attach_collaboration

router = APIRouter(prefix="/api/v1", tags=["read-api-research"], dependencies=[Depends(api._no_store)])
_LIMIT = Annotated[int, Query(ge=1, le=100)]
_OFFSET = Annotated[int, Query(ge=0)]
_START = Annotated[date | None, Query(alias="from", description="Inclusive America/Costa_Rica calendar date")]
_END = Annotated[date | None, Query(alias="to", description="Inclusive America/Costa_Rica calendar date")]
_CODE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_STACK = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
_HOT_HOURS = 30
_SCAN = 500
_SORT = Literal["date", "likes", "comments", "views", "hot"]

_POST_FIELDS = (
    "id", "shortcode", "published_at", "updated_at", "caption", "title",
    "post_type_label", "product_type", "is_animated", "likes", "comments",
    "video_views", "video_plays", "video_duration", "slide_count", "is_hot",
    "hot_rate_multiplier", "hot_marked_at", "likes_at_1h", "likes_at_8h",
    "comments_at_8h", "likes_at_24h", "comments_at_24h", "likes_at_48h",
    "comments_at_48h", "is_promo", "hidden", "is_deleted", "hashtags",
    "mentions", "music_song", "music_artist", "uses_original_audio",
    "paid_partnership", "dimensions", "hook_text", "transcript", "alt_text",
)
_CURRENT_METRICS = ("likes", "comments", "video_views", "video_plays", "video_duration", "slide_count")
_CURRENT_METADATA = ("caption", "published_at", "post_type_label", "product_type", "hook_text", "hashtags", "mentions", "music_song", "music_artist", "dimensions")
_NULLABLE_METADATA = ("uses_original_audio", "paid_partnership")
_AVAILABILITY = ("transcript_available", "alt_text_available")


def _scope(request: Request, *scopes: str) -> dict[str, Any]:
    return api.require_scope(request, *scopes)


def _handles(identity: dict[str, Any], account: str | None) -> list[str]:
    allowed = set(identity.get("account_handles", []))
    if account is None:
        return sorted(allowed)
    selected = sorted({item.strip().removeprefix("@").lower() for item in account.split(",") if item.strip()})
    if not selected or any(not re.fullmatch(r"[a-z0-9_.]{1,30}", item) for item in selected):
        raise HTTPException(422, "Provide valid comma-separated account handles.")
    if any(item not in allowed for item in selected):
        raise HTTPException(404, "Account not available for this API key.")
    return selected


def _envelope(data: Any, *, pagination: dict[str, Any] | None = None, **metadata: Any) -> dict[str, Any]:
    result = {"schema_version": api.VERSION, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
              "timezone": "America/Costa_Rica", "data": data, **metadata}
    if pagination is not None:
        result["pagination"] = pagination
    return result


def _page(total: int | None, limit: int, offset: int, *, consumed: int | None = None, has_more: bool | None = None) -> dict[str, Any]:
    more = offset + limit < total if has_more is None and total is not None else bool(has_more)
    return {"limit": limit, "offset": offset, "total": total, "has_more": more,
            "next_offset": offset + (consumed if consumed is not None else limit) if more else None}


def _registry(conn: Any, handles: list[str]) -> dict[str, dict[str, Any]]:
    columns = kit._columns(conn, "accounts")
    if not handles:
        return {}
    fields = [field for field in ("handle", "is_canonical", "hot_threshold", "group_name", "label", "research_enabled") if field in columns]
    marks = ",".join("?" for _ in handles)
    return {str(row["handle"]).lower(): dict(row) for row in conn.execute(
        f"SELECT {','.join(fields)} FROM accounts WHERE LOWER(handle) IN ({marks}) AND is_active = 1", handles).fetchall()}


def _sources(conn: Any, handles: list[str], *, public_only: bool = False) -> tuple[str, list[Any], dict[str, dict[str, Any]]]:
    """A compact union with one account-relative row per persisted post.

    Canonical curation takes precedence over a duplicate dashboard copy. This
    mirrors the canonical Research source and avoids resurrecting a removed
    HOT/Promo mark from a stale enriched copy.
    """
    registry = _registry(conn, handles)
    handles = sorted(registry)
    if public_only and {"handle", "private", "captured_at"} <= kit._columns(conn, "account_snapshots"):
        handles = [handle for handle in handles if not (row := conn.execute(
            "SELECT private FROM account_snapshots WHERE LOWER(handle) = ? AND captured_at <= ? ORDER BY captured_at DESC LIMIT 1",
            (handle, datetime.now(UTC).isoformat(timespec="seconds"))).fetchone()) or not kit._bool(row["private"])]
    params: list[Any] = []
    parts = []
    canonical = next((handle for handle in handles if registry[handle].get("is_canonical")), None)
    for table in ("posts", "dashboard_posts"):
        columns = kit._columns(conn, table)
        if "shortcode" not in columns or not handles or table == "posts" and not canonical:
            continue
        fields = []
        for field in _POST_FIELDS:
            if field == "transcript":
                expr = "CASE WHEN transcript IS NOT NULL AND TRIM(transcript) <> '' THEN 1 ELSE 0 END" if field in columns else "0"
                fields.append(f"{expr} AS transcript_available")
            elif field == "alt_text":
                expr = "CASE WHEN alt_text IS NOT NULL AND TRIM(alt_text) <> '' THEN 1 ELSE 0 END" if field in columns else "0"
                fields.append(f"{expr} AS alt_text_available")
            elif field in columns:
                fields.append(field)
            else:
                fields.append(f"NULL AS {field}")
        fields.extend(f"{int(field in columns)} AS {field}_available" for field in _CURRENT_METRICS)
        if table == "posts":
            fields.extend(("? AS account", "0 AS source_priority"))
            params.append(canonical)
            where = "shortcode IS NOT NULL AND shortcode <> ''"
            if "section" in columns:
                where += " AND section <> 'ab'"
        else:
            fields.extend(("LOWER(account) AS account", "1 AS source_priority"))
            where = f"LOWER(account) IN ({','.join('?' for _ in handles)}) AND shortcode IS NOT NULL AND shortcode <> ''"
            params.extend(handles)
        parts.append(f"SELECT {','.join(fields)} FROM {table} WHERE {where}")
    if not parts:
        # A schema-independent empty relation, with the columns callers use.
        numeric = set(_CURRENT_METRICS) | {"id", "is_animated", "is_hot", "hot_rate_multiplier", "likes_at_1h", "likes_at_8h", "comments_at_8h", "likes_at_24h", "comments_at_24h", "likes_at_48h", "comments_at_48h", "is_promo", "hidden", "is_deleted", "uses_original_audio", "paid_partnership"}
        fields = [f"CAST(NULL AS {'REAL' if field in numeric else 'TEXT'}) AS {field}" for field in _POST_FIELDS if field not in {"transcript", "alt_text"}]
        fields.extend(("NULL AS account", "0 AS source_priority", "0 AS transcript_available", "0 AS alt_text_available"))
        fields.extend(f"0 AS {field}_available" for field in _CURRENT_METRICS)
        parts.append(f"SELECT {','.join(fields)} WHERE 1 = 0")
    union = " UNION ALL ".join(parts)
    fresh = ", ".join(f"FIRST_VALUE({field}) OVER (PARTITION BY account, shortcode ORDER BY {field}_available DESC, updated_at DESC, source_priority, id DESC) AS fresh_{field}" for field in _CURRENT_METRICS)
    fresh += ", " + ", ".join(f"FIRST_VALUE({field}) OVER (PARTITION BY account, shortcode ORDER BY CASE WHEN {field} IS NULL OR {field} = '' THEN 1 ELSE 0 END, updated_at DESC, source_priority, id DESC) AS fresh_{field}" for field in _CURRENT_METADATA)
    fresh += ", " + ", ".join(f"FIRST_VALUE({field}) OVER (PARTITION BY account, shortcode ORDER BY CASE WHEN {field} IS NULL THEN 1 ELSE 0 END, updated_at DESC, source_priority, id DESC) AS fresh_{field}" for field in _NULLABLE_METADATA)
    fresh += ", " + ", ".join(f"MAX({field}) OVER (PARTITION BY account, shortcode) AS fresh_{field}" for field in _AVAILABILITY)
    names = ["transcript_available" if field == "transcript" else "alt_text_available" if field == "alt_text" else field for field in _POST_FIELDS]
    projection = ", ".join(f"fresh_{field} AS {field}" if field in _CURRENT_METRICS + _CURRENT_METADATA + _NULLABLE_METADATA + _AVAILABILITY else field for field in names)
    return (f"WITH candidates AS ({union}), ranked AS (SELECT candidates.*, {fresh}, ROW_NUMBER() OVER "
            "(PARTITION BY account, shortcode ORDER BY source_priority, updated_at DESC, id DESC) AS source_rank, "
            "MAX(COALESCE(hidden, 0)) OVER (PARTITION BY account, shortcode) AS any_hidden, "
            "MAX(COALESCE(is_deleted, 0)) OVER (PARTITION BY account, shortcode) AS any_deleted, "
            "MAX(updated_at) OVER (PARTITION BY account, shortcode) AS latest_metrics_updated_at FROM candidates), "
            f"catalogue AS (SELECT {projection}, account, source_priority, any_hidden, any_deleted, latest_metrics_updated_at FROM ranked WHERE source_rank = 1) ", params, registry)


def _filters(*, start: date | None, end: date | None, q: str | None = None, is_hot: bool | None = None,
             include_hidden: bool = False, include_deleted: bool = False, history: bool = True,
             shortlist: list[tuple[str, str]] | None = None, public_only: bool = False) -> tuple[str, list[Any]]:
    api._range(start, end)
    where = ["published_at IS NOT NULL", "published_at <> ''", "published_at <= ?"]
    params: list[Any] = [datetime.now(UTC).isoformat(timespec="seconds")]
    if start:
        where.append("published_at >= ?")
        params.append(datetime.combine(start, datetime.min.time(), tzinfo=api.LOCAL_ZONE).astimezone(UTC).isoformat(timespec="seconds"))
    if end:
        where.append("published_at < ?")
        params.append(datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=api.LOCAL_ZONE).astimezone(UTC).isoformat(timespec="seconds"))
    if not include_hidden:
        where.append("any_hidden = 0" if public_only else "COALESCE(hidden, 0) = 0")
    if not include_deleted:
        where.append("any_deleted = 0" if public_only else "COALESCE(is_deleted, 0) = 0")
    if is_hot is not None:
        where.append("COALESCE(is_hot, 0) = ?")
        params.append(int(is_hot))
    if not history:
        where.append("published_at >= ?")
        params.append((datetime.now(UTC) - timedelta(hours=_HOT_HOURS)).isoformat(timespec="seconds"))
    if q:
        term = q.strip().lower()
        if len(term) > 200:
            raise HTTPException(422, "Search text cannot exceed 200 characters.")
        term = term.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        where.append("(LOWER(COALESCE(caption, '')) LIKE ? ESCAPE '!' OR LOWER(shortcode) LIKE ? ESCAPE '!' OR LOWER(COALESCE(hook_text, '')) LIKE ? ESCAPE '!')")
        params.extend([f"%{term}%"] * 3)
    if shortlist is not None:
        if not shortlist:
            where.append("1 = 0")
        else:
            where.append("(" + " OR ".join("(account = ? AND shortcode = ?)" for _ in shortlist) + ")")
            params.extend(value for pair in shortlist for value in pair)
    return " AND ".join(where), params


def _array(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value) if value else None
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def _terms(value: Any) -> list[Any] | None:
    values = _array(value)
    if values is not None:
        return values
    # Ingestion promotes hashtags and mentions as comma-separated text;
    # imported historical rows may instead contain a serialized JSON array.
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in value.split(",") if item.strip()]
    return None


def _project(row: dict[str, Any], registry: dict[str, dict[str, Any]], *, public_only: bool = False) -> dict[str, Any]:
    code = str(row["shortcode"])
    handle = str(row["account"])
    kind = str(row.get("post_type_label") or "Image").split(" (")[0]
    product = str(row.get("product_type") or "").lower()
    kind = "Reel" if product in {"clips", "reels", "reel"} else "Carousel" if kind.lower() in {"carousel", "sidecar"} else "Video" if kind.lower().startswith("video") or row.get("is_animated") else "Image"
    promo = is_public_promo({"is_promo": row.get("is_promo"), "public_caption": row.get("caption")})
    multiplier = kit._number(row.get("hot_rate_multiplier"))
    value = {"account": handle, "shortcode": code, "permalink": f"https://www.instagram.com/p/{code}/",
             "caption": row.get("caption") or None, "published_at": row.get("published_at"), "format": kind,
             "likes": kit._number(row.get("likes")), "comments": kit._number(row.get("comments")),
             "views": kit._number(row.get("video_views")), "video_views": kit._number(row.get("video_views")),
             "video_plays": kit._number(row.get("video_plays")), "metrics_updated_at": row.get("latest_metrics_updated_at", row.get("updated_at")),
             "is_promo": promo, "is_hot": bool(row.get("is_hot")),
             "is_collab": row.get("isCollab"), "collaborators": row.get("collaborators", []),
             "hot": {"is_hot": bool(row.get("is_hot")), "score": multiplier, "multiplier": multiplier,
                     "criterion": "persisted_first_hour_pace", "observed_at": row.get("hot_marked_at"),
                     "account_threshold": kit._number(registry.get(handle, {}).get("hot_threshold")),
                     "likes_at_first_check": kit._number(row.get("likes_at_1h")),
                     "score_unit": "multiple_of_account_hourly_like_threshold"}}
    if not public_only:
        value.update({"title": row.get("title"), "ocr_text": row.get("hook_text"),
                      "hidden": bool(row.get("hidden")), "is_deleted": bool(row.get("is_deleted")),
                      "transcript_available": bool(row.get("transcript_available")), "alt_text_available": bool(row.get("alt_text_available")),
                      "video_duration": kit._number(row.get("video_duration")), "slide_count": kit._number(row.get("slide_count")),
                      "hashtags": _terms(row.get("hashtags")), "mentions": _terms(row.get("mentions")),
                      "music_song": row.get("music_song"), "music_artist": row.get("music_artist"),
                      "uses_original_audio": kit._bool(row.get("uses_original_audio")),
                      "paid_partnership": kit._bool(row.get("paid_partnership")), "dimensions": row.get("dimensions"),
                      "checkpoints": {name: kit._number(row.get(name)) for name in (
                          "likes_at_1h", "likes_at_8h", "comments_at_8h", "likes_at_24h", "comments_at_24h", "likes_at_48h", "comments_at_48h")}})
    return value


def _metric_rows(conn: Any, table: str, where: str, params: list[Any], limit: int) -> list[dict[str, Any]]:
    """Transfer scalar performance facts, never complete provider payloads.

    Explicit JSON nulls remain present: dropping them could resurrect an old
    visible like count after Instagram hides it. PostgreSQL's legacy malformed
    payload fallback is both savepoint guarded and hard bounded.
    """
    columns = kit._columns(conn, table)
    if "shortcode" not in columns:
        return []
    selected = sorted(columns & (set(kit._METRICS) | {
        "id", "account", "shortcode", "published_at", "updated_at", "enriched_at",
        "observed_at", "section", "caption", "post_type_label", "product_type", "is_animated", "is_promo"}))
    keys = sorted(set(kit._ALIASES) | set(kit._METRICS) | {
        "shortCode", "shortcode", "type", "productType", "metrics", "insights",
        "statistics", "stats", "video", "video_info", "engagement"})
    literals = ",".join("'" + key.replace("'", "''") + "'" for key in keys)
    postgres = bool(getattr(conn, "is_postgres", False) and "raw_json" in columns)
    join = ""
    raw = "NULL"
    if "raw_json" in columns:
        if postgres:
            join = " CROSS JOIN LATERAL (SELECT NULLIF(TRIM(raw_json), '')::jsonb AS metric_payload OFFSET 0) compact_metrics"
            raw = f"(SELECT jsonb_object_agg(key, value)::text FROM jsonb_each(CASE WHEN jsonb_typeof(metric_payload) = 'object' THEN metric_payload ELSE '{{}}'::jsonb END) WHERE key IN ({literals}))"
        else:
            raw = f"CASE WHEN json_valid(raw_json) THEN (SELECT json_group_object(key, CASE WHEN type = 'object' THEN json(value) ELSE value END) FROM json_each(raw_json) WHERE key IN ({literals})) ELSE NULL END"
    selected_sql = ",".join(selected)
    order = "observed_at DESC" if "observed_at" in columns else "updated_at DESC" if "updated_at" in columns else "shortcode"
    query = f"SELECT {selected_sql}, {raw} AS raw_json FROM {table}{join} WHERE {where} ORDER BY {order} LIMIT ?"
    if postgres:
        conn.execute("SAVEPOINT external_metric_json")
    try:
        rows = conn.execute(query, [*params, limit]).fetchall()
    except Exception as error:
        if not postgres:
            raise
        conn.execute("ROLLBACK TO SAVEPOINT external_metric_json")
        if getattr(error, "sqlstate", None) != "22P02":
            raise
        rows = conn.execute(f"SELECT {selected_sql}, raw_json FROM {table} WHERE {where} ORDER BY {order} LIMIT ?", [*params, limit]).fetchall()
    finally:
        if postgres:
            conn.execute("RELEASE SAVEPOINT external_metric_json")
    return [dict(row) | {"_table": table} for row in rows]


def _fresh_page_metrics(conn: Any, values: list[dict[str, Any]], registry: dict[str, dict[str, Any]], *, public_only: bool = False) -> None:
    """Use the existing public metric precedence for at most 100 results."""
    if not values:
        return
    codes = sorted({post["shortcode"] for post in values})
    handles = sorted({post["account"] for post in values})
    marks = ",".join("?" for _ in codes)
    observations = {row["shortcode"]: row for row in _metric_rows(conn, "engagement_observations", f"shortcode IN ({marks})", codes, len(codes))}
    grouped: dict[str, list[dict[str, Any]]] = {handle: [] for handle in handles}
    for handle in handles:
        own_codes = [post["shortcode"] for post in values if post["account"] == handle]
        own_marks = ",".join("?" for _ in own_codes)
        if registry.get(handle, {}).get("is_canonical"):
            columns = kit._columns(conn, "posts")
            if "shortcode" in columns:
                where = f"shortcode IN ({own_marks})" + (" AND section <> 'ab'" if "section" in columns else "")
                grouped[handle].extend(_metric_rows(conn, "posts", where, own_codes, 400))
    if {"account", "shortcode"} <= kit._columns(conn, "dashboard_posts"):
        account_marks = ",".join("?" for _ in handles)
        rows = _metric_rows(conn, "dashboard_posts", f"LOWER(account) IN ({account_marks}) AND shortcode IN ({marks})", [*handles, *codes], 400)
        for row in rows:
            grouped[str(row["account"]).lower()].append(row)
    normalized = {}
    for handle, rows in grouped.items():
        posts, _ = kit._normalize_posts(rows, observations, handle)
        normalized.update({(handle, post["shortcode"]): post for post in posts})
    for value in values:
        post = normalized.get((value["account"], value["shortcode"]))
        if not post:
            continue
        for key in ("likes", "comments", "video_views", "video_plays", "video_duration", "slide_count"):
            if key in value:
                value[key] = post["metrics"].get(key)
        value["views"] = value.get("video_views")
        value["metrics_updated_at"] = post["metrics_updated_at"]
        if not public_only:
            value["metrics"] = post["metrics"]
        value["format"] = post["format"]
        caption = post["public_caption"]
        value["caption"] = caption if isinstance(caption, str) and caption else None
        if published := kit._date(post["published_at"]):
            value["published_at"] = published.isoformat(timespec="seconds")
        value["is_promo"] = is_public_promo(post)


def _posts_page(handles: list[str], *, limit: int, offset: int, start: date | None = None, end: date | None = None,
                q: str | None = None, is_promo: bool | None = None, is_collab: bool | None = None,
                is_hot: bool | None = None, include_hidden: bool = False, include_deleted: bool = False,
                public_only: bool = False, history: bool = True,
                shortlist: list[tuple[str, str]] | None = None, sort: _SORT = "date",
                stack_id: str | None = None) -> dict[str, Any]:
    post_filter = is_promo is not None or is_collab is not None
    take = _SCAN if post_filter else limit
    with db.connect() as conn:
        source, params, registry = _sources(conn, handles, public_only=public_only)
        where, filter_params = _filters(start=start, end=end, q=q, is_hot=is_hot,
                                       include_hidden=include_hidden, include_deleted=include_deleted,
                                       history=history, shortlist=shortlist, public_only=public_only)
        if stack_id is not None:
            where += " AND EXISTS (SELECT 1 FROM topic_stack_members membership WHERE membership.stack_id = ? AND membership.post_key = (catalogue.account || ':' || catalogue.shortcode))"
            filter_params.append(stack_id)
        all_params = [*params, *filter_params]
        total = conn.execute(source + f"SELECT COUNT(*) AS n FROM catalogue WHERE {where}", all_params).fetchone()["n"]
        order = {"date": "published_at", "likes": "likes", "comments": "comments", "views": "video_views", "hot": "hot_rate_multiplier"}[sort]
        rows = [dict(row) for row in conn.execute(source + f"SELECT * FROM catalogue WHERE {where} ORDER BY CASE WHEN {order} IS NULL THEN 1 ELSE 0 END, {order} DESC, published_at DESC, account, shortcode LIMIT ? OFFSET ?",
                                                 [*all_params, take, offset]).fetchall()]
        canonical = next((handle for handle, value in registry.items() if value.get("is_canonical")), "")
        attach_collaboration(conn, rows, canonical_handle=canonical)
    data = []
    consumed = 0
    for row in rows:
        consumed += 1
        value = _project(row, registry, public_only=public_only)
        if not _CODE.fullmatch(value["shortcode"]):
            continue
        if is_promo is not None and value["is_promo"] != is_promo:
            continue
        if is_collab is not None and value["is_collab"] is not is_collab:
            continue
        data.append(value)
        if len(data) >= limit:
            break
    with db.connect() as conn:
        _fresh_page_metrics(conn, data, registry, public_only=public_only)
    more = offset + consumed < total
    pagination = _page(None if post_filter else total, limit, offset, consumed=consumed, has_more=more)
    if post_filter:
        pagination.update({"offset_type": "candidate", "scanned_count": consumed,
                           "candidate_total": total, "scan_limit": _SCAN})
    return _envelope(data, pagination=pagination)


@router.get("/hot")
def hot(request: Request, account: str | None = None, history: bool = False, limit: _LIMIT = 20, offset: _OFFSET = 0,
        start: _START = None, end: _END = None) -> dict[str, Any]:
    identity = _scope(request, "accounts", "research")
    result = _posts_page(_handles(identity, account), limit=limit, offset=offset, start=start, end=end,
                         is_hot=True, public_only=True, history=history)
    result["criteria"] = {"source": "Research persisted is_hot flag", "history": history,
                          "recent_window_hours": _HOT_HOURS, "window_applies_to": "published_at",
                          "score": "stored hot_rate_multiplier; not recalculated", "unknown_values": "null",
                          "classification": "At the stored first check, likes / actual elapsed hours reached the account's hourly threshold; the earned flag persists.",
                          "account_threshold_is_current": True,
                          "historical_threshold_note": "The current account threshold can change; the stored multiplier is authoritative for the original check."}
    return result


@router.get("/posts")
@router.get("/insights/posts")
def posts(request: Request, account: str | None = None, q: str | None = None,
          is_promo: bool | None = None, is_collab: bool | None = None, is_hot: bool | None = None,
          include_hidden: bool = False, include_deleted: bool = False,
          sort: _SORT = "date", limit: _LIMIT = 20, offset: _OFFSET = 0, start: _START = None, end: _END = None) -> dict[str, Any]:
    identity = _scope(request, "insights" if request.url.path.endswith("/insights/posts") else "research")
    return _posts_page(_handles(identity, account), limit=limit, offset=offset, start=start, end=end, q=q,
                       is_promo=is_promo, is_collab=is_collab, is_hot=is_hot,
                       include_hidden=include_hidden, include_deleted=include_deleted, sort=sort)


@router.get("/insights/top-posts")
def top_posts(request: Request, account: str | None = None, sort: _SORT = "likes", limit: _LIMIT = 20,
              offset: _OFFSET = 0, start: _START = None, end: _END = None) -> dict[str, Any]:
    return _posts_page(_handles(_scope(request, "insights"), account), limit=limit, offset=offset,
                       start=start, end=end, sort=sort)


@router.get("/research/posts/{account}/{shortcode}")
def detail(request: Request, account: str, shortcode: str) -> dict[str, Any]:
    identity = _scope(request, "research")
    handles = _handles(identity, account)
    if len(handles) != 1:
        raise HTTPException(422, "Provide one account handle.")
    if not _CODE.fullmatch(shortcode):
        raise HTTPException(404, "Post not available for this API key.")
    page = _posts_page(handles, limit=1, offset=0, shortlist=[(handles[0], shortcode)], include_hidden=True, include_deleted=True)
    if not page["data"]:
        raise HTTPException(404, "Post not available for this API key.")
    value = page["data"][0]
    with db.connect() as conn:
        registry = _registry(conn, handles)
        for table in (["posts", "dashboard_posts"] if registry[handles[0]].get("is_canonical") else ["dashboard_posts"]):
            columns = kit._columns(conn, table)
            wanted = [field for field in ("transcript", "alt_text", "analysis_summary", "llm_report", "tags", "coauthors", "tagged_users") if field in columns]
            if not wanted:
                continue
            where = "shortcode = ?" + (" AND LOWER(account) = ?" if table == "dashboard_posts" else " AND section <> 'ab'" if "section" in columns else "")
            params = [shortcode, handles[0]] if table == "dashboard_posts" else [shortcode]
            row = conn.execute(f"SELECT {','.join(wanted)} FROM {table} WHERE {where} ORDER BY updated_at DESC LIMIT 1", params).fetchone()
            if row:
                for key, entry in dict(row).items():
                    if key not in value or value[key] is None:
                        value[key] = kit._json(entry) if key == "analysis_summary" else _array(entry) if key in {"tags", "coauthors", "tagged_users"} else entry
    return _envelope(value)


def _stack_where(handles: list[str]) -> tuple[str, list[str]]:
    # post_key is a persisted account:shortcode identity. Never leak the total
    # size, terms, or membership of accounts excluded from this credential.
    return "(" + " OR ".join("post_key LIKE ? ESCAPE '!'" for _ in handles) + ")" if handles else "1 = 0", [handle.replace("_", "!_") + ":%" for handle in handles]


@router.get("/research/stacks")
def stacks(request: Request, account: str | None = None, limit: _LIMIT = 20, offset: _OFFSET = 0,
           start: _START = None, end: _END = None) -> dict[str, Any]:
    handles = _handles(_scope(request, "research"), account)
    api._range(start, end)
    with db.connect() as conn:
        if not {"post_key", "stack_id", "posted_at"} <= kit._columns(conn, "topic_stack_members"):
            return _envelope([], pagination=_page(0, limit, offset))
        where, params = _stack_where(handles)
        if start:
            where += " AND posted_at >= ?"
            params.append(datetime.combine(start, datetime.min.time(), tzinfo=api.LOCAL_ZONE).timestamp())
        if end:
            where += " AND posted_at < ?"
            params.append(datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=api.LOCAL_ZONE).timestamp())
        total = conn.execute(f"SELECT COUNT(DISTINCT stack_id) AS n FROM topic_stack_members WHERE {where}", params).fetchone()["n"]
        rows = conn.execute(f"SELECT stack_id, COUNT(*) AS post_count, MAX(posted_at) AS latest_posted_at FROM topic_stack_members WHERE {where} GROUP BY stack_id ORDER BY MAX(posted_at) DESC, stack_id LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    return _envelope([dict(row) for row in rows], pagination=_page(total, limit, offset),
                     coverage="Stored memberships for the credential's selected accounts; no grouping is generated by this read.")


@router.get("/research/stacks/{stack_id}/posts")
def stack_posts(request: Request, stack_id: str, account: str | None = None,
                limit: _LIMIT = 20, offset: _OFFSET = 0, start: _START = None, end: _END = None) -> dict[str, Any]:
    handles = _handles(_scope(request, "research"), account)
    api._range(start, end)
    if not _STACK.fullmatch(stack_id):
        raise HTTPException(404, "Stack not available for this API key.")
    with db.connect() as conn:
        if not {"post_key", "stack_id"} <= kit._columns(conn, "topic_stack_members"):
            raise HTTPException(404, "Stack not available for this API key.")
        where, params = _stack_where(handles)
        total = conn.execute(f"SELECT COUNT(*) AS n FROM topic_stack_members WHERE {where} AND stack_id = ?", [*params, stack_id]).fetchone()["n"]
        if not total:
            raise HTTPException(404, "Stack not available for this API key.")
    result = _posts_page(handles, limit=limit, offset=offset, start=start, end=end, stack_id=stack_id)
    result["stack_id"] = stack_id
    return result


def _summary(conn: Any, handles: list[str], start: date | None, end: date | None) -> dict[str, Any]:
    source, params, _ = _sources(conn, handles)
    where, fparams = _filters(start=start, end=end)
    row = conn.execute(source + f"""SELECT COUNT(*) AS post_count,
        COUNT(CASE WHEN likes >= 0 THEN 1 END) AS likes_known,
        SUM(CASE WHEN likes >= 0 THEN likes END) AS likes_total,
        AVG(CASE WHEN likes >= 0 THEN likes END) AS likes_average,
        COUNT(CASE WHEN comments >= 0 THEN 1 END) AS comments_known,
        SUM(CASE WHEN comments >= 0 THEN comments END) AS comments_total,
        AVG(CASE WHEN comments >= 0 THEN comments END) AS comments_average,
        COUNT(CASE WHEN video_views >= 0 THEN 1 END) AS views_known,
        SUM(CASE WHEN video_views >= 0 THEN video_views END) AS views_total,
        AVG(CASE WHEN video_views >= 0 THEN video_views END) AS views_average,
        SUM(CASE WHEN is_hot = 1 THEN 1 ELSE 0 END) AS hot_posts,
        MAX(updated_at) AS metrics_updated_at, MIN(published_at) AS first_published_at,
        MAX(published_at) AS last_published_at FROM catalogue WHERE {where}""", [*params, *fparams]).fetchone()
    return dict(row)


@router.get("/insights/summary")
def insights_summary(request: Request, account: str | None = None, start: _START = None, end: _END = None) -> dict[str, Any]:
    handles = _handles(_scope(request, "insights"), account)
    api._range(start, end)
    with db.connect() as conn:
        data = _summary(conn, handles, start, end)
    return _envelope(data, accounts=handles, coverage="Stored published sample; unknown metrics stay null and are excluded from averages.")


def _snapshot_summaries(conn: Any, handles: list[str]) -> list[dict[str, Any]]:
    """Two bounded reads for a page, rather than four roundtrips per account."""
    now = datetime.now(UTC)
    columns = kit._columns(conn, "account_snapshots")
    fields = [field for field in ("handle", "captured_at", "followers_count", "following_count", "posts_count", "full_name", "verified", "private") if field in columns]
    current: dict[str, dict[str, Any]] = {}
    baselines: dict[tuple[str, int], dict[str, Any]] = {}
    if handles and {"captured_at", "followers_count"} <= columns:
        marks = ",".join("?" for _ in handles)
        rows = conn.execute(f"""WITH latest AS (
            SELECT {','.join(fields)}, ROW_NUMBER() OVER (PARTITION BY LOWER(handle) ORDER BY captured_at DESC) AS position
            FROM account_snapshots WHERE LOWER(handle) IN ({marks}) AND captured_at <= ?
                AND followers_count IS NOT NULL AND followers_count >= 0
        ) SELECT * FROM latest WHERE position = 1""", [*handles, now.isoformat(timespec="seconds")]).fetchall()
        current = {str(row["handle"]).lower(): dict(row) for row in rows}
        targets: list[Any] = []
        for handle, value in current.items():
            captured = kit._date(value.get("captured_at"))
            if not captured:
                continue
            for days in (1, 7, 30):
                # Compare final readings of local calendar days, as Tracker
                # does, even when yesterday's refresh ran later in the day.
                cutoff_day = captured.astimezone(api.LOCAL_ZONE).date() - timedelta(days=days) + timedelta(days=1)
                before = datetime.combine(cutoff_day, datetime.min.time(), tzinfo=api.LOCAL_ZONE).astimezone(UTC)
                targets.extend((handle, days, before.isoformat(timespec="seconds")))
        if targets:
            placeholders = ",".join("(?, ?, ?)" for _ in range(len(targets) // 3))
            rows = conn.execute(f"""WITH targets(handle, days, before_at) AS (VALUES {placeholders}),
                prior AS (
                    SELECT targets.handle, targets.days, snapshots.captured_at, snapshots.followers_count,
                        ROW_NUMBER() OVER (PARTITION BY targets.handle, targets.days ORDER BY snapshots.captured_at DESC) AS position
                    FROM targets JOIN account_snapshots snapshots ON LOWER(snapshots.handle) = targets.handle
                        AND snapshots.captured_at < targets.before_at
                    WHERE snapshots.followers_count IS NOT NULL AND snapshots.followers_count >= 0
                ) SELECT * FROM prior WHERE position = 1""", targets).fetchall()
            baselines = {(str(row["handle"]).lower(), int(row["days"])): dict(row) for row in rows}
    result = []
    for handle in handles:
        value = current.get(handle, {})
        followers = kit._number(value.get("followers_count"))
        deltas = {}
        for days in (1, 7, 30):
            baseline = baselines.get((handle, days))
            previous = kit._number(baseline["followers_count"]) if baseline else None
            delta = followers - previous if followers is not None and previous is not None else None
            deltas[f"{days}d"] = {"delta": delta, "percent": delta / previous * 100 if delta is not None and previous else None,
                                   "baseline_captured_at": baseline["captured_at"]} if baseline else None
        result.append({"handle": handle, "followers": followers,
                       "following_count": kit._number(value.get("following_count")),
                       "posts_count": kit._number(value.get("posts_count")), "captured_at": value.get("captured_at"),
                       "full_name": value.get("full_name"), "verified": kit._bool(value.get("verified")),
                       "private": kit._bool(value.get("private")), "deltas": deltas})
    return result


@router.get("/tracker/accounts")
@router.get("/tracker/overview")
def tracker_accounts(request: Request, account: str | None = None, limit: _LIMIT = 20, offset: _OFFSET = 0) -> dict[str, Any]:
    handles = _handles(_scope(request, "tracker"), account)
    with db.connect() as conn:
        registry = _registry(conn, handles)
        data = _snapshot_summaries(conn, handles)
        engagement = {}
        if handles and {"account", "published_at", "likes"} <= kit._columns(conn, "dashboard_posts"):
            now = datetime.now(UTC)
            cutoff30 = (now - timedelta(days=30)).isoformat(timespec="seconds")
            cutoff60 = (now - timedelta(days=60)).isoformat(timespec="seconds")
            marks = ",".join("?" for _ in handles)
            # Match the existing Tracker's saved-dashboard-post methodology.
            # Group in SQL once; requesting more accounts adds no roundtrips.
            rows = conn.execute(f"""SELECT LOWER(account) AS account,
                AVG(CASE WHEN published_at >= ? AND likes >= 0 THEN likes END) AS avg_likes_30d,
                COUNT(CASE WHEN published_at >= ? THEN 1 END) AS posts_30d,
                COUNT(CASE WHEN published_at >= ? AND likes >= 0 THEN 1 END) AS likes_known_30d,
                AVG(CASE WHEN published_at >= ? AND published_at < ? AND likes >= 0 THEN likes END) AS avg_likes_previous_30d,
                COUNT(CASE WHEN published_at >= ? AND published_at < ? AND likes >= 0 THEN 1 END) AS likes_known_previous_30d
                FROM dashboard_posts WHERE LOWER(account) IN ({marks})
                    AND published_at IS NOT NULL AND published_at <> ''
                GROUP BY LOWER(account)""", [cutoff30, cutoff30, cutoff30, cutoff60, cutoff30, cutoff60, cutoff30, *handles]).fetchall()
            engagement = {row["account"]: dict(row) for row in rows}
    by_followers = sorted((value for value in data if value["followers"] is not None), key=lambda value: (-value["followers"], value["handle"]))
    for rank, value in enumerate(by_followers, 1):
        value["rank_by_followers"] = rank
    by_growth = sorted((value for value in data if value["deltas"]["7d"] is not None), key=lambda value: (-value["deltas"]["7d"]["delta"], value["handle"]))
    for rank, value in enumerate(by_growth, 1):
        value["rank_by_growth_7d"] = rank
    for value in data:
        metrics = engagement.get(value["handle"], {})
        average = metrics.get("avg_likes_30d")
        previous = metrics.get("avg_likes_previous_30d")
        value.update({"label": registry.get(value["handle"], {}).get("label"),
                      "group": registry.get(value["handle"], {}).get("group_name"),
                      "avg_likes_30d": average, "posts_30d": metrics.get("posts_30d", 0),
                      "likes_known_30d": metrics.get("likes_known_30d", 0),
                      "avg_likes_previous_30d": previous,
                      "likes_known_previous_30d": metrics.get("likes_known_previous_30d", 0),
                      "engagement_trend_pct": (average - previous) / previous * 100 if average is not None and previous else None})
        value.setdefault("rank_by_followers", None)
        value.setdefault("rank_by_growth_7d", None)
        for days in (1, 7, 30):
            value[f"delta_{days}d"] = value["deltas"][f"{days}d"]
    return _envelope(data[offset:offset + limit], pagination=_page(len(handles), limit, offset),
                     rankings_scope="Selected accounts available to this API key; rankings are calculated before pagination.",
                     engagement_methodology="Stored Dashboard post source and rolling 30-day versus previous 30-day windows; averages exclude unknown or negative like counts, while posts_30d counts every stored post in the window.")


@router.get("/tracker/accounts/{handle}/history")
def tracker_history(request: Request, handle: str, limit: _LIMIT = 20, offset: _OFFSET = 0,
                    start: _START = None, end: _END = None) -> dict[str, Any]:
    handles = _handles(_scope(request, "tracker"), handle)
    if len(handles) != 1:
        raise HTTPException(422, "Provide one account handle.")
    api._range(start, end)
    with db.connect() as conn:
        columns = kit._columns(conn, "account_snapshots")
        if not {"captured_at", "followers_count"} <= columns:
            return _envelope([], pagination=_page(0, limit, offset), account=handles[0])
        fields = [field for field in ("captured_at", "followers_count", "following_count", "posts_count", "full_name", "verified", "private") if field in columns]
        where = ["LOWER(handle) = ?", "captured_at <= ?"]
        params: list[Any] = [handles[0], datetime.now(UTC).isoformat(timespec="seconds")]
        if start:
            where.append("captured_at >= ?")
            params.append(datetime.combine(start, datetime.min.time(), tzinfo=api.LOCAL_ZONE).astimezone(UTC).isoformat(timespec="seconds"))
        if end:
            where.append("captured_at < ?")
            params.append(datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=api.LOCAL_ZONE).astimezone(UTC).isoformat(timespec="seconds"))
        clause = " AND ".join(where)
        total = conn.execute(f"SELECT COUNT(*) AS n FROM account_snapshots WHERE {clause}", params).fetchone()["n"]
        rows = conn.execute(f"SELECT {','.join(fields)} FROM account_snapshots WHERE {clause} ORDER BY captured_at DESC LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    return _envelope([{**dict(row), "followers": kit._number(row["followers_count"])} for row in rows],
                     pagination=_page(total, limit, offset), account=handles[0])


def _calendar_expression(conn: Any, field: str, *, month: bool = False) -> str:
    if getattr(conn, "is_postgres", False):
        pattern = "YYYY-MM" if month else "YYYY-MM-DD"
        return f"TO_CHAR({field}::timestamptz AT TIME ZONE 'America/Costa_Rica', '{pattern}')"
    pattern = "%Y-%m" if month else "%Y-%m-%d"
    return f"strftime('{pattern}', {field}, '-6 hours')"


@router.get("/tracker/accounts/{handle}/monthly")
def tracker_monthly(request: Request, handle: str, limit: _LIMIT = 20, offset: _OFFSET = 0,
                    start: _START = None, end: _END = None) -> dict[str, Any]:
    handles = _handles(_scope(request, "tracker"), handle)
    if len(handles) != 1:
        raise HTTPException(422, "Provide one account handle.")
    api._range(start, end)
    with db.connect() as conn:
        columns = kit._columns(conn, "account_snapshots")
        if not {"captured_at", "followers_count"} <= columns:
            return _envelope([], pagination=_page(0, limit, offset), account=handles[0])
        month = _calendar_expression(conn, "captured_at", month=True)
        source = f"""WITH monthly_reads AS (
            SELECT captured_at, followers_count, {month} AS month,
                ROW_NUMBER() OVER (PARTITION BY {month} ORDER BY captured_at) AS opening_rank,
                ROW_NUMBER() OVER (PARTITION BY {month} ORDER BY captured_at DESC) AS closing_rank
            FROM account_snapshots WHERE LOWER(handle) = ? AND captured_at <= ?
                AND followers_count IS NOT NULL AND followers_count >= 0
        ), months AS (
            SELECT month, COUNT(*) AS observation_count,
                MIN(captured_at) AS first_captured_at, MAX(captured_at) AS last_captured_at,
                MAX(CASE WHEN opening_rank = 1 THEN followers_count END) AS opening_followers,
                MAX(CASE WHEN closing_rank = 1 THEN followers_count END) AS closing_followers
            FROM monthly_reads GROUP BY month
        ), growth AS (
            SELECT months.*, LAG(month) OVER (ORDER BY month) AS previous_month,
                LAG(closing_followers) OVER (ORDER BY month) AS previous_closing_followers
            FROM months
        ) """
        params: list[Any] = [handles[0], datetime.now(UTC).isoformat(timespec="seconds")]
        where = []
        if start:
            where.append("month >= ?")
            params.append(start.strftime("%Y-%m"))
        if end:
            where.append("month <= ?")
            params.append(end.strftime("%Y-%m"))
        clause = " AND ".join(where) if where else "1 = 1"
        total = conn.execute(source + f"SELECT COUNT(*) AS n FROM growth WHERE {clause}", params).fetchone()["n"]
        rows = conn.execute(source + f"SELECT * FROM growth WHERE {clause} ORDER BY month DESC LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    data = []
    for row in rows:
        value = dict(row)
        previous = value["previous_closing_followers"]
        month_start = date.fromisoformat(value["month"] + "-01")
        if value["previous_month"] != (month_start - timedelta(days=1)).strftime("%Y-%m"):
            previous = None
            value["previous_closing_followers"] = None
        value["net_followers"] = value["closing_followers"] - previous if previous is not None else None
        value["growth_pct"] = value["net_followers"] / previous * 100 if previous else None
        value["within_month_change"] = value["closing_followers"] - value["opening_followers"]
        data.append(value)
    return _envelope(data, pagination=_page(total, limit, offset), account=handles[0],
                     methodology="Month boundaries use Costa Rica time. Net change compares observed month-end counts; a missing prior month is not invented.")


@router.get("/tracker/accounts/{handle}/engagement")
def tracker_engagement(request: Request, handle: str, granularity: Literal["day", "month"] = "month",
                       limit: _LIMIT = 20, offset: _OFFSET = 0, start: _START = None, end: _END = None) -> dict[str, Any]:
    handles = _handles(_scope(request, "tracker"), handle)
    if len(handles) != 1:
        raise HTTPException(422, "Provide one account handle.")
    api._range(start, end)
    with db.connect() as conn:
        source, params, _ = _sources(conn, handles)
        where, fparams = _filters(start=start, end=end)
        period = _calendar_expression(conn, "published_at", month=granularity == "month")
        grouped = f"SELECT {period} AS period, COUNT(*) AS post_count, COUNT(CASE WHEN likes >= 0 THEN 1 END) AS likes_known, AVG(CASE WHEN likes >= 0 THEN likes END) AS likes_average, AVG(CASE WHEN comments >= 0 THEN comments END) AS comments_average, AVG(CASE WHEN video_views >= 0 THEN video_views END) AS views_average FROM catalogue WHERE {where} GROUP BY {period}"
        total = conn.execute(source + f"SELECT COUNT(*) AS n FROM ({grouped}) engagement_periods", [*params, *fparams]).fetchone()["n"]
        rows = conn.execute(source + grouped + " ORDER BY period DESC LIMIT ? OFFSET ?", [*params, *fparams, limit, offset]).fetchall()
    return _envelope([dict(row) for row in rows], pagination=_page(total, limit, offset), account=handles[0], granularity=granularity)


@router.get("/insights/follower-growth")
def follower_growth(request: Request, account: str,
                    lookback_days: Annotated[int, Query(ge=35, le=365)] = 120) -> dict[str, Any]:
    """Same evidence-bound peak method as Insights, for one bounded account.

    The first 28 daily observations warm up the trailing baseline. Attribution
    remains a time-aligned candidate relationship, never a conversion claim.
    """
    handles = _handles(_scope(request, "insights"), account)
    if len(handles) != 1:
        raise HTTPException(422, "Provide one account handle.")
    from .main import _follower_growth_insights
    clean = handles[0]
    now = datetime.now(UTC)
    cutoff = (now - timedelta(days=lookback_days)).isoformat(timespec="seconds")
    with db.connect() as conn:
        registry = _registry(conn, handles)
        snapshots = [dict(row) for row in conn.execute("SELECT captured_at, followers_count FROM account_snapshots WHERE LOWER(handle) = ? AND captured_at >= ? AND captured_at <= ? ORDER BY captured_at DESC LIMIT 1001", (clean, cutoff, now.isoformat(timespec="seconds"))).fetchall()]
        source, params, _ = _sources(conn, handles)
        rows = [dict(row) for row in conn.execute(source + "SELECT account, shortcode, published_at, likes, video_views, post_type_label, hook_text FROM catalogue WHERE published_at >= ? AND published_at <= ? AND COALESCE(hidden,0) = 0 AND COALESCE(is_deleted,0) = 0 ORDER BY published_at DESC LIMIT 5001", [*params, cutoff, now.isoformat(timespec="seconds")]).fetchall()]
    data = _follower_growth_insights([{"handle": clean, "label": registry.get(clean, {}).get("label"), "group": registry.get(clean, {}).get("group_name")}],
                                     {clean: list(reversed(snapshots[:1000]))}, rows[:5000])
    # Every candidate is a stored Instagram post with a generated public link.
    visited = set()
    for item in data["accounts"]:
        for event in item["recent_events"] + item["peaks"]:
            if id(event) in visited:
                continue
            visited.add(id(event))
            candidates = event["candidate_posts"]
            for post in candidates:
                post["permalink"] = f"https://www.instagram.com/p/{post['shortcode']}/"
            event["candidate_post_count"] = len(candidates)
            event["candidate_posts_truncated"] = len(candidates) > 100
            event["candidate_posts"] = candidates[:100]
    return _envelope(data, coverage={"lookback_days": lookback_days, "snapshot_limit": 1000, "post_limit": 5000,
                                    "snapshots_truncated": len(snapshots) > 1000, "posts_truncated": len(rows) > 5000})


def _stored_assets(request: Request, account: str, shortcode: str) -> list[dict[str, Any]]:
    from . import media_storage
    handles = _handles(_scope(request, "research"), account)
    if len(handles) != 1:
        raise HTTPException(422, "Provide one account handle.")
    if not _CODE.fullmatch(shortcode):
        raise HTTPException(404, "Post not available for this API key.")
    page = _posts_page(handles, limit=1, offset=0, shortlist=[(handles[0], shortcode)], include_hidden=True, include_deleted=True)
    if not page["data"]:
        raise HTTPException(404, "Post not available for this API key.")
    assets = []
    remote = []
    seen = set()
    with db.connect() as conn:
        registry = _registry(conn, handles)
        for table in (["posts", "dashboard_posts"] if registry[handles[0]].get("is_canonical") else ["dashboard_posts"]):
            columns = kit._columns(conn, table)
            fields = [field for field in ("image_path", "cover_image_path", "video_path", "raw_json", "enriched_at") if field in columns]
            if not fields:
                continue
            where = "shortcode = ?" + (" AND LOWER(account) = ?" if table == "dashboard_posts" else " AND section <> 'ab'" if "section" in columns else "")
            params = [shortcode, handles[0]] if table == "dashboard_posts" else [shortcode]
            for row in conn.execute(f"SELECT {','.join(fields)} FROM {table} WHERE {where} ORDER BY updated_at DESC LIMIT 3", params).fetchall():
                for field in fields:
                    if field in {"raw_json", "enriched_at"}:
                        continue
                    reference = row[field]
                    if not media_storage.is_r2_reference(reference) or reference in seen:
                        continue
                    try:
                        key = media_storage._object_key(reference)
                    except ValueError:
                        continue
                    seen.add(reference)
                    kind = "video" if field == "video_path" else "image"
                    index = len(assets) + 1
                    assets.append({"index": index, "kind": kind, "role": "video" if kind == "video" else "cover_or_uploaded_image",
                                   "content_type": mimetypes.guess_type(key)[0] or "application/octet-stream",
                                   "download_url": f"/api/v1/research/posts/{handles[0]}/{shortcode}/media/{index}",
                                   "stored": True, "expires_possible": False,
                                   "_reference": reference})
                if "raw_json" in fields:
                    from .post_media import PostMediaError, _slides_from_item
                    payload = kit._json(row["raw_json"])
                    payload_code = payload.get("shortCode", payload.get("shortcode", shortcode))
                    if payload_code != shortcode:
                        continue
                    try:
                        media = _slides_from_item(payload)
                    except PostMediaError:
                        continue
                    for item in media[:100]:
                        url = _public_media_url(item.get("url"))
                        if not url or url in seen:
                            continue
                        seen.add(url)
                        remote.append({"kind": item["kind"], "role": "original_media_reference",
                                       "source_url": url, "poster_url": _public_media_url(item.get("poster")),
                                       "stored": False, "expires_possible": True,
                                       "observed_at": row["enriched_at"] if "enriched_at" in fields else None})
    for item in remote:
        if len(assets) >= 100:
            break
        assets.append({"index": len(assets) + 1, **item})
    return assets


def _public_media_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 8192:
        return None
    try:
        parsed = urlparse(value)
        host = parsed.hostname
        if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port not in {None, 443}:
            return None
    except ValueError:
        return None
    return value


@router.get("/research/posts/{account}/{shortcode}/media")
def post_media(request: Request, account: str, shortcode: str) -> dict[str, Any]:
    assets = _stored_assets(request, account, shortcode)
    return _envelope([{key: value for key, value in asset.items() if key != "_reference"} for asset in assets],
                     coverage={"source": "stored R2 assets and saved public HTTPS media references", "item_limit": 100,
                               "includes_all_carousel_slides": False,
                               "note": "Remote references may expire. Only stored:true items have an authenticated download_url. Never send an API key to source_url. This read never fetches Instagram or Apify."})


@router.get("/research/posts/{account}/{shortcode}/media/{index}")
def post_media_content(request: Request, account: str, shortcode: str,
                       index: int) -> StreamingResponse:
    from . import media_storage
    if index < 1:
        raise HTTPException(404, "Stored media not available.")
    assets = _stored_assets(request, account, shortcode)
    asset = next((asset for asset in assets if asset["index"] == index), None)
    if asset is None or not asset.get("_reference"):
        raise HTTPException(404, "Stored media not available.")
    if not media_storage.r2_enabled():
        raise HTTPException(503, "Stored media service is unavailable.")
    try:
        result = media_storage._client().get_object(Bucket=media_storage.R2_BUCKET, Key=media_storage._object_key(asset["_reference"]))
    except Exception:
        raise HTTPException(404, "Stored media not available.") from None
    body = result["Body"]

    def chunks():
        try:
            while chunk := body.read(64 * 1024):
                yield chunk
        finally:
            body.close()

    suffix = mimetypes.guess_extension(asset["content_type"]) or ".bin"
    filename = f"{account}-{shortcode}-{index:02d}{suffix}"
    return StreamingResponse(chunks(), media_type=asset["content_type"], headers={
        "Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "private, no-store",
        "Vary": "Authorization", "X-Content-Type-Options": "nosniff"})
