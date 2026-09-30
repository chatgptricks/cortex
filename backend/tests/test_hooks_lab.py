import sqlite3
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import hooks_lab
from app.jev_features import JevFeatureUnavailable


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "hooks.sqlite"

    @contextmanager
    def connection():
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(hooks_lab, "connect", connection)
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY, handle TEXT, is_canonical INTEGER
            );
            INSERT INTO accounts VALUES (1, 'chatgptricks', 1);
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY, caption TEXT, hook_text TEXT, published_at TEXT,
                likes INTEGER, shortcode TEXT, source_ref TEXT, title TEXT, updated_at TEXT
            );
            CREATE TABLE dashboard_posts (
                id INTEGER PRIMARY KEY, account TEXT, caption TEXT, hook_text TEXT,
                published_at TEXT, likes INTEGER, shortcode TEXT, permalink TEXT,
                updated_at TEXT
            );
            INSERT INTO posts VALUES (
                1,
                'Follow for more. These 5 ChatGPT prompts save me hours every week. The last one is wild.',
                'INSTAGRAM\nC H A T G P T PROMPTS\nLIKE\nCHATGPT PROMPTS',
                '2026-09-01T10:00:00Z', 12000, 'abc', 'https://instagram.com/p/abc',
                'Prompts', '2026-09-01T11:00:00Z'
            );
            INSERT INTO dashboard_posts VALUES (
                2, 'competitor', 'Nobody tells you this about writing prompts. Here is why.',
                'REELS\nSTOP WRITING PROMPTS LIKE THIS', '2026-09-02T10:00:00Z',
                6000, 'xyz', 'https://instagram.com/p/xyz', '2026-09-02T11:00:00Z'
            );
            """
        )

    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.user_email = request.headers.get("x-user", "dev@example.com")
        request.state.is_dev = request.headers.get("x-role") == "dev"
        request.state.queue_role_preview_active = request.headers.get("x-preview") == "1"
        return await call_next(request)

    app.include_router(hooks_lab.router)
    test_client = TestClient(app)
    test_client.headers["x-role"] = "dev"
    return test_client, connection


def test_every_post_yields_caption_and_clean_ocr_without_mutating_sources(client):
    test_client, connection = client
    payload = test_client.get("/api/dashboard/hooks?q=prompts&mode=words").json()
    assert payload["status"]["total"] == 4
    assert payload["status"]["captions"] == 2
    assert payload["status"]["ocr"] == 2
    assert {item["source_kind"] for item in payload["results"]} == {"caption", "ocr"}
    assert any(item["hook_text"] == "These 5 ChatGPT prompts save me hours every week." for item in payload["results"])
    assert any(item["hook_text"] == "CHATGPT PROMPTS" for item in payload["results"])
    with connection() as conn:
        source = conn.execute("SELECT caption, hook_text FROM posts WHERE id = 1").fetchone()
        assert source["caption"].startswith("Follow for more.")
        assert source["hook_text"].startswith("INSTAGRAM")


@pytest.mark.parametrize("headers", [{"x-role": ""}, {"x-role": "admin"}, {"x-role": "dev", "x-preview": "1"}])
def test_only_full_dev_access(client, headers):
    test_client, _ = client
    assert test_client.get("/api/dashboard/hooks", headers=headers).status_code == 403


def test_future_posts_are_added_on_next_read(client):
    test_client, connection = client
    assert test_client.get("/api/dashboard/hooks").json()["status"]["total"] == 4
    with connection() as conn:
        conn.execute(
            """INSERT INTO dashboard_posts VALUES (
                3, 'future', 'A future hook arrives automatically. More copy.', '',
                '2026-10-01T10:00:00Z', 9000, 'new', 'https://instagram.com/p/new', '2026-10-01T11:00:00Z'
            )"""
        )
    payload = test_client.get("/api/dashboard/hooks?q=future&mode=words").json()
    assert payload["status"]["total"] == 5
    assert payload["results"][0]["hook_text"] == "A future hook arrives automatically."


def test_context_search_falls_back_with_warning(client, monkeypatch):
    test_client, _ = client

    def unavailable(*args, **kwargs):
        raise JevFeatureUnavailable("No local key.")

    monkeypatch.setattr(hooks_lab, "_jev_rerank", unavailable)
    payload = test_client.get("/api/dashboard/hooks?q=prompts&mode=context").json()
    assert payload["results"]
    assert "keyword" in payload["warning"].lower()


def test_default_search_combines_words_and_jev_context(client, monkeypatch):
    test_client, _ = client
    seen = {}

    def rerank(query, candidates):
        seen["query"] = query
        seen["word_scores"] = [item["wordScore"] for item in candidates]
        return {item["id"]: 0.75 for item in candidates}

    monkeypatch.setattr(hooks_lab, "_jev_rerank", rerank)
    payload = test_client.get("/api/dashboard/hooks?q=prompts").json()

    assert payload["mode"] == "hybrid"
    assert seen["query"] == "prompts"
    assert any(score > 0 for score in seen["word_scores"])
    assert payload["results"]
    assert all(item["contextScore"] == 0.75 for item in payload["results"])
    assert all(item["rankScore"] > 0 for item in payload["results"])


def test_hybrid_search_keeps_literal_matches_ahead_of_semantic_expansion(monkeypatch):
    exact = {
        "id": "exact", "hook_text": "One prompt changed everything", "context_text": "One prompt changed everything",
        "primary_topic": "ai_tools", "categories": [], "likes": 10, "saved": False,
    }
    semantic_only = {
        "id": "semantic", "hook_text": "The most popular post ever", "context_text": "A broad AI story",
        "primary_topic": "ai_tools", "categories": [], "likes": 10_000_000, "saved": False,
    }
    monkeypatch.setattr(hooks_lab, "_all_hooks", lambda owner: [exact, semantic_only])
    monkeypatch.setattr(hooks_lab, "_jev_rerank", lambda query, candidates: {"exact": 0.1, "semantic": 1.0})

    results, warning = hooks_lab.search_hooks("prompt", "hybrid", "dev@example.com", 10)

    assert warning is None
    assert results[0]["id"] == "exact"
    assert results[0]["wordScore"] > 0
    assert results[1]["wordScore"] == 0


def test_local_bridge_verifies_dev_and_indexes_live_catalogue(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(hooks_lab, "_REMOTE_SOURCE_BASE", "https://cortex.test")
    calls = []

    class RemoteResponse:
        def __init__(self, status_code, body=None, headers=None):
            self.status_code = status_code
            self._body = body or {}
            self.headers = headers or {}
            self.content = b"catalogue"

        @property
        def is_success(self):
            return 200 <= self.status_code < 300

        def json(self):
            return self._body

    def remote_get(url, headers=None, timeout=None):
        calls.append((url, dict(headers or {})))
        if url.endswith("/api/dashboard/me"):
            return RemoteResponse(200, {"email": "user03@example.com", "is_dev": True})
        if (headers or {}).get("If-None-Match") == '"live-v1"':
            return RemoteResponse(304)
        return RemoteResponse(
            200,
            {
                "posts": [
                    {
                        "rank": 99,
                        "account": "liveaccount",
                        "shortcode": "live123",
                        "permalink": "https://instagram.com/p/live123",
                        "postDate": "2026-09-30T10:00:00Z",
                        "likes": 54321,
                        "caption": "This live caption came from Cortex. More context follows.",
                        "ocrText": "LIVE OCR HOOK",
                    }
                ]
            },
            {"ETag": '"live-v1"'},
        )

    monkeypatch.setattr("httpx.get", remote_get)
    headers = {"x-role": "admin", "authorization": "Bearer real-token"}
    first = test_client.get("/api/dashboard/hooks", headers=headers).json()
    second = test_client.get("/api/dashboard/hooks", headers=headers).json()

    assert first["status"]["total"] == 2
    assert {item["source_kind"] for item in first["results"]} == {"caption", "ocr"}
    assert first["sync"]["remote"] == 1
    assert second["sync"]["not_modified"] == 1
    assert any(url.endswith("/api/dashboard/me") for url, _ in calls)
    assert any(headers.get("If-None-Match") == '"live-v1"' for _, headers in calls)


def test_jev_categorization_is_multilabel(client, monkeypatch):
    test_client, connection = client
    test_client.get("/api/dashboard/hooks")

    def answer(state, questions):
        output = {}
        for key in questions:
            if key.startswith("topic_"):
                output[key] = {"choice": "ai_tools", "confidence": 0.9}
            else:
                output[key] = {"noul": 0.8 if key.startswith(("curiosity_", "list_number_", "how_to_")) else 0.2}
        return output

    monkeypatch.setattr(hooks_lab, "ask_jev", answer)
    result = test_client.post("/api/dashboard/hooks/categorize?limit=4").json()
    assert result["processed"] == 4
    with connection() as conn:
        row = conn.execute("SELECT primary_topic, categories_json FROM hook_sources LIMIT 1").fetchone()
        assert row["primary_topic"] == "ai_tools"
        assert set(__import__("json").loads(row["categories_json"])) == {"curiosity", "list_number", "how_to"}


def test_private_saves_and_drafts(client):
    test_client, _ = client
    hook_id = test_client.get("/api/dashboard/hooks?q=prompts").json()["results"][0]["id"]
    assert test_client.post(f"/api/dashboard/hooks/{hook_id}/save", json={"saved": True}).json()["saved"]
    created = test_client.post(
        "/api/dashboard/hooks/drafts",
        json={"topic": "prompts", "text": "My editable hook", "source_hook_ids": [hook_id]},
    ).json()
    assert created["text"] == "My editable hook"
    assert len(test_client.get("/api/dashboard/hooks/drafts").json()["drafts"]) == 1
    assert test_client.get("/api/dashboard/hooks/drafts", headers={"x-role": "dev", "x-user": "other@example.com"}).json()["drafts"] == []
    updated = test_client.patch(
        f"/api/dashboard/hooks/drafts/{created['id']}", json={"text": "Edited manually"}
    ).json()
    assert updated["text"] == "Edited manually"


def test_generation_uses_selected_sources(client, monkeypatch):
    test_client, _ = client
    hook_id = test_client.get("/api/dashboard/hooks?q=prompts").json()["results"][0]["id"]
    captured = {}

    def generate(item, sources):
        captured["sources"] = sources
        return [f"Hook {index}" for index in range(1, 7)], "test-model"

    monkeypatch.setattr(hooks_lab, "_openai_hook_variants", generate)
    payload = test_client.post(
        "/api/dashboard/hooks/generate",
        json={"topic": "prompts", "source_hook_ids": [hook_id], "count": 6},
    ).json()
    assert len(payload["hooks"]) == 6
    assert captured["sources"][0]["id"] == hook_id
