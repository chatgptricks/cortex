"""Comparable account benchmarks for Promos, from stored measurements only."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from . import db
from .account_media_kit import (
    _bool,
    _columns,
    _compact_public_rows,
    _date,
    _json,
    _normalize_posts,
    _number,
    _public_observation_rows,
    _raw_metrics,
)
from .apify_sync import list_accounts

_METRICS = ("likes", "comments", "shares", "saves", "impressions", "video_views", "video_plays")
_POST_FIELDS = {
    "id", "account", "shortcode", "section", "published_at", "updated_at", "enriched_at",
    "post_type_label", "product_type", "is_animated", "slide_count", "raw_json", "hidden", "is_deleted",
    *_METRICS,
}
_FORMATS = {"reel": "Reel", "carousel": "Carousel", "image": "Image"}


def _rows(conn: Any, table: str, handles: list[str] | None) -> list[dict[str, Any]]:
    columns = _columns(conn, table)
    selected = sorted(columns & _POST_FIELDS)
    if not selected:
        return []
    scope, parameters = "", []
    if handles is not None:
        if not handles or "account" not in columns:
            return []
        scope = f" WHERE LOWER(account) IN ({','.join('?' for _ in handles)})"
        parameters = handles
    order = "id" if "id" in columns else "shortcode"
    rows = _compact_public_rows(conn, table, selected, scope, parameters, order=order)
    for row in rows:
        # Legacy labels carry a product-type suffix. It is metadata, not a
        # separate content format (for example "Carousel (carousel_container)").
        if isinstance(row.get("post_type_label"), str):
            row["post_type_label"] = row["post_type_label"].split(" (", 1)[0].strip()
    return [row | {"_table": table} for row in rows]


def _measurement(value: Any, *, likes: bool = False) -> int | float | None:
    known = _number(value)
    # Research treats 0–3 as unavailable Instagram like readings. Apply the
    # same rule here so a hidden count cannot become a cheap CPE estimate.
    return known if known is not None and (not likes or known > 3) else None


def _stats(values: list[int | float], population: int) -> dict[str, Any]:
    return {
        "count": len(values),
        "total": sum(values) if values else None,
        "average": sum(values) / len(values) if values else None,
        "coverage_pct": len(values) / population * 100 if population else 0,
    }


def _measured_posts(records: list[dict[str, Any]], observations: dict[str, dict[str, Any]],
                    handle: str, current: datetime) -> list[dict[str, Any]]:
    """Keep metadata edits from outranking actual metric observations.

    The shared normalizer also serves historical reports whose row ordering
    uses generic updated_at. That timestamp changes on OCR, transcript and
    cover edits, so it cannot establish metric freshness for this calculator.
    Use timestamped provider observations/enrichment where available, retain
    unstamped promoted counters as a fallback, and never label them fresh.
    """
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        by_code[str(row.get("shortcode") or "").strip()].append(row)
    usable_observations = {}
    for code in by_code:
        observation = observations.get(code)
        if observation is None:
            continue
        observed = _date(observation.get("observed_at"))
        if observed is not None and observed <= current and _json(observation.get("raw_json")).get("shortCode", code) == code:
            usable_observations[code] = observation
    normalized, _ = _normalize_posts(records, usable_observations, handle)
    for post in normalized:
        code = post["shortcode"] or ""
        copies = by_code[code]
        sources: list[tuple[datetime, int, dict[str, Any]]] = []
        for row in copies:
            enriched = _date(row.get("enriched_at"))
            if enriched is not None and enriched <= current:
                sources.append((enriched, 0, _raw_metrics(_json(row.get("raw_json")))))
        observation = usable_observations.get(code)
        if observation:
            observed = _date(observation.get("observed_at"))
            payload = _json(observation.get("raw_json"))
            if observed is not None and observed <= current and payload.get("shortCode", code) == code:
                sources.append((observed, 1, _raw_metrics(payload)))
        sources.sort(key=lambda source: (source[0], source[1]), reverse=True)
        stamps = []
        for name in _METRICS:
            source = next((source for source in sources if name in source[2]), None)
            if source is None:
                continue
            measured_at, _, metrics = source
            # A later hidden/null promoted count may represent a real refresh
            # whose observation was not retained. Since updated_at cannot tell
            # that from a metadata edit, preserve unavailable core counts
            # rather than resurrecting an older visible measurement.
            newer_unavailable = name in {"likes", "comments"} and any(
                name in row and _measurement(row[name], likes=name == "likes") is None
                and (updated := _date(row.get("updated_at"))) is not None and updated > measured_at
                for row in copies
            )
            post["metrics"][name] = None if newer_unavailable else metrics[name]
            stamps.append(measured_at)
        post["metrics_updated_at"] = max(stamps).isoformat(timespec="seconds") if stamps else None
    return normalized


def build_promos_engagement(*, days: int = 30, format: str = "all", now: datetime | None = None) -> dict[str, Any]:
    """Read a compact, complete roster projection without scraping or writes.

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
    roster = list_accounts(active_only=True, promos_only=True)
    handles = [account["handle"].lower() for account in roster]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    observations: dict[str, dict[str, Any]] = {}
    snapshots: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with db.connect() as conn:
        for row in _rows(conn, "dashboard_posts", handles):
            grouped[str(row.get("account") or "").lower()].append(row)
        canonical = [account["handle"].lower() for account in roster if account.get("is_canonical")]
        if canonical:
            canonical_rows = _rows(conn, "posts", None)
            for handle in canonical:
                grouped[handle].extend(canonical_rows)
        if handles and _columns(conn, "account_snapshots"):
            rows = conn.execute(
                f"SELECT handle, followers_count, captured_at FROM account_snapshots WHERE LOWER(handle) IN ({','.join('?' for _ in handles)})",
                handles,
            ).fetchall()
            for row in rows:
                captured = _date(row["captured_at"])
                if captured is not None and captured <= current:
                    snapshots[str(row["handle"]).lower()].append(dict(row))
        if _columns(conn, "engagement_observations"):
            codes = sorted({str(row["shortcode"]) for rows in grouped.values() for row in rows if row.get("shortcode")})
            for start in range(0, len(codes), 200):
                observations.update({row["shortcode"]: row for row in _public_observation_rows(conn, codes[start:start + 200])})

    accounts = []
    for account in roster:
        handle = account["handle"].lower()
        records = grouped.get(handle, [])
        excluded_codes = {row["shortcode"] for row in records if row.get("shortcode") and (_bool(row.get("hidden")) or _bool(row.get("is_deleted")))}
        normalized = _measured_posts(records, observations, handle, current)
        posts = []
        for post in normalized:
            published = _date(post["published_at"])
            if post["hidden"] or post["is_deleted"] or post["shortcode"] in excluded_codes:
                continue
            if published is not None and (published > current or (cutoff is not None and published < cutoff)):
                continue
            if published is None and cutoff is not None:
                continue
            if format != "all" and post["format"] != _FORMATS[format]:
                continue
            posts.append(post)

        sample = [
            {name: _measurement(post["metrics"].get(name), likes=name == "likes") for name in _METRICS}
            for post in posts
        ]
        metrics = {name: _stats([row[name] for row in sample if row[name] is not None], len(sample)) for name in _METRICS}
        core = [row["likes"] + row["comments"] for row in sample if row["likes"] is not None and row["comments"] is not None]
        complete = [sum(row[name] for name in ("likes", "comments", "shares", "saves"))
                    for row in sample if all(row[name] is not None for name in ("likes", "comments", "shares", "saves"))]
        engagement = _stats(core, len(sample))
        full_engagement = _stats(complete, len(sample))
        latest = max((row for row in snapshots[handle] if _number(row.get("followers_count")) is not None),
                     key=lambda row: _date(row["captured_at"]), default=None)
        followers = _number(latest["followers_count"]) if latest else None
        dates = [_date(post["published_at"]) for post in posts if _date(post["published_at"]) is not None]
        freshness = [_date(post["metrics_updated_at"]) for post in posts if _date(post["metrics_updated_at"]) is not None]
        average = engagement["average"]
        accounts.append({
            "handle": handle, "label": account.get("label") or handle,
            "group": account.get("group"), "subcategory": account.get("subcategory"),
            "has_avatar": bool(account.get("has_avatar")),
            "followers": followers, "followers_captured_at": latest["captured_at"] if latest else None,
            "post_count": len(posts), "undated_post_count": sum(_date(post["published_at"]) is None for post in posts),
            "engagement_post_count": engagement["count"], "average_engagements": average,
            "engagement_coverage_pct": engagement["coverage_pct"],
            "engagement_rate_pct": average / followers * 100 if followers and average is not None else None,
            "complete_engagement_post_count": full_engagement["count"],
            "average_complete_engagements": full_engagement["average"],
            "complete_engagement_coverage_pct": full_engagement["coverage_pct"],
            "metrics": metrics,
            "first_post_at": min(dates).isoformat(timespec="seconds") if dates else None,
            "last_post_at": max(dates).isoformat(timespec="seconds") if dates else None,
            "metrics_updated_at": max(freshness).isoformat(timespec="seconds") if freshness else None,
        })
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
            "account_scope": "active_promos_enabled", "missing_metrics": "unavailable_not_zero",
            "excluded_posts": ["future", "hidden", "deleted", "unpublished_analyses"],
        },
    }
