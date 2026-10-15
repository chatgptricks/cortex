from __future__ import annotations

import json
import io
import sqlite3
from contextlib import contextmanager

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import db, external_api as api, external_workspace as workspace


@pytest.fixture
def setup(tmp_path, monkeypatch):
    database = tmp_path / "workspace.sqlite3"
    statements = []

    def create(conn, table, fields, extras=()):
        fields = tuple(dict.fromkeys((*fields, *extras)))
        conn.execute(f"CREATE TABLE {table} (" + ",".join(f"{field} {'INTEGER' if field in {'id', 'request_id'} else 'TEXT'}" for field in fields) + ")")

    def insert(conn, table, **values):
        fields = tuple(values)
        conn.execute(f"INSERT INTO {table} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", tuple(values.values()))

    with sqlite3.connect(database) as conn:
        create(conn, "queue_requests", workspace.QUEUE_FIELDS, ("slack_channel_id", "attachments"))
        for identity, account, stamp in ((1, "alpha", "2026-10-10"), (2, "beta", "2026-10-15"), (3, "alpha", "2026-10-11")):
            insert(conn, "queue_requests", id=identity, post_account=account, post_shortcode=f"Post{identity}", post_title=f"{account} work",
                   status="scheduled", post_permalink="https://instagram.com/p/Example/?token=SECRET", priority="medium", tags='["research"]',
                   recommended_accounts='["alpha","beta"]', reference_links='["https://example.com/source","/var/private/SECRET"]',
                   final_permalinks='[{"account":"alpha","url":"https://instagram.com/p/FinalA/"},{"account":"beta","url":"https://instagram.com/p/SECRET_BETA/"}]',
                   scheduled_date="2026-10-15", updated_at=stamp, slack_channel_id="SECRET_SLACK", attachments='[{"path":"SECRET_PATH"}]')
        # '_' is a valid handle but must never act as a LIKE wildcard.
        for identity, destination in ((4, "alpha_test"), (5, "alphaxTest")):
            insert(conn, "queue_requests", id=identity, post_account="", recommended_accounts=json.dumps([destination.lower()]), updated_at="2026-10-16")
        create(conn, "queue_request_events", workspace.EVENT_FIELDS)
        for request_id in (1, 2):
            insert(conn, "queue_request_events", id=request_id, request_id=request_id, event_type="scheduled", actor_email="ana@example.com", created_at="2026-10-15",
                   details=json.dumps({"status": "scheduled", "sourceUrl": "SECRET_URL", "slack_channel": "SECRET_SLACK", "nested": {"secret": "SECRET_NESTED"}}))
        create(conn, "post_assignments", workspace.ASSIGNMENT_FIELDS)
        create(conn, "post_assignment_events", ("id", "assignment_id", "actor_email", "event_type", "details", "created_at"))
        for identity, account in ((1, "alpha"), (2, "beta")):
            insert(conn, "post_assignments", id=identity, post_account=account, post_shortcode="Legacy", assignee_email="ana@example.com", status="queue", tags='["research"]',
                   note="Stored legacy note", recommended_account="beta", priority="high", updated_at="2026-10-15")
            insert(conn, "post_assignment_events", id=identity, assignment_id=identity, event_type="note_changed", details='{"note":"Kept note","secret":"SECRET_EVENT"}', created_at="2026-10-15")
        create(conn, "queue_designer_accounts", ("designer_email", "account_handle"))
        for handle in ("alpha", "beta"):
            insert(conn, "queue_designer_accounts", designer_email="ana@example.com", account_handle=handle)
        create(conn, "queue_schedule_drafts", workspace.DRAFT_FIELDS)
        for request_id in (1, 2):
            insert(conn, "queue_schedule_drafts", request_id=request_id, designer_email="designer@example.com", recommended_accounts='["alpha","beta"]', updated_at="2026-10-15")
        create(conn, "queue_tickets", workspace.TICKET_FIELDS, ("suggestion_payload",))
        for identity, request_id, owner in ((1, 1, "other@example.com"), (2, 2, "ana@example.com"), (3, None, "ana@example.com"), (4, None, "other@example.com")):
            insert(conn, "queue_tickets", id=identity, request_id=request_id, requester_email=owner, status="pending", requested_accounts='["alpha","beta"]', updated_at="2026-10-15", suggestion_payload='{"secret":"SECRET_SUGGESTION"}')
        for identity, state, category in ((5, "pending", "meeting"), (6, "approved", "break"), (7, "rejected", "focus"), (8, "pending", "move")):
            insert(conn, "queue_tickets", id=identity, ticket_type="time_block", request_id=None, requester_email="other@example.com", status=state, block_category=category,
                   title=f"Team {category}", reason="Visible coordinator reason", scheduled_date="2026-10-15", scheduled_start_minutes=600, duration_minutes=30, updated_at="2026-10-15")
        create(conn, "promo_opportunities", workspace.PROMO_FIELDS)
        for account in ("alpha", "beta"):
            insert(conn, "promo_opportunities", account=account, shortcode="Same", classification="disclosed", client="Example", review_status="reviewed", first_detected_at="2026-10-10", last_analyzed_at="2026-10-15",
                   analysis_json=json.dumps({"signals": ["paid partnership"], "evidence": [{"family": "metadata", "text": "ad", "secret": "SECRET_ANALYSIS"}], "secret": "SECRET_ANALYSIS"}),
                   review_override_json=json.dumps({"reviewer": "ana@example.com", "classification": "likely", "secret": "SECRET_OVERRIDE", "jev_review": {"semanticPromo": .8, "secret": "SECRET_JEV"}}))
        create(conn, "news_stories", workspace.NEWS_FIELDS, ("error",))
        insert(conn, "news_stories", id="https://news.example/story", item_json=json.dumps({"title": "Useful news", "description": "Evidence", "link": "https://news.example/story?token=SECRET", "sourceType": "news", "secret": "SECRET_RAW_NEWS"}),
               review_json=json.dumps({"score": .9, "targetAccount": "beta", "secret": "SECRET_REVIEW", "dimensions": {"insight": {"score": .9, "confidence": .8, "secret": "SECRET"}, "SECRET": {"score": 1}}}),
               saved=1, brief="Stored editorial brief", updated_at="2026-10-15", error="SECRET_ERROR")
        create(conn, "hook_sources", workspace.HOOK_FIELDS, ("raw_text", "content_hash", "source_table"))
        for identity, account, kind, likes in (("a", "alpha", "ocr", 100), ("b", "beta", "ocr", 999), ("caption", "alpha", "caption", 1000)):
            insert(conn, "hook_sources", id=identity, account=account, source_kind=kind, likes=likes, hook_text=f"{identity} hook", context_text="OCR text", categories_json='["question"]',
                   category_scores_json='{"question":0.9,"secret":"SECRET_CATEGORY"}', permalink="https://instagram.com/p/Hook/", raw_text="SECRET_RAW_HOOK", content_hash="SECRET_HASH", source_table="posts")
        create(conn, "hook_saves", ("owner_email", "hook_id", "created_at"))
        insert(conn, "hook_saves", owner_email="ana@example.com", hook_id="a")
        insert(conn, "hook_saves", owner_email="other@example.com", hook_id="b")
        create(conn, "hook_drafts", (*workspace.HOOK_DRAFT_FIELDS, "owner_email", "generation_context"))
        for identity, owner in (("own", "ana@example.com"), ("other", "other@example.com")):
            insert(conn, "hook_drafts", id=identity, owner_email=owner, text=f"{identity} text", source_hook_ids='["a","b","caption"]', updated_at="2026-10-15", generation_context='{"secret":"SECRET_CONTEXT"}')
        create(conn, "vault_links", workspace.VAULT_FIELDS, ("slack_url",))
        insert(conn, "vault_links", id="v", url="https://example.com/story", title="Vault source", tweet_text="Already stored text", text_status="ready", priority="1", discarded="0", done="0", shared_at="2026-10-15", pool_request_id=2, slack_url="https://sentient.slack.com/archives/C1/p123?thread_ts=123&token=SECRET")
        create(conn, "accounts", workspace.ACCOUNT_FIELDS, ("avatar_path",))
        for handle in ("alpha", "beta"):
            insert(conn, "accounts", handle=handle, label=handle.title(), is_active="1", avatar_path="SECRET_STORAGE_PATH")
        create(conn, "dashboard_users", workspace.USER_FIELDS, ("slack_user_id", "api_key", "password_hash"))
        insert(conn, "dashboard_users", email="ana@example.com", display_name="Ana", role="admin", is_admin="1", operating_roles='["vc"]', slack_user_id="SECRET_SLACK_ID", api_key="SECRET_API_KEY", password_hash="SECRET_HASH")
        create(conn, "usage_log", ("email", "path", "method", "ts"))
        insert(conn, "usage_log", email="ana@example.com", path="/api/dashboard/SECRET_PRIVATE", method="GET", ts="2099-10-15")
        create(conn, "account_lists", workspace.LIST_FIELDS)
        for identity, name, owner, shared, handles in ((1, "A excluded account", "ana@example.com", 0, ["beta"]), (2, "B own mixed", "ana@example.com", 0, ["alpha", "beta"]),
                                                      (3, "C shared mixed", "other@example.com", 1, ["beta", "alpha"]), (4, "D private other", "other@example.com", 0, ["alpha"]),
                                                      (5, "E malformed", "ana@example.com", 0, [])):
            insert(conn, "account_lists", id=identity, name=name, owner_email=owner, is_shared=shared, handles=json.dumps(handles), updated_at="2026-10-15")
        create(conn, "post_golden_nuggets", workspace.GOLDEN_FIELDS)
        for account, target, stamp, label in (("alpha", "beta", "2026-10-14", "golden_nugget"), ("beta", "beta", "2026-10-15", "golden_nugget"), ("alpha", "alpha", "2026-10-15", "potential")):
            insert(conn, "post_golden_nuggets", account=account, shortcode=label, label=label, target_account=target, score=.9, reviewed_at=stamp,
                   review=json.dumps({"label": label, "score": .9, "targetAccount": target, "secret": "SECRET_GOLDEN", "dimensions": {"insight": {"score": .9}}}))
        create(conn, "queue_scheduler_preferences", ("viewer_email", "hidden_users", "row_order", "updated_at"))
        create(conn, "user_preferences", ("email", "preferences", "updated_at"))
        for owner in ("ana@example.com", "other@example.com"):
            insert(conn, "queue_scheduler_preferences", viewer_email=owner, hidden_users='["designer@example.com"]', row_order='["ana@example.com","designer@example.com"]', updated_at="2026-10-15")
            insert(conn, "user_preferences", email=owner, preferences=json.dumps({"language": "es" if owner == "ana@example.com" else "en", "theme": "dark", "accent": "green",
                   "trackerFavorites": ["alpha", "beta"], "queueGuideCompleted": True, "queueDesignerScope": "all", "secret": "SECRET_PREFERENCE"}), updated_at="2026-10-15")

    @contextmanager
    def connect():
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        conn.set_trace_callback(statements.append)
        # Reads must work even when the database refuses every mutation.
        write_actions = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_CREATE_TABLE,
                         sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_DROP_INDEX}
        conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in write_actions else sqlite3.SQLITE_OK)
        try:
            yield conn
        finally:
            conn.close()

    def require_scope(request, scope):
        identity = getattr(request.state, "website_api_key", None)
        if identity is None:
            raise HTTPException(401, "API key required")
        if scope not in identity["data_scopes"] or request.headers.get("x-owner-disabled"):
            raise HTTPException(403, "Scope unavailable")
        return identity

    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(api, "require_scope", require_scope, raising=False)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        if request.headers.get("x-scopes"):
            request.state.website_api_key = {"owner_email": "ana@example.com", "data_scopes": request.headers["x-scopes"].split(","), "account_handles": request.headers.get("x-accounts", "alpha").split(",")}
        return await call_next(request)

    app.include_router(workspace.router)
    return TestClient(app), statements, database


HEADERS = {"x-scopes": "queue,promos,news,hooks,vault,settings,research"}


@pytest.mark.parametrize("path,scope", [("/queue/requests", "queue"), ("/promos", "promos"), ("/news", "news"), ("/hooks", "hooks"), ("/vault", "vault"), ("/settings/users", "settings"), ("/research/lists", "research")])
def test_area_grants_and_current_owner_permission_are_required(setup, path, scope):
    client, _, _ = setup
    assert client.get("/api/v1" + path).status_code == 401
    assert client.get("/api/v1" + path, headers={"x-scopes": "unrelated"}).status_code == 403
    assert client.get("/api/v1" + path, headers={"x-scopes": scope, "x-owner-disabled": "1"}).status_code == 403
    assert client.get("/api/v1" + path, headers={"x-scopes": scope}).status_code == 200


def test_account_filters_run_before_pagination_and_follow_related_resources(setup):
    client, statements, _ = setup
    data = client.get("/api/v1/queue/requests?limit=1", headers=HEADERS).json()
    assert [row["id"] for row in data["data"]] == [3]
    assert data["pagination"] == {"limit": 1, "offset": 0, "count": 1, "has_more": True, "next_offset": 1}
    assert client.get("/api/v1/queue/requests?limit=1&offset=1", headers=HEADERS).json()["data"][0]["id"] == 1
    assert client.get("/api/v1/queue/requests/2", headers=HEADERS).status_code == 404
    assert client.get("/api/v1/queue/requests/2/history", headers=HEADERS).status_code == 404
    assert [row["request_id"] for row in client.get("/api/v1/queue/drafts", headers=HEADERS).json()["data"]] == [1]
    assert {row["id"] for row in client.get("/api/v1/queue/tickets", headers=HEADERS).json()["data"]} == {1, 3}
    assert client.get("/api/v1/queue/tickets/2", headers=HEADERS).status_code == 404
    assert client.get("/api/v1/queue/tickets/4", headers=HEADERS).status_code == 404
    assert client.get("/api/v1/queue/requests?account=beta", headers=HEADERS).status_code == 404
    assert any("r.post_account IN ('alpha')" in query and "LIMIT 2 OFFSET 0" in query for query in statements)
    wildcard = client.get("/api/v1/queue/requests", headers={**HEADERS, "x-accounts": "alpha_test"}).json()["data"]
    assert [row["id"] for row in wildcard] == [4]


def test_hooks_stay_ocr_only_account_scoped_and_owner_private(setup):
    client, _, _ = setup
    for path in ("/hooks", "/hooks/saved"):
        response = client.get("/api/v1" + path + "?limit=1", headers=HEADERS)
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()["data"]] == ["a"]
    for identity in ("b", "caption"):
        assert client.get(f"/api/v1/hooks/{identity}", headers=HEADERS).status_code == 404
    drafts = client.get("/api/v1/hooks/drafts", headers=HEADERS).json()["data"]
    assert [row["id"] for row in drafts] == ["own"]
    assert drafts[0]["source_hook_ids"] == ["a"]
    assert client.get("/api/v1/hooks/drafts/other", headers=HEADERS).status_code == 404


def test_safe_projections_and_read_only_domain_contracts(setup):
    client, statements, _ = setup
    paths = ["/queue/requests", "/queue/requests/1", "/queue/requests/1/history", "/queue/drafts", "/queue/tickets", "/queue/tickets/1", "/queue/schedule?date=2026-10-15",
             "/promos", "/promos/alpha/Same", "/promos/alpha/Same/review", "/news", "/news/briefs", "/news/story?id=https%3A%2F%2Fnews.example%2Fstory",
             "/hooks", "/hooks/a", "/hooks/saved", "/hooks/drafts", "/hooks/drafts/own", "/vault", "/vault/v", "/settings/accounts", "/settings/users", "/settings/users/ana@example.com",
             "/queue/requests/1/attachments", "/queue/drafts/1", "/queue/summary", "/queue/report", "/queue/team", "/queue/assignments", "/queue/assignments/1", "/queue/assignments/1/history", "/settings/usage",
             "/research/lists", "/research/golden-nuggets", "/queue/time-blocks", "/queue/preferences", "/settings/preferences"]
    for path in paths:
        response = client.get("/api/v1" + path, headers=HEADERS)
        assert response.status_code == 200, (path, response.text)
        assert "SECRET" not in response.text, (path, response.text)
        assert response.headers["cache-control"] == "private, no-store"
        assert response.json()["schema_version"] == api.VERSION
    request = client.get("/api/v1/queue/requests/1", headers=HEADERS).json()["data"]
    assert request["recommended_accounts"] == ["alpha"]
    assert request["priority"] == "normal"
    assert len(request["final_permalinks"]) == 1
    news = client.get("/api/v1/news", headers=HEADERS).json()["data"][0]
    assert news["review"]["targetAccount"] is None
    assert news["review"]["dimensions"]["insight"] == {"score": .9, "confidence": .8}
    assert client.get("/api/v1/promos/beta/Same", headers=HEADERS).status_code == 404
    assert client.get("/api/v1/settings/accounts", headers=HEADERS).json()["data"][0]["handle"] == "alpha"
    assert client.get("/api/v1/queue/summary", headers=HEADERS).json()["data"]["total_requests"] == 2
    assert client.get("/api/v1/settings/usage", headers=HEADERS).json()["data"][0]["request_count"] == 1
    assert client.get("/api/v1/queue/team", headers=HEADERS).json()["data"][0]["managed_accounts"] == ["alpha"]
    assert client.get("/api/v1/queue/assignments", headers=HEADERS).json()["data"][0]["recommended_account"] is None
    for path in ("/queue/drafts/2", "/queue/requests/2/attachments", "/queue/assignments/2", "/queue/assignments/2/history"):
        assert client.get("/api/v1" + path, headers=HEADERS).status_code == 404
    assert not any(query.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP")) for query in statements)
    assert all(route.methods == {"GET"} for route in workspace.router.routes)


def test_search_uses_public_fields_and_pagination_is_bounded(setup):
    client, _, _ = setup
    assert client.get("/api/v1/news?q=SECRET_RAW_NEWS", headers=HEADERS).json()["data"] == []
    assert len(client.get("/api/v1/news?q=useful&source_type=news&saved=true", headers=HEADERS).json()["data"]) == 1
    assert client.get("/api/v1/news?source_type=x", headers=HEADERS).json()["data"] == []
    for query in ("limit=0", "limit=101", "offset=-1", "offset=100001"):
        assert client.get("/api/v1/queue/requests?" + query, headers=HEADERS).status_code == 422


def test_absent_optional_workspaces_are_empty_without_schema_writes(setup):
    client, statements, database = setup
    with sqlite3.connect(database) as conn:
        for table in ("hook_sources", "hook_saves", "hook_drafts", "vault_links", "news_stories", "promo_opportunities"):
            conn.execute(f"DROP TABLE {table}")
    for path in ("/hooks", "/hooks/saved", "/hooks/drafts", "/vault", "/news", "/promos"):
        response = client.get("/api/v1" + path, headers=HEADERS)
        assert response.status_code == 200, response.text
        assert response.json()["data"] == []
    assert not any("CREATE" in query.upper() for query in statements)


def test_attachment_bytes_require_request_scope_and_use_read_only_owned_storage(setup, monkeypatch):
    client, _, database = setup
    attachment_id = "aabbccddeeff0011"
    reference = f"r2://uploads/queue-1-{attachment_id}.pdf"
    attachment = {"id": attachment_id, "name": "../../Report\r\nInjected: secret.pdf", "size": 8,
                  "contentType": "application/pdf", "mediaRef": reference, "path": "SECRET_PATH"}
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE queue_requests SET attachments = ? WHERE id IN (1,2)", (json.dumps([attachment]),))
    calls = []
    bodies = []
    class Storage:
        def get_object(self, **kwargs):
            calls.append(kwargs)
            body = io.BytesIO(b"%PDF-1.7")
            bodies.append(body)
            return {"Body": body, "ContentType": "application/pdf"}
    monkeypatch.setattr(workspace.media_storage, "r2_enabled", lambda: True)
    monkeypatch.setattr(workspace.media_storage, "_client", lambda: Storage())
    monkeypatch.setattr(workspace.media_storage, "redirect_url", lambda *args, **kwargs: pytest.fail("Must not invoke metadata-writing redirects"))
    path = f"/api/v1/queue/requests/1/attachments/{attachment_id}/content"
    metadata = client.get("/api/v1/queue/requests/1/attachments", headers=HEADERS).json()["data"][0]
    assert metadata["download_url"] == path
    assert "mediaRef" not in metadata and "SECRET_PATH" not in json.dumps(metadata)
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"x-scopes": "news"}).status_code == 403
    assert client.get(path.replace("/requests/1/", "/requests/2/"), headers=HEADERS).status_code == 404
    assert calls == []
    response = client.get(path, headers=HEADERS)
    assert response.status_code == 200
    assert response.content == b"%PDF-1.7"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment; filename*=UTF-8''Report%0D%0A")
    assert "\r" not in response.headers["content-disposition"]
    assert bodies[0].closed
    assert calls == [{"Bucket": workspace.media_storage.R2_BUCKET, "Key": f"uploads/queue-1-{attachment_id}.pdf"}]
    assert client.get(path.replace(attachment_id, "invalid"), headers=HEADERS).status_code == 404
    assert client.get(path.replace(attachment_id, "0000000000000000"), headers=HEADERS).status_code == 404
    monkeypatch.setattr(workspace.media_storage, "r2_enabled", lambda: False)
    assert client.get(path, headers=HEADERS).status_code == 404


def test_links_preserve_resource_queries_and_remove_credential_queries():
    assert workspace._url("https://drive.google.com/open?id=abc&resourcekey=public") == "https://drive.google.com/open?id=abc&resourcekey=public"
    assert workspace._url("https://youtube.com/watch?v=abc&list=xyz") == "https://youtube.com/watch?v=abc&list=xyz"
    assert workspace._url("https://example.com/news?id=story&access_token=SECRET&X-Amz-Signature=SECRET#token=SECRET") == "https://example.com/news?id=story"
    assert workspace._url("https://user:password@example.com/") is None
    assert workspace._url("/var/private/secret") is None


def test_research_lists_enforce_owner_and_account_membership_before_pagination(setup):
    client, statements, _ = setup
    first = client.get("/api/v1/research/lists?limit=1", headers=HEADERS).json()
    assert first["data"][0]["id"] == 2
    assert first["data"][0]["handles"] == ["alpha"]
    assert first["pagination"]["has_more"]
    second = client.get("/api/v1/research/lists?limit=1&offset=1", headers=HEADERS).json()
    assert second["data"][0]["id"] == 3
    assert second["data"][0]["is_shared"] is True
    assert second["data"][0]["handles"] == ["alpha"]
    assert second["pagination"]["has_more"] is False
    assert client.get("/api/v1/research/lists?account=beta", headers=HEADERS).status_code == 404
    assert client.get("/api/v1/research/lists?q=private", headers=HEADERS).json()["data"] == []
    assert any("l.owner_email = 'ana@example.com'" in query and "json_each" in query and "LIMIT 2 OFFSET 0" in query for query in statements)


def test_research_golden_marks_keep_account_scope_and_sanitize_stored_reviews(setup):
    client, _, _ = setup
    response = client.get("/api/v1/research/golden-nuggets?limit=1&min_score=0.8", headers=HEADERS)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data) == 1 and data[0]["account"] == "alpha"
    assert data[0]["target_account"] is None
    assert data[0]["review"]["targetAccount"] is None
    assert data[0]["review"]["dimensions"]["insight"] == {"score": .9}
    assert "SECRET" not in response.text
    assert client.get("/api/v1/research/golden-nuggets?min_score=0.99", headers=HEADERS).json()["data"] == []
    assert client.get("/api/v1/research/golden-nuggets?target_account=beta", headers=HEADERS).status_code == 404


def test_team_availability_matches_coordinator_view_without_request_links(setup):
    client, _, _ = setup
    response = client.get("/api/v1/queue/time-blocks?date=2026-10-15&limit=1", headers=HEADERS).json()
    assert response["data"][0]["id"] == 5
    assert response["data"][0]["requester_email"] == "other@example.com"
    assert response["pagination"]["has_more"]
    assert "request_id" not in response["data"][0]
    assert {row["id"] for row in client.get("/api/v1/queue/time-blocks", headers=HEADERS).json()["data"]} == {5, 6}
    assert [row["id"] for row in client.get("/api/v1/queue/time-blocks?status=rejected", headers=HEADERS).json()["data"]] == [7]
    assert client.get("/api/v1/queue/time-blocks?requester=ana@example.com", headers=HEADERS).json()["data"] == []


def test_preferences_are_owner_only_and_account_favorites_are_filtered(setup):
    client, _, _ = setup
    queue = client.get("/api/v1/queue/preferences?viewer_email=other@example.com", headers=HEADERS).json()["data"]
    assert queue == {"hidden_users": ["designer@example.com"], "row_order": ["ana@example.com", "designer@example.com"], "updated_at": "2026-10-15"}
    response = client.get("/api/v1/settings/preferences?email=other@example.com", headers=HEADERS)
    preferences = response.json()["data"]["preferences"]
    assert preferences["language"] == "es"
    assert preferences["trackerFavorites"] == ["alpha"]
    assert preferences["queueDesignerScope"] == "all"
    assert "SECRET" not in response.text
    assert client.get("/api/v1/vault/v", headers=HEADERS).json()["data"]["slack_url"] == "https://sentient.slack.com/archives/C1/p123?thread_ts=123"


def test_real_queue_report_aggregates_scoped_work_and_pages_live_draft_overrides(setup):
    client, statements, database = setup
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE queue_requests SET designer_email='ana@example.com', production_points=3, priority='urgent', scheduled_start_minutes=600 WHERE id=1")
        conn.execute("UPDATE queue_requests SET designer_email='ana@example.com', production_points=999, priority='urgent' WHERE id=2")
        conn.execute("UPDATE queue_requests SET designer_email='ana@example.com', production_points=2, status='closed', actual_started_at='2026-10-15T09:00:00+00:00', completed_at='2026-10-15T10:30:00+00:00' WHERE id=3")
        for identity, completed in ((6, "2026-10-15T08:00:00+00:00"), (7, None)):
            conn.execute("INSERT INTO queue_requests (id,post_account,designer_email,status,production_points,priority,actual_started_at,completed_at,scheduled_date,scheduled_start_minutes) VALUES (?,'alpha','ana@example.com','closed',2,'medium','2026-10-15T09:00:00+00:00',?,'2026-10-14',540)", (identity, completed))
        conn.execute("INSERT INTO queue_requests (id,post_account,designer_email,status,production_points,priority,scheduled_date,scheduled_start_minutes) VALUES (8,'alpha','bob@example.com','scheduled',5,'urgent','2026-10-15',700)")
        for email in ("bob@example.com", "cara@example.com"):
            conn.execute("INSERT INTO dashboard_users (email,display_name,role,is_admin) VALUES (?,?,'viewer','0')", (email, email))
        conn.execute("UPDATE queue_schedule_drafts SET designer_email='bob@example.com',production_points=7,scheduled_date='2026-10-16',scheduled_start_minutes=800 WHERE request_id=1")
        conn.execute("INSERT INTO queue_schedule_drafts (request_id,designer_email,scheduled_date,scheduled_start_minutes,recommended_accounts) VALUES (8,'__queue_pool__','0000-00-00',0,'[\"alpha\"]')")
    response = client.get("/api/v1/queue/report?limit=1&assigned_limit=2", headers=HEADERS)
    assert response.status_code == 200, response.text
    report = response.json()["data"]
    assert report["totals"]["scheduled"] == {"count": 2, "points": 8}
    assert report["totals"]["closed"] == {"count": 3, "points": 6}
    assert report["priorities"] == {"normal": {"count": 3, "points": 6}, "urgent": {"count": 2, "points": 8}}
    assert report["designers"] == [{"email": "ana@example.com", "activeRequests": 1, "productionPoints": 3, "closedRequests": 3, "urgentRequests": 1, "averageActualMinutes": 45}]
    assert response.json()["pagination"]["designers"]["has_more"] is True
    assert [row["id"] for row in report["assignedPosts"]] == [6, 7]
    assert response.json()["pagination"]["assigned_posts"]["next_offset"] == 2
    second = client.get("/api/v1/queue/report?offset=1&assigned_limit=2&assigned_offset=2", headers=HEADERS).json()
    assert second["data"]["designers"][0]["email"] == "bob@example.com"
    assert second["data"]["designers"][0]["productionPoints"] == 5
    assert second["data"]["designers"][1]["email"] == "cara@example.com"
    assert second["data"]["designers"][1]["activeRequests"] == 0
    assigned = second["data"]["assignedPosts"]
    assert [row["id"] for row in assigned] == [3, 1]
    assert assigned[1]["is_draft"] is True
    assert assigned[1]["production_points"] == 7
    assert assigned[1]["designer_email"] == "bob@example.com"
    assert assigned[1]["recommended_accounts"] == ["alpha"]
    assert assigned[1]["committed_status"] == "scheduled"
    last = client.get("/api/v1/queue/report?assigned_limit=2&assigned_offset=4", headers=HEADERS).json()
    assert last["pagination"]["assigned_posts"]["has_more"] is False
    assert [row["id"] for row in last["data"]["assignedPosts"]] == [8]
    assert last["data"]["assignedPosts"][0]["status"] == "pool"
    assert last["data"]["assignedPosts"][0]["designer_email"] is None
    assert any("WITH scoped AS" in query and "r.post_account IN ('alpha')" in query and "LIMIT 2 OFFSET 0" in query for query in statements)
    assert client.get("/api/v1/queue/report?account=beta", headers=HEADERS).status_code == 404
    assert client.get("/api/v1/queue/report?assigned_limit=101", headers=HEADERS).status_code == 422
    assert client.get("/api/v1/queue/report?assigned_offset=100001", headers=HEADERS).status_code == 422
