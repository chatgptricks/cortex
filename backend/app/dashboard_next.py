"""Ground /next recommendations exclusively in the caller's stored dashboard data."""
import re

import httpx

from .dashboard_rankings import dashboard_post_url

_HANDLE = re.compile(r"[a-z0-9_.]{1,30}\Z")
_CODE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class _Unavailable(ValueError):
    pass


def _name(value):
    return str(value or "").strip().removeprefix("@").casefold()


def _payload(result, *, complete=True):
    if (not isinstance(result, dict) or result.get("status") != 200
            or result.get("truncated") or complete and result.get("complete") is not True
            or not isinstance(result.get("data"), dict)):
        raise _Unavailable()
    return result["data"]


async def candidate_bundle(request, slots: dict, entries: dict, read_operation) -> dict:
    """Return bounded candidates; never fetch the web, mutate Queue, or infer reuse.

    All reads re-enter existing authenticated APIs through read_operation. The
    ranking evaluates its complete competitor/format/window set before taking
    twelve candidates. This is a recent editorial shortlist, not an all-time
    maximum or proof that a post has never been recreated.
    """
    sources = []
    target = None

    async def read(path, parameters, analysis=None, *, complete=True):
        entry = next((item for item in entries.values() if item["path"] == path and not item["write"]), None)
        if entry is None:
            raise _Unavailable()
        result, source = await read_operation(request, entry, parameters, analysis)
        payload = _payload(result, complete=complete)
        if source and source not in sources:
            sources.append(source)
        return payload

    try:
        post_format = slots.get("format") or slots.get("post_format")
        if post_format not in {"reel", "image", "carousel"}:
            return {"status": "unavailable", "target": None, "candidates": [], "sources": []}
        roster = await read("/api/dashboard/accounts", {}, {
            "items_path": "accounts", "filters": [{"field": "group", "op": "in", "value": ["sentient", "competitors"]}],
            "fields": ["handle", "label", "group", "subcategory"], "limit": 100,
        })
        accounts = roster.get("data")
        if (not isinstance(accounts, list) or roster.get("complete") is not True
                or roster.get("returned_rows") != roster.get("result_rows")
                or len(accounts) != roster.get("returned_rows")):
            raise _Unavailable()
        owned = [row for row in accounts if row.get("group") == "sentient"]
        requested = _name(slots.get("account") or slots.get("target_account"))
        matches = [row for row in owned if requested and requested in {_name(row.get("handle")), _name(row.get("label"))}]
        if len(matches) != 1:
            return {"status": "invalid_target", "target": None, "candidates": [], "sources": sources,
                    "available_targets": [row["handle"] for row in owned if _HANDLE.fullmatch(_name(row.get("handle")))][:20]}
        target = {**matches[0], "handle": _name(matches[0].get("handle"))}
        if not _HANDLE.fullmatch(target["handle"]):
            raise _Unavailable()
        competitors = [row for row in accounts if row.get("group") == "competitors"
                       and _HANDLE.fullmatch(_name(row.get("handle")))]
        subcategory = str(target.get("subcategory") or "other")
        related = [row for row in competitors if subcategory != "other" and row.get("subcategory") == subcategory]
        selected = related or competitors
        handles = sorted({_name(row["handle"]) for row in selected})
        if not handles:
            return {"status": "empty", "target": target, "candidates": [], "sources": sources,
                    "reason": "no_competitor_accounts"}
        ranking = None
        for hours in (24, 168, 720):
            ranking = await read("/api/insights/top-posts", {"query": {
                "account": ",".join(handles), "post_format": post_format,
                "hours": hours, "metric": "likes", "limit": 12,
            }})
            if ranking.get("coverage", {}).get("complete") is not True or not isinstance(ranking.get("data"), list):
                raise _Unavailable()
            if ranking["data"]:
                break
        criteria = {"hours": hours, "window": ranking.get("window"), "format": post_format,
                    "metric": "likes", "same_subcategory": bool(related), "competitor_accounts": handles}
        if not ranking["data"]:
            return {"status": "empty", "target": target, "candidates": [], "sources": sources, "criteria": criteria}
        candidates, seen = [], set()
        for row in ranking["data"][:12]:
            account, code = _name(row.get("account")), str(row.get("shortcode") or "")
            if account not in handles or not _CODE.fullmatch(code):
                raise _Unavailable()
            key = account + ":" + code
            if key in seen:
                continue
            seen.add(key)
            detail = await read("/api/dashboard/posts/{account}/{shortcode}/detail", {"path": {"account": account, "shortcode": code}})
            if _name(detail.get("account")) != account or detail.get("shortcode") != code:
                raise _Unavailable()
            candidates.append({**{name: row.get(name) for name in (
                "published_at", "likes", "comments", "views", "metrics_updated_at")},
                "account": account, "shortcode": code, "key": key, "format": post_format,
                "dashboard_url": dashboard_post_url(account, code),
                "caption": str(detail.get("caption") or "")[:1200], "ocr_text": str(detail.get("ocrText") or "")[:500]})
        # Three recent captions give the selector real editorial context for
        # the destination, instead of inferring its niche from the handle.
        target_posts = await read("/api/dashboard/posts/search", {"query": {
            "account": target["handle"], "sort": "newest", "limit": 3,
        }}, complete=False)
        examples = [{"caption": str(row.get("caption") or "")[:600], "published_at": row.get("published_at")}
                    for row in target_posts.get("data", [])[:3] if _name(row.get("account")) == target["handle"]]
        return {"status": "ready", "target": target, "candidates": candidates, "sources": sources,
                "criteria": criteria, "target_examples": examples, "reuse_checked": False}
    except (ValueError, TypeError, KeyError, TimeoutError, httpx.HTTPError):
        return {"status": "unavailable", "target": target, "candidates": [], "sources": []}
