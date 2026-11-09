"""Durable, per-user receipts for Queue-changing assistant turns.

Only a hash of the request is stored, never the source message, bearer token or
conversation. In-progress/uncertain receipts are never automatically retried.
"""
import json
import re
from uuid import UUID, uuid4

from fastapi import HTTPException

from .db import connect, utc_now


_PUBLIC_FIELDS = frozenset({"answer", "sources", "reaction", "model", "ui_actions", "language", "mode", "error_code", "pending_next"})
_PRIVATE_KEYS = frozenset({"authorization", "headers", "token", "access_token", "id_token", "api_key", "apikey",
                           "credential", "credentials", "raw_message", "message", "history"})


def _owner(request):
    email = str(getattr(request.state, "user_email", "") or "").strip().lower()
    if not email or getattr(request.state, "credential_kind", "") != "firebase":
        raise HTTPException(403, "Use your signed-in Sentient Dash session.")
    return email


def _request_id(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Queue action request_id must be a UUID.") from None


def _schema(conn):
    if getattr(conn, "is_postgres", False):
        # Serialize first-use DDL across workers. Ordinary receipts take the
        # same tiny transaction lock, rather than racing CREATE TABLE on PG.
        conn.execute("SELECT pg_advisory_xact_lock(?)", (7514061201,))
    conn.execute("""CREATE TABLE IF NOT EXISTS assistant_queue_turns (
        owner_email TEXT NOT NULL,
        request_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        claim_nonce TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('in_progress', 'completed', 'uncertain')),
        response_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(owner_email, request_id)
    )""")


def _claims(request):
    claims = getattr(request.state, "assistant_queue_receipt_claims", None)
    if not isinstance(claims, dict):
        claims = {}
        request.state.assistant_queue_receipt_claims = claims
    return claims


def claim_turn(request, request_id, request_fingerprint):
    owner, request_id = _owner(request), _request_id(request_id)
    if not isinstance(request_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", request_fingerprint):
        raise ValueError("Queue request fingerprint must be a SHA-256 digest.")
    nonce, now = str(uuid4()), utc_now()
    with connect() as conn:
        _schema(conn)
        cursor = conn.execute("""INSERT INTO assistant_queue_turns
            (owner_email, request_id, fingerprint, claim_nonce, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'in_progress', ?, ?)
            ON CONFLICT(owner_email, request_id) DO NOTHING""",
            (owner, request_id, request_fingerprint, nonce, now, now))
        if cursor.rowcount:
            _claims(request)[request_id] = nonce
            return {"status": "claimed"}
        row = conn.execute("SELECT fingerprint, status, response_json FROM assistant_queue_turns WHERE owner_email = ? AND request_id = ?",
                           (owner, request_id)).fetchone()
    if not row or row["fingerprint"] != request_fingerprint:
        return {"status": "conflict"}
    if row["status"] == "completed":
        try:
            response = json.loads(row["response_json"] or "null")
        except (ValueError, TypeError):
            response = None
        if isinstance(response, dict):
            return {"status": "completed", "response": response}
    return {"status": "pending"}


def _sanitize(value):
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items() if str(key).casefold() not in _PRIVATE_KEYS}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise ValueError("Queue turn response contains unsupported data.")


def complete_turn(request, request_id, response):
    owner, request_id = _owner(request), _request_id(request_id)
    nonce = _claims(request).get(request_id)
    if not nonce:
        raise ValueError("Only the invocation owning this Queue receipt can complete it.")
    if not isinstance(response, dict):
        raise ValueError("Queue turn response must be an object.")
    public = _sanitize({key: value for key, value in response.items() if key in _PUBLIC_FIELDS})
    encoded = json.dumps(public, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode()) > 96_000:
        raise ValueError("Queue turn response exceeds its receipt limit.")
    with connect() as conn:
        _schema(conn)
        updated = conn.execute("""UPDATE assistant_queue_turns SET status = 'completed', response_json = ?, updated_at = ?
            WHERE owner_email = ? AND request_id = ? AND claim_nonce = ? AND status = 'in_progress'""",
            (encoded, utc_now(), owner, request_id, nonce))
        if updated.rowcount != 1:
            raise ValueError("Queue receipt is no longer owned by this active invocation.")


def uncertain_turn(request, request_id):
    owner, request_id = _owner(request), _request_id(request_id)
    nonce = _claims(request).get(request_id)
    if not nonce:
        return
    with connect() as conn:
        _schema(conn)
        conn.execute("""UPDATE assistant_queue_turns SET status = 'uncertain', updated_at = ?
            WHERE owner_email = ? AND request_id = ? AND claim_nonce = ? AND status = 'in_progress'""",
            (utc_now(), owner, request_id, nonce))


def release_turn(request, request_id):
    """Only for a known pre-mutation failure; never expire an ambiguous write."""
    owner, request_id = _owner(request), _request_id(request_id)
    nonce = _claims(request).get(request_id)
    if not nonce:
        return
    with connect() as conn:
        _schema(conn)
        conn.execute("""DELETE FROM assistant_queue_turns
            WHERE owner_email = ? AND request_id = ? AND claim_nonce = ? AND status = 'in_progress'""",
            (owner, request_id, nonce))
    _claims(request).pop(request_id, None)
