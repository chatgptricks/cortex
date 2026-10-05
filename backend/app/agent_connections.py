"""User-owned, revocable agent credentials. Raw secrets are returned once only."""
from datetime import UTC, datetime, timedelta
import hashlib
import re
import secrets
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from .db import connect, utc_now

PREFIX = "sad_agent_"
URL = "/api/dashboard/me/agent-connections"
def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix=URL, tags=["agent-connections"], dependencies=[Depends(_no_store)])


def ensure_schema(conn: Any) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_connections (
        id TEXT PRIMARY KEY,
        owner_email TEXT NOT NULL,
        owner_uid TEXT,
        name TEXT NOT NULL,
        key_hash TEXT UNIQUE NOT NULL,
        key_prefix TEXT NOT NULL,
        access_mode TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT,
        last_used_at TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_connections_owner ON agent_connections(owner_email)")


def _owner(request: Request) -> str:
    email = str(getattr(request.state, "user_email", "") or "").strip().lower()
    if not email:
        raise HTTPException(401, "Sign in required.")
    if getattr(request.state, "agent_connection_id", None):
        raise HTTPException(403, "Use your signed-in browser to manage agent connections.")
    return email


class CreateConnection(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    access_mode: Literal["full", "read"] = "full"
    expires_in_days: Literal[30, 90, 365] = 90


_PUBLIC_FIELDS = "id, name, key_prefix, access_mode, created_at, expires_at, revoked_at, last_used_at"


@router.get("")
def list_connections(request: Request) -> dict[str, Any]:
    owner = _owner(request)
    with connect() as conn:
        ensure_schema(conn)
        rows = conn.execute(f"SELECT {_PUBLIC_FIELDS} FROM agent_connections WHERE owner_email = ? ORDER BY created_at DESC", (owner,)).fetchall()
    return {"connections": [dict(row) for row in rows]}


@router.post("", status_code=201)
def create_connection(request: Request, payload: CreateConnection) -> dict[str, Any]:
    owner = _owner(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, "Name required.")
    now = datetime.now(UTC)
    created, expires = now.isoformat(timespec="seconds"), (now + timedelta(days=payload.expires_in_days)).isoformat(timespec="seconds")
    key = PREFIX + secrets.token_urlsafe(32)
    identity = secrets.token_hex(16)
    with connect() as conn:
        ensure_schema(conn)
        count = conn.execute("SELECT COUNT(*) AS n FROM agent_connections WHERE owner_email = ? AND revoked_at IS NULL AND expires_at > ?", (owner, created)).fetchone()["n"]
        if count >= 20:
            raise HTTPException(409, "Revoke an existing connection before creating more (limit 20).")
        conn.execute("""INSERT INTO agent_connections
            (id, owner_email, owner_uid, name, key_hash, key_prefix, access_mode, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (identity, owner, getattr(request.state, "user_uid", None), name,
            hashlib.sha256(key.encode()).hexdigest(), key[:17], payload.access_mode, created, expires))
    return {"key": key, "connection": {"id": identity, "name": name, "key_prefix": key[:17], "access_mode": payload.access_mode,
        "created_at": created, "expires_at": expires, "revoked_at": None, "last_used_at": None}}


@router.delete("/{connection_id}")
def revoke_connection(request: Request, connection_id: str) -> dict[str, Any]:
    owner = _owner(request)
    with connect() as conn:
        ensure_schema(conn)
        row = conn.execute("SELECT id FROM agent_connections WHERE id = ? AND owner_email = ?", (connection_id, owner)).fetchone()
        if not row:
            raise HTTPException(404, "Connection not found.")
        conn.execute("UPDATE agent_connections SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ? AND owner_email = ?", (utc_now(), connection_id, owner))
    return {"revoked": True, "id": connection_id}


def authenticate(key: str, path: str, method: str) -> dict[str, Any]:
    """Resolve the owner; live product roles are loaded by the normal middleware."""
    if not re.fullmatch(r"sad_agent_[A-Za-z0-9_-]{43}", key):
        raise HTTPException(401, "Invalid or expired agent connection.")
    now = utc_now()
    with connect() as conn:
        ensure_schema(conn)
        row = conn.execute("SELECT * FROM agent_connections WHERE key_hash = ? AND revoked_at IS NULL AND expires_at > ?", (hashlib.sha256(key.encode()).hexdigest(), now)).fetchone()
        if not row:
            raise HTTPException(401, "Invalid or expired agent connection.")
        if not path.startswith("/api/") or path == URL or path.startswith(URL + "/") or path.startswith(("/api/auth/", "/api/slack/")):
            raise HTTPException(403, "Agent connections cannot access login or credential management.")
        if row["access_mode"] == "read" and method not in {"GET", "HEAD", "OPTIONS"}:
            raise HTTPException(403, "This agent connection has read-only access.")
        conn.execute("UPDATE agent_connections SET last_used_at = ? WHERE id = ?", (now, row["id"]))
    return {"email": row["owner_email"], "uid": row["owner_uid"], "agent_connection_id": row["id"], "agent_access_mode": row["access_mode"]}
