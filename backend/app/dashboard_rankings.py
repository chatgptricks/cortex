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


def rank_posts(handles: list[str], start: datetime, end: datetime, metric: str, limit: int) -> dict:
    # Snapshot the complete candidate set once; new ingestion cannot shift an
    # offset and silently skip posts while we hydrate current metrics.
    with db.connect() as conn:
        if getattr(conn, "is_postgres", False):
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        else:
            conn.execute("BEGIN")
        source, params, registry = reads._sources(conn, handles)
        # A broad calendar prefilter also admits legacy timezone-offset strings.
        # Exact aware-datetime comparisons below decide membership, not date bins.
        where, filters = reads._filters(start=(start - timedelta(days=1)).date(), end=(end + timedelta(days=1)).date())
        rows = [dict(row) for row in conn.execute(source + "SELECT * FROM catalogue WHERE " + where, [*params, *filters]).fetchall()]
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
              metric: Literal["likes", "comments", "views"] = "likes",
              limit: Annotated[int, Query(ge=1, le=20)] = 1) -> dict:
    """Use this for most-liked/viewed/commented posts, including last 24 hours.

    Every matching stored post across all accessible dashboard accounts is
    evaluated using the newest retained metrics before ranking. No sampling
    or client pagination is needed. hours=24 uses exact server now minus 24h,
    not calendar days. Optional start/end allow exact explicit date windows.
    Unknown metrics stay unknown. Returns internal dashboard_url links.
    """
    finish = (end or _now()).astimezone(UTC)
    begin = start.astimezone(UTC) if start else finish - timedelta(hours=hours)
    if begin >= finish or finish - begin > timedelta(days=31):
        raise HTTPException(422, "Choose a publication window between one instant and 31 days.")
    identity = {"account_handles": [item["handle"] for item in list_accounts(active_only=True, research_only=True)]}
    handles = reads._handles(identity, account)
    response.headers["Cache-Control"] = "private, no-store"
    return rank_posts(handles, begin, finish, metric, limit)
