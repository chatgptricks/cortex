"""Fresh, read-only account reports from the data we already have.

All totals describe the stored historical sample, never unique reach or a
complete Instagram lifetime. No report operation refreshes or scrapes data.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone
from statistics import median
from typing import Any

from fastapi import HTTPException

from . import db

_TZ = timezone(timedelta(hours=-6))
_METRICS = {
    "likes": ("Likes", "count"),
    "comments": ("Comments", "count"),
    "video_views": ("Video views", "count"),
    "video_plays": ("Video plays", "count"),
    "likes_at_1h": ("Likes at first-hour check", "count"),
    "likes_at_8h": ("Likes at 8-hour check", "count"),
    "comments_at_8h": ("Comments at 8-hour check", "count"),
    "likes_at_24h": ("Likes at 24-hour check", "count"),
    "comments_at_24h": ("Comments at 24-hour check", "count"),
    "likes_at_48h": ("Likes at 48-hour check", "count"),
    "comments_at_48h": ("Comments at 48-hour check", "count"),
    "shares": ("Shares", "count"),
    "saves": ("Saves", "count"),
    "reach": ("Post reach", "count"),
    "impressions": ("Impressions", "count"),
    "clicks": ("Clicks", "count"),
    "profile_visits": ("Profile visits", "count"),
    "follows": ("Follows reported by provider", "count"),
    "video_duration": ("Video duration", "seconds"),
    "slide_count": ("Stored media items (slides / previews)", "count"),
    "hot_rate_multiplier": ("First-hour pace multiplier", "ratio"),
    "brain_global_mean_abs": ("Model attention mean", "model_score"),
    "brain_global_peak_abs": ("Model attention peak", "model_score"),
    "virality_potential": ("Model virality score", "model_score"),
    "carousel_video_slides": ("Carousel video slides", "count"),
    "carousel_slide_likes": ("Carousel slide likes (separate from parent)", "count"),
    "carousel_slide_comments": ("Carousel slide comments (separate from parent)", "count"),
    "carousel_slide_video_views": ("Carousel slide video views (separate from parent)", "count"),
    "carousel_slide_video_plays": ("Carousel slide video plays (separate from parent)", "count"),
    "carousel_slide_video_duration": ("Carousel slide video duration", "seconds"),
    "carousel_slide_likes_measured_slides": ("Carousel slides with measured likes", "count"),
    "carousel_slide_comments_measured_slides": ("Carousel slides with measured comments", "count"),
    "carousel_slide_video_views_measured_slides": ("Carousel slides with measured video views", "count"),
    "carousel_slide_video_plays_measured_slides": ("Carousel slides with measured video plays", "count"),
    "carousel_slide_video_duration_measured_slides": ("Carousel slides with measured video duration", "count"),
}
_ALIASES = {
    "likesCount": "likes", "likeCount": "likes", "like_count": "likes",
    "commentsCount": "comments", "commentCount": "comments", "comment_count": "comments",
    "videoViewCount": "video_views", "viewCount": "video_views", "video_view_count": "video_views",
    "videoPlayCount": "video_plays", "igPlayCount": "video_plays", "playCount": "video_plays",
    "videoDuration": "video_duration", "duration": "video_duration",
    "sharesCount": "shares", "shareCount": "shares", "share_count": "shares",
    "savesCount": "saves", "saveCount": "saves", "savedCount": "saves", "save_count": "saves",
    "reachCount": "reach", "impressionsCount": "impressions", "impressionCount": "impressions",
    "clicksCount": "clicks", "profileVisits": "profile_visits", "profileVisitsCount": "profile_visits",
    "followsCount": "follows",
}
_METADATA = {
    "id", "shortcode", "published_at", "updated_at", "created_at", "enriched_at",
    "caption", "title", "hook_text", "post_type_label", "product_type", "is_animated",
    "permalink", "image_path", "cover_image_path", "video_path", "raw_json", "analysis_summary",
    "hashtags", "mentions", "coauthors", "tagged_users", "music_song", "music_artist",
    "music_audio_id", "uses_original_audio", "paid_partnership", "dimensions", "transcript",
    "alt_text", "owner_full_name", "is_hot", "is_promo", "hidden", "is_deleted", "section",
}
_PROFILE_FIELDS = {
    "biography": "bio", "bio": "bio", "email": "email", "business_email": "email",
    "phone": "phone", "business_phone_number": "phone", "external_url": "website",
    "website": "website", "business_category_name": "business_category",
    "businessCategoryName": "business_category", "demographics": "demographics",
    "audience_demographics": "demographics", "country": "country", "language": "language",
    "city": "city", "business_address": "business_address",
}


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return (result if result.tzinfo else result.replace(tzinfo=UTC)).astimezone(UTC)


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _number(value: Any) -> int | float | None:
    # Negative likes are Instagram's hidden/unavailable sentinel, not zero.
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number) if number.is_integer() else number


def _snake(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value).lower()).strip("_")


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        if value.strip().lower() in {"false", "0", "no", ""}:
            return False
        if value.strip().lower() in {"true", "1", "yes"}:
            return True
        return None
    return bool(value)


def _performance_field(name: str) -> bool:
    normalized = _snake(name)
    if normalized in {"first_comment", "comments_disabled", "comments_disabled_at"} or normalized.endswith(("_id", "_ids", "_url", "_urls", "_at", "_date", "_timestamp")):
        return False
    return bool(re.search(r"(?:^|_)(?:likes?|comments?|views?|plays?|shares?|saves?|reach|impressions?|clicks?|engagement|duration|watch_time|retention|completion|replays?|profile_visits|follows)(?:_|$)", normalized))


def _raw_metrics(raw: dict[str, Any]) -> dict[str, int | float | None]:
    result: dict[str, int | float | None] = {}

    def visit(obj: dict[str, Any], prefix: str = "", depth: int = 0) -> None:
        for name, value in obj.items():
            # Nested commenter/owner metrics describe somebody else. Only
            # descend into an explicit performance container.
            if isinstance(value, dict) and depth < 3 and name.lower() in {
                "metrics", "insights", "statistics", "stats", "video", "video_info", "engagement",
            }:
                visit(value, prefix + _snake(name) + ".", depth + 1)
            elif not isinstance(value, (dict, list, bool)) and _performance_field(name):
                key = _ALIASES.get(name)
                if key is None:
                    normalized = _snake(name)
                    key = normalized if normalized in _METRICS else "provider." + prefix + normalized
                number = _number(value)
                if number is None and key not in _METRICS:
                    continue
                if key not in result or result[key] is None:
                    result[key] = number

    visit(raw)
    children = raw.get("childPosts")
    if isinstance(children, list) and children:
        result["slide_count"] = len(children)
        child_records = [child for child in children if isinstance(child, dict)]
        result["carousel_video_slides"] = sum(str(child.get("type") or "").lower().startswith("video") for child in child_records)
        child_metrics = [_raw_metrics(child) for child in child_records]
        names = {"likes", "comments", "video_views", "video_plays", "video_duration"} | {name for metrics in child_metrics for name in metrics if not name.startswith("carousel_") and name != "slide_count"}
        for name in names:
            values = [value for metrics in child_metrics if (value := metrics.get(name)) is not None]
            prefixed = "carousel_slide_" + name
            # Counts and duration are sums of separately observed slide
            # counters. A rate/ratio is averaged across measured slides.
            unit = _metric_unit(name)
            result[prefixed] = (sum(values) / len(values) if unit in {"ratio", "percent", "model_score"} else sum(values)) if values else None
            result[prefixed + "_measured_slides"] = len(values)
    return result


def _metric_unit(name: str) -> str:
    if name in _METRICS:
        return _METRICS[name][1]
    if name.endswith("_measured_slides"):
        return "count"
    if name.startswith("analysis."):
        return "model_score"
    if any(word in name for word in ("rate", "percent", "pct", "retention", "completion")):
        return "percent" if "percent" in name or "pct" in name else "ratio"
    if name.endswith(("_seconds", "_sec")):
        return "seconds"
    if name.endswith(("_milliseconds", "_ms")):
        return "milliseconds"
    if name.endswith(("_count", "_number")):
        return "count"
    return "provider_value"


def _columns(conn: Any, table: str) -> set[str]:
    # PRAGMA is translated by the existing Postgres adapter. No migration,
    # table creation or initialization is allowed in this report read.
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _post_rows(conn: Any, table: str, handle: str, canonical: bool) -> list[dict[str, Any]]:
    columns = _columns(conn, table)
    selected = sorted(columns & _METADATA | {key for key in columns if key in _METRICS or _performance_field(key)})
    if not selected:
        return []
    scope = "" if table == "posts" and canonical else " WHERE LOWER(account) = ?"
    query = f"SELECT {', '.join(selected)} FROM {table}{scope}"
    return [dict(row) | {"_table": table} for row in conn.execute(query, () if not scope else (handle,)).fetchall()]


def _row_quality(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _date(row.get("updated_at")) or datetime.min.replace(tzinfo=UTC),
        bool(_date(row.get("published_at"))),
        sum(row.get(key) is not None for key in _METRICS),
        row.get("_table") == "posts", row.get("id") or 0,
    )


def _normalize_posts(rows: list[dict[str, Any]], observations: dict[str, dict[str, Any]], handle: str) -> tuple[list[dict[str, Any]], int]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = str(row.get("shortcode") or "").strip()
        # AB tests and unpublished uploaded single analyses are not account
        # posts. A shortcode-less historical import remains a real sample.
        if row.get("_table") == "posts" and (row.get("section") == "ab" or (not code and not _date(row.get("published_at")))):
            continue
        key = code or f"{row['_table']}:{row.get('id')}"
        grouped.setdefault(key, []).append(row)
    posts: list[dict[str, Any]] = []
    for records in grouped.values():
        records.sort(key=_row_quality, reverse=True)
        # The public cover route chooses the canonical table by account.
        # Keep its row ID even when an enriched dashboard copy is newer.
        canonical_records = [record for record in records if record["_table"] == "posts"]
        preferred = max(canonical_records, key=lambda record: (bool(_date(record.get("published_at"))), _row_quality(record))) if canonical_records else records[0]
        merged: dict[str, Any] = {}
        metrics: dict[str, Any] = {}
        raw: dict[str, Any] = {}
        for record in records:
            for name, value in record.items():
                if name not in merged or merged[name] in (None, ""):
                    merged[name] = value
            payload = _json(record.get("raw_json"))
            if not raw:
                raw = payload
            for name, value in _raw_metrics(payload).items():
                if name not in metrics or metrics[name] is None:
                    metrics[name] = value
        # Promoted DB values are kept current by engagement refreshes even
        # when raw_json still describes the initial import.
        for name in set(_METRICS) | {key for key in merged if _performance_field(key)}:
            if name in merged:
                if name in {"likes", "comments"}:
                    authoritative = next((record[name] for record in records if name in record), None)
                    metrics[name] = _number(authoritative)
                else:
                    number = _number(merged[name])
                    if number is not None or name not in metrics:
                        metrics[name] = number
        code = str(merged.get("shortcode") or "").strip()
        observation = observations.get(code)
        updated = max((_date(r.get("updated_at")) for r in records if _date(r.get("updated_at"))), default=None)
        metric_at = updated
        if observation:
            observed_at = _date(observation.get("observed_at"))
            payload = _json(observation.get("raw_json"))
            if payload.get("shortCode", code) == code:
                # Preserve a hidden/null promoted reading; an older raw
                # observation must not resurrect a formerly visible count.
                for name, value in _raw_metrics(payload).items():
                    enriched_at = max((_date(r.get("enriched_at")) for r in records if _date(r.get("enriched_at"))), default=None)
                    promoted_value = _number(merged.get(name))
                    observation_is_newer = bool(observed_at and (not updated or observed_at >= updated))
                    enrichment_is_newer = bool(name not in {"likes", "comments"} and promoted_value is None and observed_at and (not enriched_at or observed_at >= enriched_at))
                    if observation_is_newer or enrichment_is_newer or name not in metrics or (metrics[name] is None and (name not in {"likes", "comments"} or name not in merged)):
                        metrics[name] = value
                if observed_at and (not metric_at or observed_at > metric_at):
                    metric_at = observed_at
                if observed_at and (not updated or observed_at >= updated):
                    raw = {**raw, **payload}
        analysis = _json(merged.get("analysis_summary"))
        for name, value in _json(analysis.get("metrics")).items():
            number = _number(value)
            if number is not None:
                metrics.setdefault("analysis." + _snake(name), number)
        product = str(merged.get("product_type") or raw.get("productType") or "").lower()
        kind = str(merged.get("post_type_label") or raw.get("type") or "Image")
        if product in {"clips", "reels", "reel"}:
            kind = "Reel"
        elif kind.lower() in {"sidecar", "carousel"} or (metrics.get("slide_count") or 0) > 1:
            kind = "Carousel"
        elif kind.lower().startswith("video") or merged.get("is_animated"):
            kind = "Video"
        else:
            kind = "Image"
        likes, comments = metrics.get("likes"), metrics.get("comments")
        known = [value for value in (likes, comments) if value is not None]
        music = _json(raw.get("musicInfo"))
        raw_coauthors = raw.get("coauthorProducers")
        raw_tagged = raw.get("taggedUsers")
        posts.append({
            "id": preferred.get("id"), "shortcode": code or None,
            "published_at": merged.get("published_at"),
            "permalink": merged.get("permalink") or (f"https://www.instagram.com/p/{code}/" if code else None),
            "cover_url": f"/api/dashboard/covers/{handle}/{preferred.get('id')}",
            "cover_path": merged.get("image_path") or merged.get("cover_image_path"),
            "local_media_path": merged.get("image_path") or merged.get("cover_image_path"),
            "caption": merged.get("caption") or merged.get("title") or "",
            "hook_text": merged.get("hook_text") or "", "format": kind,
            "metrics": metrics, "engagements": sum(known) if known else None,
            "engagement_complete": likes is not None and comments is not None,
            "metrics_updated_at": metric_at.isoformat(timespec="seconds") if metric_at else None,
            "is_hot": bool(_bool(merged.get("is_hot"))), "is_promo": bool(_bool(merged.get("is_promo"))),
            "hidden": bool(_bool(merged.get("hidden"))), "is_deleted": bool(_bool(merged.get("is_deleted"))),
            "hashtags": merged.get("hashtags") or raw.get("hashtags"),
            "mentions": merged.get("mentions") or raw.get("mentions"),
            "coauthors": merged.get("coauthors") or raw_coauthors, "tagged_users": merged.get("tagged_users") or raw_tagged,
            "music_song": merged.get("music_song") or music.get("song_name"), "music_artist": merged.get("music_artist") or music.get("artist_name"),
            "uses_original_audio": _bool(merged.get("uses_original_audio") if merged.get("uses_original_audio") is not None else music.get("uses_original_audio")),
            "paid_partnership": _bool(merged.get("paid_partnership") if merged.get("paid_partnership") is not None else raw.get("isPaidPartnership")), "dimensions": merged.get("dimensions"),
            "has_transcript": bool(merged.get("transcript")), "has_alt_text": bool(merged.get("alt_text")),
        })
    return posts, len(rows) - len(posts)


def _stats(values: list[Any], population: int, unit: str = "count") -> dict[str, Any]:
    known = [number for value in values if (number := _number(value)) is not None]
    return {
        "total": sum(known) if known and unit not in {"ratio", "percent", "model_score"} else None,
        "average": sum(known) / len(known) if known else None,
        "median": median(known) if known else None, "min": min(known) if known else None,
        "max": max(known) if known else None, "count": len(known),
        "coverage_pct": len(known) / population * 100 if population else 0,
    }


def _catalog(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = list(_METRICS) + sorted({name for post in posts for name in post["metrics"]} - set(_METRICS))
    result = []
    for name in names:
        label, unit = _METRICS.get(name, (name.replace("provider.", "").replace("analysis.", "Model ").replace("_", " ").replace(".", " / ").title(), "provider_value"))
        unit = _metric_unit(name)
        source = "stored carousel slide measurements" if name.startswith("carousel_") else "model" if name.startswith("analysis.") or name in {"brain_global_mean_abs", "brain_global_peak_abs", "virality_potential"} else "stored Instagram measurements"
        result.append({"key": name, "label": label, "unit": unit, "source": source})
    return result


def _period(posts: list[dict[str, Any]], catalog: list[dict[str, Any]], followers: Any, days: float | None) -> dict[str, Any]:
    n = len(posts)
    cadence_count = sum(_date(post.get("published_at")) is not None for post in posts)
    measurements = {metric["key"]: _stats([post["metrics"].get(metric["key"]) for post in posts], n, metric["unit"]) for metric in catalog}
    engagement = _stats([post["engagements"] for post in posts], n)
    complete = sum(post["engagement_complete"] for post in posts)
    likes = measurements["likes"]["average"]
    views = measurements["video_views"]["average"]
    return {
        "post_count": n, "posts_per_week": cadence_count / days * 7 if days else None,
        "cadence_post_count": cadence_count,
        "metrics": measurements, "engagements": engagement,
        "complete_engagement_posts": complete,
        "engagement_rate_pct": engagement["average"] / followers * 100 if followers and engagement["average"] is not None else None,
        "like_rate_pct": likes / followers * 100 if followers and likes is not None else None,
        "view_rate_pct": views / followers * 100 if followers and views is not None else None,
        "hot_posts": sum(post["is_hot"] for post in posts),
        "promo_posts": sum(post["is_promo"] for post in posts),
        "paid_partnership_posts": sum(bool(post["paid_partnership"]) for post in posts),
        "deleted_posts": sum(post["is_deleted"] for post in posts),
        "hidden_posts": sum(post["hidden"] for post in posts),
    }


def _history(snapshots: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_day = {}
    usable_by_day = {}
    for snap in sorted(snapshots, key=lambda item: _date(item.get("captured_at")) or datetime.min.replace(tzinfo=UTC)):
        captured = _date(snap.get("captured_at"))
        if captured:
            day = captured.astimezone(_TZ).date()
            by_day[day] = snap
            if _number(snap.get("followers_count")) is not None:
                usable_by_day[day] = snap
    daily = sorted(by_day.items())
    history = []
    previous = None
    for day, snap in daily:
        value = _number(snap.get("followers_count"))
        history.append({"date": snap["captured_at"], "local_date": day.isoformat(), "followers": value,
                        "following": _number(snap.get("following_count")), "profile_posts": _number(snap.get("posts_count")),
                        "delta": value - previous if value is not None and previous is not None else None,
                        "full_name": snap.get("full_name"), "verified": _bool(snap.get("verified")),
                        "private": _bool(snap.get("private"))})
        if value is not None:
            previous = value
    usable = sorted(usable_by_day.items())
    growth = {}
    for days in (1, 7, 30, 90):
        baseline = None
        latest = usable[-1] if usable else None
        if latest:
            cutoff = latest[0] - timedelta(days=days)
            baseline = next((item for item in reversed(usable) if item[0] <= cutoff), None)
            if days == 1 and baseline and baseline[0] != cutoff:
                baseline = None
        growth[f"{days}d"] = _growth(baseline, latest, days)
    growth["all_time"] = _growth(usable[0] if len(usable) > 1 else None, usable[-1] if usable else None, None)
    return history, growth


def _growth(baseline: Any, latest: Any, days: int | None) -> dict[str, Any] | None:
    if not baseline or not latest or baseline is latest:
        return None
    start = _number(baseline[1].get("followers_count"))
    end = _number(latest[1].get("followers_count"))
    delta = end - start
    return {"delta": delta, "pct": delta / start * 100 if start else None,
            "from": baseline[1]["captured_at"], "to": latest[1]["captured_at"],
            "observed_days": (latest[0] - baseline[0]).days, "requested_days": days}


def _terms(value: Any) -> list[str]:
    if isinstance(value, list):
        result = []
        for term in value:
            text = term.get("username") if isinstance(term, dict) else term
            if text is not None and str(text).strip():
                result.append(str(text).strip().lstrip("#@"))
        return result
    if not value:
        return []
    if isinstance(value, str) and value.startswith("["):
        try:
            return _terms(json.loads(value))
        except ValueError:
            pass
    return [term.strip().lstrip("#@") for term in re.split(r"[\s,]+", str(value)) if term.strip().lstrip("#@")]


def _term_counts(posts: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    def terms(post: dict[str, Any]) -> list[str]:
        value = post.get(key)
        # Song and artist names are names, never tokenized words.
        if key in {"music_song", "music_artist"}:
            return [str(value).strip()] if value and str(value).strip() else []
        return _terms(value)
    counts = Counter(term for post in posts for term in set(terms(post)))
    return [{"label": label, "post_count": count} for label, count in counts.most_common()]


def _top(posts: list[dict[str, Any]], key: str = "engagements", limit: int = 6) -> list[dict[str, Any]]:
    def value(post: dict[str, Any]) -> Any:
        return post.get(key) if key == "engagements" else post["metrics"].get(key)
    eligible = [post for post in posts if value(post) is not None and not post["is_deleted"] and not post["hidden"]]
    ranked = sorted(eligible, key=lambda post: (value(post), post["metrics"].get("likes") or 0, _date(post["published_at"]) or datetime.min.replace(tzinfo=UTC)), reverse=True)
    return [{**post, "caption": post["caption"][:500], "hook_text": post["hook_text"][:240], "rank_metric": key, "rank_value": value(post)} for post in ranked[:limit]]


def build_account_media_kit(handle: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Read an account's current stored data on every invocation."""
    clean = handle.strip().lstrip("@").lower()
    current = now or datetime.now(UTC)
    current = (current if current.tzinfo else current.replace(tzinfo=UTC)).astimezone(UTC)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE LOWER(handle) = ?", (clean,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Unknown account.")
        registry = dict(row)
        canonical = bool(registry.get("is_canonical"))
        rows = _post_rows(conn, "dashboard_posts", clean, canonical)
        if canonical:
            rows += _post_rows(conn, "posts", clean, True)
        snapshots = [dict(row) for row in conn.execute("SELECT * FROM account_snapshots WHERE LOWER(handle) = ? ORDER BY captured_at", (clean,)).fetchall()] if _columns(conn, "account_snapshots") else []
        observations: dict[str, dict[str, Any]] = {}
        if _columns(conn, "engagement_observations"):
            codes = sorted({str(row.get("shortcode")) for row in rows if row.get("shortcode")})
            for start in range(0, len(codes), 200):
                batch = codes[start:start + 200]
                data = conn.execute(f"SELECT shortcode, observed_at, raw_json FROM engagement_observations WHERE shortcode IN ({','.join('?' for _ in batch)})", batch).fetchall()
                observations.update({row["shortcode"]: dict(row) for row in data})
    posts, duplicates = _normalize_posts(rows, observations, clean)
    valid = [(post, _date(post["published_at"])) for post in posts]
    dated = [(post, date) for post, date in valid if date and date <= current]
    # All history includes undated historical imports. Future-dated rows are
    # excluded everywhere, and dates outside a window never enter that window.
    all_posts = [post for post, date in valid if date is None or date <= current]
    recent = [post for post, date in dated if current - timedelta(days=30) <= date]
    prior = [post for post, date in dated if current - timedelta(days=60) <= date < current - timedelta(days=30)]
    ninety = [post for post, date in dated if current - timedelta(days=90) <= date]
    snapshots = sorted([snap for snap in snapshots if (captured := _date(snap.get("captured_at"))) and captured <= current], key=lambda snap: _date(snap["captured_at"]))
    latest = next((snap for snap in reversed(snapshots) if _number(snap.get("followers_count")) is not None), None)
    newest = snapshots[-1] if snapshots else {}
    followers = _number(latest.get("followers_count")) if latest else None
    account = {
        "handle": clean, "label": registry.get("label") or clean,
        "name": newest.get("full_name") or (latest or {}).get("full_name") or registry.get("label") or clean,
        "platform": "Instagram", "profile_url": f"https://www.instagram.com/{clean}/",
        "avatar_url": f"/api/dashboard/avatar/{clean}" if registry.get("avatar_path") else None,
        "avatar_path": registry.get("avatar_path"),
        "group": registry.get("category") or registry.get("group_name"), "subcategory": registry.get("subcategory"),
        "is_active": bool(registry.get("is_active", True)), "followers": followers,
        "following": next((_number(snap.get("following_count")) for snap in reversed(snapshots) if _number(snap.get("following_count")) is not None), None),
        "profile_posts": next((_number(snap.get("posts_count")) for snap in reversed(snapshots) if _number(snap.get("posts_count")) is not None), None),
        "verified": _bool(newest.get("verified")),
        "private": _bool(newest.get("private")),
        "profile_captured_at": latest.get("captured_at") if latest else None,
        "created_at": registry.get("created_at"),
    }
    for source in (newest, registry):
        for field, target in _PROFILE_FIELDS.items():
            if source.get(field) not in (None, ""):
                account.setdefault(target, source[field])
    catalog = _catalog(all_posts)
    for post in all_posts:
        post["engagement_rate_pct"] = post["engagements"] / followers * 100 if followers and post["engagements"] is not None else None
    history, growth = _history(snapshots)
    oldest = min((date for _, date in dated), default=None)
    newest_post = max((date for _, date in dated), default=None)
    span_days = max(1, (current - oldest).total_seconds() / 86400) if oldest else None
    summary = {
        "all_time": _period(all_posts, catalog, followers, span_days),
        "last_30_days": _period(recent, catalog, followers, 30),
        "previous_30_days": _period(prior, catalog, followers, 30),
        "last_90_days": _period(ninety, catalog, followers, 90),
    }
    trend = {}
    for name in ("likes", "comments", "video_views", "video_plays"):
        a = summary["last_30_days"]["metrics"][name]["average"]
        b = summary["previous_30_days"]["metrics"][name]["average"]
        trend[name] = (a - b) / b * 100 if a is not None and b else None
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {"formats": {}, "weekdays": {}, "hours": {}, "months": {}}
    for post in all_posts:
        groups["formats"].setdefault(post["format"], []).append(post)
    for post, date in dated:
        local = date.astimezone(_TZ)
        for dimension, label in (("weekdays", local.strftime("%A")), ("hours", f"{local.hour:02d}:00"), ("months", local.strftime("%Y-%m"))):
            groups[dimension].setdefault(label, []).append(post)
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    breakdowns = {}
    for dimension, entries in groups.items():
        ordered = sorted(entries, key=lambda value: weekdays.index(value) if dimension == "weekdays" else value)
        breakdowns[dimension] = [{"label": label, "share_pct": len(entries[label]) / len(all_posts) * 100 if all_posts else 0, **_period(entries[label], catalog, followers, None)} for label in ordered]
    content = {key: _term_counts(all_posts, key) for key in ("hashtags", "mentions", "coauthors", "tagged_users", "music_song", "music_artist")}
    content["music"] = content["music_song"]
    content["metadata_counts"] = {key: sum(bool(_terms(post.get(key))) if key in {"hashtags", "mentions", "coauthors", "tagged_users"} else bool(post.get(key)) for post in all_posts) for key in ("hashtags", "mentions", "coauthors", "tagged_users", "music_song", "music_artist", "uses_original_audio", "paid_partnership", "dimensions", "has_transcript", "has_alt_text", "hook_text")}
    coverage = {
        "post_count": len(all_posts), "dated_posts": len(dated), "undated_posts": sum(date is None for _, date in valid),
        "future_posts_excluded": sum(bool(date and date > current) for _, date in valid),
        "duplicate_or_unpublished_rows_excluded": duplicates,
        "oldest_post_at": oldest.isoformat(timespec="seconds") if oldest else None,
        "newest_post_at": newest_post.isoformat(timespec="seconds") if newest_post else None,
        "last_metrics_update_at": max((post["metrics_updated_at"] for post in all_posts if post["metrics_updated_at"]), default=None),
        "snapshot_count": len(snapshots), "snapshot_days": len(history),
        "profile_snapshot_at": account["profile_captured_at"],
        "stored_vs_profile_posts_pct": len(all_posts) / account["profile_posts"] * 100 if account["profile_posts"] else None,
        "unavailable_metrics": [metric["label"] for metric in catalog if not summary["all_time"]["metrics"][metric["key"]]["count"]],
        "notes": [
            "All stored history covers only posts saved in Sentient Dash; it is not a guaranteed complete Instagram lifetime.",
            "Last 30 days selects posts published in the rolling 30-day window; their metrics are the latest stored cumulative readings, not engagement earned within those 30 days.",
            "Averages, medians and totals use known non-negative measurements only. Missing and hidden counts are unavailable, not zero.",
            "Engagements are measured likes plus comments. Partial readings use only the known components; complete sample counts are reported separately.",
            "Engagement rate is average measured likes plus comments divided by the current stored follower count, not reach or a historical follower count.",
            "Views and plays are separate measurements and are never added together. Post reach totals, when present, are non-deduplicated and not unique account reach.",
            "Carousel slide measurements remain separate from parent-post counters. Slide counts and durations are summed per parent post; slide rates are averaged. Measured-slide counts show the known subset and missing slide counters remain unavailable.",
            "Stored media item counts come from carousel children or the provider's images array. For Reels, images may be preview assets; this count does not establish carousel format or additional published posts.",
            "Follower growth compares final Costa Rica calendar-day snapshots against the latest usable snapshot; its actual baseline dates and duration are shown.",
            "Top posts rank by measured likes plus comments; hidden and deleted posts remain in historical totals but are excluded from showcase rankings.",
            "Posting-day and posting-hour patterns use Costa Rica time and indicate observed associations, not a guarantee of future performance.",
            "Posting cadence uses only posts with a valid publication date; undated historical samples remain in engagement totals.",
            "Model scores are internal analysis signals, not observed audience outcomes. Demographics and contact information are shown only if stored.",
            "This report queries existing data on every download and does not start a scrape or paid refresh.",
        ],
    }
    return {
        "schema_version": 1, "generated_at": current.isoformat(timespec="seconds"), "timezone": "America/Costa_Rica",
        "account": account, "summary": summary,
        "periods": {key: {"from": (current - timedelta(days=days)).isoformat(timespec="seconds") if days else coverage["oldest_post_at"], "to": (current - timedelta(days=30)).isoformat(timespec="seconds") if key == "previous_30_days" else current.isoformat(timespec="seconds")} for key, days in (("all_time", None), ("last_30_days", 30), ("previous_30_days", 60), ("last_90_days", 90))},
        "trends_pct": trend, "follower_history": history, "follower_growth": growth,
        "breakdowns": breakdowns, "content": content,
        "best_posts": {"all_time": _top(all_posts), "last_30_days": _top(recent), "by_metric": {key: {"all_time": _top(all_posts, key), "last_30_days": _top(recent, key)} for key in ("likes", "comments", "video_views", "video_plays")}},
        "coverage": coverage, "metric_catalog": catalog,
        "metrics_appendix": [{**metric, **{key: period["metrics"][metric["key"]] for key, period in summary.items()}} for metric in catalog],
    }
