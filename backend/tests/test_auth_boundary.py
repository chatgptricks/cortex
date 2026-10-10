"""Private API handlers must remain unreachable when Firebase is unavailable."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import main


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.setattr(main, "FIREBASE_APP", None)
    monkeypatch.setattr(main, "log_usage_event", lambda *args: None)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.middleware("http")(main._require_firebase_user)
    reached = []

    def handler():
        reached.append(True)
        return {"private_data": "only returned after authorization"}

    for path in ("/api/dashboard/auth-test", "/api/admin/auth-test", "/api/health",
                 "/docs", "/openapi.json", "/api/dashboard/avatar/test-account",
                 "/.well-known/oauth-protected-resource"):
        app.add_api_route(path, handler, methods=["GET", "POST", "OPTIONS"])
    return TestClient(app), reached


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("path", ["/api/dashboard/auth-test", "/api/admin/auth-test"])
@pytest.mark.parametrize("token", [None, "unverified-firebase-token"])
def test_missing_firebase_blocks_private_reads_and_writes(boundary, method, path, token):
    client, reached = boundary
    headers = {"Authorization": "Bearer " + token} if token else {}
    response = client.request(method, path, headers=headers)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "Firebase authentication is not configured."}
    assert reached == []


@pytest.mark.parametrize("path", ["/api/health", "/docs", "/openapi.json",
    "/api/dashboard/avatar/test-account", "/.well-known/oauth-protected-resource"])
def test_missing_firebase_preserves_explicit_public_routes(boundary, path):
    client, reached = boundary
    assert client.get(path).status_code == 200
    assert reached == [True]


def test_missing_firebase_preserves_preflight_only(boundary):
    client, reached = boundary
    assert client.options("/api/dashboard/auth-test").status_code == 200
    assert reached == [True]
    assert client.get("/api/dashboard/auth-test").status_code == 503
    assert reached == [True]


def test_configured_firebase_requires_verified_allowlisted_identity(boundary, monkeypatch):
    client, reached = boundary
    monkeypatch.setattr(main, "FIREBASE_APP", object())

    def verify(token):
        if token == "invalid":
            raise ValueError("Invalid token")
        return {"email": token, "uid": "verified-test-user"}

    access = {"is_admin": False, "operating_role": "pd", "operating_roles": '["pd"]',
              "time_zone": "America/Costa_Rica"}
    monkeypatch.setattr(main.firebase_auth, "verify_id_token", verify)
    monkeypatch.setattr(main, "get_dashboard_user_access",
                        lambda email: access if email == "allowed@example.com" else None)
    path = "/api/dashboard/auth-test"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer outside@example.com"}).status_code == 403
    assert reached == []
    assert client.get(path, headers={"Authorization": "Bearer allowed@example.com"}).status_code == 200
    assert reached == [True]
    assert client.post("/api/admin/auth-test",
                       headers={"Authorization": "Bearer allowed@example.com"}).status_code == 403
    assert reached == [True]
    access["is_admin"] = True
    assert client.post("/api/admin/auth-test",
                       headers={"Authorization": "Bearer allowed@example.com"}).status_code == 200
    assert reached == [True, True]


@pytest.mark.parametrize("verify", ["true", "1", "yes", "on"])
def test_health_database_verification_requires_authenticated_admin(monkeypatch, verify):
    monkeypatch.setattr(main, "FIREBASE_APP", object())
    monkeypatch.setattr(main, "log_usage_event", lambda *args: None)
    monkeypatch.setattr(main.firebase_auth, "verify_id_token", lambda token: {"email": token, "uid": "test"})
    monkeypatch.setattr(main, "get_dashboard_user_access", lambda email: {
        "is_admin": email == "admin@example.com", "operating_role": "pd", "operating_roles": '["pd"]',
        "time_zone": "America/Costa_Rica",
    })
    checks = []

    def readiness(check):
        checks.append(check)
        return {"ready": True}

    monkeypatch.setattr(main, "_runtime_data_readiness", readiness)
    app = FastAPI()
    app.middleware("http")(main._require_firebase_user)
    app.add_api_route("/api/health", main.health, methods=["GET"])
    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
    path = "/api/health?verify=" + verify
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer viewer@example.com"}).status_code == 403
    assert checks == []
    response = client.get(path, headers={"Authorization": "Bearer admin@example.com"})
    assert response.status_code == 200 and response.json()["data"] == {"ready": True}
    assert response.headers["cache-control"] == "private, no-store"
    assert checks == ["all"]
    monkeypatch.setattr(main, "FIREBASE_APP", None)
    assert client.get("/api/health").status_code == 200
    assert client.get(path).status_code == 503
    assert checks == ["all"]
