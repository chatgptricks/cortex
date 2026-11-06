"""Complete rankings over the dashboard's stored posts, never a page sample."""
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import AwareDatetime

from . import db, external_research as reads
from .apify_sync import list_accounts

router = APIRouter(prefix="/api/insights", tags=["Insights"])


def _now() -> datetime:
    return datetime.now(UTC)


def dashboard_post_url(account: str, shortcode: str) -> str:
    return "https://sentientdash.app/index.html?post=" + quote(account + ":" + shortcode, safe="")


def candidate_codes(conn, *, start=None, end=None, query="", text_scope="all", promos_only=False, handles=None):
    """A safe superset of keys BEFORE expensive metadata precedence windows.

    Keep every duplicate of a candidate key in _sources so canonical visibility
    and newer metadata still win, including copies outside the raw date range.
    """
    codes = set()
    registry = reads._registry(conn, handles) if handles is not None else None
    for table in ("posts", "dashboard_posts"):
        columns = reads.kit._columns(conn, table)
        if "shortcode" not in columns: continue
        if table == "posts" and registry is not None and not any(a.get("is_canonical") for a in registry.values()): continue
        where, params = ["shortcode IS NOT NULL", "shortcode <> ''"], []
        if table == "dashboard_posts" and registry is not None:
            if not registry: continue
            where.append("LOWER(account) IN (" + ",".join("?" for _ in registry) + ")")
            params.extend(registry)
        if start is not None and end is not None and "published_at" in columns:
            where.extend(["published_at >= ?", "published_at < ?"])
            params.extend([(start - timedelta(days=1)).date().isoformat(), (end + timedelta(days=2)).date().isoformat()])
        if query.strip():
            fields = [f for f in (["caption"] if text_scope == "caption" else ["caption", "hook_text", "shortcode", "music_song", "music_artist"]) if f in columns]
            if not fields: continue
            term = query.strip().lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
            where.append("(" + " OR ".join(f"LOWER(COALESCE({f}, '')) LIKE ? ESCAPE '!'" for f in fields) + ")")
            params.extend([f"%{term}%"] * len(fields))
        if promos_only and not query.strip():
            marks = []
            if "is_promo" in columns: marks.append("is_promo = 1")
            if "caption" in columns:
                marks.append("LOWER(caption) LIKE ?")
                params.append("%#aitoolsentient%")
            if not marks: continue
            where.append("(" + " OR ".join(marks) + ")")
        codes.update(row["shortcode"] for row in conn.execute(f"SELECT DISTINCT shortcode FROM {table} WHERE " + " AND ".join(where), params).fetchall())
    return sorted(codes)


def rank_posts(handles: list[str], start: datetime, end: datetime, metric: str, limit: int, post_format: str = "all") -> dict:
    # Snapshot the complete candidate set once; new ingestion cannot shift an
    # offset and silently skip posts while we hydrate current metrics.
    with db.connect() as conn:
        if getattr(conn, "is_postgres", False):
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '25s'")
        else:
            conn.execute("BEGIN")
        codes = candidate_codes(conn, start=start, end=end, handles=handles)
        source, params, registry = reads._sources(conn, handles, candidate_codes=codes if len(codes) <= 20000 else None)
        # A broad calendar prefilter also admits legacy timezone-offset strings.
        # Exact aware-datetime comparisons below decide membership, not date bins.
        where = "COALESCE(hidden, 0) = 0 AND COALESCE(is_deleted, 0) = 0 AND published_at IS NOT NULL AND published_at <> ''"
        rows = [dict(row) for row in conn.execute(source + "SELECT * FROM catalogue WHERE " + where, params).fetchall()]
        unique = {}
        unknown = 0
        matched_rows = 0
        for offset in range(0, len(rows), 100):
            batch = [reads._project(row, registry) for row in rows[offset:offset + 100]]
            reads._fresh_page_metrics(conn, batch, registry)
            for post in batch:
                published = reads.kit._date(post.get("published_at"))
                if published is None or not start <= published <= end:
                    continue
                kind = str(post.get("format") or "").lower()
                if post_format != "all" and not (kind.startswith(post_format) or post_format == "carousel" and kind == "sidecar"):
                    continue
                matched_rows += 1
                code = post["shortcode"]
                previous = unique.get(code)
                observed = reads.kit._date(post.get("metrics_updated_at")) or datetime.min.replace(tzinfo=UTC)
                previous_at = reads.kit._date(previous.get("metrics_updated_at")) if previous else None
                if previous is None or observed > (previous_at or datetime.min.replace(tzinfo=UTC)):
                    unique[code] = post
        available = []
        for post in unique.values():
            value = post.get(metric)
            if value is None or value < 0:
                unknown += 1
                continue
            available.append(post)
        available.sort(key=lambda post: (-post[metric], post["account"], post["shortcode"]))
        return {
            "window": {"from": start.isoformat(), "to": end.isoformat()},
            "metric": metric,
            "coverage": {"complete": True, "accounts": len(registry), "matching_rows": matched_rows, "posts": len(unique), "unknown_metric": unknown},
            "data": [{key: post.get(key) for key in ("account", "shortcode", "published_at", "likes", "comments", "views", "metrics_updated_at")} | {"dashboard_url": dashboard_post_url(post["account"], post["shortcode"])} for post in available[:limit]],
        }


@router.get("/top-posts", summary="Find the exact top posts across the complete dashboard within a rolling time window")
def top_posts(response: Response,
              hours: Annotated[int, Query(ge=1, le=744)] = 24,
              start: Annotated[AwareDatetime | None, Query(description="Optional inclusive publication timestamp with timezone; otherwise now minus hours")] = None,
              end: Annotated[AwareDatetime | None, Query(description="Optional inclusive publication timestamp with timezone; otherwise server now")] = None,
              account: str | None = None,
              post_format: Literal["all", "reel", "carousel", "image", "video"] = "all",
              all_time: bool = False,
              metric: Literal["likes", "comments", "views"] = "likes",
              limit: Annotated[int, Query(ge=1, le=100)] = 1) -> dict:
    """Use this for most-liked/viewed/commented posts, including last 24 hours.

    Every matching stored post across all accessible dashboard accounts is
    evaluated using the newest retained metrics before ranking. No sampling
    or client pagination is needed. hours=24 uses exact server now minus 24h,
    not calendar days. Optional start/end allow exact explicit date windows.
    post_format filters reels, carousels, images or videos. all_time=true ranks
    the entire retained history; combine account and limit=3 for the three best
    reels ever from a page. Unknown metrics stay unknown. Returns internal dashboard_url links.
    """
    finish = (end or _now()).astimezone(UTC)
    begin = datetime(1970, 1, 1, tzinfo=UTC) if all_time else start.astimezone(UTC) if start else finish - timedelta(hours=hours)
    if begin >= finish or (not all_time and finish - begin > timedelta(days=31)):
        raise HTTPException(422, "Choose a publication window between one instant and 31 days.")
    identity = {"account_handles": [item["handle"] for item in list_accounts(active_only=True, research_only=True)]}
    handles = reads._handles(identity, account)
    response.headers["Cache-Control"] = "private, no-store"
    return rank_posts(handles, begin, finish, metric, limit, post_format)
