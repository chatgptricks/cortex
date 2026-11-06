"""Exercise the real read-only MCP path and the live owner permission gate."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def test_read_only_agent_discovers_data_and_tracks_owner_admin_changes(tmp_path, monkeypatch):
    from app import agent_connections, db, main, product_mcp
    from app.hot_calibration import router

    path = tmp_path / "agent-hot.sqlite"

    @contextmanager
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(agent_connections, "connect", connect)
    db.init_db()
    key = "sad_agent_" + "x" * 43
    now = datetime.now(UTC)
    with connect() as connection:
        connection.execute(
            "INSERT INTO agent_connections "
            "(id, owner_email, owner_uid, name, key_hash, key_prefix, access_mode, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("read-agent", "teammate@example.com", "test-uid", "Read agent",
             hashlib.sha256(key.encode()).hexdigest(), key[:17], "read", now.isoformat(),
             (now + timedelta(days=30)).isoformat()),
        )
    access = {"is_admin": True, "operating_role": "vc", "operating_roles": '["vc","pd"]'}
    monkeypatch.setattr(main, "FIREBASE_APP", object())
    monkeypatch.setattr(main, "get_dashboard_user_access", lambda email: access)
    monkeypatch.setattr(main, "log_usage_event", lambda *args: None)
    app = FastAPI()
    app.middleware("http")(main._require_firebase_user)
    app.include_router(router)

    @app.get("/api/dashboard/me")
    def me(request: Request):
        return main.dashboard_me(request)

    product_mcp.install(app)

    def rpc(client, name, arguments):
        response = client.post("/mcp", headers={
            "Authorization": "Bearer " + key,
            "Accept": "application/json, text/event-stream",
        }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}})
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        return result, json.loads(result["content"][0]["text"])

    with TestClient(app) as client:
        discovery = client.post("/mcp", headers={
            "Authorization": "Bearer " + key,
            "Accept": "application/json, text/event-stream",
        }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
        tool = next(tool for tool in discovery["result"]["tools"]
                    if tool["name"] == "get_admin_engagement_checkpoints")
        assert tool["annotations"]["readOnlyHint"] is True
        assert "confirm" not in tool["inputSchema"]["properties"]
        result, data = rpc(client, tool["name"], {"query": {"days": 90, "limit": 1}})
        assert not result.get("isError"), data
        assert data["status"] == 200

        access["is_admin"] = False
        result, data = rpc(client, tool["name"], {"query": {"days": 90, "limit": 1}})
        assert result["isError"] is True
        assert data["status"] == 403
        _, me = rpc(client, "product_me", {})
        assert me["data"]["is_admin"] is False

        access["is_admin"] = True
        _, me = rpc(client, "product_me", {})
        assert me["data"]["is_admin"] is True
        assert me["data"]["agent_access_mode"] == "read"
        result, data = rpc(client, tool["name"], {"query": {"days": 90, "limit": 1}})
        assert data["status"] == 200 and not result.get("isError")
        assert client.get("/api/admin/engagement/checkpoints").status_code == 401
