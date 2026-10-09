"""OAuth protocol/security tests with a local fixture DB and browser identity.

The main middleware and hosted MCP integration are covered separately; this
suite never loads credentials or contacts an external authorization server.
"""
import base64
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import mcp_oauth as oauth

CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
OWNER = {"x-test-email": "ana@example.com", "x-test-uid": "ana-uid", "x-test-auth": "firebase"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "oauth.sqlite"

    @contextmanager
    def connect():
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(oauth, "connect", connect)
    monkeypatch.delenv("SENTIENT_OAUTH_ISSUER", raising=False)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.user_email = request.headers.get("x-test-email")
        request.state.user_uid = request.headers.get("x-test-uid")
        request.state.auth_method = request.headers.get("x-test-auth")
        request.state.oauth_grant_id = request.headers.get("x-test-oauth")
        request.state.agent_connection_id = request.headers.get("x-test-agent")
        request.state.queue_role_preview_active = request.headers.get("x-test-preview") == "1"
        return await call_next(request)

    app.include_router(oauth.router)
    with TestClient(app, follow_redirects=False) as host:
        yield host


def register(client, **overrides):
    payload = {"client_name": "ChatGPT test", "redirect_uris": [CALLBACK], **overrides}
    response = client.post("/oauth/register", json=payload)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["client_id"]


def authorize(client, client_id, **overrides):
    params = {"client_id": client_id, "redirect_uri": CALLBACK, "response_type": "code",
              "resource": oauth.resource_url(), "scope": oauth.READ_SCOPE, "state": "test state & value",
              "code_challenge_method": "S256", "code_challenge": CHALLENGE, **overrides}
    response = client.get("/oauth/authorize", params=params)
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(oauth.frontend_url() + "?")
    return parse_qs(urlsplit(location).query)["transaction"][0]


def approve(client, transaction, owner=None, approve=True):
    response = client.post(oauth.AUTHORIZATION_URL, headers=owner or OWNER,
                           json={"transaction": transaction, "approve": approve})
    assert response.status_code == 200, response.text
    query = parse_qs(urlsplit(response.json()["redirect_url"]).query)
    assert query["iss"] == [oauth.issuer_url()]
    assert query["state"] == ["test state & value"]
    return query


def exchange(client, registered_client, code, **overrides):
    return client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": registered_client,
                       "redirect_uri": CALLBACK, "code": code, "code_verifier": VERIFIER,
                       "resource": oauth.resource_url(), **overrides})


def connected(client, scope=None):
    client_id = register(client)
    transaction = authorize(client, client_id, scope=scope or oauth.READ_SCOPE)
    query = approve(client, transaction)
    result = exchange(client, client_id, query["code"][0])
    assert result.status_code == 200, result.text
    return client_id, result.json(), transaction, query["code"][0]


def refresh(client, client_id, token, **overrides):
    return client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id,
                       "refresh_token": token, "resource": oauth.resource_url(), **overrides})


def test_discovery_and_complete_browser_flow_store_only_hashes(client):
    for path in ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"]:
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()["resource"] == oauth.resource_url()
        assert response.json()["authorization_servers"] == [oauth.issuer_url()]
        assert response.headers["cache-control"] == "no-store"
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert "client_id_metadata_document_supported" not in metadata
    client_id, tokens, transaction, code = connected(client)
    assert tokens["access_token"].startswith(oauth.ACCESS_PREFIX)
    assert tokens["refresh_token"].startswith(oauth.REFRESH_PREFIX)
    assert 1 <= tokens["expires_in"] <= 900
    identity = oauth.authenticate_access_token(tokens["access_token"], "/mcp", "POST")
    assert identity["email"] == "ana@example.com"
    assert identity["uid"] == "ana-uid"
    assert identity["oauth_scopes"] == [oauth.READ_SCOPE]
    assert identity["agent_access_mode"] == "read"
    assert identity["auth_method"] == identity["credential_kind"] == "oauth"
    with oauth.connect() as conn:
        rows = [dict(row) for table in ["mcp_oauth_authorizations", "mcp_oauth_codes", "mcp_oauth_tokens"] for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
    persisted = json.dumps(rows)
    for secret in [transaction, code, tokens["access_token"], tokens["refresh_token"]]:
        assert secret not in persisted
    listed = client.get(oauth.CONNECTIONS_URL, headers=OWNER).json()["connections"]
    assert listed[0]["client_name"] == "ChatGPT test"
    assert listed[0]["last_used_at"]
    assert not any("token" in key for key in listed[0])


@pytest.mark.parametrize("redirect", [
    "http://chatgpt.com/connector_platform_oauth_redirect", "https://chatgpt.com.evil.example/connector_platform_oauth_redirect",
    "https://evil.example/connector_platform_oauth_redirect", "https://user@chatgpt.com/connector_platform_oauth_redirect",
    CALLBACK + "?next=x", CALLBACK + "#fragment", CALLBACK + "/", "https://chatgpt.com/connector/oauth/a/b",
    "https://chatgpt.com:443/connector_platform_oauth_redirect", "https://[invalid/connector_platform_oauth_redirect",
])
def test_registration_rejects_callback_variations(client, redirect):
    response = client.post("/oauth/register", json={"redirect_uris": [redirect]})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"
    assert "location" not in response.headers


@pytest.mark.parametrize("payload", [
    None, [], {"client_name": ["bad"], "redirect_uris": [CALLBACK]},
    {"client_name": "bad\nname", "redirect_uris": [CALLBACK]},
    {"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "client_secret_post"},
    {"redirect_uris": [CALLBACK], "scope": [oauth.READ_SCOPE]},
    {"redirect_uris": [CALLBACK], "grant_types": ["refresh_token"]},
    {"redirect_uris": [CALLBACK], "grant_types": "authorization_code"},
    {"redirect_uris": [CALLBACK], "grant_types": [{"invalid": True}]},
])
def test_registration_rejects_invalid_metadata_without_server_errors(client, payload):
    response = client.post("/oauth/register", content=json.dumps(payload), headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


def test_registration_accepts_per_connection_chatgpt_callback_and_code_only_client(client):
    client_id = register(client, redirect_uris=["https://chatgpt.com/connector/oauth/callback-123"])
    assert client_id
    code_only = register(client, grant_types=["authorization_code"])
    transaction = authorize(client, code_only)
    code = approve(client, transaction)["code"][0]
    issued = exchange(client, code_only, code).json()
    assert "refresh_token" not in issued
    assert issued["access_token"]
    reversed_id = register(client, grant_types=["refresh_token", "authorization_code"])
    assert reversed_id


def test_invalid_json_oversized_body_and_duplicate_form_parameters(client):
    assert client.post("/oauth/register", content="not json").status_code == 400
    assert client.post("/oauth/register", content=" " * 16385).status_code == 400
    assert client.post("/oauth/token", json={"grant_type": "refresh_token"}).status_code == 400
    assert client.post("/oauth/token", content="client_id=a&client_id=b", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 400
    assert client.post("/oauth/token", content="x=" + "a" * 16385, headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 400


def test_browser_validation_errors_are_not_cached_or_echoed(client):
    secret = oauth.TRANSACTION_PREFIX + "x" * 43
    response = client.post(oauth.AUTHORIZATION_URL, headers=OWNER, json={"transaction": secret, "approve": "yes"})
    assert response.status_code == 422
    assert secret not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert client.get(oauth.AUTHORIZATION_URL, headers=OWNER).headers["cache-control"] == "no-store"


def test_authorization_never_redirects_to_unregistered_client_or_callback(client):
    client_id = register(client)
    for params in [{"client_id": "unknown", "redirect_uri": CALLBACK},
                   {"client_id": client_id, "redirect_uri": "https://evil.example"},
                   [("client_id", client_id), ("client_id", client_id), ("redirect_uri", CALLBACK)]]:
        response = client.get("/oauth/authorize", params=params)
        assert response.status_code == 400
        assert "location" not in response.headers


@pytest.mark.parametrize("override,error", [
    ({"resource": "https://wrong.example/mcp"}, "invalid_target"),
    ({"code_challenge_method": "plain"}, "invalid_request"),
    ({"code_challenge": "bad"}, "invalid_request"),
    ({"response_type": "token"}, "unsupported_response_type"),
    ({"scope": oauth.WRITE_SCOPE}, "invalid_scope"),
    ({"scope": "admin"}, "invalid_scope"),
])
def test_safe_authorization_errors_return_state_and_exact_issuer(client, override, error):
    client_id = register(client)
    response = client.get("/oauth/authorize", params={"client_id": client_id, "redirect_uri": CALLBACK,
        "state": "callback-state", "response_type": "code", "resource": oauth.resource_url(),
        "code_challenge_method": "S256", "code_challenge": CHALLENGE, **override})
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["error"] == [error]
    assert query["state"] == ["callback-state"]
    assert query["iss"] == [oauth.issuer_url()]


def test_browser_consent_binds_identity_and_denial_is_single_use(client):
    client_id = register(client)
    transaction = authorize(client, client_id)
    response = client.get(oauth.AUTHORIZATION_URL, params={"transaction": transaction}, headers=OWNER)
    assert response.status_code == 200
    assert response.json()["email"] == "ana@example.com"
    bob = {**OWNER, "x-test-email": "bob@example.com", "x-test-uid": "bob-uid"}
    assert client.get(oauth.AUTHORIZATION_URL, params={"transaction": transaction}, headers=bob).status_code == 400
    assert client.post(oauth.AUTHORIZATION_URL, json={"transaction": transaction, "approve": True}, headers=bob).status_code == 400
    query = approve(client, transaction, approve=False)
    assert query["error"] == ["access_denied"]
    assert "code" not in query
    assert client.post(oauth.AUTHORIZATION_URL, json={"transaction": transaction, "approve": True}, headers=OWNER).status_code == 400
    assert client.get(oauth.CONNECTIONS_URL, headers=OWNER).json()["connections"] == []


@pytest.mark.parametrize("headers", [{}, {**OWNER, "x-test-auth": "oauth"}, {**OWNER, "x-test-agent": "agent-id"},
                                      {**OWNER, "x-test-oauth": "grant-id"}, {**OWNER, "x-test-preview": "1"},
                                      {**OWNER, "x-test-uid": ""}])
def test_browser_endpoints_require_firebase_and_actual_owner(client, headers):
    transaction = authorize(client, register(client))
    assert client.get(oauth.AUTHORIZATION_URL, params={"transaction": transaction}, headers=headers).status_code in {401, 403}
    assert client.post(oauth.AUTHORIZATION_URL, json={"transaction": transaction, "approve": True}, headers=headers).status_code in {401, 403}
    assert client.get(oauth.CONNECTIONS_URL, headers=headers).status_code in {401, 403}
    assert client.delete(oauth.CONNECTIONS_URL + "/unknown", headers=headers).status_code in {401, 403}


def test_code_redeem_requires_original_client_redirect_resource_and_pkce_and_is_single_use(client):
    client_id, other_id = register(client), register(client)
    transaction = authorize(client, client_id)
    code = approve(client, transaction)["code"][0]
    for override in [{"code_verifier": "wrong" * 16}, {"redirect_uri": CALLBACK + "/"},
                     {"resource": "https://wrong.example/mcp"}, {"client_id": other_id},
                     {"scope": oauth.READ_SCOPE + " " + oauth.WRITE_SCOPE}]:
        response = exchange(client, client_id, code, **override)
        assert response.status_code == 400
    valid = exchange(client, client_id, code)
    assert valid.status_code == 200
    assert valid.headers["cache-control"] == "no-store"
    assert exchange(client, client_id, code).status_code == 400


def test_expired_authorization_code_and_transaction_cannot_be_used(client):
    client_id = register(client)
    transaction = authorize(client, client_id)
    with oauth.connect() as conn:
        conn.execute("UPDATE mcp_oauth_authorizations SET expires_at = ?", ("2000-01-01T00:00:00+00:00",))
    assert client.get(oauth.AUTHORIZATION_URL, params={"transaction": transaction}, headers=OWNER).status_code == 400
    transaction = authorize(client, client_id)
    code = approve(client, transaction)["code"][0]
    with oauth.connect() as conn:
        conn.execute("UPDATE mcp_oauth_codes SET expires_at = ?", ("2000-01-01T00:00:00+00:00",))
    assert exchange(client, client_id, code).status_code == 400


def test_resource_only_access_and_internal_route_read_full_limits(client):
    _, read, _, _ = connected(client)
    identity = oauth.authenticate_access_token(read["access_token"], "/mcp/", "POST")
    oauth.validate_internal_grant(identity["oauth_grant_id"], identity["email"], identity["uid"], identity["oauth_scopes"], "/api/dashboard/me", "GET")
    with pytest.raises(HTTPException) as error:
        oauth.authenticate_access_token(read["access_token"], "/api/dashboard/me", "GET")
    assert error.value.status_code == 403
    with pytest.raises(HTTPException):
        oauth.enforce_delegated_route("/api/dashboard/queue/v2/create", "POST", "read")
    _, full, _, _ = connected(client, oauth.READ_SCOPE + " " + oauth.WRITE_SCOPE)
    assert oauth.authenticate_access_token(full["access_token"], "/mcp", "POST")["agent_access_mode"] == "full"
    oauth.enforce_delegated_route("/api/dashboard/queue/v2/create", "POST", "full")
    for path in [oauth.AUTHORIZATION_URL, oauth.CONNECTIONS_URL, oauth.CONNECTIONS_URL + "/123",
                 "/api/dashboard/me/agent-connections", "/api/dashboard/me/api-keys", "/api/auth/custom-token",
                 "/api/v1", "/api/v1/posts", "/api/slack/interactions", "/docs"]:
        with pytest.raises(HTTPException):
            oauth.enforce_delegated_route(path, "GET", "full")


def test_refresh_rotation_narrowing_resource_binding_and_reuse_revokes_entire_grant(client):
    client_id, original, _, _ = connected(client, oauth.READ_SCOPE + " " + oauth.WRITE_SCOPE)
    invalid = refresh(client, client_id, original["refresh_token"], resource="https://wrong.example/mcp")
    assert invalid.status_code == 400
    narrowed = refresh(client, client_id, original["refresh_token"], scope=oauth.READ_SCOPE)
    assert narrowed.status_code == 200
    new = narrowed.json()
    assert new["refresh_token"] != original["refresh_token"]
    assert new["scope"] == oauth.READ_SCOPE
    assert refresh(client, client_id, new["refresh_token"], scope=oauth.READ_SCOPE + " " + oauth.WRITE_SCOPE).status_code == 400
    assert oauth.authenticate_access_token(new["access_token"], "/mcp", "POST")["agent_access_mode"] == "read"
    replay = refresh(client, client_id, original["refresh_token"])
    assert replay.status_code == 400
    for access in [original["access_token"], new["access_token"]]:
        with pytest.raises(HTTPException):
            oauth.authenticate_access_token(access, "/mcp", "POST")
    assert refresh(client, client_id, new["refresh_token"]).status_code == 400
    assert client.get(oauth.CONNECTIONS_URL, headers=OWNER).json()["connections"][0]["revoked_at"]


def test_revocation_requires_original_client_and_management_original_owner_uid(client):
    client_id, tokens, _, _ = connected(client)
    other_id = register(client)
    assert client.post("/oauth/revoke", data={"client_id": other_id, "token": tokens["access_token"]}).status_code == 200
    identity = oauth.authenticate_access_token(tokens["access_token"], "/mcp", "POST")
    for headers in [{**OWNER, "x-test-uid": "new-uid"}, {**OWNER, "x-test-email": "bob@example.com"}]:
        assert client.get(oauth.CONNECTIONS_URL, headers=headers).json()["connections"] == []
        assert client.delete(oauth.CONNECTIONS_URL + "/" + identity["oauth_grant_id"], headers=headers).status_code == 404
    assert client.post("/oauth/revoke", data={"client_id": client_id, "token": tokens["refresh_token"]}).status_code == 200
    with pytest.raises(HTTPException):
        oauth.authenticate_access_token(tokens["access_token"], "/mcp", "POST")
    _, second, _, _ = connected(client)
    second_identity = oauth.authenticate_access_token(second["access_token"], "/mcp", "POST")
    assert client.delete(oauth.CONNECTIONS_URL + "/" + second_identity["oauth_grant_id"], headers=OWNER).status_code == 200
    with pytest.raises(HTTPException):
        oauth.authenticate_access_token(second["access_token"], "/mcp", "POST")


def test_expiry_issuer_and_invalid_persisted_scope_fail_closed(client, monkeypatch):
    _, tokens, _, _ = connected(client)
    monkeypatch.setenv("SENTIENT_OAUTH_ISSUER", "https://other.example")
    with pytest.raises(HTTPException):
        oauth.authenticate_access_token(tokens["access_token"], "/mcp", "POST")
    monkeypatch.delenv("SENTIENT_OAUTH_ISSUER")
    with oauth.connect() as conn:
        conn.execute("UPDATE mcp_oauth_tokens SET scopes = ? WHERE kind = 'access'", ("[]",))
    with pytest.raises(HTTPException):
        oauth.authenticate_access_token(tokens["access_token"], "/mcp", "POST")
    with oauth.connect() as conn:
        conn.execute("UPDATE mcp_oauth_tokens SET scopes = ?, expires_at = ? WHERE kind = 'access'", (json.dumps([oauth.READ_SCOPE]), "2000-01-01T00:00:00+00:00"))
    with pytest.raises(HTTPException):
        oauth.authenticate_access_token(tokens["access_token"], "/mcp", "POST")


def test_registration_rate_and_cleanup_preserve_clients_and_live_replay_records(client):
    _, tokens, _, _ = connected(client)
    refresh_row = oauth._hash(tokens["refresh_token"])
    with oauth.connect() as conn:
        conn.execute("UPDATE mcp_oauth_tokens SET consumed_at = ? WHERE token_hash = ?", (oauth.utc_now(), refresh_row))
    register(client)
    with oauth.connect() as conn:
        assert conn.execute("SELECT * FROM mcp_oauth_tokens WHERE token_hash = ?", (refresh_row,)).fetchone()
        conn.execute("UPDATE mcp_oauth_grants SET expires_at = ?", ("2000-01-01T00:00:00+00:00",))
    register(client)
    with oauth.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM mcp_oauth_clients").fetchone()["n"] == 3
        assert conn.execute("SELECT COUNT(*) AS n FROM mcp_oauth_tokens").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM mcp_oauth_codes").fetchone()["n"] == 0
    for _ in range(17):
        register(client)
    limited = client.post("/oauth/register", json={"redirect_uris": [CALLBACK]})
    assert limited.status_code == 429
    assert limited.json()["error"] == "temporarily_unavailable"


@pytest.mark.parametrize("path,limit,extra", [
    ("/oauth/token", 120, {"grant_type": "authorization_code", "code": "invalid"}),
    ("/oauth/revoke", 60, {"token": "invalid"}),
])
def test_public_rate_limit_cannot_be_bypassed_by_varying_client_ids(client, monkeypatch, path, limit, extra):
    fixed = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(oauth, "datetime", FixedDateTime)
    monkeypatch.setattr(oauth, "utc_now", lambda: fixed.isoformat(timespec="seconds"))
    for index in range(limit + 3):
        response = client.post(path, data={"client_id": f"unknown-client-{index}",
                               "resource": oauth.resource_url(), **extra})
        assert response.status_code == (400 if index < limit else 429)
        assert response.json()["error"] == ("invalid_client" if index < limit else "temporarily_unavailable")
        assert response.headers["cache-control"] == "no-store"
    with oauth.connect() as conn:
        # The aggregate bucket is checked before creating any new client key.
        # Only the allowed attempts can allocate client-specific counters.
        assert conn.execute("SELECT COUNT(*) AS n FROM mcp_oauth_rate_limits").fetchone()["n"] == limit + 1
        assert conn.execute("SELECT MAX(hits) AS n FROM mcp_oauth_rate_limits").fetchone()["n"] == limit
        assert conn.execute("SELECT COUNT(*) AS n FROM mcp_oauth_clients").fetchone()["n"] == 0
