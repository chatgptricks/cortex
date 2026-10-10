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
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field

from . import account_media_kit, db
from .promo_classification import is_public_promo
from .public_collaboration import public_collaboration
from .public_media_kit import project_public_media_kit

PREFIX = "sad_api_"
MANAGEMENT_URL = "/api/dashboard/me/api-keys"
VERSION = "1.0"
RATE_LIMIT = 60
LOCAL_ZONE = timezone(timedelta(hours=-6))
_HANDLE = re.compile(r"[a-z0-9_.]{1,30}\Z")
_READ_PATH = re.compile(r"/api/v1/accounts(?:/[a-z0-9_.]{1,30}(?:/(?:media-kit|posts|followers/history))?)?/?\Z")
_PUBLIC_FIELDS = "id, name, key_prefix, account_handles, created_at, expires_at, revoked_at, last_used_at"


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    response.headers["X-Content-Type-Options"] = "nosniff"


management_router = APIRouter(prefix=MANAGEMENT_URL, tags=["website-api-keys"], dependencies=[
    Depends(_no_store), Depends(HTTPBearer(auto_error=False, scheme_name="FirebaseSession", description="Signed-in Dashboard Firebase ID token"))])
router = APIRouter(prefix="/api/v1", tags=["website-api-v1"], dependencies=[
    Depends(_no_store), Depends(HTTPBearer(auto_error=False, scheme_name="WebsiteAPIKey", description="Account-scoped website API key, beginning sad_api_"))])


def ensure_schema(conn: Any) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS website_api_keys (
        id TEXT PRIMARY KEY, owner_email TEXT NOT NULL, name TEXT NOT NULL,
        key_hash TEXT UNIQUE NOT NULL, key_prefix TEXT NOT NULL,
        account_handles TEXT NOT NULL, created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL, revoked_at TEXT, last_used_at TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_website_api_keys_owner ON website_api_keys(owner_email)")
    conn.execute("""CREATE TABLE IF NOT EXISTS website_api_rate_windows (
        key_id TEXT PRIMARY KEY, minute_window BIGINT NOT NULL,
        request_count INTEGER NOT NULL
    )""")


def _admin(email: str, access: dict[str, Any] | None) -> bool:
    from .slack_alerts import DEV_EMAILS
    return access is not None and (bool(access.get("is_admin")) or email in DEV_EMAILS)


def _browser_owner(request: Request) -> str:
    if getattr(request.state, "auth_method", None) != "firebase":
        raise HTTPException(403, "Use your signed-in browser to manage website API keys.")
    email = str(getattr(request.state, "user_email", "") or "").strip().lower()
    if not email:
        raise HTTPException(401, "Sign in required.")
    return email


def _active_accounts(conn: Any) -> dict[str, dict[str, Any]]:
    return {str(row["handle"]).lower(): dict(row) for row in conn.execute(
        "SELECT handle, is_active FROM accounts WHERE is_active = 1 ORDER BY handle"
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
    return value


class CreateAPIKey(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    account_handles: list[str] = Field(min_length=1, max_length=100)
    expires_in_days: Literal[30, 90, 365] = 90


@management_router.get("")
def list_keys(request: Request) -> dict[str, Any]:
    owner = _browser_owner(request)
    allowed = _admin(owner, db.get_dashboard_user_access(owner))
    with db.connect() as conn:
        ensure_schema(conn)
        keys = [_connection(row) for row in conn.execute(
            f"SELECT {_PUBLIC_FIELDS} FROM website_api_keys WHERE owner_email = ? ORDER BY created_at DESC", (owner,)
        ).fetchall()]
        accounts = _public_accounts(conn, sorted(_active_accounts(conn))) if allowed else []
    return {"keys": keys, "available_accounts": [{"handle": value["handle"], "public_name": value["public_name"]} for value in accounts], "can_create": allowed,
            "rate_limit_per_minute": RATE_LIMIT}


@management_router.post("", status_code=201)
def create_key(request: Request, payload: CreateAPIKey) -> dict[str, Any]:
    owner = _browser_owner(request)
    if not _admin(owner, db.get_dashboard_user_access(owner)):
        raise HTTPException(403, "Admin or Dev access is required to create website API keys.")
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
        row = {"id": identity, "name": name, "key_prefix": key[:15], "account_handles": handles,
               "created_at": created, "expires_at": expires, "revoked_at": None, "last_used_at": None}
        conn.execute("""INSERT INTO website_api_keys
            (id, owner_email, name, key_hash, key_prefix, account_handles, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (identity, owner, name, hashlib.sha256(key.encode()).hexdigest(),
                key[:15], json.dumps(handles), created, expires))
    return {"key": key, "connection": row}


@management_router.delete("/{key_id}")
def revoke_key(request: Request, key_id: str) -> dict[str, Any]:
    owner = _browser_owner(request)
    with db.connect() as conn:
        ensure_schema(conn)
        row = conn.execute("SELECT id FROM website_api_keys WHERE id = ? AND owner_email = ?", (key_id, owner)).fetchone()
        if row is None:
            raise HTTPException(404, "API key not found.")
        conn.execute("UPDATE website_api_keys SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ? AND owner_email = ?", (db.utc_now(), key_id, owner))
    return {"revoked": True, "id": key_id}


def authenticate(key: str, path: str, method: str) -> dict[str, Any]:
    if not re.fullmatch(r"sad_api_[A-Za-z0-9_-]{43}", key):
        raise HTTPException(401, "Invalid or expired website API key.")
    if method != "GET" or not _READ_PATH.fullmatch(path):
        raise HTTPException(403, "Website API keys can only read the versioned website API.")
    now = datetime.now(UTC)
    timestamp = now.isoformat(timespec="seconds")
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    with db.connect() as conn:
        ensure_schema(conn)
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
        active = _active_accounts(conn)
        handles = [handle for handle in json.loads(row["account_handles"]) if handle in active]
        parts = path.strip("/").split("/")
        if len(parts) > 3 and parts[3] not in handles:
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
    return {"id": row["id"], "owner_email": owner, "account_handles": handles}


def _identity(request: Request) -> dict[str, Any]:
    value = getattr(request.state, "website_api_key", None)
    if not value:
        raise HTTPException(401, "A website API key is required.")
    return value


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


@router.get("/accounts/{handle}/posts")
def posts(request: Request, handle: str, limit: Annotated[int, Query(ge=1, le=100)] = 20,
          offset: Annotated[int, Query(ge=0)] = 0,
          is_promo: Annotated[bool | None, Query(description="Filter Research Promo classification: manual mark or #aitoolsentient in the published caption")] = None,
          start: Annotated[date | None, Query(alias="from", description="Inclusive Costa Rica calendar date")] = None,
          end: Annotated[date | None, Query(alias="to", description="Inclusive Costa Rica calendar date")] = None) -> dict[str, Any]:
    clean = _handle(request, handle)
    _range(start, end)
    now = datetime.now(UTC)
    with db.connect() as conn:
        row = conn.execute("SELECT is_canonical FROM accounts WHERE handle = ?", (clean,)).fetchone()
        canonical = bool(row["is_canonical"])
        rows = account_media_kit._post_rows(conn, "dashboard_posts", clean, canonical)
        if canonical:
            rows += account_media_kit._post_rows(conn, "posts", clean, True)
        snapshots = conn.execute("SELECT * FROM account_snapshots WHERE LOWER(handle) = ? ORDER BY captured_at DESC", (clean,)).fetchall()
        latest = next((dict(snap) for snap in snapshots if (captured := account_media_kit._date(snap["captured_at"])) and captured <= now), {})
        observations = {}
        if account_media_kit._columns(conn, "engagement_observations"):
            codes = sorted({str(value["shortcode"]) for value in rows if value.get("shortcode")})
            for index in range(0, len(codes), 200):
                batch = codes[index:index + 200]
                readings = conn.execute(f"SELECT shortcode, observed_at, raw_json FROM engagement_observations WHERE shortcode IN ({','.join('?' for _ in batch)})", batch).fetchall()
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
    return {"schema_version": VERSION, "generated_at": now.isoformat(timespec="seconds"),
            "data": data[offset:offset + limit], "pagination": _pagination(data, limit, offset)}


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
