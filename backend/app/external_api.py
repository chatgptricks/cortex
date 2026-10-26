"""Account-scoped website credentials and versioned, public-data-only reads.

Website keys cannot inherit the owner's internal Dashboard or MCP access.
Reads use stored observations and never enqueue refreshes or provider calls.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field

from . import account_media_kit, db
from .promo_classification import is_public_promo
from .public_collaboration import public_collaboration
from .public_media_kit import project_public_media_kit
from .media_storage import redirect_url

PREFIX = "sad_api_"
MANAGEMENT_URL = "/api/dashboard/me/api-keys"
VERSION = "1.0"
RATE_LIMIT = 60
LOCAL_ZONE = timezone(timedelta(hours=-6))
_HANDLE = re.compile(r"[a-z0-9_.]{1,30}\Z")
_SHORTCODE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_PUBLIC_FIELDS = "id, name, key_prefix, account_handles, created_at, expires_at, revoked_at, last_used_at"
DATA_SCOPES = ("accounts", "research", "tracker", "insights", "queue", "promos", "news", "hooks", "vault", "settings")
GLOBAL_SCOPES = frozenset({"queue", "news", "vault", "hooks", "settings"})
_PUBLIC_FIELDS += ", data_scopes, owner_email"


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    response.headers["X-Content-Type-Options"] = "nosniff"


management_router = APIRouter(prefix=MANAGEMENT_URL, tags=["website-api-keys"], dependencies=[
    Depends(_no_store), Depends(HTTPBearer(auto_error=False, scheme_name="FirebaseSession", description="Signed-in Dashboard Firebase ID token"))])
router = APIRouter(prefix="/api/v1", tags=["website-api-v1"], dependencies=[
    Depends(_no_store), Depends(HTTPBearer(auto_error=False, scheme_name="WebsiteAPIKey", description="Account-scoped website API key, beginning sad_api_"))])
PROFILE_PREVIEW_PATH = "/api/decks/profile-preview"
profile_router = APIRouter(tags=["deck-profiles"], dependencies=[
    Depends(_no_store), Depends(HTTPBearer(auto_error=False, scheme_name="WebsiteAPIKey"))])


def ensure_schema(conn: Any) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS website_api_keys (
        id TEXT PRIMARY KEY, owner_email TEXT NOT NULL, name TEXT NOT NULL,
        key_hash TEXT UNIQUE NOT NULL, key_prefix TEXT NOT NULL,
        account_handles TEXT NOT NULL, created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL, revoked_at TEXT, last_used_at TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_website_api_keys_owner ON website_api_keys(owner_email)")
    db._ensure_column(conn, "website_api_keys", "data_scopes", "data_scopes TEXT NOT NULL DEFAULT '[\"accounts\"]'")
    conn.execute("""CREATE TABLE IF NOT EXISTS website_api_rate_windows (
        key_id TEXT PRIMARY KEY, minute_window BIGINT NOT NULL,
        request_count INTEGER NOT NULL
    )""")


def _admin(email: str, access: dict[str, Any] | None) -> bool:
    from .private_roster import user_flags
    if access is None:
        return False
    try:
        roles = json.loads(access.get("operating_roles") or "[]")
    except (TypeError, ValueError):
        roles = []
    return bool(access.get("is_admin")) or bool(user_flags(email, roles).get("is_dev"))


def _dev(email: str, access: dict[str, Any] | None) -> bool:
    from .private_roster import user_flags
    if access is None:
        return False
    try:
        roles = json.loads(access.get("operating_roles") or "[]")
    except (TypeError, ValueError):
        roles = []
    return bool(user_flags(email, roles).get("is_dev"))


def _browser_owner(request: Request) -> str:
    if getattr(request.state, "auth_method", None) != "firebase":
        raise HTTPException(403, "Use your signed-in browser to manage website API keys.")
    email = str(getattr(request.state, "user_email", "") or "").strip().lower()
    if not email:
        raise HTTPException(401, "Sign in required.")
    return email


def _active_accounts(conn: Any) -> dict[str, dict[str, Any]]:
    columns = account_media_kit._columns(conn, "accounts")
    selected = ["handle", "is_active"] + sorted(columns & {"group_name", "subcategory"})
    return {str(row["handle"]).lower(): dict(row) for row in conn.execute(
        f"SELECT {', '.join(selected)} FROM accounts WHERE is_active = 1 ORDER BY handle"
    ).fetchall()}


def _public_accounts(conn: Any, handles: list[str]) -> list[dict[str, Any]]:
    """Profile names come from observations; registry labels are internal."""
    names: dict[str, str] = {}
    if "full_name" in account_media_kit._columns(conn, "account_snapshots"):
        if not handles:
            return []
        placeholders = ",".join("?" for _ in handles)
        for row in conn.execute(f"SELECT handle, full_name, captured_at FROM account_snapshots WHERE LOWER(handle) IN ({placeholders}) ORDER BY captured_at DESC", handles).fetchall():
            handle = str(row["handle"]).lower()
            captured = account_media_kit._date(row["captured_at"])
            if handle in handles and handle not in names and captured and captured <= datetime.now(UTC):
                name = row["full_name"]
                if isinstance(name, str) and name.strip():
                    names[handle] = name.strip()[:200]
    return [{"handle": handle, "public_name": names.get(handle, f"@{handle}"),
             "profile_url": f"https://www.instagram.com/{handle}/"} for handle in handles]


def _connection(row: Any) -> dict[str, Any]:
    value = dict(row)
    value["account_handles"] = json.loads(value["account_handles"])
    value["data_scopes"] = _stored_scopes(value.get("data_scopes"))
    return value


def _stored_scopes(value: Any) -> list[str]:
    if value is None:
        return ["accounts"]
    try:
        scopes = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return []
    return [scope for scope in DATA_SCOPES if isinstance(scopes, list) and scope in scopes]


def _available_scopes(owner: str, access: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not _admin(owner, access):
        return []
    from .private_roster import user_flags
    try:
        roles = json.loads(access.get("operating_roles") or "[]")
    except (TypeError, ValueError):
        roles = []
    flags = user_flags(owner, roles)
    dev = bool(flags.get("is_dev"))
    allowed = set(DATA_SCOPES) - {"news", "hooks", "vault"}
    if dev:
        allowed.update({"news", "hooks", "vault"})
    elif flags.get("can_access_news"):
        allowed.add("news")
    return [{"id": scope, "global": scope in GLOBAL_SCOPES} for scope in DATA_SCOPES if scope in allowed]


def _validate_scopes(scopes: list[str], owner: str, access: dict[str, Any] | None) -> list[str]:
    selected = [scope for scope in DATA_SCOPES if scope in scopes]
    if not selected or any(scope not in DATA_SCOPES for scope in scopes):
        raise HTTPException(422, "Choose one or more supported read-only data areas.")
    allowed = {item["id"] for item in _available_scopes(owner, access)}
    if not set(selected).issubset(allowed):
        raise HTTPException(403, "Your current product permissions do not include every selected data area.")
    return selected


class CreateAPIKey(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    account_handles: list[str] = Field(min_length=1, max_length=1000)
    expires_in_days: Literal[30, 90, 365] = 90
    data_scopes: list[str] = Field(default_factory=lambda: ["accounts"], min_length=1, max_length=len(DATA_SCOPES))


class UpdateAPIKeyScopes(BaseModel):
    data_scopes: list[str] = Field(min_length=1, max_length=len(DATA_SCOPES))


@management_router.get("")
def list_keys(request: Request) -> dict[str, Any]:
    owner = _browser_owner(request)
    access = db.get_dashboard_user_access(owner)
    allowed = _admin(owner, access)
    can_manage_all = _dev(owner, access)
    with db.connect() as conn:
        ensure_schema(conn)
        where = "owner_email = ?"
        params: tuple[Any, ...] = (owner,)
        if can_manage_all:
            where = "owner_email = ? OR (revoked_at IS NULL AND expires_at > ?)"
            params = (owner, datetime.now(UTC).isoformat(timespec="seconds"))
        keys = [_connection(row) for row in conn.execute(
            f"SELECT {_PUBLIC_FIELDS} FROM website_api_keys WHERE {where} ORDER BY created_at DESC, id DESC", params
        ).fetchall()]
        for key in keys:
            key["can_edit"] = allowed and key["owner_email"] == owner
            key["can_revoke"] = can_manage_all or key["owner_email"] == owner
        active = _active_accounts(conn) if allowed else {}
        accounts = _public_accounts(conn, sorted(active)) if allowed else []
    return {"keys": keys, "available_accounts": [{"handle": value["handle"], "public_name": value["public_name"],
                "group": "sentient" if active[value["handle"]].get("group_name") == "sentient" else "others",
                "group_name": active[value["handle"]].get("group_name") or "others",
                "subcategory": active[value["handle"]].get("subcategory") or "other"} for value in accounts],
            "available_scopes": _available_scopes(owner, access), "can_create": allowed, "can_manage_all": can_manage_all,
            "rate_limit_per_minute": RATE_LIMIT, "max_accounts_per_key": 1000}


@management_router.post("", status_code=201)
def create_key(request: Request, payload: CreateAPIKey) -> dict[str, Any]:
    owner = _browser_owner(request)
    access = db.get_dashboard_user_access(owner)
    if not _admin(owner, access):
        raise HTTPException(403, "Admin or Dev access is required to create website API keys.")
    scopes = _validate_scopes(payload.data_scopes, owner, access)
    name = payload.name.strip()
    handles = sorted(set(handle.strip().lstrip("@").lower() for handle in payload.account_handles))
    if not name or any(not _HANDLE.fullmatch(handle) for handle in handles):
        raise HTTPException(422, "Provide a name and valid Instagram account handles.")
    now = datetime.now(UTC)
    created = now.isoformat(timespec="seconds")
    expires = (now + timedelta(days=payload.expires_in_days)).isoformat(timespec="seconds")
    key = PREFIX + secrets.token_urlsafe(32)
    identity = secrets.token_hex(16)
    with db.connect() as conn:
        ensure_schema(conn)
        active = _active_accounts(conn)
        if any(handle not in active for handle in handles):
            raise HTTPException(422, "Choose active Dashboard accounts only.")
        count = conn.execute("SELECT COUNT(*) AS n FROM website_api_keys WHERE owner_email = ? AND revoked_at IS NULL AND expires_at > ?", (owner, created)).fetchone()["n"]
        if count >= 20:
            raise HTTPException(409, "Revoke an existing API key before creating more (limit 20).")
        row = {"id": identity, "owner_email": owner, "name": name, "key_prefix": key[:15], "account_handles": handles,
               "can_edit": True, "can_revoke": True,
               "created_at": created, "expires_at": expires, "revoked_at": None, "last_used_at": None, "data_scopes": scopes}
        conn.execute("""INSERT INTO website_api_keys
            (id, owner_email, name, key_hash, key_prefix, account_handles, created_at, expires_at, data_scopes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (identity, owner, name, hashlib.sha256(key.encode()).hexdigest(),
                key[:15], json.dumps(handles), created, expires, json.dumps(scopes)))
    return {"key": key, "connection": row}


@management_router.patch("/{key_id}")
def update_key_scopes(request: Request, key_id: str, payload: UpdateAPIKeyScopes) -> dict[str, Any]:
    owner = _browser_owner(request)
    access = db.get_dashboard_user_access(owner)
    scopes = _validate_scopes(payload.data_scopes, owner, access)
    with db.connect() as conn:
        ensure_schema(conn)
        row = conn.execute(f"SELECT {_PUBLIC_FIELDS} FROM website_api_keys WHERE id = ? AND owner_email = ? AND revoked_at IS NULL AND expires_at > ?",
                           (key_id, owner, datetime.now(UTC).isoformat(timespec="seconds"))).fetchone()
        if row is None:
            raise HTTPException(404, "Active API key not found.")
        conn.execute("UPDATE website_api_keys SET data_scopes = ? WHERE id = ? AND owner_email = ?", (json.dumps(scopes), key_id, owner))
        updated = _connection(row)
        updated["data_scopes"] = scopes
        updated.update(can_edit=True, can_revoke=True)
    return {"connection": updated}


def transient_storage_error(error: Exception) -> bool:
    """Retryable database pressure must not masquerade as an auth failure."""
    if type(error).__name__ == "PoolTimeout":
        return True
    return getattr(error, "sqlstate", None) in {"08000", "08003", "08006", "53300", "53400", "57014", "57P01", "57P02", "57P03"}


@management_router.delete("/{key_id}")
def revoke_key(request: Request, key_id: str) -> dict[str, Any]:
    owner = _browser_owner(request)
    can_manage_all = _dev(owner, db.get_dashboard_user_access(owner))
    with db.connect() as conn:
        ensure_schema(conn)
        where = "id = ?" if can_manage_all else "id = ? AND owner_email = ?"
        params = (key_id,) if can_manage_all else (key_id, owner)
        row = conn.execute(f"SELECT id FROM website_api_keys WHERE {where}", params).fetchone()
        if row is None:
            raise HTTPException(404, "API key not found.")
        conn.execute(f"UPDATE website_api_keys SET revoked_at = COALESCE(revoked_at, ?) WHERE {where}", (db.utc_now(), *params))
    return {"revoked": True, "id": key_id}


def _versioned_routes():
    from .external_research import router as research_router
    from .external_workspace import router as workspace_router
    return [route for source in (router, research_router, workspace_router) for route in source.routes
            if "GET" in getattr(route, "methods", set())]


def _route_scopes(path: str) -> tuple[str, ...]:
    if path == PROFILE_PREVIEW_PATH:
        return ("accounts",)
    part = path.removeprefix("/api/v1").strip("/").split("/")[0]
    if part in {"capabilities", "openapi.json"}:
        return ()
    if part == "hot":
        return ("accounts", "research")
    if part == "accounts" or path.startswith("/api/v1/posts/"):
        return ("accounts",)
    if part == "posts":
        return ("research",)
    return (part,) if part in DATA_SCOPES else ("research",)


def authenticate(key: str, path: str, method: str) -> dict[str, Any]:
    if not re.fullmatch(r"sad_api_[A-Za-z0-9_-]{43}", key):
        raise HTTPException(401, "Invalid or expired website API key.")
    # Public profile lookup is an explicit, bounded action for the deck editor.
    # Ordinary versioned reads remain GET-only and never contact a provider.
    profile_action = path == PROFILE_PREVIEW_PATH and method == "POST"
    if not profile_action and (method != "GET" or not any(route.path_regex.fullmatch(path.rstrip("/")) for route in _versioned_routes())):
        raise HTTPException(403, "Website API keys can only read the versioned website API.")
    now = datetime.now(UTC)
    timestamp = now.isoformat(timespec="seconds")
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    with db.connect() as conn:
        row = conn.execute("SELECT owner_email FROM website_api_keys WHERE key_hash = ? AND revoked_at IS NULL AND expires_at > ?", (key_hash, timestamp)).fetchone()
        if row is None:
            raise HTTPException(401, "Invalid or expired website API key.")
        owner = row["owner_email"]
    # The normal owner lookup opens its own pooled connection. Release the
    # credential-read connection first so concurrent website requests cannot
    # fill the pool while each waits for a second connection from that pool.
    access = db.get_dashboard_user_access(owner)
    if not _admin(owner, access):
        raise HTTPException(403, "The API key owner no longer has Admin or Dev access.")
    now = datetime.now(UTC)
    timestamp = now.isoformat(timespec="seconds")
    with db.connect() as conn:
        # Recheck after the owner lookup: revocation/expiry during that gap
        # must fail before reading accounts or consuming the request quota.
        row = conn.execute("SELECT * FROM website_api_keys WHERE key_hash = ? AND owner_email = ? AND revoked_at IS NULL AND expires_at > ?", (key_hash, owner, timestamp)).fetchone()
        if row is None:
            raise HTTPException(401, "Invalid or expired website API key.")
        available = _available_scopes(owner, access)
        allowed_scopes = {item["id"] for item in available}
        scopes = [scope for scope in _stored_scopes(dict(row).get("data_scopes")) if scope in allowed_scopes]
        required = _route_scopes(path)
        if required and not set(required).intersection(scopes):
            raise HTTPException(403, "This API key cannot read this data area. Update its read-only areas in API connections.")
        active = _active_accounts(conn)
        handles = [handle for handle in json.loads(row["account_handles"]) if handle in active]
        parts = path.strip("/").split("/")
        if len(parts) > 3 and parts[2] == "accounts" and parts[3] not in handles:
            raise HTTPException(404, "Account not available for this API key.")
        minute = int(now.timestamp()) // 60
        # Both SQLite and PostgreSQL serialize conflicting upserts. The
        # conditional RETURNING admits precisely sixty calls, even across
        # concurrent workers; denied calls do not grow the counter.
        admitted = conn.execute("""INSERT INTO website_api_rate_windows (key_id, minute_window, request_count)
            VALUES (?, ?, 1) ON CONFLICT(key_id) DO UPDATE SET
            minute_window = excluded.minute_window,
            request_count = CASE WHEN website_api_rate_windows.minute_window = excluded.minute_window
                THEN website_api_rate_windows.request_count + 1 ELSE 1 END
            WHERE website_api_rate_windows.minute_window <> excluded.minute_window
                OR website_api_rate_windows.request_count < ?
            RETURNING request_count""", (row["id"], minute, RATE_LIMIT)).fetchone()
        if admitted is None:
            raise HTTPException(429, "Website API rate limit exceeded.", headers={"Retry-After": str(60 - int(now.timestamp()) % 60)})
        conn.execute("UPDATE website_api_keys SET last_used_at = ? WHERE id = ?", (timestamp, row["id"]))
    return {"id": row["id"], "owner_email": owner, "account_handles": handles,
            "data_scopes": scopes, "available_scopes": available,
            "rate_limit_remaining": RATE_LIMIT - int(admitted["request_count"]), "rate_limit_reset": (minute + 1) * 60}


def _identity(request: Request) -> dict[str, Any]:
    value = getattr(request.state, "website_api_key", None)
    if not value:
        raise HTTPException(401, "A website API key is required.")
    return value


def require_scope(request: Request, *scopes: str) -> dict[str, Any]:
    """Domain adapters only receive the already revalidated key identity."""
    identity = _identity(request)
    granted = identity.get("data_scopes", ["accounts"])
    if not set(scopes).intersection(granted):
        raise HTTPException(403, "This API key cannot read this data area.")
    return identity


@router.get("/capabilities", summary="Discover this key's readable areas, accounts and operations")
def capabilities(request: Request) -> dict[str, Any]:
    identity = _identity(request)
    scopes = set(identity.get("data_scopes", ["accounts"]))
    operations = []
    for route in _versioned_routes():
        required = _route_scopes(route.path)
        operations.append({"method": "GET", "path": route.path, "required_scopes": list(required),
                           "available": not required or bool(scopes.intersection(required)),
                           "summary": route.summary or route.name.replace("_", " ")})
    return {"schema_version": VERSION, "base_url": "/api/v1", "read_only": True, "methods": ["GET"],
            "account_handles": identity["account_handles"], "data_scopes": sorted(scopes),
            "available_scopes": identity.get("available_scopes", []), "rate_limit_per_minute": RATE_LIMIT,
            "operations": sorted(operations, key=lambda item: item["path"]),
            "restrictions": ["Only stored data; no refresh, scraping, generation or product mutations.",
                             "Account records are limited to the key's selected active accounts.",
                             "Workspace areas require an explicit grant and the owner's current product permission."]}


@router.get("/openapi.json", summary="Machine-readable contract for all supported read-only data routes")
def api_contract(request: Request) -> dict[str, Any]:
    from fastapi.openapi.utils import get_openapi
    identity = _identity(request)
    scopes = set(identity.get("data_scopes", ["accounts"]))
    spec = get_openapi(title="Sentient Dash read-only data API", version=VERSION, routes=_versioned_routes())
    schemas = spec.setdefault("components", {}).setdefault("schemas", {})
    nullable_number = {"type": ["number", "null"]}
    nullable_text = {"type": ["string", "null"]}
    schemas["ReadPagination"] = {"type": "object", "required": ["limit", "offset", "has_more", "next_offset"], "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "offset": {"type": "integer", "minimum": 0},
        "total": {"type": ["integer", "null"]}, "count": {"type": "integer"}, "has_more": {"type": "boolean"},
        "next_offset": {"type": ["integer", "null"]}, "offset_type": {"type": "string", "enum": ["candidate"]},
        "scanned_count": {"type": "integer"}, "candidate_total": {"type": "integer"}, "scan_limit": {"type": "integer"}},
        "description": "Always follow next_offset. Candidate scans can return a short/empty page with has_more=true and total=null."}
    schemas["HotEvidence"] = {"type": "object", "properties": {
        "is_hot": {"type": "boolean"}, "score": nullable_number, "multiplier": nullable_number,
        "criterion": {"type": "string"}, "observed_at": nullable_text,
        "account_threshold": nullable_number, "likes_at_first_check": nullable_number, "score_unit": {"type": "string"}}}
    schemas["ReadPost"] = {"type": "object", "required": ["shortcode", "permalink", "likes", "comments"], "properties": {
        **{name: nullable_text for name in ("account", "caption", "published_at", "metrics_updated_at", "title", "ocr_text")},
        "shortcode": {"type": "string"}, "permalink": {"type": "string", "format": "uri"},
        "format": {"type": "string"}, **{name: nullable_number for name in ("likes", "comments", "views", "video_views", "video_plays")},
        "is_promo": {"type": "boolean"}, "is_hot": {"type": "boolean"}, "is_collab": {"type": ["boolean", "null"]},
        "collaborators": {"type": "array", "items": {"type": "string"}}, "cover_url": {"type": "string"},
        "hot": {"$ref": "#/components/schemas/HotEvidence"}},
        "description": "Known fields of a stored post. Unknown metrics remain null. Research grants may add OCR, curation and enrichment fields."}
    schemas["QueueReportPagination"] = {"type": "object", "required": ["designers", "assigned_posts"], "properties": {
        "designers": {"$ref": "#/components/schemas/ReadPagination"},
        "assigned_posts": {"$ref": "#/components/schemas/ReadPagination"}},
        "description": "Independent designer and assigned-post pages; follow each page's next_offset with its own offset parameter."}
    schemas["ReadMusicPost"] = {"type": "object", "required": ["account", "shortcode", "song", "artist", "audio_id", "is_original_audio", "likes"], "properties": {
        **{name: nullable_text for name in ("account", "shortcode", "published_at", "permalink", "format", "metrics_updated_at", "song", "artist", "audio_id", "audio_url")},
        "accounts": {"type": "array", "items": {"type": "string"}, "description": "Represented accounts restricted to the request's authorized account selection."},
        "is_original_audio": {"type": ["boolean", "null"]},
        **{name: nullable_number for name in ("likes", "comments", "views")}},
        "description": "Retained music and engagement for one account/post row. Exact audio IDs are strings; missing and placeholder IDs are null. Original-audio names remain unchanged. Deduplicate shortcode for cross-account totals."}
    schemas["ReadEnvelope"] = {"type": "object", "required": ["schema_version", "data"], "properties": {
        "schema_version": {"type": "string"}, "generated_at": {"type": "string", "format": "date-time"},
        "data": {"oneOf": [{"type": "object"}, {"type": "array", "items": {"type": "object"}}]},
        "pagination": {"oneOf": [{"$ref": "#/components/schemas/ReadPagination"}, {"$ref": "#/components/schemas/QueueReportPagination"}]}},
        "description": "Common stored-data envelope. Domain fields are defined in the bilingual guide; optional metadata differs by endpoint."}
    for name, data_schema in (("PostPage", {"type": "array", "items": {"$ref": "#/components/schemas/ReadPost"}}),
                              ("MusicPostPage", {"type": "array", "items": {"$ref": "#/components/schemas/ReadMusicPost"}}),
                              ("PostResult", {"$ref": "#/components/schemas/ReadPost"})):
        schemas[name] = {"allOf": [{"$ref": "#/components/schemas/ReadEnvelope"}, {"type": "object", "properties": {"data": data_schema}}]}
    schemas["MusicPostPage"]["allOf"][1]["properties"].update({
        "coverage": {"type": "object", "properties": {
            "scope": {"type": "string", "const": "returned_page"},
            **{field: {"type": "integer", "minimum": 0} for field in ("posts", "with_music_metadata", "unknown_music", "with_audio_id")}}},
        "ordering": {"type": "object", "properties": {
            "sort": {"type": "string"}, "basis": {"type": "string", "const": "stored_post_index"},
            "direction": {"type": "string", "const": "descending"}, "note": {"type": "string"}},
            "description": "Pagination ranks stored post fields before bounded hydration. Returned metrics may be newer; collect pages and re-sort for an exact current ranking."},
    })
    for path, operations in spec.get("paths", {}).items():
        for operation in operations.values():
            if isinstance(operation, dict):
                required = _route_scopes(path)
                operation["x-required-scopes"] = list(required)
                operation["x-available"] = not required or bool(scopes.intersection(required))
                if path.endswith("/cover") or path.endswith("/content") or path.endswith("/media/{index}"):
                    operation["responses"]["200"] = {"description": "Stored asset bytes", "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}
                    if path.endswith("/cover"):
                        operation["responses"].pop("200", None)
                        operation["responses"]["307"] = {"description": "Redirect to the stored asset; never forward the API credential to another origin."}
                elif path not in {"/api/v1/capabilities", "/api/v1/openapi.json"}:
                    schema = "PostPage" if path.endswith("/posts") or path == "/api/v1/hot" else "ReadEnvelope"
                    if path == "/api/v1/insights/music":
                        schema = "MusicPostPage"
                    if path in {"/api/v1/posts/{shortcode}", "/api/v1/accounts/{handle}/posts/{shortcode}"}:
                        schema = "PostResult"
                    operation["responses"]["200"] = {"description": "Stored data within the key's account and area grants", "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}}}
    spec.setdefault("components", {}).setdefault("securitySchemes", {})["WebsiteAPIKey"] = {
        "type": "http", "scheme": "bearer", "description": "Account- and area-scoped sad_api_ key"}
    spec["security"] = [{"WebsiteAPIKey": []}]
    return spec


def _handle(request: Request, handle: str) -> str:
    identity = _identity(request)
    if handle not in identity["account_handles"]:
        raise HTTPException(404, "Account not available for this API key.")
    return handle


def _envelope(report: dict[str, Any], data: Any) -> dict[str, Any]:
    return {"schema_version": VERSION, "generated_at": report["generated_at"],
            "data_updated_at": {"profile": report["account"].get("profile_captured_at"),
                                "engagement": report["coverage"].get("last_metrics_update_at")}, "data": data}


def _profile_report(handle: str) -> dict[str, Any]:
    """Read profile observations and update clocks without aggregating posts."""
    now = datetime.now(UTC)
    metrics_dates = []
    with db.connect() as conn:
        registry = conn.execute("SELECT is_canonical FROM accounts WHERE handle = ?", (handle,)).fetchone()
        canonical = bool(registry["is_canonical"])
        snapshots = [dict(row) for row in conn.execute("SELECT * FROM account_snapshots WHERE LOWER(handle) = ? ORDER BY captured_at", (handle,)).fetchall()]
        tables = ["dashboard_posts", "posts"] if canonical else ["dashboard_posts"]
        for table in tables:
            columns = account_media_kit._columns(conn, table)
            scope, parameters = ("", ()) if table == "posts" else (" WHERE LOWER(account) = ?", (handle,))
            if "updated_at" in columns:
                value = conn.execute(f"SELECT MAX(updated_at) AS updated_at FROM {table}{scope}", parameters).fetchone()["updated_at"]
                if parsed := account_media_kit._date(value):
                    metrics_dates.append(parsed)
            if "shortcode" in columns and account_media_kit._columns(conn, "engagement_observations"):
                value = conn.execute(f"SELECT MAX(observed_at) AS observed_at FROM engagement_observations WHERE shortcode IN (SELECT shortcode FROM {table}{scope})", parameters).fetchone()["observed_at"]
                if parsed := account_media_kit._date(value):
                    metrics_dates.append(parsed)
    snapshots = sorted([snap for snap in snapshots if (captured := account_media_kit._date(snap.get("captured_at"))) and captured <= now], key=lambda snap: account_media_kit._date(snap["captured_at"]))
    latest = next((snap for snap in reversed(snapshots) if account_media_kit._number(snap.get("followers_count")) is not None), {})
    newest = snapshots[-1] if snapshots else {}
    account = {"handle": handle, "public_name": newest.get("full_name") or latest.get("full_name") or f"@{handle}",
               "public_bio": next((snap[field] for snap in reversed(snapshots) for field in ("biography", "bio")
                    if isinstance(snap.get(field), str) and snap[field].strip()), None),
               "followers": account_media_kit._number(latest.get("followers_count")),
               "profile_posts": next((account_media_kit._number(snap.get("posts_count")) for snap in reversed(snapshots)
                    if account_media_kit._number(snap.get("posts_count")) is not None), None),
               "verified": account_media_kit._bool(newest.get("verified")),
               "private": account_media_kit._bool(newest.get("private")), "profile_captured_at": latest.get("captured_at")}
    updated = max((value for value in metrics_dates if value <= now), default=None)
    return {"generated_at": now.isoformat(timespec="seconds"), "account": account,
            "coverage": {"last_metrics_update_at": updated.isoformat(timespec="seconds") if updated else None}}


@router.get("/accounts")
def accounts(request: Request) -> dict[str, Any]:
    identity = _identity(request)
    with db.connect() as conn:
        data = _public_accounts(conn, identity["account_handles"])
    return {"schema_version": VERSION, "data": data}


class ProfilePreviewRequest(BaseModel):
    handle: str = Field(min_length=1, max_length=30)


@profile_router.post(PROFILE_PREVIEW_PATH, summary="Look up one public Instagram profile for a deck")
def deck_profile_preview(request: Request, payload: ProfilePreviewRequest) -> dict[str, Any]:
    """No account registration or internal data access. The existing preview
    reader caches profiles for ten minutes and limits provider calls globally.
    Stored-account reads keep their original account and area restrictions.
    """
    _identity(request)
    handle = payload.handle.strip().lstrip("@").lower()
    if not _HANDLE.fullmatch(handle):
        raise HTTPException(400, "Use an Instagram handle.")
    from .main import admin_preview_account
    return admin_preview_account(handle)


@router.get("/accounts/{handle}")
def profile(request: Request, handle: str) -> dict[str, Any]:
    report = _profile_report(_handle(request, handle))
    return _envelope(report, project_public_media_kit(report)["account"])


@router.get("/accounts/{handle}/media-kit")
def media_kit(request: Request, handle: str) -> dict[str, Any]:
    report = account_media_kit.build_account_media_kit(_handle(request, handle), strict_public_exclusions=True)
    # No image preparation or internal storage references enter JSON.
    return _envelope(report, project_public_media_kit(report))


def _range(start: date | None, end: date | None) -> None:
    if start and end and start > end:
        raise HTTPException(422, "from must be on or before to.")


def _pagination(data: list[Any], limit: int, offset: int) -> dict[str, Any]:
    total = len(data)
    next_offset = offset + limit if offset + limit < total else None
    return {"limit": limit, "offset": offset, "total": total, "has_more": next_offset is not None, "next_offset": next_offset}


def _public_posts(clean: str, start: date | None = None, end: date | None = None,
                  is_promo: bool | None = None, shortcode: str | None = None) -> tuple[datetime, list[dict[str, Any]]]:
    """Exported posts for one account (or one shortcode), newest first, after public exclusions."""
    now = datetime.now(UTC)
    with db.connect() as conn:
        row = conn.execute("SELECT is_canonical FROM accounts WHERE handle = ?", (clean,)).fetchone()
        canonical = bool(row["is_canonical"])
        rows = account_media_kit._post_rows(conn, "dashboard_posts", clean, canonical, shortcode, compact_public=True)
        if canonical:
            rows += account_media_kit._post_rows(conn, "posts", clean, True, shortcode, compact_public=True)
        snapshots = conn.execute("SELECT * FROM account_snapshots WHERE LOWER(handle) = ? ORDER BY captured_at DESC", (clean,)).fetchall()
        latest = next((dict(snap) for snap in snapshots if (captured := account_media_kit._date(snap["captured_at"])) and captured <= now), {})
        observations = {}
        if account_media_kit._columns(conn, "engagement_observations"):
            codes = sorted({str(value["shortcode"]) for value in rows if value.get("shortcode")})
            for index in range(0, len(codes), 200):
                batch = codes[index:index + 200]
                readings = account_media_kit._public_observation_rows(conn, batch)
                observations.update({reading["shortcode"]: dict(reading) for reading in readings})
    excluded_codes = {str(row.get("shortcode") or "").strip() for row in rows
                      if account_media_kit._bool(row.get("hidden")) or account_media_kit._bool(row.get("is_deleted"))}
    normalized, _ = account_media_kit._normalize_posts(rows, observations, clean)
    data = []
    if not account_media_kit._bool(latest.get("private")):
        for post in normalized:
            published = account_media_kit._date(post["published_at"])
            code = post["shortcode"]
            if post["hidden"] or post["is_deleted"] or code in excluded_codes or not code or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code) or not published or published > now:
                continue
            day = published.astimezone(LOCAL_ZONE).date()
            if start and day < start or end and day > end:
                continue
            caption = post["public_caption"]
            promo = is_public_promo(post)
            if is_promo is not None and promo != is_promo:
                continue
            data.append({"shortcode": code, "caption": caption if isinstance(caption, str) and caption else None,
                         "is_promo": promo,
                         **public_collaboration(post, clean),
                         "published_at": published.isoformat(timespec="seconds"),
                         "permalink": f"https://www.instagram.com/p/{code}/", "format": post["format"],
                         **{metric: post["metrics"].get(metric) for metric in ("likes", "comments", "video_views", "video_plays")},
                         "metrics_updated_at": post["metrics_updated_at"]})
    data.sort(key=lambda post: (post["published_at"], post["shortcode"]), reverse=True)
    return now, data


@router.get("/accounts/{handle}/posts")
def posts(request: Request, handle: str, limit: Annotated[int, Query(ge=1, le=100)] = 20,
          offset: Annotated[int, Query(ge=0)] = 0,
          is_promo: Annotated[bool | None, Query(description="Filter Research Promo classification: manual mark or #aitoolsentient in the published caption")] = None,
          start: Annotated[date | None, Query(alias="from", description="Inclusive Costa Rica calendar date")] = None,
          end: Annotated[date | None, Query(alias="to", description="Inclusive Costa Rica calendar date")] = None) -> dict[str, Any]:
    clean = _handle(request, handle)
    _range(start, end)
    if is_promo is None:
        # Ordinary pages must not deserialize every provider payload for the
        # account. The bounded Research projection preserves the established
        # metric precedence and public exclusions for just this result page.
        from .external_research import _posts_page
        result = _posts_page([clean], limit=limit, offset=offset, start=start, end=end, public_only=True)
        fields = ("shortcode", "caption", "is_promo", "is_collab", "collaborators", "published_at", "permalink", "format",
                  "likes", "comments", "video_views", "video_plays", "metrics_updated_at")
        return {"schema_version": VERSION, "generated_at": result["generated_at"],
                "data": [{field: post.get(field) for field in fields} for post in result["data"]], "pagination": result["pagination"]}
    now, data = _public_posts(clean, start, end, is_promo)
    return {"schema_version": VERSION, "generated_at": now.isoformat(timespec="seconds"),
            "data": data[offset:offset + limit], "pagination": _pagination(data, limit, offset)}


def _public_post(request: Request, handle: str, shortcode: str) -> tuple[str, datetime, dict[str, Any]]:
    clean = _handle(request, handle)
    if not _SHORTCODE.fullmatch(shortcode):
        raise HTTPException(404, "Post not available for this API key.")
    now, data = _public_posts(clean, shortcode=shortcode)
    post = next((post for post in data if post["shortcode"] == shortcode), None)
    if post is None:
        raise HTTPException(404, "Post not available for this API key.")
    return clean, now, post


@router.get("/accounts/{handle}/posts/{shortcode}")
def post(request: Request, handle: str, shortcode: str) -> dict[str, Any]:
    """One exported post by its Instagram shortcode, with the same fields as the list."""
    clean, now, value = _public_post(request, handle, shortcode)
    return {"schema_version": VERSION, "generated_at": now.isoformat(timespec="seconds"),
            "data": {**value, "cover_url": f"/api/v1/accounts/{clean}/posts/{shortcode}/cover"}}


@router.get("/posts/{shortcode}")
def post_by_shortcode(request: Request, shortcode: str) -> dict[str, Any]:
    """Find one exported post by shortcode across the key's accounts."""
    identity = _identity(request)
    if not _SHORTCODE.fullmatch(shortcode):
        raise HTTPException(404, "Post not available for this API key.")
    handles = identity["account_handles"]
    with db.connect() as conn:
        candidates: list[str] = []
        if "shortcode" in account_media_kit._columns(conn, "dashboard_posts"):
            candidates += [str(row["account"]).lower() for row in conn.execute(
                "SELECT DISTINCT LOWER(account) AS account FROM dashboard_posts WHERE shortcode = ?", (shortcode,)).fetchall()]
        if "shortcode" in account_media_kit._columns(conn, "posts") and conn.execute(
                "SELECT 1 FROM posts WHERE shortcode = ? LIMIT 1", (shortcode,)).fetchone():
            candidates += [str(row["handle"]) for row in conn.execute("SELECT handle FROM accounts WHERE is_canonical = 1").fetchall()]
    for handle in dict.fromkeys(candidate for candidate in candidates if candidate in handles):
        now, data = _public_posts(handle, shortcode=shortcode)
        if data:
            return {"schema_version": VERSION, "generated_at": now.isoformat(timespec="seconds"),
                    "data": {"account": handle, **data[0], "cover_url": f"/api/v1/accounts/{handle}/posts/{shortcode}/cover"}}
    raise HTTPException(404, "Post not available for this API key.")


@router.get("/accounts/{handle}/posts/{shortcode}/cover")
def post_cover(request: Request, handle: str, shortcode: str) -> Response:
    """Redirect to the stored cover of an exported post. Never fetches from Instagram."""
    clean, _, _ = _public_post(request, handle, shortcode)
    with db.connect() as conn:
        references = []
        if "cover_image_path" in account_media_kit._columns(conn, "dashboard_posts"):
            row = conn.execute("""SELECT cover_image_path FROM dashboard_posts WHERE LOWER(account) = ? AND shortcode = ?
                AND cover_image_path IS NOT NULL AND cover_image_path <> '' ORDER BY id DESC LIMIT 1""", (clean, shortcode)).fetchone()
            references.append(row["cover_image_path"] if row else None)
        registry = conn.execute("SELECT is_canonical FROM accounts WHERE handle = ?", (clean,)).fetchone()
        if registry and registry["is_canonical"] and "image_path" in account_media_kit._columns(conn, "posts"):
            row = conn.execute("""SELECT image_path FROM posts WHERE shortcode = ? AND image_path IS NOT NULL AND image_path <> ''
                ORDER BY id DESC LIMIT 1""", (shortcode,)).fetchone()
            references.append(row["image_path"] if row else None)
    target = next((url for reference in references if reference and (url := redirect_url(reference))), None)
    if target is None:
        raise HTTPException(404, "No stored cover for this post.")
    return RedirectResponse(target, status_code=307)


@router.get("/accounts/{handle}/followers/history")
def followers_history(request: Request, handle: str, limit: Annotated[int, Query(ge=1, le=100)] = 20,
                      offset: Annotated[int, Query(ge=0)] = 0,
                      start: Annotated[date | None, Query(alias="from", description="Inclusive Costa Rica calendar date")] = None,
                      end: Annotated[date | None, Query(alias="to", description="Inclusive Costa Rica calendar date")] = None) -> dict[str, Any]:
    clean = _handle(request, handle)
    _range(start, end)
    now = datetime.now(UTC)
    with db.connect() as conn:
        snapshots = [dict(row) for row in conn.execute("SELECT * FROM account_snapshots WHERE LOWER(handle) = ? ORDER BY captured_at", (clean,)).fetchall()]
    snapshots = [snap for snap in snapshots if (captured := account_media_kit._date(snap["captured_at"])) and captured <= now]
    history, _ = account_media_kit._history(snapshots)
    data = [{"date": value["local_date"], "captured_at": value["date"], "followers": value["followers"]}
            for value in history if (not start or value["local_date"] >= start.isoformat()) and (not end or value["local_date"] <= end.isoformat())]
    return {"schema_version": VERSION, "generated_at": now.isoformat(timespec="seconds"), "timezone": "America/Costa_Rica",
            "data": data[offset:offset + limit], "pagination": _pagination(data, limit, offset)}
