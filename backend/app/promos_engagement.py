"""Comparable benchmarks for every active dashboard account, from stored data."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from fastapi import HTTPException

from . import db
from .account_media_kit import (
    _bool,
    _columns,
    _ALIASES,
    _date,
    _json,
    _number,
    _snake,
)
from .apify_sync import list_accounts
from . import promos_engagement_cache as benchmark_cache

_METRICS = ("likes", "comments", "shares", "saves", "impressions", "video_views", "video_plays")
_POST_FIELDS = {
    "id", "account", "shortcode", "section", "published_at", "updated_at", "enriched_at",
    "post_type_label", "product_type", "is_animated", "slide_count", "raw_json", "hidden", "is_deleted",
    *_METRICS,
}
_FORMATS = {"reel": "Reel", "carousel": "Carousel", "image": "Image"}
_BLOCK_SIZE = 5000
_RAW_KEYS = {*_METRICS, *(key for key, metric in _ALIASES.items() if metric in _METRICS),
             *(key.replace("_", "") for key in _METRICS),
             "shortCode", "type", "productType", "slide_count"}
_CONTAINERS = {"metrics", "insights", "statistics", "stats", "video", "video_info", "engagement"}


def _engagement_roster() -> list[dict[str, Any]]:
    """Tool-specific Research/Promos flags do not restrict Engagement accounts."""
    return list_accounts(active_only=True)


@lru_cache(maxsize=256)
def _counter_name(name: str) -> str | None:
    metric = _ALIASES.get(name)
    if metric is not None:
        return metric if metric in _METRICS else None
    metric = _snake(name)
    return metric if metric in _METRICS else None


def _counter_values(payload: dict[str, Any]) -> dict[str, int | float | None]:
    result: dict[str, int | float | None] = {}

    def visit(value: dict[str, Any], depth: int = 0) -> None:
        for name, raw in value.items():
            if isinstance(raw, dict) and depth < 3 and name.lower() in _CONTAINERS:
                visit(raw, depth + 1)
            elif not isinstance(raw, (dict, list, bool)) and (metric := _counter_name(name)) is not None:
                if metric not in result or result[metric] is None:
                    result[metric] = _number(raw)
    visit(payload)
    return result


def _child_count(payload: dict[str, Any]) -> int | float | None:
    children = payload.get("childPosts")
    return len(children) if isinstance(children, list) and children else _number(payload.get("slide_count"))


def _metric_json(conn: Any) -> str:
    """Project parent counters only; carousel children/media are not counters."""
    keys = ",".join(f"'{key}'" for key in sorted({key.lower() for key in _RAW_KEYS}))
    containers = ",".join(f"'{key}'" for key in sorted(_CONTAINERS))
    if getattr(conn, "is_postgres", False):
        payload = "CASE WHEN jsonb_typeof(public_payload) = 'object' THEN public_payload ELSE '{}'::jsonb END"
        return f"""((SELECT COALESCE(jsonb_object_agg(key, value), '{{}}'::jsonb)
            FROM jsonb_each({payload})
            WHERE (LOWER(key) IN ({keys}) AND jsonb_typeof(value) NOT IN ('array','object','boolean'))
               OR (LOWER(key) IN ({containers}) AND jsonb_typeof(value) = 'object'))
            || CASE WHEN jsonb_typeof(public_payload->'childPosts') = 'array'
                    THEN jsonb_build_object('childPosts', to_jsonb(array_fill(NULL::integer,
                        ARRAY[jsonb_array_length(public_payload->'childPosts')])))
                    ELSE '{{}}'::jsonb END)::text"""
    payload = "CASE WHEN json_valid(raw_json) THEN raw_json ELSE '{}' END"
    compact = f"""(SELECT COALESCE(json_group_object(key, json(CASE WHEN type = 'object'
        THEN value ELSE json_quote(value) END)), '{{}}') FROM json_each({payload})
        WHERE (LOWER(key) IN ({keys}) AND type NOT IN ('array','object','true','false'))
           OR (LOWER(key) IN ({containers}) AND type = 'object'))"""
    return f"""CASE WHEN json_type({payload}, '$.childPosts') = 'array'
        THEN json_set({compact}, '$.childPosts', json((SELECT json_group_array(NULL)
            FROM json_each({payload}, '$.childPosts'))))
        ELSE {compact} END"""


def _read_metrics(conn: Any, table: str, selected: list[str], where: str,
                  parameters: list[Any], *, order: str = "id", limit: int | None = None) -> list[dict[str, Any]]:
    """One bounded query per cohort; split malformed JSON without OFFSET scans."""
    postgres = bool(getattr(conn, "is_postgres", False))
    projection = ", ".join(f"{_metric_json(conn)} AS raw_json" if name == "raw_json" else name for name in selected)
    fields = ", ".join(selected)
    source = f"(SELECT {fields} FROM {table} WHERE {where} ORDER BY {order}" + (" LIMIT ?" if limit else "") + ") public_rows"
    query_params = [*parameters, limit] if limit else parameters
    join = " CROSS JOIN LATERAL (SELECT NULLIF(TRIM(raw_json), '')::jsonb AS public_payload OFFSET 0) public_json" if postgres and "raw_json" in selected else ""
    if postgres:
        conn.execute("SAVEPOINT promo_metric_payload")
    failed = False
    try:
        rows = conn.execute(f"SELECT {projection} FROM {source}{join}", query_params).fetchall()
    except Exception as error:
        if not postgres:
            raise
        conn.execute("ROLLBACK TO SAVEPOINT promo_metric_payload")
        if getattr(error, "sqlstate", None) != "22P02":
            raise
        failed = True
        safe = ", ".join("NULL AS raw_json" if name == "raw_json" else name for name in selected)
        rows = conn.execute(f"SELECT {safe} FROM {source}", query_params).fetchall()
    finally:
        if postgres:
            conn.execute("RELEASE SAVEPOINT promo_metric_payload")
    if failed and len(rows) > 1:
        keys = [row[order] for row in rows]
        middle = len(keys) // 2
        result = []
        for part in (keys[:middle], keys[middle:]):
            result.extend(_read_metrics(conn, table, selected,
                f"({where}) AND {order} IN ({','.join('?' for _ in part)})", [*parameters, *part], order=order))
        return result
    result = [dict(row) for row in rows]
    for row in result:
        if isinstance(row.get("post_type_label"), str):
            row["post_type_label"] = row["post_type_label"].split(" (", 1)[0].strip()
    return result


def _rows(conn: Any, table: str, columns: set[str], handle: str,
          codes: list[str]) -> list[dict[str, Any]]:
    selected = sorted(columns & _POST_FIELDS)
    if not selected or not codes:
        return []
    where = f"shortcode IN ({','.join('?' for _ in codes)})"
    parameters: list[Any] = list(codes)
    if table == "dashboard_posts":
        where += " AND account = ?"
        parameters.append(handle)
    rows = _read_metrics(conn, table, selected, where, parameters)
    return [row | {"_table": table} for row in rows]


def _date_predicate(cutoff: datetime | None, current: datetime) -> tuple[str, list[Any]]:
    # Coarse dates retain timezone offsets around the boundary. Exact UTC
    # validation still happens after duplicate normalization below.
    if cutoff is None:
        return "", []
    return " AND published_at >= ? AND published_at < ?", [
        (cutoff - timedelta(days=1)).date().isoformat(),
        (current + timedelta(days=2)).date().isoformat(),
    ]


def _record_blocks(conn: Any, handle: str, canonical: bool, columns: dict[str, set[str]],
                   cutoff: datetime | None, current: datetime):
    date_sql, dates = _date_predicate(cutoff, current)
    after = ""
    tables = ["dashboard_posts", *(["posts"] if canonical and columns["posts"] else [])]
    while True:
        parts = [f"SELECT shortcode FROM dashboard_posts WHERE account = ? AND shortcode > ? AND TRIM(shortcode) != ''{date_sql}"]
        parameters: list[Any] = [handle, after, *dates]
        if canonical and columns["posts"]:
            parts.append(f"SELECT shortcode FROM posts WHERE shortcode > ? AND TRIM(shortcode) != ''{date_sql}")
            parameters.extend([after, *dates])
        # LIMIT applies to shortcode keys before any provider JSON is parsed.
        # Fetching every copy of those keys retains canonical deduplication,
        # hidden/deleted flags, and copies with missing/conflicting dates.
        rows = conn.execute(
            f"SELECT shortcode FROM ({' UNION '.join(parts)}) cohort ORDER BY shortcode LIMIT ?",
            [*parameters, _BLOCK_SIZE],
        ).fetchall()
        codes = [row["shortcode"] for row in rows]
        if not codes:
            break
        records = []
        for table in tables:
            records.extend(_rows(conn, table, columns[table], handle, codes))
        observations = {}
        if columns["engagement_observations"]:
            observations = {row["shortcode"]: row for row in _read_metrics(conn,
                "engagement_observations", ["shortcode", "observed_at", "raw_json"],
                f"shortcode IN ({','.join('?' for _ in codes)})", codes, order="shortcode")}
        yield records, observations
        after = codes[-1]
        if len(codes) < _BLOCK_SIZE:
            break
    # Dated imports without an Instagram shortcode remain legitimate samples.
    for table in tables:
        selected = sorted(columns[table] & _POST_FIELDS)
        if not selected:
            continue
        after_id = 0
        while True:
            where = f"(shortcode IS NULL OR TRIM(shortcode) = ''){date_sql} AND id > ?"
            params: list[Any] = [*dates, after_id]
            if table == "dashboard_posts":
                where += " AND account = ?"
                params.append(handle)
            records = _read_metrics(conn, table, selected, where, params, limit=_BLOCK_SIZE)
            if not records:
                break
            yield [row | {"_table": table} for row in records], {}
            after_id = records[-1]["id"]
            if len(records) < _BLOCK_SIZE:
                break


def _measurement(value: Any, *, likes: bool = False) -> int | float | None:
    known = _number(value)
    # Research treats 0–3 as unavailable Instagram like readings. Apply the
    # same rule here so a hidden count cannot become a cheap CPE estimate.
    return known if known is not None and (not likes or known > 3) else None


def _measured_posts(records: list[dict[str, Any]], observations: dict[str, dict[str, Any]],
                    handle: str, current: datetime) -> list[dict[str, Any]]:
    """Normalize only the benchmark fields, keeping metric clocks separate.

    This mirrors the shared report's duplicate and promoted-counter rules,
    without constructing captions, media, collaboration or model metrics for
    every historical post. Provider keys are mapped once per process.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        code = str(row.get("shortcode") or "").strip()
        if row["_table"] == "posts" and (row.get("section") == "ab" or (not code and _date(row.get("published_at")) is None)):
            continue
        grouped[code or f"{row['_table']}:{row.get('id')}"].append(row)
    posts = []
    for copies in grouped.values():
        def quality(row):
            return (_date(row.get("updated_at")) or datetime.min.replace(tzinfo=UTC),
                    bool(_date(row.get("published_at"))),
                    sum(row.get(name) is not None for name in (*_METRICS, "slide_count")),
                    row["_table"] == "posts", row.get("id") or 0)
        copies.sort(key=quality, reverse=True)
        merged, metrics, raw = {}, {}, {}
        sources = []
        raw_slides = None
        for row in copies:
            for name, value in row.items():
                if name not in merged or merged[name] in (None, ""):
                    merged[name] = value
            payload = _json(row.get("raw_json"))
            counters = _counter_values(payload)
            if not raw:
                raw = payload
            if raw_slides is None:
                raw_slides = _child_count(payload)
            for name, value in counters.items():
                if name not in metrics or metrics[name] is None:
                    metrics[name] = value
            enriched = _date(row.get("enriched_at"))
            if enriched is not None and enriched <= current:
                sources.append((enriched, 0, counters, _child_count(payload)))
        for name in _METRICS:
            if name not in merged:
                continue
            if name in {"likes", "comments"}:
                metrics[name] = _number(next((row[name] for row in copies if name in row), None))
            else:
                promoted = _number(merged[name])
                if promoted is not None or name not in metrics:
                    metrics[name] = promoted
        code = str(merged.get("shortcode") or "").strip()
        observation = observations.get(code)
        if observation:
            observed = _date(observation.get("observed_at"))
            payload = _json(observation.get("raw_json"))
            if observed is not None and observed <= current and payload.get("shortCode", code) == code:
                sources.append((observed, 1, _counter_values(payload), _child_count(payload)))
                updated = max((_date(row.get("updated_at")) for row in copies if _date(row.get("updated_at")) is not None), default=None)
                if updated is None or observed >= updated:
                    raw = {**raw, **payload}
        sources.sort(key=lambda item: (item[0], item[1]), reverse=True)
        stamps = []
        for name in _METRICS:
            source = next((item for item in sources if name in item[2]), None)
            if source is None:
                continue
            measured_at, _, counters, _ = source
            # Preserve a later unavailable promoted core count: generic
            # updated_at alone cannot safely prove an older visible reading
            # still applies, even though metadata edits also touch this clock.
            unavailable = name in {"likes", "comments"} and any(
                name in row and _measurement(row[name], likes=name == "likes") is None
                and (updated := _date(row.get("updated_at"))) is not None and updated > measured_at
                for row in copies
            )
            metrics[name] = None if unavailable else counters[name]
            stamps.append(measured_at)
        slides = next((item[3] for item in sources if item[3] is not None), None)
        if slides is None:
            slides = _number(merged.get("slide_count"))
        if slides is None:
            slides = raw_slides
        product = str(merged.get("product_type") or raw.get("productType") or "").lower()
        kind = str(merged.get("post_type_label") or raw.get("type") or "Image")
        if product in {"clips", "reels", "reel"} or kind.lower() in {"reel", "reels"}:
            kind = "Reel"
        elif kind.lower() in {"sidecar", "carousel"} or (slides or 0) > 1:
            kind = "Carousel"
        elif kind.lower().startswith("video") or merged.get("is_animated"):
            kind = "Video"
        else:
            kind = "Image"
        posts.append({
            "id": merged.get("id"), "shortcode": code or None,
            "published_at": merged.get("published_at"), "format": kind, "metrics": metrics,
            "hidden": any(_bool(row.get("hidden")) for row in copies),
            "is_deleted": any(_bool(row.get("is_deleted")) for row in copies),
            "metrics_updated_at": max(stamps).isoformat(timespec="seconds") if stamps else None,
        })
    return posts


def _account_benchmark(conn: Any, account: dict[str, Any], columns: dict[str, set[str]],
                       cutoff: datetime | None, current: datetime, format: str) -> dict[str, Any]:
    """Reduce each bounded cohort immediately, including the all-history view."""
    handle = account["handle"].lower()
    totals = {name: [0, 0] for name in (*_METRICS, "core", "complete")}
    count = undated = 0
    first = last = freshest = None
    for records, observations in _record_blocks(conn, handle, bool(account.get("is_canonical")), columns, cutoff, current):
        excluded = {str(row["shortcode"]).strip() for row in records if row.get("shortcode") and (_bool(row.get("hidden")) or _bool(row.get("is_deleted")))}
        for post in _measured_posts(records, observations, handle, current):
            published = _date(post["published_at"])
            if post["hidden"] or post["is_deleted"] or post["shortcode"] in excluded:
                continue
            if published is not None and (published > current or (cutoff is not None and published < cutoff)):
                continue
            if published is None and cutoff is not None:
                continue
            if format != "all" and post["format"] != _FORMATS[format]:
                continue
            count += 1
            if published is None:
                undated += 1
            else:
                first = min(first, published) if first else published
                last = max(last, published) if last else published
            stamp = _date(post["metrics_updated_at"])
            if stamp is not None:
                freshest = max(freshest, stamp) if freshest else stamp
            values = {name: _measurement(post["metrics"].get(name), likes=name == "likes") for name in _METRICS}
            for name, value in values.items():
                if value is not None:
                    totals[name][0] += 1
                    totals[name][1] += value
            if values["likes"] is not None and values["comments"] is not None:
                totals["core"][0] += 1
                totals["core"][1] += values["likes"] + values["comments"]
            if all(values[name] is not None for name in ("likes", "comments", "shares", "saves")):
                totals["complete"][0] += 1
                totals["complete"][1] += sum(values[name] for name in ("likes", "comments", "shares", "saves"))

    def summary(name: str) -> dict[str, Any]:
        known, total = totals[name]
        return {"count": known, "total": total if known else None,
                "average": total / known if known else None, "coverage_pct": known / count * 100 if count else 0}

    core, complete = summary("core"), summary("complete")
    return {
        "post_count": count, "undated_post_count": undated,
        "engagement_post_count": core["count"], "average_engagements": core["average"],
        "engagement_coverage_pct": core["coverage_pct"],
        "complete_engagement_post_count": complete["count"],
        "average_complete_engagements": complete["average"],
        "complete_engagement_coverage_pct": complete["coverage_pct"],
        "metrics": {name: summary(name) for name in _METRICS},
        "first_post_at": first.isoformat(timespec="seconds") if first else None,
        "last_post_at": last.isoformat(timespec="seconds") if last else None,
        "metrics_updated_at": freshest.isoformat(timespec="seconds") if freshest else None,
    }


def calculate_promos_engagement(*, days: int = 30, format: str = "all", now: datetime | None = None,
                                _roster: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Read selected active dashboard benchmarks without scraping or writes.

    Publication dates select the sample; each counter is its latest stored
    cumulative value. Every account remains in the result even if its sample
    or follower measurement is unavailable.
    """
    if days not in {0, 30, 90} or format not in {"all", *_FORMATS}:
        raise ValueError("Unsupported engagement window or format")
    current = _date(now) if now is not None else datetime.now(UTC)
    if current is None:
        raise ValueError("Invalid report time")
    cutoff = current - timedelta(days=days) if days else None
    roster = _engagement_roster() if _roster is None else _roster
    handles = [account["handle"].lower() for account in roster]
    snapshots: dict[str, list[dict[str, Any]]] = defaultdict(list)
    accounts = []
    with db.connect() as conn:
        columns = {table: _columns(conn, table) for table in ("dashboard_posts", "posts", "engagement_observations", "account_snapshots")}
        if handles and columns["account_snapshots"]:
            rows = conn.execute(
                f"SELECT handle, followers_count, captured_at FROM account_snapshots WHERE handle IN ({','.join('?' for _ in handles)})",
                handles,
            ).fetchall()
            for row in rows:
                captured = _date(row["captured_at"])
                if captured is not None and captured <= current:
                    snapshots[str(row["handle"]).lower()].append(dict(row))
        for account in roster:
            handle = account["handle"].lower()
            benchmark = _account_benchmark(conn, account, columns, cutoff, current, format)
            latest = max((row for row in snapshots[handle] if _number(row.get("followers_count")) is not None),
                         key=lambda row: _date(row["captured_at"]), default=None)
            followers = _number(latest["followers_count"]) if latest else None
            average = benchmark["average_engagements"]
            accounts.append({
                "handle": handle, "label": account.get("label") or handle,
                "group": account.get("group"), "subcategory": account.get("subcategory"),
                "has_avatar": bool(account.get("has_avatar")),
                "followers": followers, "followers_captured_at": latest["captured_at"] if latest else None,
                "engagement_rate_pct": average / followers * 100 if followers and average is not None else None,
                **benchmark,
            })
    return _report(accounts, days, format, current)


def _report(accounts: list[dict[str, Any]], days: int, format: str, current: datetime) -> dict[str, Any]:
    cutoff = current - timedelta(days=days) if days else None
    return {
        "accounts": accounts, "generated_at": current.isoformat(timespec="seconds"), "format": format,
        "period": {"days": days, "from": cutoff.isoformat(timespec="seconds") if cutoff else None, "to": current.isoformat(timespec="seconds")},
        "methodology": {
            "engagement_components": ["likes", "comments"],
            "complete_engagement_components": ["likes", "comments", "shares", "saves"],
            "engagement_population": "posts_with_known_likes_and_comments",
            "likes_minimum": 4, "followers_basis": "latest_usable_stored_snapshot",
            "post_date_basis": "publication_date", "metrics_basis": "latest_stored_cumulative_measurements",
            "metric_freshness_basis": "provider_observation_or_enrichment_time_when_known",
            "account_scope": "active_dashboard_accounts", "missing_metrics": "unavailable_not_zero",
            "excluded_posts": ["future", "hidden", "deleted", "unpublished_analyses"],
        },
    }


def engagement_accounts(*, now: datetime | None = None) -> dict[str, Any]:
    """List every active dashboard account, including Leads, without metrics."""
    current = _date(now) if now is not None else datetime.now(UTC)
    return {"accounts": [
        {name: account.get(name) for name in ("handle", "label", "group", "subcategory", "has_avatar")}
        for account in _engagement_roster()
    ], "generated_at": current.isoformat(timespec="seconds")}


def build_promos_engagement(*, days: int = 30, format: str = "all", now: datetime | None = None,
                            accounts: list[str] | None = None) -> dict[str, Any]:
    """Reuse selected dashboard accounts' calculations for exactly seven days."""
    if days not in {0, 30, 90} or format not in {"all", *_FORMATS}:
        raise ValueError("Unsupported engagement window or format")
    current = _date(now) if now is not None else datetime.now(UTC)
    if current is None:
        raise ValueError("Invalid report time")
    roster = _engagement_roster()
    if accounts is not None:
        requested = {value.strip().lstrip("@").lower() for value in accounts}
        allowed = {account["handle"].lower() for account in roster}
        if requested - allowed:
            raise HTTPException(status_code=422, detail="One or more selected accounts are unavailable in the dashboard.")
        roster = [account for account in roster if account["handle"].lower() in requested]
    handles = [account["handle"].lower() for account in roster]
    with db.connect() as conn:
        if not _columns(conn, "promos_engagement_cache"):
            benchmark_cache.ensure_schema(conn)
        cached = benchmark_cache.load(conn, handles, days, format, current)
    missing = [account for account in roster if account["handle"].lower() not in cached]
    recalculated = {}
    if missing:
        fresh = calculate_promos_engagement(days=days, format=format, now=current, _roster=missing)
        recalculated = {account["handle"]: account for account in fresh["accounts"]}
        with db.connect() as conn:
            for handle, account in recalculated.items():
                benchmark_cache.save(conn, handle, days, format, current, account)
    result = []
    for account in roster:
        handle = account["handle"].lower()
        from_cache = handle in cached
        measured, calculated = cached[handle] if from_cache else (recalculated[handle], current)
        result.append({
            **{name: measured[name] for name in benchmark_cache.FIELDS},
            "handle": handle, "label": account.get("label") or handle,
            "group": account.get("group"), "subcategory": account.get("subcategory"),
            "has_avatar": bool(account.get("has_avatar")),
            "calculated_at": calculated.isoformat(),
            "cache_expires_at": (calculated + benchmark_cache.TTL).isoformat(),
            "calculation_period": {"days": days, "from": (calculated - timedelta(days=days)).isoformat() if days else None,
                                   "to": calculated.isoformat()},
            "from_cache": from_cache,
        })
    report = _report(result, days, format, current)
    report["cache"] = {"ttl_days": 7, "cached_count": len(cached), "recalculated_count": len(recalculated)}
    return report
