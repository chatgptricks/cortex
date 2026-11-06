"""Search complete Research captions and navigate to matching dashboard posts."""
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response

from . import db, external_research as reads
from .apify_sync import list_accounts
from .dashboard_rankings import dashboard_post_url

router = APIRouter(prefix="/api/dashboard/posts", tags=["Research"])


@router.get("/search", summary="Search all Research posts by complete caption, account, date, promo status and engagement")
def search_posts(response: Response, q: Annotated[str, Query(max_length=200)] = "",
                 text_scope: Literal["all", "caption"] = "all", account: str | None = None,
                 start: Annotated[date | None, Query(alias="from")] = None,
                 end: Annotated[date | None, Query(alias="to")] = None,
                 min_likes: Annotated[int | None, Query(ge=0)] = None,
                 min_comments: Annotated[int | None, Query(ge=0)] = None,
                 promos_only: bool = False,
                 sort: Literal["newest", "oldest", "likes", "comments"] = "newest",
                 limit: Annotated[int, Query(ge=1, le=100)] = 20,
                 offset: Annotated[int, Query(ge=0)] = 0,
                 include_keys: bool = False) -> dict:
    """Search complete captions, never their card previews. Newest/oldest sort by
    publication time. Use account for the latest post of a handle, promos_only
    for promo lists. Engagement filters/rankings merge retained observations
    across all candidates before pagination. include_keys returns ALL matching
    account:shortcode keys for the Research page's local filters. data is paginated.
    """
    handles = reads._handles({"account_handles": [a["handle"] for a in list_accounts(active_only=True, research_only=True)]}, account)
    with db.connect() as conn:
        source, params, registry = reads._sources(conn, handles)
        where, values = reads._filters(start=start, end=end)
        params.extend(values)
        if q.strip():
            term = q.strip().lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
            fields = ["caption"] if text_scope == "caption" else ["caption", "hook_text", "shortcode", "music_song", "music_artist"]
            where += " AND (" + " OR ".join(f"LOWER(COALESCE({field}, '')) LIKE ? ESCAPE '!'" for field in fields) + ")"
            params.extend([f"%{term}%"] * len(fields))
        rows = [dict(row) for row in conn.execute(source + "SELECT * FROM catalogue WHERE " + where, params).fetchall()]
        posts = [reads._project(row, registry) for row in rows]
        if promos_only:
            posts = [post for post in posts if post.get("is_promo")]
        needs_metrics = min_likes is not None or min_comments is not None or sort in {"likes", "comments"}
        if needs_metrics:
            for at in range(0, len(posts), 100): reads._fresh_page_metrics(conn, posts[at:at + 100], registry)
        if min_likes is not None: posts = [p for p in posts if p.get("likes") is not None and p["likes"] >= min_likes]
        if min_comments is not None: posts = [p for p in posts if p.get("comments") is not None and p["comments"] >= min_comments]
        if sort in {"likes", "comments"}:
            posts.sort(key=lambda p: (p.get(sort) is not None, p.get(sort) or 0, p["shortcode"]), reverse=True)
        else:
            posts.sort(key=lambda p: (reads.kit._date(p["published_at"]), p["shortcode"]), reverse=sort == "newest")
        selected = posts[offset:offset + limit]
        if not needs_metrics: reads._fresh_page_metrics(conn, selected, registry)
    response.headers["Cache-Control"] = "private, no-store"
    fields = ("account", "shortcode", "published_at", "likes", "comments", "is_promo", "caption", "metrics_updated_at")
    result = {"data": [{k: p.get(k) for k in fields} | {"dashboard_url": dashboard_post_url(p["account"], p["shortcode"])} for p in selected],
              "pagination": {"total": len(posts), "offset": offset, "limit": limit, "has_more": offset + limit < len(posts), "next_offset": offset + limit if offset + limit < len(posts) else None}}
    if include_keys: result["matching_post_keys"] = [p["account"] + ":" + p["shortcode"] for p in posts]
    return result
