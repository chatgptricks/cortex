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


def _hook(hook_id, hook, likes, context=None):
    return {
        "id": hook_id, "hook_text": hook, "context_text": context or hook,
        "primary_topic": "ai_tools", "categories": [], "likes": likes, "saved": False,
    }


def _search(monkeypatch, rows, query, *, jev=None, limit=10, mode="hybrid"):
    calls = []
    monkeypatch.setattr(hooks_lab, "_all_hooks", lambda owner, terms=None: [dict(row) for row in rows])
    monkeypatch.setattr(hooks_lab, "_top_liked_hooks", lambda owner, limit, exclude=None: [
        dict(row) for row in sorted(rows, key=lambda r: -r["likes"]) if row["id"] not in (exclude or set())
    ][:limit])

    def rerank(q, candidates):
        calls.append([item["id"] for item in candidates])
        return {item["id"]: (jev or {}).get(item["id"], 0.0) for item in candidates}

    monkeypatch.setattr(hooks_lab, "_jev_rerank", rerank)
    results, warning = hooks_lab.search_hooks(query, mode, "dev@example.com", limit)
    return results, warning, calls


def test_exact_phrase_beats_partial_matches_with_far_more_likes(monkeypatch):
    rows = [
        _hook("viral-partial", "The best free tools nobody uses", 9_000_000, "Tools you need. Prompts inside."),
        _hook("exact", "These ChatGPT prompts save hours", 120),
        _hook("both-words", "Prompts that make ChatGPT better", 3_000),
    ]
    results, _, _ = _search(monkeypatch, rows, "chatgpt prompts")
    assert [item["id"] for item in results] == ["exact", "both-words", "viral-partial"]
    assert [item["matchType"] for item in results] == ["exact", "exact", "partial"]


def test_likes_only_break_ties_between_equally_exact_hooks(monkeypatch):
    rows = [_hook("low", "Claude just changed everything", 10), _hook("high", "Claude just changed my work", 50_000)]
    results, _, _ = _search(monkeypatch, rows, "claude")
    assert [item["id"] for item in results] == ["high", "low"]


def test_jev_never_reorders_or_displaces_exact_matches(monkeypatch):
    rows = [
        _hook("exact", "One prompt changed everything", 10),
        _hook("semantic", "The most popular post ever", 10_000_000),
        _hook("weak", "Unrelated cooking video", 5_000_000),
    ]
    results, warning, calls = _search(monkeypatch, rows, "prompt", jev={"semantic": 0.9, "weak": 0.2})
    assert warning is None
    assert [item["id"] for item in results] == ["exact", "semantic"]
    assert results[1]["matchType"] == "related"
    assert calls == [["semantic", "weak"]]


def test_jev_is_skipped_when_exact_matches_fill_the_page(monkeypatch):
    rows = [_hook(f"p{i}", f"Prompt number {i}", i) for i in range(5)] + [_hook("viral", "Something else", 10**8)]
    results, _, calls = _search(monkeypatch, rows, "prompt", limit=5)
    assert all(item["id"].startswith("p") for item in results)
    assert calls == []


def test_short_and_partial_words_do_not_create_false_matches(monkeypatch):
    rows = [
        _hook("ai", "AI tools that feel illegal", 1),
        _hook("said", "He said nothing about it", 10**6),
        _hook("pro", "Go pro in one week", 10**6),
        _hook("automation", "Automation saved my team", 5),
    ]
    assert [r["id"] for r in _search(monkeypatch, rows, "ai")[0]] == ["ai"]
    assert [r["id"] for r in _search(monkeypatch, rows, "prompts", mode="words")[0]] == []
    assert [r["id"] for r in _search(monkeypatch, rows, "automat", mode="words")[0]] == ["automation"]


def test_sql_prefilter_matches_folded_words(client):
    test_client, _ = client
    test_client.get("/api/dashboard/hooks")
    rows = hooks_lab._all_hooks("dev@example.com", ["prompts"])
    assert rows and all(row["search_text"] and " prompt" in row["search_text"] for row in rows)
    assert hooks_lab._all_hooks("dev@example.com", ["nonexistentword"]) == []


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


def test_repeat_reads_only_index_changed_posts(client):
    # Production runs this against Postgres: re-writing every hook on every
    # search made each Hooks request take minutes.
    test_client, connection = client
    test_client.get("/api/dashboard/hooks")
    assert hooks_lab.sync_sources() == {"scanned": 0, "inserted": 0, "updated": 0, "busy": 0}
    with connection() as conn:
        conn.execute("UPDATE dashboard_posts SET likes = 7000, updated_at = '2026-09-03T00:00:00Z' WHERE id = 2")
    assert hooks_lab.sync_sources()["scanned"] == 1
    with connection() as conn:
        likes = {row["likes"] for row in conn.execute("SELECT likes FROM hook_sources WHERE source_id = 2")}
    assert likes == {7000}


def test_large_first_build_runs_in_background(client, monkeypatch):
    test_client, _connection = client
    started = []
    monkeypatch.setattr(hooks_lab, "_BACKGROUND_BUILD_THRESHOLD", 1)
    monkeypatch.setattr(hooks_lab.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: started.append(kw["name"])})())
    try:
        assert hooks_lab.sync_sources()["building"] == 1
        assert started == ["hooks-index-build"]
    finally:
        hooks_lab._SOURCE_SYNC_LOCK.release()


def test_the_same_post_appears_once(monkeypatch):
    canonical = {**_hook("posts:1:caption", "These ChatGPT prompts help", 105_157), "shortcode": "ABC", "source_kind": "caption"}
    mirrored = {**_hook("dashboard_posts:9:caption", "These ChatGPT prompts help", 104_440), "shortcode": "ABC", "source_kind": "caption"}
    collab = {**_hook("dashboard_posts:7:caption", "These ChatGPT prompts help", 104_421), "shortcode": "ABC", "source_kind": "caption"}
    other = {**_hook("dashboard_posts:5:caption", "More ChatGPT prompts", 10), "shortcode": "XYZ", "source_kind": "caption"}
    results, _, _ = _search(monkeypatch, [mirrored, collab, canonical, other], "chatgpt prompts", mode="words")
    assert [item["id"] for item in results] == ["posts:1:caption", "dashboard_posts:5:caption"]


def test_account_mentions_are_not_search_words(monkeypatch):
    rows = [
        _hook("mention", "Made with the new @luma_ai model. Follow @excel_india", 10**7),
        _hook("word", "AI can now build Excel sheets", 5),
    ]
    assert [r["id"] for r in _search(monkeypatch, rows, "ai", mode="words")[0]] == ["word"]
    assert [r["id"] for r in _search(monkeypatch, rows, "excel", mode="words")[0]] == ["word"]
