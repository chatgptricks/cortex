"""Stored, read-only workspace projections for the versioned integration API.

Do not reuse Dashboard GET handlers here: some synchronize, clean up or call
providers. Every database statement in this module is a bounded SELECT (or
read-only schema inspection); optional workspaces are never initialized by a
reader. Explicit projections keep operational metadata out of integrations.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from . import db, external_api as api, media_storage
from .user_preferences import VALIDATORS as PREFERENCE_VALIDATORS

router = APIRouter(prefix="/api/v1", tags=["workspace-api-v1"], dependencies=[Depends(api._no_store)])
LIMIT = Query(default=50, ge=1, le=100)
OFFSET = Query(default=0, ge=0, le=100000)


def _scope(request: Request, name: str) -> dict[str, Any]:
    return api.require_scope(request, name)


def _json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _strings(value: Any) -> list[str]:
    return [item for item in _json(value, []) if isinstance(item, str)]


def _flag(value: Any) -> bool | None:
    if value in (0, "0", False):
        return False
    if value in (1, "1", True):
        return True
    return None


def _url(value: Any) -> str | None:
    """Public HTTP links only, retaining useful non-credential query state."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        blocked = {"token", "access_token", "refresh_token", "api_key", "apikey", "sig", "signature", "authorization", "auth"}
        query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
                 if key.lower() not in blocked and not key.lower().startswith("x-amz-")]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), ""))
    except ValueError:
        return None


def _columns(conn: Any, table: str) -> set[str]:
    # table names are hard-coded by callers, never supplied by a client.
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _json_field(conn: Any, column: str, key: str) -> str:
    if getattr(conn, "is_postgres", False):
        return f"(CAST({column} AS jsonb)->>'{key}')"
    return f"CASE WHEN json_valid({column}) THEN json_extract({column}, '$.{key}') ELSE NULL END"


def _projection(columns: set[str], fields: tuple[str, ...], alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{field} AS {field}" if field in columns else f"NULL AS {field}" for field in fields)


def _handles(identity: dict[str, Any], account: str | None = None) -> list[str]:
    granted = sorted({str(item).lower() for item in identity.get("account_handles", [])})
    if account is None:
        return granted
    account = account.strip().lower().lstrip("@")
    if account not in granted:
        raise HTTPException(404, "Account not available for this API key.")
    return [account]


def _accounts_sql(column: str, handles: list[str]) -> tuple[str, list[Any]]:
    if not handles:
        return "1 = 0", []
    return f"{column} IN ({','.join('?' for _ in handles)})", list(handles)


def _envelope(data: Any, pagination: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {"schema_version": api.VERSION, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "data": data}
    if pagination is not None:
        result["pagination"] = pagination
    return result


def _page(rows: list[Any], limit: int, offset: int, project=lambda row: dict(row)) -> dict[str, Any]:
    more = len(rows) > limit
    items = [project(dict(row)) for row in rows[:limit]]
    return _envelope(items, {"limit": limit, "offset": offset, "count": len(items), "has_more": more,
                             "next_offset": offset + limit if more else None})


def _missing() -> None:
    raise HTTPException(404, "Resource not available for this API key.")


def _limited(conn: Any, table: str, fields: tuple[str, ...], where: str, params: list[Any],
             order: str, limit: int, offset: int, *, alias: str = "", joins: str = "") -> list[Any]:
    columns = _columns(conn, table)
    if not columns:
        return []
    return conn.execute(f"SELECT {_projection(columns, fields, alias)} FROM {table} {alias} {joins} "
                        f"WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", (*params, limit + 1, offset)).fetchall()


def _array_membership(conn: Any, column: str, handles: list[str]) -> tuple[str, list[Any]]:
    predicate, params = _accounts_sql("member.value", handles)
    if getattr(conn, "is_postgres", False):
        source = f"jsonb_array_elements_text(CASE WHEN jsonb_typeof(CAST({column} AS jsonb)) = 'array' THEN CAST({column} AS jsonb) ELSE '[]'::jsonb END) AS member(value)"
        return f"EXISTS (SELECT 1 FROM {source} WHERE {predicate})", params
    source = f"json_each(CASE WHEN json_valid({column}) THEN {column} ELSE '[]' END) member"
    return f"EXISTS (SELECT 1 FROM {source} WHERE member.type = 'text' AND {predicate})", params


LIST_FIELDS = ("id", "name", "handles", "owner_email", "is_shared", "created_at", "updated_at")
GOLDEN_FIELDS = ("account", "shortcode", "label", "target_account", "score", "review", "reviewed_by", "reviewed_at")


@router.get("/research/lists")
def research_lists(request: Request, account: str | None = None, q: str = Query(default="", max_length=200),
                   limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    identity = _scope(request, "research")
    handles = _handles(identity, account)
    with db.connect() as conn:
        membership, params = _array_membership(conn, "l.handles", handles)
        where = f"(l.owner_email = ? OR l.is_shared = 1) AND ({membership})"
        params = [identity["owner_email"], *params]
        if q:
            where += " AND LOWER(l.name) LIKE ?"
            params.append(f"%{q.lower()}%")
        rows = _limited(conn, "account_lists", LIST_FIELDS, where, params, "LOWER(l.name), l.id", limit, offset, alias="l")
    def project(row):
        row["handles"] = [handle for handle in _strings(row["handles"]) if handle in handles]
        row["is_shared"] = _flag(row["is_shared"])
        return row
    return _page(rows, limit, offset, project)


@router.get("/research/golden-nuggets")
def research_golden_nuggets(request: Request, account: str | None = None,
                            min_score: float | None = Query(default=None, ge=0, le=1), target_account: str | None = None,
                            limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    identity = _scope(request, "research")
    handles = _handles(identity, account)
    where, params = _accounts_sql("account", handles)
    where += " AND label = 'golden_nugget'"
    if min_score is not None:
        where += " AND score >= ?"
        params.append(min_score)
    if target_account is not None:
        where += " AND target_account = ?"
        params.append(_handles(identity, target_account)[0])
    with db.connect() as conn:
        rows = _limited(conn, "post_golden_nuggets", GOLDEN_FIELDS, where, params, "reviewed_at DESC, account, shortcode", limit, offset)
    def project(row):
        row["target_account"] = row.get("target_account") if row.get("target_account") in _handles(identity) else None
        row["review"] = _review(row.get("review"), _handles(identity))
        return row
    return _page(rows, limit, offset, project)


QUEUE_FIELDS = ("id", "post_account", "post_shortcode", "post_title", "is_custom", "post_permalink", "post_caption",
                "post_type", "production_points", "minutes_per_pp", "priority", "tags", "brief", "notes",
                "reference_links", "status", "designer_email", "coordinator_email", "recommended_accounts",
                "scheduled_date", "scheduled_start_minutes", "actual_started_at", "completed_at", "closed_at",
                "final_permalink", "final_permalinks", "cancellation_reason", "attachments", "created_at", "updated_at")
DRAFT_FIELDS = ("request_id", "coordinator_email", "designer_email", "scheduled_date", "scheduled_start_minutes",
                "recommended_accounts", "production_points", "minutes_per_pp", "updated_at")
TICKET_FIELDS = ("id", "ticket_type", "requester_email", "request_id", "status", "block_category", "title",
                 "scheduled_date", "scheduled_start_minutes", "duration_minutes", "requested_production_points",
                 "requested_accounts", "reason", "reviewer_email", "review_note", "reviewed_at", "created_at", "updated_at")


def _queue_where(handles: list[str], alias: str = "r") -> tuple[str, list[Any]]:
    source, params = _accounts_sql(f"{alias}.post_account", handles)
    # Vault/manual sources can have no account. They are available only when
    # explicitly associated with a granted publishing account.
    destinations = " OR ".join(f"{alias}.recommended_accounts LIKE ? ESCAPE '!'" for _ in handles)
    if destinations:
        source = f"({source} OR ({alias}.post_account = '' AND ({destinations})))"
        params.extend(f'%"{handle.replace("!", "!!").replace("_", "!_").replace("%", "!%")}"%' for handle in handles)
    return source, params


def _queue_project(row: dict[str, Any], handles: list[str]) -> dict[str, Any]:
    for field in ("recommended_accounts", "requested_accounts"):
        if field in row:
            row[field] = [item for item in _strings(row[field]) if item in handles]
    if "tags" in row:
        row["tags"] = _strings(row["tags"])
    if "reference_links" in row:
        row["reference_links"] = [link for item in _strings(row["reference_links"]) if (link := _url(item))]
    for field in ("post_permalink", "final_permalink"):
        if field in row:
            row[field] = _url(row[field])
    if "final_permalinks" in row:
        row["final_permalinks"] = [
            {"account": item.get("account"), "url": _url(item.get("url") or item.get("permalink"))}
            for item in _json(row["final_permalinks"], [])
            if isinstance(item, dict) and item.get("account") in handles and _url(item.get("url") or item.get("permalink"))
        ]
    if "is_custom" in row:
        row["is_custom"] = _flag(row["is_custom"])
    if "attachments" in row:
        row["attachments"] = [
            {"id": item["id"], "name": str(item.get("name") or "").replace("\\", "/").rsplit("/", 1)[-1],
             "size": item.get("size") if isinstance(item.get("size"), (int, float)) else None,
             "content_type": item.get("contentType") if isinstance(item.get("contentType"), str) else None,
             "uploaded_at": item.get("uploadedAt") if isinstance(item.get("uploadedAt"), str) else None,
             "download_url": f"/api/v1/queue/requests/{row['id']}/attachments/{item['id']}/content"}
            for item in _json(row["attachments"], []) if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
    if "priority" in row:
        row["priority"] = "urgent" if row["priority"] == "urgent" else "normal"
    return row


def _queue_request(conn: Any, identity: dict[str, Any], request_id: int) -> dict[str, Any]:
    if not _columns(conn, "queue_requests"):
        _missing()
    where, params = _queue_where(_handles(identity))
    rows = _limited(conn, "queue_requests", QUEUE_FIELDS, f"({where}) AND r.id = ?", [*params, request_id], "r.id", 1, 0, alias="r")
    if not rows:
        _missing()
    return _queue_project(dict(rows[0]), _handles(identity))


@router.get("/queue/requests")
@router.get("/queue/schedule")
def queue_requests(request: Request, account: str | None = None, status: str | None = Query(default=None, max_length=40),
                   date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                   limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    identity = _scope(request, "queue")
    handles = _handles(identity, account)
    where, params = _queue_where(handles)
    if status:
        where += " AND r.status = ?"
        params.append(status)
    if date:
        where += " AND r.scheduled_date = ?"
        params.append(date)
    elif request.url.path.rstrip("/").endswith("/schedule"):
        where += " AND r.scheduled_date IS NOT NULL"
    with db.connect() as conn:
        rows = _limited(conn, "queue_requests", QUEUE_FIELDS, where, params, "r.updated_at DESC, r.id DESC", limit, offset, alias="r")
    return _page(rows, limit, offset, lambda row: _queue_project(row, handles))


@router.get("/queue/requests/{request_id}")
def queue_request(request: Request, request_id: int) -> dict[str, Any]:
    identity = _scope(request, "queue")
    with db.connect() as conn:
        return _envelope(_queue_request(conn, identity, request_id))


@router.get("/queue/requests/{request_id}/attachments")
def queue_attachments(request: Request, request_id: int) -> dict[str, Any]:
    identity = _scope(request, "queue")
    with db.connect() as conn:
        return _envelope(_queue_request(conn, identity, request_id)["attachments"])


@router.get("/queue/requests/{request_id}/attachments/{attachment_id}/content")
def queue_attachment_content(request: Request, request_id: int, attachment_id: str) -> StreamingResponse:
    identity = _scope(request, "queue")
    if not re.fullmatch(r"[0-9a-f]{16}", attachment_id):
        _missing()
    with db.connect() as conn:
        _queue_request(conn, identity, request_id)
        # Read the stored reference only after the request's account boundary.
        row = conn.execute("SELECT attachments FROM queue_requests WHERE id = ?", (request_id,)).fetchone()
    attachment = next((item for item in _json(row["attachments"], [])
                       if isinstance(item, dict) and item.get("id") == attachment_id), None)
    if attachment is None or not media_storage.is_r2_reference(attachment.get("mediaRef")):
        _missing()
    try:
        key = media_storage._object_key(attachment["mediaRef"])
        if not key.startswith("uploads/queue-") or not media_storage.r2_enabled():
            _missing()
        # The normal private redirect helper repairs object metadata with a
        # copy_object write. This endpoint deliberately uses only get_object
        # and streams privately, including for older objects awaiting repair.
        stored = media_storage._client().get_object(Bucket=media_storage.R2_BUCKET, Key=key)
        body = stored["Body"]
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(404, "Stored attachment is not available.") from None
    filename = str(attachment.get("name") or "attachment").replace("\\", "/").rsplit("/", 1)[-1]
    content_type = stored.get("ContentType") or attachment.get("contentType") or "application/octet-stream"
    if not isinstance(content_type, str) or not re.fullmatch(r"[\w.+-]+/[\w.+-]+", content_type):
        content_type = "application/octet-stream"
    def content():
        try:
            while chunk := body.read(256 * 1024):
                yield chunk
        finally:
            body.close()
    return StreamingResponse(content(), media_type=content_type, headers={
        "Cache-Control": "private, no-store", "Vary": "Authorization", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
    })


@router.get("/queue/summary")
def queue_summary(request: Request, account: str | None = None) -> dict[str, Any]:
    identity = _scope(request, "queue")
    where, params = _queue_where(_handles(identity, account))
    with db.connect() as conn:
        rows = [] if not _columns(conn, "queue_requests") else conn.execute(
            f"SELECT r.status, COUNT(*) AS count, COALESCE(SUM(r.production_points), 0) AS production_points "
            f"FROM queue_requests r WHERE {where} GROUP BY r.status ORDER BY r.status", params).fetchall()
    statuses = [{"status": row["status"], "count": int(row["count"]), "production_points": int(row["production_points"])} for row in rows]
    return _envelope({"total_requests": sum(row["count"] for row in statuses), "statuses": statuses})


def _report_assigned(row: dict[str, Any], handles: list[str]) -> dict[str, Any]:
    draft = {key: row.pop(f"draft_{key}", None) for key in DRAFT_FIELDS}
    item = _queue_project(row, handles)
    item["is_draft"] = draft["request_id"] is not None
    if not item["is_draft"]:
        return item
    returning_to_pool = draft["designer_email"] == "__queue_pool__"
    item.update({
        "committed_status": item["status"], "status": "pool" if returning_to_pool else "scheduled",
        "designer_email": None if returning_to_pool else draft["designer_email"],
        "scheduled_date": None if returning_to_pool else draft["scheduled_date"],
        "scheduled_start_minutes": None if returning_to_pool else draft["scheduled_start_minutes"],
        "recommended_accounts": [handle for handle in _strings(draft["recommended_accounts"]) if handle in handles],
        "draft_coordinator_email": draft["coordinator_email"], "draft_updated_at": draft["updated_at"],
    })
    for field in ("production_points", "minutes_per_pp"):
        if draft[field] is not None:
            item[field] = int(draft[field])
    return item


@router.get("/queue/report")
def queue_report(request: Request, account: str | None = None, limit: int = LIMIT, offset: int = OFFSET,
                 assigned_limit: int = Query(default=50, ge=1, le=100),
                 assigned_offset: int = Query(default=0, ge=0, le=100000)) -> dict[str, Any]:
    identity = _scope(request, "queue")
    handles = _handles(identity, account)
    where, params = _queue_where(handles)
    totals = {status: {"count": 0, "points": 0} for status in ("pool", "scheduled", "in_progress", "completed", "closed", "cancelled")}
    priorities = {priority: {"count": 0, "points": 0} for priority in ("normal", "urgent")}
    designer_rows, assigned_rows = [], []
    with db.connect() as conn:
        columns = _columns(conn, "queue_requests")
        if columns:
            for row in conn.execute(
                f"SELECT r.status, COUNT(*) AS count, COALESCE(SUM(r.production_points), 0) AS points "
                f"FROM queue_requests r WHERE {where} GROUP BY r.status", params).fetchall():
                if isinstance(row["status"], str):
                    totals[row["status"]] = {"count": int(row["count"]), "points": int(row["points"])}
            for row in conn.execute(
                "SELECT CASE WHEN r.priority = 'urgent' THEN 'urgent' ELSE 'normal' END AS priority, "
                f"COUNT(*) AS count, COALESCE(SUM(r.production_points), 0) AS points FROM queue_requests r "
                f"WHERE {where} GROUP BY CASE WHEN r.priority = 'urgent' THEN 'urgent' ELSE 'normal' END", params).fetchall():
                priorities[row["priority"]] = {"count": int(row["count"]), "points": int(row["points"])}
            if _columns(conn, "dashboard_users"):
                if getattr(conn, "is_postgres", False):
                    minutes = "EXTRACT(EPOCH FROM (CAST(completed_at AS timestamptz) - CAST(actual_started_at AS timestamptz))) / 60.0"
                else:
                    minutes = "(julianday(completed_at) - julianday(actual_started_at)) * 1440.0"
                rounded_minutes = f"CASE WHEN ({minutes}) < 0 THEN 0 ELSE ROUND({minutes}) END"
                # Account scope is applied inside the aggregation, before the
                # designer roster is paginated. Zero-work current users remain
                # visible, matching the coordinator's productivity report.
                designer_rows = conn.execute(
                    "WITH scoped AS (SELECT r.designer_email, r.status, r.production_points, r.priority, r.completed_at, r.actual_started_at "
                    f"FROM queue_requests r WHERE {where}), productivity AS (SELECT designer_email, "
                    "SUM(CASE WHEN status IN ('scheduled','in_progress','completed') THEN 1 ELSE 0 END) AS active_requests, "
                    "SUM(CASE WHEN status IN ('scheduled','in_progress','completed') THEN COALESCE(production_points, 0) ELSE 0 END) AS production_points, "
                    "SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed_requests, "
                    "SUM(CASE WHEN status IN ('scheduled','in_progress','completed') AND priority = 'urgent' THEN 1 ELSE 0 END) AS urgent_requests, "
                    f"AVG(CASE WHEN status = 'closed' AND completed_at IS NOT NULL AND actual_started_at IS NOT NULL THEN {rounded_minutes} ELSE NULL END) AS average_minutes "
                    "FROM scoped WHERE designer_email IS NOT NULL GROUP BY designer_email) "
                    "SELECT u.email, COALESCE(p.active_requests, 0) AS active_requests, COALESCE(p.production_points, 0) AS production_points, "
                    "COALESCE(p.closed_requests, 0) AS closed_requests, COALESCE(p.urgent_requests, 0) AS urgent_requests, p.average_minutes "
                    "FROM dashboard_users u LEFT JOIN productivity p ON p.designer_email = u.email "
                    "ORDER BY active_requests DESC, u.email LIMIT ? OFFSET ?", (*params, limit + 1, offset)).fetchall()
            draft_columns = _columns(conn, "queue_schedule_drafts")
            if draft_columns:
                draft_select = ", ".join(f"d.{field} AS draft_{field}" if field in draft_columns else f"NULL AS draft_{field}" for field in DRAFT_FIELDS)
                joins = "LEFT JOIN queue_schedule_drafts d ON d.request_id = r.id"
                assigned_where = "((r.designer_email IS NOT NULL AND r.designer_email <> '') OR d.request_id IS NOT NULL)"
                order_date = "CASE WHEN d.designer_email = '__queue_pool__' THEN '9999-99-99' ELSE COALESCE(d.scheduled_date, r.scheduled_date, '9999-99-99') END"
                order_start = "CASE WHEN d.designer_email = '__queue_pool__' THEN 9999 ELSE COALESCE(d.scheduled_start_minutes, r.scheduled_start_minutes, 9999) END"
            else:
                draft_select = ", ".join(f"NULL AS draft_{field}" for field in DRAFT_FIELDS)
                joins = ""
                assigned_where = "r.designer_email IS NOT NULL AND r.designer_email <> ''"
                order_date, order_start = "COALESCE(r.scheduled_date, '9999-99-99')", "COALESCE(r.scheduled_start_minutes, 9999)"
            assigned_rows = conn.execute(
                f"SELECT {_projection(columns, QUEUE_FIELDS, 'r')}, {draft_select} FROM queue_requests r {joins} "
                f"WHERE ({where}) AND ({assigned_where}) ORDER BY {order_date}, {order_start}, r.id LIMIT ? OFFSET ?",
                (*params, assigned_limit + 1, assigned_offset)).fetchall()
    def designer(row):
        return {"email": row["email"], "activeRequests": int(row["active_requests"]), "productionPoints": int(row["production_points"]),
                "closedRequests": int(row["closed_requests"]), "urgentRequests": int(row["urgent_requests"]),
                "averageActualMinutes": round(float(row["average_minutes"])) if row["average_minutes"] is not None else None}
    designers_page = _page(designer_rows, limit, offset, designer)
    assigned_page = _page(assigned_rows, assigned_limit, assigned_offset, lambda row: _report_assigned(row, handles))
    return _envelope({"totals": totals, "priorities": priorities, "designers": designers_page["data"], "assignedPosts": assigned_page["data"]},
                     {"designers": designers_page["pagination"], "assigned_posts": assigned_page["pagination"]})


EVENT_FIELDS = ("id", "request_id", "actor_email", "event_type", "details", "created_at")
EVENT_DETAIL_FIELDS = ("title", "postType", "productionPoints", "minutesPerPP", "status", "fromStatus", "toStatus",
                       "priority", "designerEmail", "scheduledDate", "scheduledStartMinutes", "note", "reason", "reviewNote")


@router.get("/queue/requests/{request_id}/history")
def queue_history(request: Request, request_id: int, limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    identity = _scope(request, "queue")
    def project(row):
        details = _json(row["details"], {})
        row["details"] = {key: details[key] for key in EVENT_DETAIL_FIELDS if key in details and isinstance(details[key], (str, int, float, bool, type(None)))}
        return row
    with db.connect() as conn:
        _queue_request(conn, identity, request_id)
        rows = _limited(conn, "queue_request_events", EVENT_FIELDS, "request_id = ?", [request_id], "created_at DESC, id DESC", limit, offset)
    return _page(rows, limit, offset, project)


@router.get("/queue/drafts")
def queue_drafts(request: Request, account: str | None = None, limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    identity = _scope(request, "queue")
    handles = _handles(identity, account)
    where, params = _queue_where(handles)
    with db.connect() as conn:
        rows = _limited(conn, "queue_schedule_drafts", DRAFT_FIELDS, where, params, "d.updated_at DESC, d.request_id", limit, offset,
                        alias="d", joins="JOIN queue_requests r ON r.id = d.request_id")
    return _page(rows, limit, offset, lambda row: _queue_project(row, handles))


@router.get("/queue/drafts/{request_id}")
def queue_draft(request: Request, request_id: int) -> dict[str, Any]:
    identity = _scope(request, "queue")
    handles = _handles(identity)
    where, params = _queue_where(handles)
    with db.connect() as conn:
        rows = _limited(conn, "queue_schedule_drafts", DRAFT_FIELDS, f"({where}) AND d.request_id = ?", [*params, request_id], "d.request_id", 1, 0,
                        alias="d", joins="JOIN queue_requests r ON r.id = d.request_id")
    if not rows:
        _missing()
    return _envelope(_queue_project(dict(rows[0]), handles))


def _ticket_rows(conn: Any, identity: dict[str, Any], limit: int, offset: int, status=None, ticket_id=None):
    where, params = _queue_where(_handles(identity))
    where = f"(({where}) OR (t.request_id IS NULL AND t.requester_email = ?))"
    params.append(identity["owner_email"])
    if status:
        where += " AND t.status = ?"
        params.append(status)
    if ticket_id is not None:
        where += " AND t.id = ?"
        params.append(ticket_id)
    return _limited(conn, "queue_tickets", TICKET_FIELDS, where, params, "t.updated_at DESC, t.id DESC", limit, offset,
                    alias="t", joins="LEFT JOIN queue_requests r ON r.id = t.request_id")


@router.get("/queue/tickets")
def queue_tickets(request: Request, status: str | None = Query(default=None, max_length=40), limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    identity = _scope(request, "queue")
    with db.connect() as conn:
        rows = _ticket_rows(conn, identity, limit, offset, status)
    return _page(rows, limit, offset, lambda row: _queue_project(row, _handles(identity)))


@router.get("/queue/tickets/{ticket_id}")
def queue_ticket(request: Request, ticket_id: int) -> dict[str, Any]:
    identity = _scope(request, "queue")
    with db.connect() as conn:
        rows = _ticket_rows(conn, identity, 1, 0, ticket_id=ticket_id)
    if not rows:
        _missing()
    return _envelope(_queue_project(dict(rows[0]), _handles(identity)))


TIME_BLOCK_FIELDS = ("id", "ticket_type", "requester_email", "status", "block_category", "title", "scheduled_date",
                     "scheduled_start_minutes", "duration_minutes", "reason", "reviewer_email", "review_note", "reviewed_at", "created_at", "updated_at")


@router.get("/queue/time-blocks")
def queue_time_blocks(request: Request, date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                      requester: str | None = Query(default=None, max_length=254),
                      status: Literal["pending", "approved", "rejected"] | None = None,
                      limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    # An explicit Queue grant from a current Admin/Dev includes the team's
    # availability, as the coordinator scheduler does. Availability is a
    # global roster resource; do not expose links to excluded source requests.
    _scope(request, "queue")
    where = "ticket_type = 'time_block' AND LOWER(COALESCE(block_category, '')) NOT IN ('move','account_request','post_suggestion','new_account')"
    params: list[Any] = []
    if status is None:
        where += " AND status IN ('pending','approved')"
    else:
        where += " AND status = ?"
        params.append(status)
    for field, value in (("scheduled_date", date), ("requester_email", requester)):
        if value:
            where += f" AND {field} = ?"
            params.append(value)
    with db.connect() as conn:
        rows = _limited(conn, "queue_tickets", TIME_BLOCK_FIELDS, where, params, "scheduled_date DESC, scheduled_start_minutes, id", limit, offset)
    return _page(rows, limit, offset)


@router.get("/queue/preferences")
def queue_preferences(request: Request) -> dict[str, Any]:
    identity = _scope(request, "queue")
    with db.connect() as conn:
        rows = _limited(conn, "queue_scheduler_preferences", ("hidden_users", "row_order", "updated_at"),
                        "viewer_email = ?", [identity["owner_email"]], "viewer_email", 1, 0)
    row = dict(rows[0]) if rows else {}
    def emails(value):
        return list(dict.fromkeys(item.strip().lower() for item in _strings(value) if item.strip()))
    return _envelope({"hidden_users": emails(row.get("hidden_users")), "row_order": emails(row.get("row_order")), "updated_at": row.get("updated_at")})


PROMO_FIELDS = ("account", "shortcode", "classification", "client", "product", "analysis_json", "review_status",
                "review_override_json", "published_at", "first_detected_at", "last_analyzed_at")


def _promo_project(row: dict[str, Any]) -> dict[str, Any]:
    analysis = _json(row.pop("analysis_json", None), {})
    review = _json(row.pop("review_override_json", None), {})
    row["review"] = {key: review.get(key) for key in ("classification", "client", "product", "reviewer", "reviewed_at", "note") if key in review and isinstance(review[key], (str, int, float, bool, type(None)))}
    semantic = _json(review.get("jev_review"), {})
    row["review"]["jev_review"] = {key: semantic[key] for key in ("semanticPromo", "needsReview", "commercialRelationship", "relationshipConfidence", "deterministicClassification", "recommendation", "guidance", "mode")
                                  if key in semantic and isinstance(semantic[key], (str, float, int, bool, type(None)))} or None
    for key in ("classification", "client", "product"):
        if row["review"].get(key) is not None:
            row[key] = row["review"][key]
    row["is_promo"] = row.get("classification") not in {None, "not_promo"}
    detected = analysis.get("detector_classification") or analysis.get("classification")
    row["detector_classification"] = detected if isinstance(detected, str) else None
    row["detector_version"] = analysis.get("detector_version") if isinstance(analysis.get("detector_version"), str) else None
    row["signals"] = _strings(analysis.get("signals"))
    row["evidence"] = [{key: item.get(key) for key in ("family", "rule", "source", "text") if isinstance(item.get(key), str)}
                       for item in _json(analysis.get("evidence"), []) if isinstance(item, dict)]
    row["client_candidates"] = [{key: item.get(key) for key in ("name", "source", "evidence") if isinstance(item.get(key), str)}
                                for item in _json(analysis.get("client_candidates"), []) if isinstance(item, dict)]
    row["links"] = [{"url": url, "source": item.get("source") if isinstance(item.get("source"), str) else None} for item in _json(analysis.get("links"), [])
                    if isinstance(item, dict) and (url := _url(item.get("url")))]
    for key in ("product", "cta", "promo_code", "classification_source"):
        if key not in row:
            row[key] = analysis.get(key) if isinstance(analysis.get(key), (str, type(None))) else None
    return row


@router.get("/promos")
def promos(request: Request, account: str | None = None, classification: str | None = Query(default=None, max_length=40),
           review_status: str | None = Query(default=None, max_length=40), q: str = Query(default="", max_length=200),
           limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    identity = _scope(request, "promos")
    where, params = _accounts_sql("account", _handles(identity, account))
    for field, value in (("classification", classification), ("review_status", review_status)):
        if value:
            where += f" AND {field} = ?"
            params.append(value)
    if q:
        where += " AND (LOWER(COALESCE(client, '')) LIKE ? OR LOWER(COALESCE(product, '')) LIKE ?)"
        params.extend([f"%{q.lower()}%"] * 2)
    with db.connect() as conn:
        rows = _limited(conn, "promo_opportunities", PROMO_FIELDS, where, params, "last_analyzed_at DESC, account, shortcode", limit, offset)
    return _page(rows, limit, offset, _promo_project)


@router.get("/promos/{account}/{shortcode}")
@router.get("/promos/{account}/{shortcode}/review")
def promo(request: Request, account: str, shortcode: str) -> dict[str, Any]:
    identity = _scope(request, "promos")
    handle = _handles(identity, account)[0]
    with db.connect() as conn:
        rows = _limited(conn, "promo_opportunities", PROMO_FIELDS, "account = ? AND shortcode = ?", [handle, shortcode], "account", 1, 0)
    if not rows:
        _missing()
    item = _promo_project(dict(rows[0]))
    return _envelope(item["review"] if request.url.path.rstrip("/").endswith("/review") else item)


NEWS_FIELDS = ("id", "item_json", "review_json", "saved", "brief", "reviewed_at", "updated_at")
NEWS_ITEM_FIELDS = ("title", "description", "published", "source", "publisher", "author", "sourceType", "feedLabel", "feedGroup", "socialSignal", "coverageCount")
REVIEW_FIELDS = ("reviewVersion", "label", "score", "confidence", "jevSignal", "targetAccount", "targetAccountConfidence",
                 "strongSignalCount", "editorialAngle", "postFormat", "viralPotential", "evidenceQuality", "mode", "evidenceSource", "evidenceText")


def _review(value: Any, handles: list[str]) -> dict[str, Any] | None:
    parsed = _json(value, {})
    if not parsed:
        return None
    result = {key: parsed[key] for key in REVIEW_FIELDS if key in parsed and isinstance(parsed[key], (str, float, int, bool, type(None)))}
    if result.get("targetAccount") not in handles:
        result["targetAccount"] = None
    result["strengths"] = _strings(parsed.get("strengths"))
    result["weaknesses"] = _strings(parsed.get("weaknesses"))
    result["dimensions"] = {key: {metric: number for metric in ("score", "confidence")
                                   if isinstance((number := detail.get(metric)), (int, float))}
                            for key, detail in _json(parsed.get("dimensions"), {}).items()
                            if key in {"insight", "audience_value", "hook", "repurpose", "evergreen", "concreteness", "distinctiveness", "viral_potential", "timeliness", "evidence_quality"} and isinstance(detail, dict)}
    novelty = _json(parsed.get("novelty"), {})
    result["novelty"] = {key: novelty[key] for key in ("score", "confidence", "isNewAngle") if isinstance(novelty.get(key), (int, float, bool))} or None
    gates = _json(parsed.get("classificationGates"), {})
    result["classification_gates"] = {
        level: {key: value for key, value in detail.items()
                if key in {"score", "jevSignal", "criticalDimensionFloor", "strongSignals", "novelty", "viralPotential", "evidenceQuality"}
                and isinstance(value, (int, float))}
        for level, detail in gates.items() if level in {"golden", "potential"} and isinstance(detail, dict)
    }
    result["existing_candidates"] = [
        {key: item[key] for key in ("account", "shortcode", "text") if isinstance(item.get(key), str)}
        for item in _json(parsed.get("existingCandidates"), []) if isinstance(item, dict) and item.get("account") in handles
    ]
    return result


def _news_project(row: dict[str, Any], handles: list[str]) -> dict[str, Any]:
    item = _json(row.pop("item_json", None), {})
    result = {key: item.get(key) for key in NEWS_ITEM_FIELDS if isinstance(item.get(key), (str, int, float, bool, type(None)))}
    result.update({key: row.get(key) for key in ("id", "brief", "reviewed_at", "updated_at")})
    result["saved"] = _flag(row.get("saved"))
    result["link"] = _url(item.get("link"))
    result["image"] = _url(item.get("image"))
    result["coverage_feeds"] = _strings(item.get("coverageFeeds"))
    result["related_stories"] = [
        {"title": related.get("title") if isinstance(related.get("title"), str) else None,
         "link": link, "source": related.get("source") if isinstance(related.get("source"), str) else None}
        for related in _json(item.get("relatedStories"), []) if isinstance(related, dict) and (link := _url(related.get("link") or related.get("url")))
    ]
    result["review"] = _review(row.get("review_json"), handles)
    return result


@router.get("/news")
@router.get("/news/briefs")
def news(request: Request, saved: bool | None = None, source_type: str | None = Query(default=None, max_length=30),
         q: str = Query(default="", max_length=200), limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    identity = _scope(request, "news")
    where, params = "1 = 1", []
    if saved is not None:
        where += " AND saved = ?"
        params.append(int(saved))
    if request.url.path.rstrip("/").endswith("/briefs"):
        where += " AND brief <> ''"
    with db.connect() as conn:
        if q:
            where += f" AND (LOWER({_json_field(conn, 'item_json', 'title')}) LIKE ? OR LOWER({_json_field(conn, 'item_json', 'description')}) LIKE ?)"
            params.extend([f"%{q.lower()}%"] * 2)
        if source_type:
            where += f" AND {_json_field(conn, 'item_json', 'sourceType')} = ?"
            params.append(source_type)
        rows = _limited(conn, "news_stories", NEWS_FIELDS, where, params, "updated_at DESC, id", limit, offset)
    return _page(rows, limit, offset, lambda row: _news_project(row, _handles(identity)))


@router.get("/news/story")
def news_story(request: Request, id: str = Query(min_length=1, max_length=4096)) -> dict[str, Any]:
    identity = _scope(request, "news")
    with db.connect() as conn:
        rows = _limited(conn, "news_stories", NEWS_FIELDS, "id = ?", [id], "id", 1, 0)
    if not rows:
        _missing()
    return _envelope(_news_project(dict(rows[0]), _handles(identity)))


HOOK_FIELDS = ("id", "source_kind", "account", "shortcode", "permalink", "published_at", "likes", "hook_text",
               "context_text", "primary_topic", "categories_json", "category_scores_json", "categorized_at", "created_at", "updated_at")
HOOK_DRAFT_FIELDS = ("id", "topic", "text", "source_hook_ids", "created_at", "updated_at")


def _hook_project(row: dict[str, Any]) -> dict[str, Any]:
    row["permalink"] = _url(row["permalink"])
    row["categories"] = _strings(row.pop("categories_json", None))
    row["category_scores"] = {key: value for key, value in _json(row.pop("category_scores_json", None), {}).items()
                              if key in row["categories"] and isinstance(value, (int, float))}
    return row


@router.get("/hooks")
@router.get("/hooks/saved")
def hooks(request: Request, account: str | None = None, q: str = Query(default="", max_length=200),
          topic: str | None = Query(default=None, max_length=80), limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    identity = _scope(request, "hooks")
    where, params = _accounts_sql("h.account", _handles(identity, account))
    where += " AND h.source_kind = 'ocr'"
    if q:
        where += " AND (LOWER(h.hook_text) LIKE ? OR LOWER(h.context_text) LIKE ?)"
        params.extend([f"%{q.lower()}%"] * 2)
    if topic:
        where += " AND h.primary_topic = ?"
        params.append(topic)
    if request.url.path.rstrip("/").endswith("/saved"):
        where += " AND EXISTS (SELECT 1 FROM hook_saves s WHERE s.hook_id = h.id AND s.owner_email = ?)"
        params.append(identity["owner_email"])
    with db.connect() as conn:
        rows = _limited(conn, "hook_sources", HOOK_FIELDS, where, params, "COALESCE(h.likes, -1) DESC, h.id", limit, offset, alias="h")
    return _page(rows, limit, offset, _hook_project)


def _draft_project(conn: Any, row: dict[str, Any], handles: list[str]) -> dict[str, Any]:
    ids = _strings(row.pop("source_hook_ids", None))
    row["source_hook_ids"] = []
    if ids and _columns(conn, "hook_sources"):
        where, params = _accounts_sql("account", handles)
        allowed = {item["id"] for item in conn.execute(f"SELECT id FROM hook_sources WHERE ({where}) AND source_kind = 'ocr' "
                   f"AND id IN ({','.join('?' for _ in ids)})", (*params, *ids)).fetchall()}
        row["source_hook_ids"] = [item for item in ids if item in allowed]
    return row


@router.get("/hooks/drafts")
def hook_drafts(request: Request, limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    identity = _scope(request, "hooks")
    with db.connect() as conn:
        rows = _limited(conn, "hook_drafts", HOOK_DRAFT_FIELDS, "owner_email = ?", [identity["owner_email"]], "updated_at DESC, id", limit, offset)
        return _page(rows, limit, offset, lambda row: _draft_project(conn, row, _handles(identity)))


@router.get("/hooks/drafts/{draft_id}")
def hook_draft(request: Request, draft_id: str) -> dict[str, Any]:
    identity = _scope(request, "hooks")
    with db.connect() as conn:
        rows = _limited(conn, "hook_drafts", HOOK_DRAFT_FIELDS, "owner_email = ? AND id = ?", [identity["owner_email"], draft_id], "id", 1, 0)
        if not rows:
            _missing()
        return _envelope(_draft_project(conn, dict(rows[0]), _handles(identity)))


@router.get("/hooks/{hook_id}")
def hook(request: Request, hook_id: str) -> dict[str, Any]:
    identity = _scope(request, "hooks")
    where, params = _accounts_sql("account", _handles(identity))
    with db.connect() as conn:
        rows = _limited(conn, "hook_sources", HOOK_FIELDS, f"({where}) AND source_kind = 'ocr' AND id = ?", [*params, hook_id], "id", 1, 0)
    if not rows:
        _missing()
    return _envelope(_hook_project(dict(rows[0])))


VAULT_FIELDS = ("id", "url", "title", "source", "shared_at", "slack_url", "priority", "discarded", "done", "tweet_text",
                "tweet_author", "tweet_image", "tweet_avatar", "tweet_media_type", "text_status", "pool_request_id")


def _vault_project(row: dict[str, Any]) -> dict[str, Any]:
    for field in ("url", "slack_url", "tweet_image", "tweet_avatar"):
        row[field] = _url(row.get(field))
    for field in ("discarded", "done"):
        row[field] = _flag(row.get(field))
    # A shared Vault item must not disclose an excluded account's Queue id.
    row.pop("pool_request_id", None)
    return row


@router.get("/vault")
def vault(request: Request, discarded: bool | None = None, done: bool | None = None,
          q: str = Query(default="", max_length=200), limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    _scope(request, "vault")
    where, params = "1 = 1", []
    for field, value in (("discarded", discarded), ("done", done)):
        if value is not None:
            where += f" AND {field} = ?"
            params.append(int(value))
    if q:
        where += " AND (LOWER(title) LIKE ? OR LOWER(tweet_text) LIKE ?)"
        params.extend([f"%{q.lower()}%"] * 2)
    with db.connect() as conn:
        rows = _limited(conn, "vault_links", VAULT_FIELDS, where, params, "priority, shared_at DESC, id", limit, offset)
    return _page(rows, limit, offset, _vault_project)


@router.get("/vault/{link_id}")
def vault_link(request: Request, link_id: str) -> dict[str, Any]:
    _scope(request, "vault")
    with db.connect() as conn:
        rows = _limited(conn, "vault_links", VAULT_FIELDS, "id = ?", [link_id], "id", 1, 0)
    if not rows:
        _missing()
    return _envelope(_vault_project(dict(rows[0])))


ACCOUNT_FIELDS = ("handle", "label", "group_name", "category", "subcategory", "hot_threshold", "scrape_mode",
                  "research_enabled", "promos_enabled", "is_canonical", "is_active", "created_at", "updated_at")
USER_FIELDS = ("email", "display_name", "role", "operating_role", "operating_roles", "is_admin", "time_zone",
               "minutes_per_pp", "created_at", "updated_at")
ASSIGNMENT_FIELDS = ("id", "post_account", "post_shortcode", "assignee_email", "status", "note", "priority", "due_date",
                     "tags", "position", "recommended_account", "created_by_email", "created_at", "updated_at")


def _assignment_project(row: dict[str, Any], handles: list[str]) -> dict[str, Any]:
    row["tags"] = _strings(row.get("tags"))
    row["recommended_account"] = row.get("recommended_account") if row.get("recommended_account") in handles else None
    row["priority"] = "urgent" if row.get("priority") == "urgent" else "normal"
    return row


@router.get("/queue/assignments")
def queue_assignments(request: Request, account: str | None = None, assignee: str | None = Query(default=None, max_length=254),
                      status: str | None = Query(default=None, max_length=40), limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    identity = _scope(request, "queue")
    handles = _handles(identity, account)
    where, params = _accounts_sql("post_account", handles)
    for field, value in (("assignee_email", assignee), ("status", status)):
        if value:
            where += f" AND {field} = ?"
            params.append(value)
    with db.connect() as conn:
        rows = _limited(conn, "post_assignments", ASSIGNMENT_FIELDS, where, params, "updated_at DESC, id DESC", limit, offset)
    return _page(rows, limit, offset, lambda row: _assignment_project(row, handles))


def _assignment(conn: Any, identity: dict[str, Any], assignment_id: int) -> dict[str, Any]:
    handles = _handles(identity)
    where, params = _accounts_sql("post_account", handles)
    rows = _limited(conn, "post_assignments", ASSIGNMENT_FIELDS, f"({where}) AND id = ?", [*params, assignment_id], "id", 1, 0)
    if not rows:
        _missing()
    return _assignment_project(dict(rows[0]), handles)


@router.get("/queue/assignments/{assignment_id}")
def queue_assignment(request: Request, assignment_id: int) -> dict[str, Any]:
    identity = _scope(request, "queue")
    with db.connect() as conn:
        return _envelope(_assignment(conn, identity, assignment_id))


@router.get("/queue/assignments/{assignment_id}/history")
def queue_assignment_history(request: Request, assignment_id: int, limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    identity = _scope(request, "queue")
    with db.connect() as conn:
        _assignment(conn, identity, assignment_id)
        rows = _limited(conn, "post_assignment_events", ("id", "assignment_id", "actor_email", "event_type", "details", "created_at"),
                        "assignment_id = ?", [assignment_id], "created_at DESC, id DESC", limit, offset)
    def project(row):
        details = _json(row["details"], {})
        row["details"] = {key: details[key] for key in (*EVENT_DETAIL_FIELDS, "note", "dueDate", "assigneeEmail") if key in details and isinstance(details[key], (str, int, float, bool, type(None)))}
        return row
    return _page(rows, limit, offset, project)


@router.get("/queue/team")
def queue_team(request: Request, limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    identity = _scope(request, "queue")
    with db.connect() as conn:
        rows = _limited(conn, "dashboard_users", ("email", "display_name", "operating_role", "operating_roles", "time_zone", "minutes_per_pp"),
                        "1 = 1", [], "email", limit, offset)
        emails = [row["email"] for row in rows[:limit]]
        managed: dict[str, list[str]] = {}
        if emails and _columns(conn, "queue_designer_accounts"):
            where, params = _accounts_sql("account_handle", _handles(identity))
            mappings = conn.execute(f"SELECT designer_email, account_handle FROM queue_designer_accounts "
                                    f"WHERE ({where}) AND designer_email IN ({','.join('?' for _ in emails)}) "
                                    "ORDER BY designer_email, account_handle LIMIT 10000", (*params, *emails)).fetchall()
            for mapping in mappings:
                managed.setdefault(mapping["designer_email"], []).append(mapping["account_handle"])
        def project(row):
            row["operating_roles"] = _strings(row["operating_roles"])
            row["managed_accounts"] = managed.get(row["email"], [])
            return row
        return _page(rows, limit, offset, project)


@router.get("/settings/accounts")
def settings_accounts(request: Request, account: str | None = None, limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    identity = _scope(request, "settings")
    where, params = _accounts_sql("handle", _handles(identity, account))
    def project(row):
        for field in ("research_enabled", "promos_enabled", "is_canonical", "is_active"):
            row[field] = _flag(row[field])
        return row
    with db.connect() as conn:
        rows = _limited(conn, "accounts", ACCOUNT_FIELDS, where, params, "handle", limit, offset)
    return _page(rows, limit, offset, project)


def _user_project(row: dict[str, Any]) -> dict[str, Any]:
    row["operating_roles"] = _strings(row["operating_roles"])
    row["is_admin"] = bool(_flag(row["is_admin"]) or row["role"] == "admin")
    return row


@router.get("/settings/users")
def settings_users(request: Request, q: str = Query(default="", max_length=200), limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    _scope(request, "settings")
    where, params = "1 = 1", []
    if q:
        where += " AND (LOWER(email) LIKE ? OR LOWER(display_name) LIKE ?)"
        params.extend([f"%{q.lower()}%"] * 2)
    with db.connect() as conn:
        rows = _limited(conn, "dashboard_users", USER_FIELDS, where, params, "email", limit, offset)
    return _page(rows, limit, offset, _user_project)


@router.get("/settings/usage")
def settings_usage(request: Request, days: int = Query(default=30, ge=1, le=90),
                   limit: int = LIMIT, offset: int = OFFSET) -> dict[str, Any]:
    _scope(request, "settings")
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    with db.connect() as conn:
        # URL paths may contain query state or private object identifiers.
        # Export counts and clocks, never raw requests or authentication data.
        rows = [] if not _columns(conn, "usage_log") else conn.execute(
            "SELECT email, COUNT(*) AS request_count, MAX(ts) AS last_seen_at FROM usage_log "
            "WHERE ts >= ? GROUP BY email ORDER BY last_seen_at DESC, email LIMIT ? OFFSET ?",
            (since, limit + 1, offset)).fetchall()
    result = _page(rows, limit, offset)
    result["period"] = {"days": days, "since": since}
    return result


@router.get("/settings/preferences")
def settings_preferences(request: Request) -> dict[str, Any]:
    identity = _scope(request, "settings")
    with db.connect() as conn:
        rows = _limited(conn, "user_preferences", ("preferences", "updated_at"), "email = ?", [identity["owner_email"]], "email", 1, 0)
    row = dict(rows[0]) if rows else {}
    preferences = {}
    for key, value in _json(row.get("preferences"), {}).items():
        if key not in PREFERENCE_VALIDATORS:
            continue
        try:
            preferences[key] = PREFERENCE_VALIDATORS[key](value)
        except (ValueError, TypeError):
            continue
    if "trackerFavorites" in preferences:
        preferences["trackerFavorites"] = [handle for handle in preferences["trackerFavorites"] if handle in _handles(identity)]
    return _envelope({"preferences": preferences, "updated_at": row.get("updated_at")})


@router.get("/settings/users/{email}")
def settings_user(request: Request, email: str) -> dict[str, Any]:
    _scope(request, "settings")
    with db.connect() as conn:
        rows = _limited(conn, "dashboard_users", USER_FIELDS, "email = ?", [email.lower()], "email", 1, 0)
    if not rows:
        _missing()
    return _envelope(_user_project(dict(rows[0])))
