"""Read-only, compact music catalogue over already retained provider data."""
from __future__ import annotations

from datetime import UTC, datetime
import math
from typing import Any


_FIELDS = (
    "id", "account", "shortcode", "section", "published_at", "updated_at", "enriched_at",
    "observed_at", "likes", "comments", "post_type_label", "product_type", "video_views",
    "video_plays", "video_duration", "slide_count", "music_song", "music_artist",
    "music_audio_id", "uses_original_audio", "is_hot", "hot_rate_multiplier", "permalink",
    "hidden", "is_deleted", "is_animated", "likes_at_24h", "likes_at_48h",
)
_RAW = {
    "r_song": "musicInfo.song_name", "r_artist": "musicInfo.artist_name",
    "r_audio": "musicInfo.audio_id", "r_original": "musicInfo.uses_original_audio",
    "r_code": "shortCode", "r_type": "type", "r_product": "productType",
    "r_likes": "likesCount", "r_comments": "commentsCount", "r_views": "videoViewCount",
    "r_plays": "videoPlayCount", "r_igplays": "igPlayCount", "r_duration": "videoDuration",
}
_WINDOW = 12_000


def _columns(conn: Any, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _date(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    return str(value).strip() or None if isinstance(value, (str, int)) and not isinstance(value, bool) else None


def _bool(value: Any) -> bool | None:
    if value is True or value == 1 or isinstance(value, str) and value.lower() in {"true", "1"}:
        return True
    if value is False or value == 0 or isinstance(value, str) and value.lower() in {"false", "0"}:
        return False
    return None


def _number(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return None
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError, OverflowError):
        return None


def _projection(conn: Any, has_raw: bool) -> str:
    postgres = bool(getattr(conn, "is_postgres", False))
    fields = []
    for alias, path in _RAW.items():
        if not has_raw:
            expression = "NULL"
        elif postgres:
            expression = "music_payload #>> '{" + path.replace(".", ",") + "}'"
        else:
            expression = f"CASE WHEN json_valid(raw_json) THEN json_extract(raw_json, '$.{path}') END"
        fields.append(f"{expression} AS {alias}")
    present = "jsonb_exists(music_payload, 'likesCount')" if postgres else "CASE WHEN json_valid(raw_json) THEN json_type(raw_json, '$.likesCount') IS NOT NULL ELSE 0 END"
    fields.append(f"{present if has_raw else '0'} AS r_has_likes")
    return ", ".join(fields)


def _read_source(conn: Any, table: str, columns: set[str], where: str = "1 = 1",
                 params: list[Any] | None = None) -> list[dict[str, Any]]:
    """Extract scalar facts in SQL; malformed legacy JSON never transfers blobs.

    A normal Postgres source is one batch. Only malformed batches are bisected,
    projecting null metadata for the individual bad row after a savepoint.
    """
    if "shortcode" not in columns:
        return []
    selected = [name for name in _FIELDS if name in columns]
    raw = "raw_json" in columns
    postgres = bool(getattr(conn, "is_postgres", False) and raw)
    raw_source = "raw_json"
    promoted_music = {"music_song", "music_artist", "music_audio_id", "uses_original_audio", "product_type"}
    if raw and table == "dashboard_posts" and promoted_music <= columns:
        # Fully promoted audio needs no second parse of a potentially huge
        # carousel payload. Unknown/incomplete audio still gets a stored-data
        # recovery attempt; absence of a provider field is never guessed.
        complete = " AND ".join(f"NULLIF(TRIM({name}), '') IS NOT NULL" for name in sorted(promoted_music - {"uses_original_audio"}))
        raw_source = f"CASE WHEN {complete} AND uses_original_audio IS NOT NULL THEN NULL ELSE raw_json END AS raw_json"
    source_fields = ", ".join([*selected, *([raw_source] if raw else [])])
    order = "id" if "id" in columns else "shortcode"
    source = f"(SELECT {source_fields} FROM {table} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?) music_rows"
    join = " CROSS JOIN LATERAL (SELECT NULLIF(TRIM(raw_json), '')::jsonb AS music_payload OFFSET 0) music_json" if postgres else ""
    projection = ", ".join(selected) + ", " + _projection(conn, raw)

    def window(offset: int, take: int) -> list[dict[str, Any]]:
        split = False
        if postgres:
            conn.execute("SAVEPOINT insights_music_json")
        try:
            rows = conn.execute(f"SELECT {projection} FROM {source}{join}", [*(params or []), take, offset]).fetchall()
        except Exception as error:
            if not postgres:
                raise
            conn.execute("ROLLBACK TO SAVEPOINT insights_music_json")
            if getattr(error, "sqlstate", None) != "22P02":
                raise
            if take > 1:
                split = True
            else:
                empty = ", ".join(selected) + ", " + _projection(conn, False)
                rows = conn.execute(f"SELECT {empty} FROM {source}", [*(params or []), take, offset]).fetchall()
        finally:
            if postgres:
                conn.execute("RELEASE SAVEPOINT insights_music_json")
        if split:
            half = take // 2
            return window(offset, half) + window(offset + half, take - half)
        return [dict(row) | {"_table": table} for row in rows]

    result = []
    while True:
        batch = window(len(result), _WINDOW)
        result.extend(batch)
        if len(batch) < _WINDOW:
            return result


def _music(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ms": _text(row.get("music_song")) or _text(row.get("r_song")),
        "ma": _text(row.get("music_artist")) or _text(row.get("r_artist")),
        "ai": _text(row.get("music_audio_id")) or _text(row.get("r_audio")),
        "oa": _bool(row.get("uses_original_audio")) if row.get("uses_original_audio") is not None else _bool(row.get("r_original")),
    }


def build_music_catalogue(conn: Any, accounts: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """One physical post per shortcode, preserving every represented account."""
    current = now or datetime.now(UTC)
    registry = {a["handle"].lower(): a for a in accounts}
    columns = {table: _columns(conn, table) for table in ("dashboard_posts", "posts", "engagement_observations")}
    rows = _read_source(conn, "dashboard_posts", columns["dashboard_posts"])
    canonical = next((a["handle"] for a in accounts if a.get("is_canonical")), None)
    if canonical:
        rows += [row | {"account": canonical} for row in _read_source(conn, "posts", columns["posts"])]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        account = str(row.get("account") or "").lower()
        code = _text(row.get("shortcode"))
        if account not in registry or row.get("section") == "ab":
            continue
        if row["_table"] == "posts" and not code and not _date(row.get("published_at")):
            continue  # Uploaded, unpublished single analyses are not posts.
        date = _date(row.get("published_at"))
        if date and date > current:
            continue
        row["account"] = account
        key = code or f"legacy-{row['_table']}-{row.get('id')}"
        grouped.setdefault(key, []).append(row)

    observations = {}
    codes = sorted({str(row["shortcode"]) for group in grouped.values() for row in group if row.get("shortcode")})
    batch_size = _WINDOW if getattr(conn, "is_postgres", False) else 400
    for start in range(0, len(codes), batch_size):
        batch = codes[start:start + batch_size]
        where, params = ("shortcode = ANY(?)", [batch]) if getattr(conn, "is_postgres", False) else (f"shortcode IN ({','.join('?' for _ in batch)})", batch)
        for row in _read_source(conn, "engagement_observations", columns["engagement_observations"], where, params):
            # A mismatched retained payload must not decorate another post.
            if row.get("r_code") and str(row["r_code"]) != str(row["shortcode"]):
                continue
            observations[str(row["shortcode"])] = row

    def timestamp(row: dict[str, Any]) -> datetime:
        return _date(row.get("observed_at") or row.get("enriched_at") or row.get("updated_at")) or datetime.min.replace(tzinfo=UTC)

    posts = []
    excluded = 0
    for code, records in grouped.items():
        # Curation on any duplicate must not be resurrected by a collaborator.
        if any(_bool(row.get("hidden")) or _bool(row.get("is_deleted")) for row in records):
            excluded += 1
            continue
        records.sort(key=lambda row: (_date(row.get("updated_at")) or datetime.min.replace(tzinfo=UTC), row["_table"] == "posts", row.get("id") or 0), reverse=True)
        base = records[0]
        observation = observations.get(code)
        facts = sorted(records + ([observation] if observation else []), key=timestamp, reverse=True)
        music: dict[str, Any] = {"ms": None, "ma": None, "ai": None, "oa": None}
        anchored = False
        for fact in facts:
            candidate = _music(fact)
            if not any(candidate[key] for key in ("ms", "ma", "ai")) and candidate["oa"] is not True:
                continue  # A false flag alone does not establish an audio.
            if not anchored:
                music = candidate
                anchored = True
                continue
            if not music["ai"] or music["ai"] != candidate["ai"]:
                # A newer title without an ID may describe a replacement
                # track. Only the same exact ID permits cross-snapshot fills.
                continue
            for key, value in candidate.items():
                if music[key] is None and value is not None:
                    music[key] = value
        if not anchored:
            music["oa"] = next((_music(r)["oa"] for r in facts if _music(r)["oa"] is not None), None)
        raw_type = next((r.get("product_type") or r.get("r_product") for r in facts if r.get("product_type") or r.get("r_product")), None)
        label = str(base.get("post_type_label") or next((r.get("r_type") for r in facts if r.get("r_type")), "Image")).split(" (")[0].strip()
        label = "Carousel" if label.lower() in {"sidecar", "carousel"} else "Video" if label.lower() in {"video", "reel"} or base.get("is_animated") else label
        likes = _number(base.get("likes"))
        comments = _number(base.get("comments"))
        measured = _date(base.get("updated_at"))
        if observation and (_date(observation.get("observed_at")) or datetime.min.replace(tzinfo=UTC)) >= (measured or datetime.min.replace(tzinfo=UTC)):
            if observation.get("r_has_likes"):
                likes = _number(observation.get("r_likes"))
            if observation.get("r_comments") is not None:
                comments = _number(observation["r_comments"])
            measured = _date(observation.get("observed_at"))
        def metric(promoted: str, *raw: str) -> float | int | None:
            return next((value for r in facts for key in (promoted, *raw) if (value := _number(r.get(key))) is not None), None)
        account = base["account"]
        date = next((_date(r.get("published_at")) for r in records if _date(r.get("published_at"))), None)
        posts.append({
            "a": account, "aa": sorted({r["account"] for r in records}),
            "g": registry[account]["group"], "sc": code, "d": date.isoformat() if date else None,
            "l": likes if likes is not None and likes > 3 else None, "c": comments,
            "t": label or "Image", "pt": raw_type, "v": metric("video_views", "r_views"),
            "pl": metric("video_plays", "r_plays", "r_igplays"), "dur": metric("video_duration", "r_duration"),
            "sl": metric("slide_count"), "hot": int(any(r.get("is_hot") for r in records)),
            "hm": metric("hot_rate_multiplier"), "l24": metric("likes_at_24h"), "l48": metric("likes_at_48h"),
            "u": base.get("permalink") or (f"https://www.instagram.com/p/{code}/" if base.get("shortcode") else None),
            "mu": measured.isoformat() if measured else None, **music,
        })
    posts.sort(key=lambda p: (p["d"] or "", p["sc"]), reverse=True)
    known = sum(bool(p["ms"] or p["ma"] or p["ai"] or p["oa"] is True) for p in posts)
    return {
        "posts": posts,
        "accounts": [{"handle": a["handle"], "group": a["group"], "threshold": a.get("hot_threshold"), "label": a.get("label")} for a in accounts],
        "coverage": {"total_posts": len(posts), "metadata_posts": known, "unknown_audio_posts": len(posts) - known,
                     "audio_id_posts": sum(bool(p["ai"]) for p in posts), "excluded_posts": excluded,
                     "source": "stored_metadata", "generated_at": current.isoformat()},
    }
