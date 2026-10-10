from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.responses import Response

from app import main, private_media


@pytest.fixture
def media_routes(monkeypatch):
    monkeypatch.setattr(main, "FIREBASE_APP", object())
    monkeypatch.setattr(main, "log_usage_event", lambda *args: None)
    monkeypatch.setattr(main.firebase_auth, "verify_id_token", lambda token: {"email": token, "uid": "test"})
    monkeypatch.setattr(main, "get_dashboard_user_access", lambda email: {
        "is_admin": email == "admin@example.com", "operating_role": "pd", "operating_roles": '["pd"]',
        "time_zone": "America/Costa_Rica",
    })
    monkeypatch.setattr(private_media, "_signing_key", lambda: b"isolated-test-key")
    clock = {"now": 1_000_000}
    monkeypatch.setattr(private_media.time, "time", lambda: clock["now"])
    reached = []
    app = FastAPI()
    app.middleware("http")(main._require_firebase_user)

    @app.api_route("/api/dashboard/user-avatar/{slack_user_id}", methods=["GET", "HEAD"])
    def avatar(slack_user_id: str):
        reached.append(slack_user_id)
        return Response(b"isolated-image", media_type="image/png", headers={"Cache-Control": "private, no-store"})

    def redirect(reference, *, private=False):
        assert private is True
        reached.append("alert-image")
        return "https://media.example.com/temporary-alert-link"

    monkeypatch.setattr(main, "redirect_url", redirect)
    app.add_api_route("/api/admin/alert-image/{filename}", main.admin_alert_image, methods=["GET", "HEAD"])
    return TestClient(app, follow_redirects=False), reached, clock


def test_staff_avatar_without_capability_or_session_is_rejected(media_routes):
    client, reached, _ = media_routes
    response = client.get("/api/dashboard/user-avatar/U10000000")
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
    assert reached == []
    response = client.get("/api/dashboard/user-avatar/U10000000",
                          headers={"Authorization": "Bearer viewer@example.com"})
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    assert reached == ["U10000000"]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_issued_avatar_capability_reaches_only_its_read_handler(media_routes, method):
    client, reached, _ = media_routes
    link = private_media.staff_avatar_url("U10000000")
    response = client.request(method, link)
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    assert reached == ["U10000000"]
    assert client.post(link).status_code == 401
    assert client.get(link.replace("U10000000", "U10000001")).status_code == 401
    alert = "/api/admin/alert-image/alert-" + "0" * 32 + ".png"
    assert client.get(alert + "?" + link.split("?", 1)[1]).status_code == 401
    assert reached == ["U10000000"]


@pytest.mark.parametrize("failure", ["expired", "missing-key"])
def test_invalid_avatar_capability_never_reaches_a_private_handler(media_routes, monkeypatch, failure):
    client, reached, clock = media_routes
    link = private_media.staff_avatar_url("U10000000")
    if failure == "expired":
        clock["now"] += 3600
    else:
        monkeypatch.setattr(private_media, "_signing_key", lambda: None)
    assert client.get(link).status_code == 401
    assert reached == []
    monkeypatch.setattr(main, "FIREBASE_APP", None)
    response = client.get(link)
    assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
    assert reached == []


def test_alert_image_requires_admin_or_dev_session(media_routes):
    client, reached, _ = media_routes
    path = "/api/admin/alert-image/alert-" + "0" * 32 + ".png"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer viewer@example.com"}).status_code == 403
    assert reached == []
    response = client.get(path, headers={"Authorization": "Bearer admin@example.com"})
    assert response.status_code == 307 and response.headers["cache-control"] == "private, no-store"
    assert response.headers["location"] == "https://media.example.com/temporary-alert-link"
    assert reached == ["alert-image"]
    response = client.get(path, headers={"Authorization": "Bearer developer@example.com"})
    assert response.status_code == 307 and reached == ["alert-image", "alert-image"]
