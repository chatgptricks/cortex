"""Exercise upload privacy through real HTTP routes without external services."""
import json
from collections import OrderedDict
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from app import db, main, media_storage, slack_alerts


@pytest.fixture
def upload_routes(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "media-upload.sqlite3")
    monkeypatch.setattr(db, "ensure_directories", lambda: None)
    db.init_db()
    monkeypatch.setattr(main, "connect", db.connect)
    monkeypatch.setattr(main, "FIREBASE_APP", object())
    monkeypatch.setattr(main, "log_usage_event", lambda *args: None)
    monkeypatch.setattr(main.firebase_auth, "verify_id_token", lambda token: {"email": token, "uid": "test"})

    def user_access(email):
        if email not in {"admin@example.com", "pd@example.com", "viewer@example.com"}:
            return None
        return {
            "is_admin": email == "admin@example.com", "operating_role": "pd", "operating_roles": '["pd"]',
            "time_zone": "America/Costa_Rica",
        }

    monkeypatch.setattr(main, "get_dashboard_user_access", user_access)
    writes, signed_reads, notifications, cover_fetches = [], [], [], []

    class FakeR2Client:
        def put_object(self, **kwargs):
            writes.append(kwargs)

        def head_object(self, *, Bucket, Key):
            stored = next(item for item in writes if item["Bucket"] == Bucket and item["Key"] == Key)
            return {"CacheControl": stored["CacheControl"], "ContentType": stored["ContentType"],
                    "ETag": '"example-etag"'}

        def generate_presigned_url(self, operation, **kwargs):
            assert operation == "get_object"
            signed_reads.append(kwargs)
            return "https://media.example.com/temporary-image"

    monkeypatch.setattr(media_storage, "R2_BUCKET", "example-media")
    monkeypatch.setattr(media_storage, "r2_enabled", lambda: True)
    monkeypatch.setattr(media_storage, "_client", lambda: FakeR2Client())
    monkeypatch.setattr(media_storage, "_private_metadata_cache", OrderedDict())
    monkeypatch.setattr(main, "TRICKS_DASH_REFRESH_PASSWORD", "example-refresh-password")
    monkeypatch.setattr(slack_alerts, "slack_configured", lambda: True)

    def capture_notification(message, *, title=None, image_url=None):
        notifications.append({"message": message, "title": title, "image_url": image_url})
        return True

    monkeypatch.setattr(slack_alerts, "notify_custom", capture_notification)
    source_url = "https://cdn.example.com/public-cover.webp"

    def fetch_cover(url, **kwargs):
        assert url == source_url
        cover_fetches.append(url)
        return httpx.Response(200, content=b"public-cover", headers={"content-type": "image/webp"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fetch_cover)
    monkeypatch.setattr(main, "get_account_config", lambda account: {"is_canonical": False})
    with db.connect() as conn:
        request_id = conn.execute(
            """INSERT INTO queue_requests
               (post_account, post_shortcode, production_points, status, designer_email,
                coordinator_email, created_at, updated_at)
               VALUES ('', 'manual-example', 3, 'scheduled', 'pd@example.com', 'vc@example.com', 'now', 'now')"""
        ).lastrowid
        cover_id = conn.execute(
            """INSERT INTO dashboard_posts (account, shortcode, cover_source_url, created_at, updated_at)
               VALUES ('example.account', 'EXAMPLE', ?, 'now', 'now')""", (source_url,)
        ).lastrowid

    app = FastAPI()
    app.middleware("http")(main._require_firebase_user)
    app.add_api_route("/api/dashboard/queue/v2/requests/{request_id}/attachments",
                      main.dashboard_queue_v2_add_attachment, methods=["POST"])
    app.add_api_route("/api/dashboard/queue/v2/requests/{request_id}/attachments/{attachment_id}",
                      main.dashboard_queue_v2_attachment, methods=["GET"])
    app.add_api_route("/api/admin/slack-custom", main.admin_slack_custom, methods=["POST"])
    app.add_api_route("/api/dashboard/covers/{account}/{post_id}", main.dashboard_cover, methods=["GET"])
    with TestClient(app, follow_redirects=False) as client:
        yield SimpleNamespace(client=client, connect=db.connect, request_id=request_id, cover_id=cover_id,
                              writes=writes, signed_reads=signed_reads, notifications=notifications,
                              cover_fetches=cover_fetches)


def test_queue_attachment_is_stored_without_public_cache_and_remains_private(upload_routes):
    routes = upload_routes
    path = f"/api/dashboard/queue/v2/requests/{routes.request_id}/attachments"
    headers = {"Authorization": "Bearer pd@example.com"}
    response = routes.client.post(path, headers=headers,
                                  files={"file": ("brief.pdf", b"internal-brief", "application/pdf")})
    assert response.status_code == 200
    attachment = response.json()["request"]["attachments"][0]
    stored = routes.writes[0]
    assert len(routes.writes) == 1
    assert stored["Body"] == b"internal-brief" and stored["ContentType"] == "application/pdf"
    assert stored["CacheControl"] == "private, no-store"
    assert attachment["mediaRef"] == "r2://" + stored["Key"]
    with routes.connect() as conn:
        saved = conn.execute("SELECT attachments FROM queue_requests WHERE id = ?", (routes.request_id,)).fetchone()
    assert json.loads(saved["attachments"]) == [attachment]

    download = routes.client.get(path + "/" + attachment["id"], headers=headers)
    assert download.status_code == 307 and download.headers["cache-control"] == "private, no-store"
    assert routes.signed_reads[-1]["Params"]["Key"] == stored["Key"]
    assert routes.signed_reads[-1]["ExpiresIn"] == 300


def test_queue_upload_rejects_unauthorized_users_before_storing_bytes(upload_routes):
    routes = upload_routes
    path = f"/api/dashboard/queue/v2/requests/{routes.request_id}/attachments"
    for headers, status in [({}, 401), ({"Authorization": "Bearer viewer@example.com"}, 403)]:
        response = routes.client.post(path, headers=headers,
                                      files={"file": ("brief.pdf", b"internal-brief", "application/pdf")})
        assert response.status_code == status
    assert routes.writes == []
    with routes.connect() as conn:
        saved = conn.execute("SELECT attachments FROM queue_requests WHERE id = ?", (routes.request_id,)).fetchone()
    assert json.loads(saved["attachments"]) == []


def test_admin_alert_image_is_stored_without_public_cache(upload_routes):
    routes = upload_routes
    response = routes.client.post(
        "/api/admin/slack-custom", headers={"Authorization": "Bearer admin@example.com"},
        data={"password": "example-refresh-password", "message": "Internal review", "title": "Example alert"},
        files={"image": ("screenshot.png", b"internal-screenshot", "image/png")},
    )
    assert response.status_code == 200 and response.json() == {"sent": True}
    assert len(routes.writes) == 1
    stored = routes.writes[0]
    assert stored["Key"].startswith("uploads/alert-") and stored["Key"].endswith(".png")
    assert stored["Body"] == b"internal-screenshot" and stored["ContentType"] == "image/png"
    assert stored["CacheControl"] == "private, no-store"
    assert routes.signed_reads[-1]["Params"]["Key"] == stored["Key"]
    assert routes.signed_reads[-1]["ExpiresIn"] == 86400
    assert routes.notifications == [{"message": "Internal review", "title": "Example alert",
                                     "image_url": "https://media.example.com/temporary-image"}]


def test_alert_upload_requires_admin_access_and_refresh_password_before_storage(upload_routes):
    routes = upload_routes
    for email, password, status in [("viewer@example.com", "example-refresh-password", 403),
                                    ("admin@example.com", "incorrect-example-password", 401)]:
        response = routes.client.post(
            "/api/admin/slack-custom", headers={"Authorization": f"Bearer {email}"},
            data={"password": password, "message": "Internal review"},
            files={"image": ("screenshot.png", b"internal-screenshot", "image/png")},
        )
        assert response.status_code == status
    assert routes.writes == [] and routes.notifications == []


def test_public_cover_keeps_public_storage_cache_and_reuses_cached_object(upload_routes):
    routes = upload_routes
    path = f"/api/dashboard/covers/example.account/{routes.cover_id}"
    response = routes.client.get(path)
    assert response.status_code == 307 and response.headers["cache-control"].startswith("public,")
    assert len(routes.writes) == 1
    stored = routes.writes[0]
    assert stored["Body"] == b"public-cover" and stored["ContentType"] == "image/webp"
    assert stored["CacheControl"] == "public, max-age=31536000, immutable"
    assert "ResponseCacheControl" not in routes.signed_reads[-1]["Params"]
    with routes.connect() as conn:
        saved = conn.execute("SELECT cover_image_path FROM dashboard_posts WHERE id = ?", (routes.cover_id,)).fetchone()
    assert saved["cover_image_path"] == "r2://" + stored["Key"]

    assert routes.client.get(path).status_code == 307
    assert len(routes.writes) == 1 and len(routes.cover_fetches) == 1
