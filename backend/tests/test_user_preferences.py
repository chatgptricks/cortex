import sqlite3
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import user_preferences


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "preferences.sqlite"

    @contextmanager
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(user_preferences, "connect", connect)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.user_email = request.headers.get("x-test-user", "")
        return await call_next(request)

    app.include_router(user_preferences.router)
    return TestClient(app)


URL = "/api/dashboard/me/preferences"


def test_preferences_follow_the_user_not_the_browser(client):
    saved = client.post(URL, headers={"x-test-user": "Ana@Example.com"}, json={"preferences": {
        "language": "es", "theme": "light", "accent": "#A1B2C3", "trackerFavorites": ["@OpenAI", "openai", "sama"],
        "queueGuideCompleted": True, "queueDesignerScope": "designer@example.com", "effects": "subtle",
    }})
    assert saved.status_code == 200

    mine = client.get(URL, headers={"x-test-user": "ana@example.com"}).json()["preferences"]
    assert mine == {
        "language": "es", "theme": "light", "accent": "#a1b2c3", "trackerFavorites": ["openai", "sama"],
        "queueGuideCompleted": True, "queueDesignerScope": "designer@example.com", "effects": "subtle",
    }
    assert client.get(URL, headers={"x-test-user": "bob@example.com"}).json()["preferences"] == {}


def test_updates_merge_and_null_clears_a_key(client):
    headers = {"x-test-user": "ana@example.com"}
    client.post(URL, headers=headers, json={"preferences": {"language": "es", "theme": "dark"}})
    client.post(URL, headers=headers, json={"preferences": {"theme": "light", "language": None}})

    assert client.get(URL, headers=headers).json()["preferences"] == {"theme": "light"}


@pytest.mark.parametrize("change", [
    {"theme": "blue"}, {"language": "fr"}, {"accent": "red"}, {"accentCustom": "lime"},
    {"queueGuideCompleted": "yes"}, {"trackerFavorites": ["bad handle!"]}, {"somethingElse": 1},
])
def test_invalid_or_unknown_preferences_are_rejected(client, change):
    response = client.post(URL, headers={"x-test-user": "ana@example.com"}, json={"preferences": change})
    assert response.status_code == 400


def test_signed_out_requests_are_rejected(client):
    assert client.get(URL).status_code == 401
    assert client.post(URL, json={"preferences": {"theme": "dark"}}).status_code == 401
