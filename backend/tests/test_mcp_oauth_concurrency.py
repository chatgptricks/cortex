"""Deterministically overlap refresh reads against independent SQLite sessions.

These tests preserve strict refresh reuse detection: duplicate refreshes revoke
the entire grant, including the tokens returned to the winning request.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
from threading import Barrier

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import mcp_oauth as oauth


@pytest.fixture
def refresh_store(tmp_path, monkeypatch):
    database = tmp_path / "refresh.sqlite3"
    controls = {"barrier": None, "fail_refresh_insert": False}

    class OverlappingCursor:
        def __init__(self, cursor, barrier):
            self.cursor, self.barrier = cursor, barrier

        def fetchone(self):
            row = self.cursor.fetchone()
            # Both requests must observe the old token before either consumes it.
            self.barrier.wait(timeout=10)
            return row

    class Connection:
        def __init__(self, raw):
            self.raw = raw

        def execute(self, statement, params=()):
            if (controls["fail_refresh_insert"] and
                    statement.startswith("INSERT INTO mcp_oauth_tokens") and params[1] == "refresh"):
                raise sqlite3.OperationalError("injected storage failure")
            cursor = self.raw.execute(statement, params)
            if (controls["barrier"] is not None and
                    statement.startswith("SELECT * FROM mcp_oauth_tokens WHERE token_hash") and
                    "kind = 'refresh'" in statement):
                return OverlappingCursor(cursor, controls["barrier"])
            return cursor

    @contextmanager
    def connect():
        raw = sqlite3.connect(database, timeout=10)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA journal_mode=WAL")
        try:
            yield Connection(raw)
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            raw.close()

    monkeypatch.setattr(oauth, "connect", connect)
    monkeypatch.delenv("SENTIENT_OAUTH_ISSUER", raising=False)

    def create_grant():
        client_id = "client-" + oauth._secret("")
        grant = {"id": "grant-" + oauth._secret(""), "client_id": client_id,
                 "owner_email": "ana@example.com", "owner_uid": "ana-uid",
                 "resource": oauth.resource_url(), "issuer": oauth.issuer_url(),
                 "scopes": json.dumps(oauth.SCOPES), "created_at": oauth.utc_now(),
                 "expires_at": oauth._date_after(3600)}
        with connect() as conn:
            oauth.ensure_schema(conn)
            conn.execute("INSERT INTO mcp_oauth_clients (id, name, redirect_uris, created_at, issuer, grant_types) VALUES (?, ?, ?, ?, ?, ?)",
                         (client_id, "Concurrent test", "[]", oauth.utc_now(), oauth.issuer_url(),
                          json.dumps(["authorization_code", "refresh_token"])))
            conn.execute("INSERT INTO mcp_oauth_grants (id, client_id, owner_email, owner_uid, resource, issuer, scopes, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         tuple(grant.values()))
            tokens = oauth._issue_tokens(conn, grant, oauth.SCOPES)
        return client_id, grant["id"], tokens

    return create_grant, controls


def exchange(client_id, refresh_token):
    request = Request({"type": "http", "client": ("127.0.0.1", 1234)})
    try:
        response = oauth._exchange({"grant_type": "refresh_token", "client_id": client_id,
                                   "refresh_token": refresh_token, "resource": oauth.resource_url()}, request)
    except oauth.OAuthError as exc:
        return exc.error
    return json.loads(response.body)


def test_overlapping_refresh_of_same_token_has_one_winner_and_revokes_family(refresh_store):
    create_grant, controls = refresh_store
    client_id, grant_id, original = create_grant()
    controls["barrier"] = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: exchange(client_id, original["refresh_token"]), range(2)))
    controls["barrier"] = None
    issued = [result for result in results if isinstance(result, dict)]
    assert len(issued) == 1
    assert results.count("invalid_grant") == 1
    with oauth.connect() as conn:
        grant = conn.execute("SELECT revoked_at FROM mcp_oauth_grants WHERE id = ?", (grant_id,)).fetchone()
        counts = conn.execute("SELECT kind, COUNT(*) AS n FROM mcp_oauth_tokens WHERE grant_id = ? GROUP BY kind", (grant_id,)).fetchall()
    assert grant["revoked_at"]
    # Exactly the original pair and one replacement pair; no double issuance.
    assert {row["kind"]: row["n"] for row in counts} == {"access": 2, "refresh": 2}
    for access in (original["access_token"], issued[0]["access_token"]):
        with pytest.raises(HTTPException) as error:
            oauth.authenticate_access_token(access, "/mcp", "POST")
        assert error.value.status_code == 401
    assert exchange(client_id, issued[0]["refresh_token"]) == "invalid_grant"


def test_overlapping_refreshes_of_independent_grants_both_remain_usable(refresh_store):
    create_grant, controls = refresh_store
    grants = [create_grant(), create_grant()]
    controls["barrier"] = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda item: exchange(item[0], item[2]["refresh_token"]), grants))
    controls["barrier"] = None
    for (client_id, grant_id, _), result in zip(grants, results):
        assert isinstance(result, dict)
        assert oauth.authenticate_access_token(result["access_token"], "/mcp", "POST")["oauth_grant_id"] == grant_id
        assert isinstance(exchange(client_id, result["refresh_token"]), dict)


def test_failed_rotation_rolls_back_consumption_and_partial_token_issuance(refresh_store):
    create_grant, controls = refresh_store
    client_id, grant_id, original = create_grant()
    controls["fail_refresh_insert"] = True
    with pytest.raises(sqlite3.OperationalError, match="injected storage failure"):
        exchange(client_id, original["refresh_token"])
    controls["fail_refresh_insert"] = False
    with oauth.connect() as conn:
        tokens = conn.execute("SELECT consumed_at FROM mcp_oauth_tokens WHERE grant_id = ?", (grant_id,)).fetchall()
        grant = conn.execute("SELECT revoked_at FROM mcp_oauth_grants WHERE id = ?", (grant_id,)).fetchone()
    assert len(tokens) == 2
    assert all(row["consumed_at"] is None for row in tokens)
    assert grant["revoked_at"] is None
    assert oauth.authenticate_access_token(original["access_token"], "/mcp", "POST")["oauth_grant_id"] == grant_id
    retry = exchange(client_id, original["refresh_token"])
    assert isinstance(retry, dict)
    assert oauth.authenticate_access_token(retry["access_token"], "/mcp", "POST")["oauth_grant_id"] == grant_id
