import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import golden_nuggets, main


@pytest.fixture
def connect(tmp_path, monkeypatch):
    path = tmp_path / "nuggets.sqlite"

    @contextmanager
    def _connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(golden_nuggets, "connect", _connect)
    return _connect


@pytest.fixture
def client(connect):
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.user_email = "viewer@example.com"
        request.state.is_dev = request.headers.get("x-test-role") == "dev"
        request.state.queue_role_preview_active = False
        return await call_next(request)

    app.include_router(golden_nuggets.router)
    return TestClient(app)


GOLDEN = {"label": "golden_nugget", "score": 0.81, "targetAccount": "chatgptricks", "mode": "jev_golden_nugget"}


def test_confirmed_nugget_is_shared_with_every_user(client):
    golden_nuggets.record_review("@Source", "ABC", GOLDEN, "dev@example.com")
    golden_nuggets.record_review("source", "ABC", GOLDEN, "dev@example.com")

    items = client.get("/api/dashboard/golden-nuggets").json()["items"]

    assert items == [{
        "account": "source",
        "shortcode": "ABC",
        "label": "golden_nugget",
        "targetAccount": "chatgptricks",
        "score": 0.81,
        "reviewedAt": items[0]["reviewedAt"],
    }]


def test_downgraded_review_clears_the_mark(client):
    golden_nuggets.record_review("source", "ABC", GOLDEN, "dev@example.com")
    golden_nuggets.record_review("source", "ABC", {"label": "potential", "score": 0.6}, "dev@example.com")

    assert client.get("/api/dashboard/golden-nuggets").json()["items"] == []


def test_potential_is_not_stored(client):
    golden_nuggets.record_review("source", "XYZ", {"label": "potential", "score": 0.6})

    assert client.get("/api/dashboard/golden-nuggets").json()["items"] == []


def test_only_dev_can_remove_a_mark(client):
    golden_nuggets.record_review("source", "ABC", GOLDEN)

    assert client.delete("/api/dashboard/golden-nuggets/source/ABC").status_code == 403
    assert client.delete("/api/dashboard/golden-nuggets/source/ABC", headers={"x-test-role": "dev"}).status_code == 200
    assert client.get("/api/dashboard/golden-nuggets").json()["items"] == []


def test_jev_review_endpoint_records_the_result(monkeypatch, connect):
    monkeypatch.setattr(main, "_jev_post_snapshot", lambda account, shortcode: {"account": "source", "shortcode": "ABC", "text": "A post"})

    @contextmanager
    def main_connect():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE accounts (handle TEXT, label TEXT, is_active INTEGER, group_name TEXT)")
        yield conn

    monkeypatch.setattr(main, "connect", main_connect)
    monkeypatch.setattr(main, "golden_nugget_review", lambda *_: GOLDEN)
    request = SimpleNamespace(state=SimpleNamespace(user_email="dev@example.com"))

    result = main.dashboard_jev_golden_nugget(request, "source", "ABC")

    assert result["label"] == "golden_nugget"
    with connect() as conn:
        row = conn.execute("SELECT account, shortcode, reviewed_by FROM post_golden_nuggets").fetchone()
    assert tuple(row) == ("source", "ABC", "dev@example.com")
