"""Ground /next recommendations exclusively in the caller's stored dashboard data."""
import json
import re

import anyio
import httpx

from . import dashboard_next_reuse
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


async def candidate_bundle(request, slots: dict, entries: dict, read_operation, *, excluded_keys=None, state=None) -> dict:
    """Return bounded candidates checked against the destination's stored history.

    Discovery reads re-enter authenticated APIs through read_operation. The
    complete target history is read internally only after roster verification.
    Ranking evaluates its complete competitor/format/window set before taking
    twelve eligible candidates. Publication identity checks cover all stored
    target history; nearby prior stories are supplied for semantic review.
    A response-local state reuses verified reads and history across semantic
    review batches. Rejected candidates are skipped before their detail read.
    """
    sources = []
    target = None
    stage = "candidate_roster"
    state = {} if state is None else state
    excluded_keys = set(excluded_keys or ())
    if state.setdefault("request", request) is not request:
        return {"status": "unavailable", "target": None, "candidates": [], "sources": []}
    read_cache = state.setdefault("reads", {})

    async def read(path, parameters, analysis=None, *, complete=True):
        entry = next((item for item in entries.values() if item["path"] == path and not item["write"]), None)
        if entry is None:
            raise _Unavailable()
        cache_key = json.dumps([path, parameters, analysis], sort_keys=True)
        if cache_key not in read_cache:
            read_cache[cache_key] = await read_operation(request, entry, parameters, analysis)
        result, source = read_cache[cache_key]
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
        # The target is validated through the authenticated roster before any
        # internal history read. Never download its full history to the model.
        if state.get("history_target") != target["handle"]:
            state["history"] = await anyio.to_thread.run_sync(dashboard_next_reuse.load_history, target["handle"])
            state["history_target"] = target["handle"]
        history = state["history"]
        stage = "candidate_ranking"
        ranking = None
        candidates, seen = [], set()
        for hours in (24, 168, 720):
            ranking = await read("/api/insights/top-posts", {"query": {
                "account": ",".join(handles), "post_format": post_format,
                "hours": hours, "metric": "likes", "limit": 50,
            }})
            if ranking.get("coverage", {}).get("complete") is not True or not isinstance(ranking.get("data"), list):
                raise _Unavailable()
            for row in ranking["data"]:
                account, code = _name(row.get("account")), str(row.get("shortcode") or "")
                if account not in handles or not _CODE.fullmatch(code):
                    raise _Unavailable()
                key = account + ":" + code
                if key in seen:
                    continue
                seen.add(key)
                if key in excluded_keys:
                    continue
                stage = "candidate_detail"
                detail = await read("/api/dashboard/posts/{account}/{shortcode}/detail", {"path": {"account": account, "shortcode": code}})
                if _name(detail.get("account")) != account or detail.get("shortcode") != code:
                    raise _Unavailable()
                candidate = {**{name: row.get(name) for name in (
                    "published_at", "likes", "comments", "views", "metrics_updated_at")},
                    "account": account, "shortcode": code, "key": key, "format": post_format,
                    "dashboard_url": dashboard_post_url(account, code),
                    "caption": str(detail.get("caption") or ""), "ocr_text": str(detail.get("ocrText") or "")}
                eligible = history.filter_candidates([candidate])
                for item in eligible:
                    if not item.get("reuse_uncertain"):
                        candidates.append({**item, "caption": item["caption"][:2400], "ocr_text": item["ocr_text"][:1200]})
                if len(candidates) >= 12:
                    break
            if candidates:
                break
        criteria = {"hours": hours, "window": ranking.get("window"), "format": post_format,
                    "metric": "likes", "same_subcategory": bool(related), "competitor_accounts": handles}
        if not candidates:
            return {"status": "empty", "target": target, "candidates": [], "sources": sources, "criteria": criteria,
                    "reason": "no_unpublished_candidates" if seen else "no_recent_candidates"}
        return {"status": "ready", "target": target, "candidates": candidates, "sources": sources,
                "criteria": criteria, "target_examples": history.examples, "history_complete": True,
                "history_posts_checked": history.post_count, "_history": history}
    except (ValueError, TypeError, KeyError, TimeoutError, httpx.HTTPError) as exc:
        return {"status": "unavailable", "target": target, "candidates": [], "sources": [],
                "error_code": getattr(exc, "code", stage)}
