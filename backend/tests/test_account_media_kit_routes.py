import sys
from types import ModuleType

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import account_media_kit, main


@pytest.fixture
def report_client(monkeypatch):
    state = {"calls": [], "renders": [], "styles": [], "role": "admin", "failure": None}

    def build(handle):
        state["calls"].append(handle)
        if handle == "missing":
            raise HTTPException(status_code=404, detail="Unknown account.")
        return {"generated_at": "2026-10-09T04:30:00+00:00", "account": {"handle": handle}, "revision": len(state["calls"])}

    renderer = ModuleType("app.media_kit_pdf")

    def render(report, *, theme="light", accent="#00A991"):
        state["renders"].append(report)
        state["styles"].append({"theme": theme, "accent": accent})
        if state["failure"]:
            raise RuntimeError("Internal render details must stay private")
        return b"%PDF-1.4\n" + str(report["revision"]).encode() + b"\n%%EOF"

    renderer.render_media_kit_pdf = render
    monkeypatch.setitem(sys.modules, "app.media_kit_pdf", renderer)
    monkeypatch.setattr(account_media_kit, "build_account_media_kit", build)
    monkeypatch.setattr(main, "build_account_media_kit", build)
    monkeypatch.setattr(main, "FIREBASE_APP", object())
    monkeypatch.setattr(main.firebase_auth, "verify_id_token", lambda token: {"email": "admin@example.com", "uid": "test"})
    monkeypatch.setattr(main, "get_dashboard_user_access", lambda email: {
        "is_admin": state["role"] == "admin", "operating_role": state["role"], "operating_roles": f'["{state["role"]}"]',
    })
    monkeypatch.setattr(main, "log_usage_event", lambda *args: None)
    app = FastAPI()
    app.middleware("http")(main._require_firebase_user)
    app.add_api_route("/api/admin/accounts/{handle}/media-kit", main.admin_account_media_kit)
    app.add_api_route("/api/admin/accounts/{handle}/media-kit.pdf", main.admin_account_media_kit_pdf)
    return TestClient(app), state


def test_pdf_fresh_reads_headers_and_report_local_date(report_client):
    client, state = report_client
    first = client.get("/api/admin/accounts/chatgptricks/media-kit.pdf", headers={"Authorization": "Bearer test"})
    second = client.get("/api/admin/accounts/chatgptricks/media-kit.pdf", headers={"Authorization": "Bearer test"})
    assert first.status_code == second.status_code == 200
    assert first.headers["content-type"] == "application/pdf"
    assert first.headers["content-disposition"] == 'attachment; filename="chatgptricks-media-kit-2026-10-08.pdf"'
    assert "no-store" in first.headers["cache-control"]
    assert first.headers["x-content-type-options"] == "nosniff"
    assert "Authorization" in first.headers["vary"]
    assert first.content.startswith(b"%PDF-")
    assert first.content != second.content
    assert state["calls"] == ["chatgptricks", "chatgptricks"]
    assert len(state["renders"]) == 2
    assert state["styles"] == [{"theme": "light", "accent": "#00A991"}] * 2


def test_pdf_forwards_each_download_style_without_changing_report_or_defaults(report_client):
    client, state = report_client
    headers = {"Authorization": "Bearer test"}
    path = "/api/admin/accounts/chatgptricks/media-kit.pdf"
    for preferences in ({"theme": "dark", "accent": "#A855F7"},
                        {"theme": "light", "accent": "#a3e635"},
                        {"theme": "dark", "accent": "#000000"},
                        {"theme": "light", "accent": "#FFFFFF"}, {}):
        response = client.get(path, params=preferences, headers=headers)
        assert response.status_code == 200
        assert response.content.startswith(b"%PDF-")
        assert "no-store" in response.headers["cache-control"]
        assert "#" not in response.headers["content-disposition"]
    assert state["styles"] == [
        {"theme": "dark", "accent": "#A855F7"},
        {"theme": "light", "accent": "#a3e635"},
        {"theme": "dark", "accent": "#000000"},
        {"theme": "light", "accent": "#FFFFFF"},
        {"theme": "light", "accent": "#00A991"},
    ]
    assert len(state["calls"]) == 5
    assert all("theme" not in report and "accent" not in report for report in state["renders"])
    data = client.get("/api/admin/accounts/chatgptricks/media-kit", headers=headers).json()
    assert "theme" not in data and "accent" not in data


@pytest.mark.parametrize("field,value", [
    ("theme", "Dark"), ("theme", "system"), ("theme", ""),
    ("accent", "#fff"), ("accent", "FFFFFF"), ("accent", "#12345678"),
    ("accent", "#12GG56"), ("accent", "red"), ("accent", "#123456\n"),
])
def test_invalid_style_is_rejected_before_account_read_or_render(report_client, field, value):
    client, state = report_client
    response = client.get("/api/admin/accounts/chatgptricks/media-kit.pdf", params={field: value},
                          headers={"Authorization": "Bearer test"})
    assert response.status_code == 422
    assert any(error["loc"] == ["query", field] for error in response.json()["detail"])
    assert not state["calls"] and not state["renders"] and not state["styles"]


def test_styled_pdf_preserves_auth_before_query_validation(report_client):
    client, state = report_client
    path = "/api/admin/accounts/chatgptricks/media-kit.pdf"
    assert client.get(path, params={"theme": "dark", "accent": "#A855F7"}).status_code == 401
    state["role"] = "sales"
    response = client.get(path, params={"theme": "system", "accent": "red"},
                          headers={"Authorization": "Bearer test"})
    assert response.status_code == 403
    assert not state["calls"] and not state["renders"]


@pytest.mark.parametrize("extension", ["", ".pdf"])
def test_report_requires_auth_and_admin_access(report_client, extension):
    client, state = report_client
    path = f"/api/admin/accounts/chatgptricks/media-kit{extension}"
    assert client.get(path).status_code == 401
    state["role"] = "sales"
    assert client.get(path, headers={"Authorization": "Bearer test"}).status_code == 403
    state["role"] = "pd"
    assert client.get(path, headers={"Authorization": "Bearer test"}).status_code == 403
    assert not state["calls"]


def test_json_is_also_fresh_and_private(report_client):
    client, state = report_client
    response = client.get("/api/admin/accounts/chatgptricks/media-kit", headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    assert response.json()["revision"] == 1
    assert state["calls"] == ["chatgptricks"]
    assert not state["renders"]


def test_unknown_account_and_render_failure_are_useful_errors(report_client):
    client, state = report_client
    headers = {"Authorization": "Bearer test"}
    missing = client.get("/api/admin/accounts/missing/media-kit.pdf", headers=headers)
    assert missing.status_code == 404
    assert not state["renders"]
    state["failure"] = True
    failed = client.get("/api/admin/accounts/chatgptricks/media-kit.pdf", headers=headers)
    assert failed.status_code == 500
    assert failed.json()["detail"] == "Could not generate the PDF media kit. Please try again."
    assert "Internal" not in failed.text
