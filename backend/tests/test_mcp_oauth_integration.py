"""Exercise OAuth and legacy credentials through the real MCP/auth boundary."""
from __future__ import annotations

import json
import base64
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient


def rpc(client, credential, method, params=None):
    headers = {"Accept": "application/json, text/event-stream"}
    if credential:
        headers["Authorization"] = "Bearer " + credential
    return client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
    })


def tool_data(response):
    assert response.status_code == 200, response.text
    payload = response.json()["result"]
    return payload, json.loads(payload["content"][0]["text"])


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    from app import agent_connections, db, external_api, main, mcp_oauth, product_mcp

    database = tmp_path / "mcp-integration.sqlite3"

    @contextmanager
    def connect():
        connection = sqlite3.connect(database, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    users = {
        "ana@example.com": {"is_admin": True, "operating_role": "pd", "operating_roles": '["pd"]'},
        "bob@example.com": {"is_admin": False, "operating_role": "pd", "operating_roles": '["pd"]'},
    }
    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(main, "connect", connect)
    monkeypatch.setattr(agent_connections, "connect", connect)
    monkeypatch.setattr(mcp_oauth, "connect", connect)
    monkeypatch.setattr(main, "FIREBASE_APP", object())
    monkeypatch.setattr(main, "get_dashboard_user_access", lambda email: users.get(email))
    if hasattr(mcp_oauth, "get_dashboard_user_access"):
        monkeypatch.setattr(mcp_oauth, "get_dashboard_user_access", lambda email: users.get(email))
    monkeypatch.setattr(main, "log_usage_event", lambda *args: None)

    def firebase_identity(token, *args, **kwargs):
        if token not in users:
            raise ValueError("Invalid test Firebase session")
        return {"email": token, "uid": "firebase-uid-" + token, "email_verified": True}

    monkeypatch.setattr(main.firebase_auth, "verify_id_token", firebase_identity)
    app = FastAPI()
    app.middleware("http")(main._require_firebase_user)
    app.include_router(agent_connections.router)
    app.include_router(external_api.management_router)
    app.include_router(mcp_oauth.router)

    @app.get("/api/dashboard/me")
    def me(request: Request):
        return main.dashboard_me(request)

    @app.get("/api/dashboard/identity-canary")
    def identity_canary(request: Request):
        return {
            "email": request.state.user_email,
            "uid": request.state.user_uid,
            "is_admin": request.state.is_admin,
            "received_bearer": bool(request.headers.get("authorization")),
        }

    @app.get("/api/admin/security-canary")
    def admin_canary():
        return {"private_admin_data": True}

    actions = []

    @app.post("/api/dashboard/test-action")
    def action(request: Request):
        actions.append(request.state.user_email)
        return {"owner": request.state.user_email}

    product_mcp.install(app)
    with TestClient(app) as client:
        yield client, users, connect, actions


def create_legacy(client, owner="ana@example.com", mode="full"):
    response = client.post("/api/dashboard/me/agent-connections",
        headers={"Authorization": "Bearer " + owner},
        json={"name": "Integration legacy", "access_mode": mode})
    assert response.status_code == 201, response.text
    return response.json()["key"]


def create_oauth(client, owner="ana@example.com", mode="full"):
    from app import mcp_oauth

    redirect = "https://chatgpt.com/connector_platform_oauth_redirect"
    verifier = "integration-verifier-" + "a" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    registration = client.post("/oauth/register", json={"client_name": "ChatGPT integration",
        "redirect_uris": [redirect], "token_endpoint_auth_method": "none"})
    assert registration.status_code == 201, registration.text
    client_id = registration.json()["client_id"]
    response = client.get("/oauth/authorize", params={"client_id": client_id, "redirect_uri": redirect,
        "response_type": "code", "resource": mcp_oauth.resource_url(),
        "scope": "sentient:read sentient:write" if mode == "full" else "sentient:read",
        "code_challenge_method": "S256", "code_challenge": challenge, "state": "state-integration"},
        follow_redirects=False)
    assert response.status_code == 302, response.text
    transaction = parse_qs(urlsplit(response.headers["location"]).query)["transaction"][0]
    browser = {"Authorization": "Bearer " + owner}
    consent_details = client.get(mcp_oauth.AUTHORIZATION_URL,
        params={"transaction": transaction}, headers=browser)
    assert consent_details.status_code == 200, consent_details.text
    assert consent_details.json()["email"] == owner
    consent = client.post(mcp_oauth.AUTHORIZATION_URL, headers=browser,
        json={"transaction": transaction, "approve": True})
    assert consent.status_code == 200, consent.text
    callback = parse_qs(urlsplit(consent.json()["redirect_url"]).query)
    assert callback["state"] == ["state-integration"]
    assert callback["iss"] == [mcp_oauth.issuer_url()]
    tokens = client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": client_id,
        "redirect_uri": redirect, "code": callback["code"][0], "code_verifier": verifier,
        "resource": mcp_oauth.resource_url()})
    assert tokens.status_code == 200, tokens.text
    return {**tokens.json(), "client_id": client_id}


def test_oauth_discovery_and_mcp_challenge_are_public(hosted):
    from app import mcp_oauth

    client, *_ = hosted
    for path in ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"]:
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert response.json()["resource"] == mcp_oauth.resource_url()
        assert response.json()["authorization_servers"] == [mcp_oauth.issuer_url()]
    metadata = client.get("/.well-known/oauth-authorization-server")
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["code_challenge_methods_supported"] == ["S256"]
    challenge = rpc(client, None, "tools/list")
    assert challenge.status_code == 401
    assert "resource_metadata=" in challenge.headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource/mcp" in challenge.headers["www-authenticate"]


def test_oauth_and_legacy_tools_keep_owners_and_recheck_current_roles(hosted):
    client, users, *_ = hosted
    oauth = create_oauth(client, owner="bob@example.com")["access_token"]
    legacy = create_legacy(client, owner="ana@example.com")
    initialize = rpc(client, oauth, "initialize", {"protocolVersion": "2025-06-18",
        "capabilities": {}, "clientInfo": {"name": "ChatGPT", "version": "1.0"}})
    assert initialize.status_code == 200, initialize.text
    assert initialize.json()["result"]["serverInfo"]["name"] == "sentient-dash"

    def inspect(credential):
        _, data = tool_data(rpc(client, credential, "tools/call",
            {"name": "get_dashboard_identity_canary", "arguments": {}}))
        assert data["status"] == 200, data
        return data["data"]

    # Concurrent requests catch accidentally shared or mutable forwarding state.
    with ThreadPoolExecutor(max_workers=2) as executor:
        oauth_identity, legacy_identity = list(executor.map(inspect, [oauth, legacy]))
    assert oauth_identity["email"] == "bob@example.com"
    assert oauth_identity["uid"] == "firebase-uid-bob@example.com"
    assert oauth_identity["received_bearer"] is False
    assert legacy_identity["email"] == "ana@example.com"
    assert legacy_identity["received_bearer"] is True

    admin_tool = {"name": "get_admin_security_canary", "arguments": {}}
    result, data = tool_data(rpc(client, oauth, "tools/call", admin_tool))
    assert result["isError"] and data["status"] == 403
    users["bob@example.com"]["is_admin"] = True
    result, data = tool_data(rpc(client, oauth, "tools/call", admin_tool))
    assert not result.get("isError") and data["status"] == 200
    del users["bob@example.com"]
    assert rpc(client, oauth, "tools/list").status_code == 403
    assert rpc(client, legacy, "tools/list").status_code == 200


def test_oauth_read_write_scope_and_confirmation_are_enforced(hosted):
    client, _, _, actions = hosted
    full = create_oauth(client)["access_token"]
    read = create_oauth(client, mode="read")["access_token"]
    full_tools = rpc(client, full, "tools/list").json()["result"]["tools"]
    assert "post_dashboard_test_action" in {tool["name"] for tool in full_tools}
    write = next(tool for tool in full_tools if tool["name"] == "post_dashboard_test_action")
    assert write["securitySchemes"] == [{"type": "oauth2", "scopes": ["sentient:read", "sentient:write"]}]
    assert write["_meta"]["securitySchemes"] == write["securitySchemes"]
    read_tools = rpc(client, read, "tools/list").json()["result"]["tools"]
    # OAuth clients must discover protected actions so they can request more scopes.
    assert "post_dashboard_test_action" in {tool["name"] for tool in read_tools}
    legacy_read = create_legacy(client, mode="read")
    legacy_tools = rpc(client, legacy_read, "tools/list").json()["result"]["tools"]
    assert "post_dashboard_test_action" not in {tool["name"] for tool in legacy_tools}
    call = {"name": "post_dashboard_test_action", "arguments": {"confirm": True}}
    denied = rpc(client, read, "tools/call", call).json()
    assert denied["result"]["isError"]
    challenge = denied["result"]["_meta"]["mcp/www_authenticate"][0]
    assert 'error="insufficient_scope"' in challenge
    assert 'scope="sentient:read sentient:write"' in challenge
    no_confirm = rpc(client, full, "tools/call", {"name": call["name"], "arguments": {}}).json()
    assert no_confirm.get("error") or no_confirm["result"].get("isError")
    assert actions == []
    result, data = tool_data(rpc(client, full, "tools/call", call))
    assert not result.get("isError") and data["status"] == 200
    assert actions == ["ana@example.com"]


def test_oauth_bearer_cannot_escape_mcp_or_manage_credentials(hosted):
    from app import mcp_oauth

    client, *_ = hosted
    token = create_oauth(client)["access_token"]
    paths = ["/api/dashboard/me", "/api/dashboard/identity-canary", "/api/admin/security-canary",
        "/api/dashboard/me/agent-connections", "/api/dashboard/me/api-keys",
        mcp_oauth.CONNECTIONS_URL, mcp_oauth.AUTHORIZATION_URL, "/api/v1", "/api/health",
        "/api/dashboard/avatar/alpha"]
    for path in paths:
        response = client.get(path, headers={"Authorization": "Bearer " + token,
            "X-MCP-Internal": "true", "X-Sentient-MCP-OAuth": token})
        assert response.status_code in {401, 403}, (path, response.status_code, response.text)
    legacy = create_legacy(client)
    for credential in [token, legacy]:
        assert client.get(mcp_oauth.CONNECTIONS_URL,
            headers={"Authorization": "Bearer " + credential}).status_code == 403
    for path in [mcp_oauth.CONNECTIONS_URL, mcp_oauth.AUTHORIZATION_URL]:
        response = client.get(path, headers={"Authorization": "Bearer ana@example.com"})
        assert response.status_code != 403, (path, response.text)


def test_each_oauth_tool_revalidates_the_original_access_token(hosted, monkeypatch):
    from app import main

    client, _, _, actions = hosted
    token = create_oauth(client)["access_token"]
    verify = main.authenticate_access_token
    checks = []

    def expires_before_internal_call(credential, path, method):
        if credential == token:
            checks.append((path, method))
            if len(checks) == 2:
                raise HTTPException(401, "Access token expired before tool invocation.")
        return verify(credential, path, method)

    monkeypatch.setattr(main, "authenticate_access_token", expires_before_internal_call)
    result, data = tool_data(rpc(client, token, "tools/call",
        {"name": "post_dashboard_test_action", "arguments": {"confirm": True}}))
    assert checks == [("/mcp", "POST"), ("/mcp", "POST")]
    assert result["isError"] and data["status"] == 401
    assert 'error="invalid_token"' in result["_meta"]["mcp/www_authenticate"][0]
    assert actions == []


def test_oauth_transport_keeps_origin_protection_and_canonical_alias(hosted):
    client, *_ = hosted
    token = create_oauth(client)["access_token"]
    response = client.post("/mcp", headers={"Authorization": "Bearer " + token,
        "Origin": "https://evil.example", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 403
    alias = client.post("/mcp/", headers={"Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert alias.status_code == 200, alias.text


def test_local_development_bypass_does_not_open_oauth_or_consent(hosted, monkeypatch):
    from app import main, mcp_oauth

    client, *_ = hosted
    token = create_oauth(client)["access_token"]
    monkeypatch.setattr(main, "FIREBASE_APP", None)
    assert rpc(client, token, "tools/list").status_code == 200
    for credential in ["sad_oauth_at_invalid", "sad_oauth_rt_invalid", "sad_oauth_unknown"]:
        assert rpc(client, credential, "tools/list").status_code in {401, 403}
        assert client.get("/api/dashboard/identity-canary",
            headers={"Authorization": "Bearer " + credential}).status_code in {401, 403}
    assert client.get("/api/dashboard/identity-canary", headers={"Authorization": "Bearer " + token}).status_code == 403
    for path in [mcp_oauth.CONNECTIONS_URL, mcp_oauth.AUTHORIZATION_URL]:
        assert client.get(path).status_code in {401, 403, 503}


def test_browser_revocation_invalidates_oauth_without_touching_legacy(hosted):
    from app import mcp_oauth

    client, *_ = hosted
    token = create_oauth(client)["access_token"]
    legacy = create_legacy(client)
    connections = client.get(mcp_oauth.CONNECTIONS_URL,
        headers={"Authorization": "Bearer ana@example.com"}).json()["connections"]
    assert len(connections) == 1
    revoked = client.delete(mcp_oauth.CONNECTIONS_URL + "/" + connections[0]["id"],
        headers={"Authorization": "Bearer ana@example.com"})
    assert revoked.status_code == 200, revoked.text
    assert rpc(client, token, "tools/list").status_code == 401
    assert rpc(client, legacy, "tools/list").status_code == 200


def test_browser_identity_still_cannot_use_hosted_mcp(hosted):
    client, *_ = hosted
    assert rpc(client, "ana@example.com", "tools/list").status_code == 401
    legacy = create_legacy(client)
    assert rpc(client, legacy, "tools/list").status_code == 200


def test_internal_oauth_scope_cannot_be_forged_or_retargeted():
    from app import product_mcp

    context = {"marker": object(), "token": "sad_oauth_at_spoofed",
        "path": "/api/dashboard/me", "method": "GET"}
    scope = {"path": context["path"], "method": context["method"], "sentient_mcp_oauth": context}
    assert product_mcp.internal_oauth_token(scope) is None
    context["marker"] = product_mcp._INTERNAL_OAUTH_MARKER
    assert product_mcp.internal_oauth_token(scope) == context["token"]
    scope["path"] = "/api/dashboard/me/agent-connections"
    assert product_mcp.internal_oauth_token(scope) is None
    scope["path"] = context["path"]
    scope["method"] = "POST"
    assert product_mcp.internal_oauth_token(scope) is None


def test_catalogue_never_exposes_login_or_oauth_management():
    from app.product_mcp import catalogue

    forbidden = ["/api/auth/custom-token", "/api/dashboard/me/agent-connections",
        "/api/dashboard/me/api-keys", "/api/dashboard/me/oauth-connections",
        "/api/dashboard/me/oauth-connections/{grant_id}", "/api/dashboard/me/oauth/authorization"]
    paths = {path: {"get": {"summary": "Sensitive credential route"}} for path in forbidden}
    paths["/api/dashboard/me"] = {"get": {"summary": "Current owner"}}
    result = catalogue({"paths": paths})
    assert {tool["path"] for tool in result.values()} == {"/api/dashboard/me"}
