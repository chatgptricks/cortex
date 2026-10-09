"""Additive, user-owned OAuth for MCP; legacy agent codes remain independent.

The authorization server issues opaque credentials for this MCP resource only.
Browser consent uses the existing Firebase session, never a delegated credential.
Secrets are persisted as SHA-256 hashes and redemption/rotation is atomic in both
SQLite and PostgreSQL. Product roles remain the live middleware's responsibility.
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
import re
import secrets
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, StrictBool
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse

from .db import connect, utc_now

ACCESS_PREFIX = "sad_oauth_at_"
REFRESH_PREFIX = "sad_oauth_rt_"
TRANSACTION_PREFIX = "sad_oauth_tx_"
CODE_PREFIX = "sad_oauth_code_"
READ_SCOPE = "sentient:read"
WRITE_SCOPE = "sentient:write"
SCOPES = [READ_SCOPE, WRITE_SCOPE]
AUTHORIZATION_URL = "/api/dashboard/me/oauth/authorization"
CONNECTIONS_URL = "/api/dashboard/me/oauth-connections"
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class _NoStoreRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def no_store(request: Request) -> Response:
            try:
                response = await handler(request)
            except RequestValidationError:
                # Validation diagnostics must not echo transaction/code secrets.
                response = JSONResponse({"detail": "Invalid OAuth browser request."}, status_code=422)
            except OAuthError as exc:
                response = _error(exc)
            response.headers.update(NO_STORE)
            return response

        return no_store


router = APIRouter(tags=["mcp-oauth"], route_class=_NoStoreRoute)


def issuer_url() -> str:
    value = os.getenv("SENTIENT_OAUTH_ISSUER", "https://cortex-api-db2e.onrender.com").rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise ValueError("SENTIENT_OAUTH_ISSUER must be an HTTPS origin")
    return value


def resource_url() -> str:
    return issuer_url() + "/mcp"


def frontend_url() -> str:
    return "https://sentientdash.app/oauth.html"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _date_after(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


class OAuthError(Exception):
    def __init__(self, error: str, description: str, status: int = 400):
        self.error, self.description, self.status = error, description, status


def _json(data: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=NO_STORE)


def _error(exc: OAuthError) -> JSONResponse:
    return _json({"error": exc.error, "error_description": exc.description}, exc.status)


def _scope(value: str | None, default: list[str] | None = None) -> list[str]:
    if value is not None and not isinstance(value, str):
        raise OAuthError("invalid_scope", "scope must be a space-delimited string.")
    scopes = list(dict.fromkeys((value or "").split())) if value is not None else list(default or [READ_SCOPE])
    if READ_SCOPE not in scopes or any(s not in SCOPES for s in scopes):
        raise OAuthError("invalid_scope", "Request sentient:read, optionally with sentient:write.")
    return [s for s in SCOPES if s in scopes]


def _mode(scopes: list[str]) -> str:
    return "full" if WRITE_SCOPE in scopes else "read"


def _saved_scopes(value: str) -> list[str]:
    try:
        scopes = json.loads(value)
        if not isinstance(scopes, list) or READ_SCOPE not in scopes or any(not isinstance(s, str) or s not in SCOPES for s in scopes):
            raise ValueError
    except (ValueError, TypeError):
        raise OAuthError("invalid_grant", "Invalid connection scope.") from None
    return [s for s in SCOPES if s in scopes]


def ensure_schema(conn: Any) -> None:
    statements = [
        """CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, redirect_uris TEXT NOT NULL,
            created_at TEXT NOT NULL, issuer TEXT NOT NULL, grant_types TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS mcp_oauth_authorizations (
            transaction_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL, state TEXT, code_challenge TEXT NOT NULL,
            resource TEXT NOT NULL, issuer TEXT NOT NULL, scopes TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT,
            owner_email TEXT, owner_uid TEXT)""",
        """CREATE TABLE IF NOT EXISTS mcp_oauth_grants (
            id TEXT PRIMARY KEY, client_id TEXT NOT NULL, owner_email TEXT NOT NULL,
            owner_uid TEXT NOT NULL, resource TEXT NOT NULL, issuer TEXT NOT NULL,
            scopes TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            revoked_at TEXT, last_used_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS mcp_oauth_codes (
            code_hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL, code_challenge TEXT NOT NULL,
            expires_at TEXT NOT NULL, consumed_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS mcp_oauth_tokens (
            token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, grant_id TEXT NOT NULL,
            scopes TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS mcp_oauth_rate_limits (
            key TEXT PRIMARY KEY, hits INTEGER NOT NULL, expires_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_mcp_oauth_grants_owner ON mcp_oauth_grants(owner_email)",
        "CREATE INDEX IF NOT EXISTS idx_mcp_oauth_tokens_grant ON mcp_oauth_tokens(grant_id)",
    ]
    for statement in statements:
        conn.execute(statement)


def _rate(request: Request, action: str, limit: int, seconds: int = 60, client_id: str = "") -> None:
    # The peer address is server-provided; do not trust a spoofable forwarded IP.
    peer = request.client.host if request.client else "unknown"
    bucket = int(datetime.now(UTC).timestamp()) // seconds
    # Public clients are not authenticated at this point. A caller-provided
    # client_id must never create an independent escape from the peer limit.
    keys = [_hash(f"{action}:{peer}::{bucket}")]
    if client_id:
        keys.append(_hash(f"{action}:{peer}:{client_id}:{bucket}"))
    now = utc_now()
    expires = _date_after(seconds * 2)
    accepted = True
    with connect() as conn:
        ensure_schema(conn)
        for key in keys:
            conn.execute("INSERT OR IGNORE INTO mcp_oauth_rate_limits (key, hits, expires_at) VALUES (?, 0, ?)", (key, expires))
            if conn.execute("UPDATE mcp_oauth_rate_limits SET hits = hits + 1 WHERE key = ? AND hits < ?", (key, limit)).rowcount != 1:
                accepted = False
                break
        # Bounded housekeeping does not remove clients, grants, or replay records.
        conn.execute("DELETE FROM mcp_oauth_rate_limits WHERE key IN (SELECT key FROM mcp_oauth_rate_limits WHERE expires_at < ? LIMIT 100)", (now,))
        conn.execute("DELETE FROM mcp_oauth_authorizations WHERE transaction_hash IN (SELECT transaction_hash FROM mcp_oauth_authorizations WHERE expires_at < ? LIMIT 100)", (now,))
        # Keep spent refresh records throughout the grant's life for replay
        # detection. Never delete registered clients or the user's history.
        for table, column in [("mcp_oauth_codes", "code_hash"), ("mcp_oauth_tokens", "token_hash")]:
            conn.execute(f"DELETE FROM {table} WHERE {column} IN (SELECT t.{column} FROM {table} t JOIN mcp_oauth_grants g ON g.id = t.grant_id WHERE g.expires_at <= ? LIMIT 100)", (now,))
    if not accepted:
        raise OAuthError("temporarily_unavailable", "Too many requests; try again later.", 429)


def _valid_redirect(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 1024:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(parsed.scheme == "https" and parsed.hostname == "chatgpt.com" and
                parsed.netloc == "chatgpt.com" and port is None and not parsed.username and
                not parsed.password and not parsed.query and not parsed.fragment and
                (parsed.path == "/connector_platform_oauth_redirect" or
                 re.fullmatch(r"/connector/oauth/[A-Za-z0-9_-]{1,200}", parsed.path)))


def _client(conn: Any, client_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM mcp_oauth_clients WHERE id = ? AND issuer = ?", (client_id, issuer_url())).fetchone()
    if not row:
        raise OAuthError("invalid_client", "Unknown OAuth client.")
    return dict(row)


def _callback(row: dict[str, Any], **values: str) -> str:
    # Only called after redirect_uri was matched to a registered exact value.
    if row.get("state") is not None:
        values["state"] = row["state"]
    values["iss"] = row["issuer"]
    parsed = urlsplit(row["redirect_uri"])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(values), ""))


def _browser_owner(request: Request) -> tuple[str, str]:
    state = request.state
    if getattr(state, "auth_method", None) != "firebase" or getattr(state, "agent_connection_id", None) or getattr(state, "oauth_grant_id", None) or getattr(state, "credential_kind", None) in {"agent", "oauth"}:
        raise HTTPException(403, "Use your signed-in browser for OAuth consent and connections.", headers=NO_STORE)
    if getattr(state, "queue_role_preview_active", False):
        raise HTTPException(403, "Leave role preview before authorizing an OAuth connection.", headers=NO_STORE)
    email = str(getattr(state, "user_email", "") or "").strip().lower()
    uid = str(getattr(state, "user_uid", "") or "").strip()
    if not email or not uid:
        raise HTTPException(401, "Sign in required.", headers=NO_STORE)
    return email, uid


@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
def protected_resource_metadata() -> JSONResponse:
    return _json({"resource": resource_url(), "authorization_servers": [issuer_url()],
                  "scopes_supported": SCOPES, "bearer_methods_supported": ["header"],
                  "resource_name": "Sentient Dash MCP"})


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
def authorization_server_metadata() -> JSONResponse:
    issuer = issuer_url()
    return _json({"issuer": issuer, "authorization_endpoint": issuer + "/oauth/authorize",
                  "token_endpoint": issuer + "/oauth/token", "registration_endpoint": issuer + "/oauth/register",
                  "revocation_endpoint": issuer + "/oauth/revoke", "scopes_supported": SCOPES,
                  "response_types_supported": ["code"], "response_modes_supported": ["query"],
                  "grant_types_supported": ["authorization_code", "refresh_token"],
                  "token_endpoint_auth_methods_supported": ["none"], "code_challenge_methods_supported": ["S256"],
                  "authorization_response_iss_parameter_supported": True})


def _register(payload: dict[str, Any], request: Request) -> JSONResponse:
    _rate(request, "register", 20, 3600)
    name = payload.get("client_name", "ChatGPT")
    redirects = payload.get("redirect_uris")
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise OAuthError("invalid_client_metadata", "client_name must contain 1 to 80 printable characters.")
    if not isinstance(redirects, list) or not 1 <= len(redirects) <= 5 or any(not _valid_redirect(uri) for uri in redirects):
        raise OAuthError("invalid_redirect_uri", "Use an exact supported HTTPS ChatGPT OAuth callback.")
    grant_types = payload.get("grant_types", ["authorization_code", "refresh_token"])
    if payload.get("token_endpoint_auth_method", "none") != "none" or not isinstance(grant_types, list) or any(not isinstance(g, str) for g in grant_types) or "authorization_code" not in grant_types or not set(grant_types).issubset({"authorization_code", "refresh_token"}) or payload.get("response_types", ["code"]) != ["code"]:
        raise OAuthError("invalid_client_metadata", "This public client uses authorization_code, refresh_token, code responses and PKCE with auth none.")
    grant_types = [g for g in ("authorization_code", "refresh_token") if g in grant_types]
    if payload.get("scope") is not None:
        _scope(payload["scope"] if isinstance(payload["scope"], str) else "")
    client_id = "sad_oauth_client_" + secrets.token_urlsafe(24)
    created = utc_now()
    redirects = list(dict.fromkeys(redirects))
    with connect() as conn:
        ensure_schema(conn)
        count = conn.execute("SELECT COUNT(*) AS n FROM mcp_oauth_clients").fetchone()["n"]
        if count >= 5000:
            raise OAuthError("temporarily_unavailable", "OAuth registration capacity reached.", 503)
        conn.execute("INSERT INTO mcp_oauth_clients (id, name, redirect_uris, created_at, issuer, grant_types) VALUES (?, ?, ?, ?, ?, ?)", (client_id, name.strip(), json.dumps(redirects), created, issuer_url(), json.dumps(grant_types)))
    return _json({"client_id": client_id, "client_id_issued_at": int(datetime.fromisoformat(created).timestamp()),
                  "client_name": name.strip(), "redirect_uris": redirects, "token_endpoint_auth_method": "none",
                  "grant_types": grant_types, "response_types": ["code"],
                  "scope": " ".join(SCOPES)}, 201)


@router.post("/oauth/register", include_in_schema=False)
async def register(request: Request) -> JSONResponse:
    try:
        body = await _bounded_body(request)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise OAuthError("invalid_client_metadata", "Provide JSON client metadata.") from None
        if not isinstance(payload, dict):
            raise OAuthError("invalid_client_metadata", "Provide JSON client metadata.")
        return await run_in_threadpool(_register, payload, request)
    except OAuthError as exc:
        return _error(exc)


def _authorize(request: Request) -> Response:
    _rate(request, "authorize", 60)
    params = request.query_params
    if any(len(params.getlist(key)) != 1 for key in params):
        raise OAuthError("invalid_request", "OAuth parameters must not be repeated.")
    client_id, redirect = params.get("client_id", ""), params.get("redirect_uri", "")
    with connect() as conn:
        ensure_schema(conn)
        client = _client(conn, client_id)
    if redirect not in json.loads(client["redirect_uris"]):
        raise OAuthError("invalid_request", "redirect_uri must exactly match a registered callback.")
    state = params.get("state")
    safe = {"redirect_uri": redirect, "state": state, "issuer": issuer_url()}
    try:
        if state is not None and len(state) > 1024:
            # Do not echo an oversized state back to a callback.
            safe["state"] = None
            raise OAuthError("invalid_request", "state is too long.")
        if params.get("response_type") != "code":
            raise OAuthError("unsupported_response_type", "Only response_type=code is supported.")
        if params.get("resource") != resource_url():
            raise OAuthError("invalid_target", "resource must be the canonical Sentient Dash MCP URL.")
        challenge = params.get("code_challenge", "")
        if params.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
            raise OAuthError("invalid_request", "A valid S256 PKCE challenge is required.")
        scopes = _scope(params.get("scope"))
    except OAuthError as exc:
        return RedirectResponse(_callback(safe, error=exc.error, error_description=exc.description), status_code=302, headers=NO_STORE)
    transaction = _secret(TRANSACTION_PREFIX)
    with connect() as conn:
        ensure_schema(conn)
        conn.execute("""INSERT INTO mcp_oauth_authorizations
            (transaction_hash, client_id, redirect_uri, state, code_challenge, resource, issuer, scopes, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (_hash(transaction), client_id, redirect, state, challenge, resource_url(), issuer_url(), json.dumps(scopes), utc_now(), _date_after(600)))
    return RedirectResponse(frontend_url() + "?" + urlencode({"transaction": transaction}), status_code=302, headers=NO_STORE)


@router.get("/oauth/authorize", include_in_schema=False)
def authorize(request: Request) -> Response:
    try:
        return _authorize(request)
    except OAuthError as exc:
        return _error(exc)


def _authorization(conn: Any, transaction: str, email: str, uid: str) -> dict[str, Any]:
    if not re.fullmatch(re.escape(TRANSACTION_PREFIX) + r"[A-Za-z0-9_-]{43}", transaction):
        raise HTTPException(400, "Invalid or expired authorization request.", headers=NO_STORE)
    row = conn.execute("""UPDATE mcp_oauth_authorizations SET owner_email = ?, owner_uid = ?
        WHERE transaction_hash = ? AND consumed_at IS NULL AND expires_at > ?
        AND issuer = ? AND resource = ?
        AND (owner_email IS NULL OR (owner_email = ? AND owner_uid = ?)) RETURNING *""",
        (email, uid, _hash(transaction), utc_now(), issuer_url(), resource_url(), email, uid)).fetchone()
    if not row:
        raise HTTPException(400, "Invalid, expired, or already used authorization request; sign in with its original account.", headers=NO_STORE)
    return dict(row)


@router.get(AUTHORIZATION_URL)
def browser_authorization(request: Request, transaction: str) -> JSONResponse:
    email, uid = _browser_owner(request)
    with connect() as conn:
        ensure_schema(conn)
        row = _authorization(conn, transaction, email, uid)
        client = _client(conn, row["client_id"])
    return _json({"client_name": client["name"], "scopes": json.loads(row["scopes"]),
                  "resource": row["resource"], "expires_at": row["expires_at"], "email": email})


class Consent(BaseModel):
    transaction: str = Field(min_length=1, max_length=128)
    approve: StrictBool


@router.post(AUTHORIZATION_URL)
def browser_consent(request: Request, payload: Consent) -> JSONResponse:
    email, uid = _browser_owner(request)
    now = utc_now()
    with connect() as conn:
        ensure_schema(conn)
        row = _authorization(conn, payload.transaction, email, uid)
        _client(conn, row["client_id"])
        consumed = conn.execute("UPDATE mcp_oauth_authorizations SET consumed_at = ? WHERE transaction_hash = ? AND consumed_at IS NULL RETURNING transaction_hash", (now, row["transaction_hash"])).fetchone()
        if not consumed:
            raise HTTPException(400, "Authorization request already used.", headers=NO_STORE)
        if not payload.approve:
            redirect = _callback(row, error="access_denied", error_description="The user declined this connection.")
        else:
            grant_id, code = secrets.token_hex(16), _secret(CODE_PREFIX)
            conn.execute("""INSERT INTO mcp_oauth_grants
                (id, client_id, owner_email, owner_uid, resource, issuer, scopes, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (grant_id, row["client_id"], email, uid, row["resource"], row["issuer"], row["scopes"], now, _date_after(90 * 86400)))
            conn.execute("INSERT INTO mcp_oauth_codes (code_hash, grant_id, redirect_uri, code_challenge, expires_at) VALUES (?, ?, ?, ?, ?)", (_hash(code), grant_id, row["redirect_uri"], row["code_challenge"], _date_after(120)))
            redirect = _callback(row, code=code)
    return _json({"redirect_url": redirect})


def _active_grant(conn: Any, grant_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM mcp_oauth_grants WHERE id = ? AND revoked_at IS NULL AND expires_at > ? AND issuer = ? AND resource = ?", (grant_id, utc_now(), issuer_url(), resource_url())).fetchone()
    if not row:
        raise OAuthError("invalid_grant", "The connection has expired or was revoked.")
    _saved_scopes(row["scopes"])
    return dict(row)


def _issue_tokens(conn: Any, grant: dict[str, Any], scopes: list[str], allow_refresh: bool = True) -> dict[str, Any]:
    now = datetime.now(UTC)
    remaining = max(0, int((datetime.fromisoformat(grant["expires_at"]) - now).total_seconds()))
    seconds = min(900, remaining)
    if seconds <= 0:
        raise OAuthError("invalid_grant", "The connection has expired.")
    access, refresh = _secret(ACCESS_PREFIX), _secret(REFRESH_PREFIX)
    tokens = [(access, "access", (now + timedelta(seconds=seconds)).isoformat(timespec="seconds"))]
    if allow_refresh:
        tokens.append((refresh, "refresh", grant["expires_at"]))
    for token, kind, expires in tokens:
        conn.execute("INSERT INTO mcp_oauth_tokens (token_hash, kind, grant_id, scopes, expires_at) VALUES (?, ?, ?, ?, ?)", (_hash(token), kind, grant["id"], json.dumps(scopes), expires))
    result = {"access_token": access, "token_type": "Bearer", "expires_in": seconds, "scope": " ".join(scopes)}
    if allow_refresh:
        result["refresh_token"] = refresh
    return result


async def _bounded_body(request: Request) -> bytes:
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 16384:
            raise OAuthError("invalid_request", "Request body is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


async def _form(request: Request) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise OAuthError("invalid_request", "Use application/x-www-form-urlencoded.")
    try:
        pairs = parse_qsl((await _bounded_body(request)).decode("utf-8"), keep_blank_values=True,
                          max_num_fields=30, encoding="utf-8", errors="strict")
    except (ValueError, UnicodeDecodeError):
        raise OAuthError("invalid_request", "Provide valid UTF-8 form parameters.") from None
    if len({key for key, _ in pairs}) != len(pairs):
        raise OAuthError("invalid_request", "OAuth form parameters must be strings and must not be repeated.")
    form = dict(pairs)
    if request.headers.get("authorization") or form.get("client_secret"):
        raise OAuthError("invalid_client", "This registered public client uses authentication method none.", 401)
    return form


def _exchange(payload: dict[str, str], request: Request) -> JSONResponse:
    client_id = payload.get("client_id", "")
    _rate(request, "token", 120, 60, client_id)
    if payload.get("resource") != resource_url():
        raise OAuthError("invalid_target", "resource must be the canonical Sentient Dash MCP URL.")
    replay = False
    result = None
    with connect() as conn:
        ensure_schema(conn)
        client = _client(conn, client_id)
        supported_grants = json.loads(client["grant_types"])
        if payload.get("grant_type") not in supported_grants:
            raise OAuthError("unsupported_grant_type", "This public client is not registered for the requested grant type.")
        if payload.get("grant_type") == "authorization_code":
            code, verifier = payload.get("code", ""), payload.get("code_verifier", "")
            row = conn.execute("SELECT * FROM mcp_oauth_codes WHERE code_hash = ?", (_hash(code),)).fetchone()
            if not row or row["consumed_at"] or row["expires_at"] <= utc_now():
                raise OAuthError("invalid_grant", "Invalid, expired, or already used authorization code.")
            grant = _active_grant(conn, row["grant_id"])
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if grant["client_id"] != client_id or payload.get("redirect_uri") != row["redirect_uri"] or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) or not secrets.compare_digest(challenge, row["code_challenge"]):
                raise OAuthError("invalid_grant", "Code, client, redirect_uri, or PKCE verifier does not match.")
            scopes = _saved_scopes(grant["scopes"])
            if payload.get("scope") is not None and _scope(payload["scope"]) != scopes:
                raise OAuthError("invalid_scope", "Authorization code scopes cannot be changed at redemption.")
            consumed = conn.execute("UPDATE mcp_oauth_codes SET consumed_at = ? WHERE code_hash = ? AND consumed_at IS NULL AND expires_at > ? RETURNING code_hash", (utc_now(), row["code_hash"], utc_now())).fetchone()
            if not consumed:
                raise OAuthError("invalid_grant", "Authorization code already used.")
            result = _issue_tokens(conn, grant, scopes, "refresh_token" in supported_grants)
        elif payload.get("grant_type") == "refresh_token":
            row = conn.execute("SELECT * FROM mcp_oauth_tokens WHERE token_hash = ? AND kind = 'refresh'", (_hash(payload.get("refresh_token", "")),)).fetchone()
            if not row:
                raise OAuthError("invalid_grant", "Invalid refresh token.")
            grant = _active_grant(conn, row["grant_id"])
            if grant["client_id"] != client_id or row["expires_at"] <= utc_now():
                raise OAuthError("invalid_grant", "Invalid or expired refresh token.")
            previous = _saved_scopes(row["scopes"])
            if row["consumed_at"]:
                consumed = None
                scopes = previous
            else:
                scopes = _scope(payload.get("scope"), previous)
                if not set(scopes).issubset(previous):
                    raise OAuthError("invalid_scope", "Refresh cannot increase the authorized scopes.")
                consumed = conn.execute("UPDATE mcp_oauth_tokens SET consumed_at = ? WHERE token_hash = ? AND consumed_at IS NULL AND expires_at > ? RETURNING token_hash", (utc_now(), row["token_hash"], utc_now())).fetchone()
            if not consumed:
                # Commit the family revocation before returning an OAuth error.
                conn.execute("UPDATE mcp_oauth_grants SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?", (utc_now(), grant["id"]))
                replay = True
            else:
                result = _issue_tokens(conn, grant, scopes)
        else:
            raise OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token.")
    if replay:
        raise OAuthError("invalid_grant", "Refresh token reuse detected; reconnect to Sentient Dash.")
    return _json(result or {})


@router.post("/oauth/token", include_in_schema=False)
async def token(request: Request) -> JSONResponse:
    try:
        payload = await _form(request)
        return await run_in_threadpool(_exchange, payload, request)
    except OAuthError as exc:
        return _error(exc)


def _revoke(payload: dict[str, str], request: Request) -> JSONResponse:
    client_id = payload.get("client_id", "")
    _rate(request, "revoke", 60, 60, client_id)
    with connect() as conn:
        ensure_schema(conn)
        _client(conn, client_id)
        row = conn.execute("SELECT g.id FROM mcp_oauth_tokens t JOIN mcp_oauth_grants g ON g.id = t.grant_id WHERE t.token_hash = ? AND g.client_id = ?", (_hash(payload.get("token", "")), client_id)).fetchone()
        if row:
            conn.execute("UPDATE mcp_oauth_grants SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?", (utc_now(), row["id"]))
    return _json({})


@router.post("/oauth/revoke", include_in_schema=False)
async def revoke(request: Request) -> JSONResponse:
    try:
        payload = await _form(request)
        if not payload.get("token"):
            raise OAuthError("invalid_request", "token is required.")
        return await run_in_threadpool(_revoke, payload, request)
    except OAuthError as exc:
        return _error(exc)


@router.get(CONNECTIONS_URL)
def list_connections(request: Request) -> JSONResponse:
    email, uid = _browser_owner(request)
    with connect() as conn:
        ensure_schema(conn)
        rows = conn.execute("""SELECT g.id, c.name AS client_name, g.scopes, g.created_at,
            g.expires_at, g.revoked_at, g.last_used_at FROM mcp_oauth_grants g
            JOIN mcp_oauth_clients c ON c.id = g.client_id
            WHERE g.owner_email = ? AND g.owner_uid = ? ORDER BY g.created_at DESC""", (email, uid)).fetchall()
    connections = []
    for row in rows:
        value = dict(row)
        value["scopes"] = json.loads(value["scopes"])
        value["access_mode"] = _mode(value["scopes"])
        connections.append(value)
    return _json({"connections": connections})


@router.delete(CONNECTIONS_URL + "/{grant_id}")
def revoke_connection(request: Request, grant_id: str) -> JSONResponse:
    email, uid = _browser_owner(request)
    with connect() as conn:
        ensure_schema(conn)
        count = conn.execute("UPDATE mcp_oauth_grants SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ? AND owner_email = ? AND owner_uid = ?", (utc_now(), grant_id, email, uid)).rowcount
        if not count:
            raise HTTPException(404, "Connection not found.", headers=NO_STORE)
    return _json({"revoked": True, "id": grant_id})


def enforce_delegated_route(path: str, method: str, mode: str) -> None:
    blocked = (AUTHORIZATION_URL, CONNECTIONS_URL, "/api/dashboard/me/agent-connections", "/api/dashboard/me/api-keys")
    if (not path.startswith("/api/") and path not in {"/mcp", "/mcp/"}) or any(path == p or path.startswith(p + "/") for p in blocked) or path.startswith(("/api/auth/", "/api/slack/", "/api/v1/")) or path in {"/api/auth", "/api/slack", "/api/v1"}:
        raise HTTPException(403, "OAuth connections cannot access login or credential management.", headers=NO_STORE)
    if mode not in {"read", "full"} or (mode == "read" and path not in {"/mcp", "/mcp/"} and method.upper() not in {"GET", "HEAD", "OPTIONS"}):
        raise HTTPException(403, "This OAuth connection has read-only access.", headers=NO_STORE)


def _identity(grant: dict[str, Any], scopes: list[str]) -> dict[str, Any]:
    return {"email": grant["owner_email"], "uid": grant["owner_uid"], "oauth_grant_id": grant["id"],
            "oauth_scopes": scopes, "agent_access_mode": _mode(scopes), "auth_method": "oauth", "credential_kind": "oauth"}


def authenticate_access_token(token: str, path: str, method: str) -> dict[str, Any]:
    if path not in {"/mcp", "/mcp/"}:
        raise HTTPException(403, "OAuth access tokens are only accepted by the MCP resource.", headers=NO_STORE)
    if not re.fullmatch(re.escape(ACCESS_PREFIX) + r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(401, "Invalid or expired OAuth access token.", headers=NO_STORE)
    try:
        with connect() as conn:
            ensure_schema(conn)
            row = conn.execute("SELECT * FROM mcp_oauth_tokens WHERE token_hash = ? AND kind = 'access' AND expires_at > ?", (_hash(token), utc_now())).fetchone()
            if not row:
                raise OAuthError("invalid_token", "Invalid or expired OAuth access token.")
            grant = _active_grant(conn, row["grant_id"])
            scopes = _saved_scopes(row["scopes"])
            if not set(scopes).issubset(_saved_scopes(grant["scopes"])):
                raise OAuthError("invalid_token", "Invalid token scope.")
            enforce_delegated_route(path, method, _mode(scopes))
            conn.execute("UPDATE mcp_oauth_grants SET last_used_at = ? WHERE id = ?", (utc_now(), grant["id"]))
        return _identity(grant, scopes)
    except OAuthError as exc:
        raise HTTPException(401, exc.description, headers=NO_STORE) from None


def validate_internal_grant(grant_id: str, owner_email: str, owner_uid: str | None,
                            scopes: list[str], path: str, method: str) -> dict[str, Any]:
    """For a sealed in-process MCP context only; never call from client headers.

    The caller must additionally revalidate the original access token before
    each forwarding operation, so a token expiring mid-request is not extended.
    """
    try:
        with connect() as conn:
            ensure_schema(conn)
            grant = _active_grant(conn, grant_id)
            if grant["owner_email"] != owner_email or grant["owner_uid"] != owner_uid or not isinstance(scopes, list) or READ_SCOPE not in scopes or any(not isinstance(s, str) or s not in SCOPES for s in scopes) or not set(scopes).issubset(_saved_scopes(grant["scopes"])):
                raise OAuthError("invalid_grant", "Invalid delegated OAuth context.")
            enforce_delegated_route(path, method, _mode(scopes))
        return _identity(grant, scopes)
    except OAuthError as exc:
        raise HTTPException(401, exc.description, headers=NO_STORE) from None
