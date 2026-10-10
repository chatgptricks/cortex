from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import account_media_kit, agent_connections, db, external_api as api, main


class FixedNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 9, 18, 0, 15, tzinfo=UTC).astimezone(tz or UTC)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    database = tmp_path / "website-api.sqlite3"

    @contextmanager
    def connect():
        conn = sqlite3.connect(database, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    with connect() as conn:
        conn.executescript("""
            CREATE TABLE dashboard_users (email TEXT PRIMARY KEY, role TEXT, is_admin INTEGER,
                operating_role TEXT, operating_roles TEXT, time_zone TEXT);
            INSERT INTO dashboard_users VALUES ('ana@example.com', 'admin', 1, 'vc', '["vc","pd"]', 'America/Costa_Rica');
            INSERT INTO dashboard_users VALUES ('bob@example.com', 'viewer', 0, 'pd', '["pd"]', 'America/Costa_Rica');
            CREATE TABLE accounts (handle TEXT PRIMARY KEY, label TEXT, is_active INTEGER,
                is_canonical INTEGER, scrape_mode TEXT, group_name TEXT);
            INSERT INTO accounts VALUES ('alpha', 'CANARY_INTERNAL_LABEL', 1, 1, 'posts', 'sentient');
            INSERT INTO accounts VALUES ('beta', 'CANARY_OTHER_ACCOUNT', 1, 0, 'posts', 'sentient');
            INSERT INTO accounts VALUES ('inactive', 'CANARY_INACTIVE_ACCOUNT', 0, 0, 'posts', 'sentient');
            CREATE TABLE account_snapshots (handle TEXT, captured_at TEXT, followers_count INTEGER,
                full_name TEXT, biography TEXT, posts_count INTEGER, verified INTEGER, private INTEGER);
            INSERT INTO account_snapshots VALUES ('alpha','2026-10-01T22:00:00+00:00',100,'Public Alpha','Public bio',3,1,0);
            INSERT INTO account_snapshots VALUES ('alpha','2026-10-02T03:00:00+00:00',120,'Public Alpha','Public bio',3,1,0);
            INSERT INTO account_snapshots VALUES ('alpha','2026-10-02T08:00:00+00:00',130,'Public Alpha','Public bio',3,1,0);
            INSERT INTO account_snapshots VALUES ('beta','2026-10-02T08:00:00+00:00',999,'Public Beta','Other bio',1,0,0);
            CREATE TABLE dashboard_posts (id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT,
                published_at TEXT, updated_at TEXT, caption TEXT, title TEXT, hook_text TEXT,
                likes INTEGER, comments INTEGER, video_views INTEGER, video_plays INTEGER,
                post_type_label TEXT, product_type TEXT, hidden INTEGER, is_deleted INTEGER,
                raw_json TEXT, analysis_summary TEXT, permalink TEXT);
            INSERT INTO dashboard_posts VALUES (1,'alpha','PublicA','2026-10-01T23:00:00+00:00','2026-10-08T12:00:00+00:00',
                'Published caption','CANARY_PRIVATE_TITLE','CANARY_PRIVATE_HOOK',100,10,500,600,'Reel','clips',0,0,
                '{"credentials":"CANARY_SECRET"}','{"metrics":{"secret":0.5}}','http://internal/CANARY_TOKEN');
            INSERT INTO dashboard_posts VALUES (2,'alpha','PublicB','2026-10-02T02:00:00+00:00','2026-10-08T12:00:00+00:00',
                NULL,'CANARY_PRIVATE_TITLE','CANARY_PRIVATE_HOOK',-1,0,NULL,NULL,'Image','feed',0,0,NULL,NULL,NULL);
            INSERT INTO dashboard_posts VALUES (3,'alpha','PublicC','2026-10-02T09:00:00+00:00','2026-10-08T12:00:00+00:00',
                'Another public caption',NULL,NULL,50,3,NULL,NULL,'Image','feed',0,0,NULL,NULL,NULL);
            INSERT INTO dashboard_posts VALUES (4,'alpha','Hidden','2026-10-02T09:00:00+00:00','2026-10-08T12:00:00+00:00',
                'CANARY_HIDDEN_CAPTION',NULL,NULL,5000,3,NULL,NULL,'Image','feed',1,0,NULL,NULL,NULL);
            INSERT INTO dashboard_posts VALUES (5,'alpha','Deleted','2026-10-02T09:00:00+00:00','2026-10-08T12:00:00+00:00',
                'CANARY_DELETED_CAPTION',NULL,NULL,5000,3,NULL,NULL,'Image','feed',0,1,NULL,NULL,NULL);
            INSERT INTO dashboard_posts VALUES (6,'beta','Other','2026-10-02T09:00:00+00:00','2026-10-08T12:00:00+00:00',
                'CANARY_OTHER_CAPTION',NULL,NULL,999999,3,NULL,NULL,'Image','feed',0,0,NULL,NULL,NULL);
            INSERT INTO dashboard_posts VALUES (7,'alpha','Future','2026-12-01T09:00:00+00:00','2026-10-08T12:00:00+00:00',
                'CANARY_FUTURE_CAPTION',NULL,NULL,5000,3,NULL,NULL,'Image','feed',0,0,NULL,NULL,NULL);
            CREATE TABLE posts (id INTEGER PRIMARY KEY, section TEXT, shortcode TEXT, published_at TEXT,
                updated_at TEXT, caption TEXT, title TEXT, likes INTEGER, comments INTEGER, hidden INTEGER,
                is_deleted INTEGER, image_path TEXT);
            INSERT INTO posts VALUES (10,'historical','PublicA','2026-10-01T23:00:00+00:00','2026-10-07T12:00:00+00:00',
                'Published caption','CANARY_CANONICAL_TITLE',50,5,0,0,'CANARY_PRIVATE_PATH');
            INSERT INTO posts VALUES (11,'ab','InternalDraft','2026-10-01T23:00:00+00:00','2026-10-07T12:00:00+00:00',
                'CANARY_INTERNAL_DRAFT',NULL,50000,5,0,0,'CANARY_PRIVATE_PATH');
            ALTER TABLE dashboard_posts ADD COLUMN is_promo INTEGER DEFAULT 0;
            ALTER TABLE posts ADD COLUMN is_promo INTEGER DEFAULT 0;
        """)
        api.ensure_schema(conn)
    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(agent_connections, "connect", connect)
    monkeypatch.setattr(main, "connect", connect)
    monkeypatch.setattr(main, "FIREBASE_APP", object())
    monkeypatch.setattr(main.firebase_auth, "verify_id_token", lambda token: {"email": token, "uid": "test-uid"})
    monkeypatch.setattr(main, "log_usage_event", lambda *args: None)
    monkeypatch.setattr(api, "datetime", FixedNow)
    monkeypatch.setattr(account_media_kit, "datetime", FixedNow)
    app = FastAPI()
    app.middleware("http")(main._require_firebase_user)
    app.include_router(api.management_router)
    app.include_router(api.router)

    @app.get("/api/admin/users")
    @app.get("/api/health")
    @app.get("/api/dashboard/avatar/alpha")
    @app.get("/mcp")
    def internal():
        return {"CANARY_INTERNAL_ENDPOINT": True}

    return TestClient(app), connect


def create(client, owner="ana@example.com", **overrides):
    response = client.post(api.MANAGEMENT_URL, headers={"Authorization": f"Bearer {owner}"},
                           json={"name": "User 10 media kit", "account_handles": ["alpha"], **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def headers(key):
    return {"Authorization": f"Bearer {key}"}


def test_secret_returned_once_hashed_at_rest_and_owner_only_management(setup):
    client, connect = setup
    created = create(client)
    key = created["key"]
    assert key.startswith("sad_api_") and len(key) == 51
    listed = client.get(api.MANAGEMENT_URL, headers=headers("ana@example.com"))
    assert listed.headers["cache-control"] == "private, no-store"
    assert listed.json()["keys"] == [created["connection"]]
    assert listed.json()["can_create"] is True
    assert key not in listed.text and "key_hash" not in listed.text
    assert listed.json()["available_accounts"] == [{"handle": "alpha", "public_name": "Public Alpha"}, {"handle": "beta", "public_name": "Public Beta"}]
    with connect() as conn:
        row = dict(conn.execute("SELECT * FROM website_api_keys").fetchone())
    assert key not in row.values() and len(row["key_hash"]) == 64
    assert client.get(api.MANAGEMENT_URL, headers=headers("bob@example.com")).json()["keys"] == []
    assert client.delete(api.MANAGEMENT_URL + "/" + row["id"], headers=headers("bob@example.com")).status_code == 404
    assert client.post(api.MANAGEMENT_URL, headers=headers("bob@example.com"), json={"name": "Bad", "account_handles": ["alpha"]}).status_code == 403


@pytest.mark.parametrize("accounts", [[], ["inactive"], ["missing"], ["alpha/../beta"], [""]])
def test_key_creation_validates_active_explicit_account_scope(setup, accounts):
    client, _ = setup
    response = client.post(api.MANAGEMENT_URL, headers=headers("ana@example.com"), json={"name": "Test", "account_handles": accounts})
    assert response.status_code == 422


def test_key_cannot_escape_versioned_read_routes_even_with_local_auth_disabled(setup, monkeypatch):
    client, _ = setup
    key = create(client)["key"]
    monkeypatch.setattr(main, "FIREBASE_APP", None)
    for path in ["/api/admin/users", "/api/health", "/api/dashboard/avatar/alpha", "/mcp", api.MANAGEMENT_URL]:
        assert client.get(path, headers=headers(key)).status_code == 403
    assert client.post("/api/v1/accounts", headers=headers(key)).status_code == 403
    assert client.get("/api/v1/accounts").status_code == 401
    assert client.get("/api/v1/accounts", headers=headers("ana@example.com")).status_code == 401
    assert client.get(api.MANAGEMENT_URL).status_code == 401
    assert client.get(api.MANAGEMENT_URL, headers=headers("ana@example.com")).status_code == 503


def test_agent_keys_cannot_manage_website_credentials_or_discover_them(setup):
    client, connect = setup
    import hashlib
    key = "sad_agent_" + "A" * 43
    with connect() as conn:
        agent_connections.ensure_schema(conn)
        conn.execute("INSERT INTO agent_connections (id,owner_email,name,key_hash,key_prefix,access_mode,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",
                     ("agent", "ana@example.com", "Agent", hashlib.sha256(key.encode()).hexdigest(), key[:17], "full", "2020-01-01", "2099-01-01"))
    for method in ("GET", "POST", "DELETE"):
        path = api.MANAGEMENT_URL + ("/some-id" if method == "DELETE" else "")
        assert client.request(method, path, headers=headers(key)).status_code == 403
    assert client.get("/api/v1/accounts", headers=headers(key)).status_code == 401
    from app.product_mcp import catalogue
    spec = main.app.openapi()
    assert not any(value["path"].startswith((api.MANAGEMENT_URL, "/api/v1/")) for value in catalogue(spec).values())


def test_scoped_public_reads_exclude_internal_data_and_report_stored_freshness(setup):
    client, _ = setup
    key = create(client)["key"]
    scoped = client.get("/api/v1/accounts", headers=headers(key))
    assert scoped.json() == {"schema_version": "1.0", "data": [{"handle": "alpha", "public_name": "Public Alpha", "profile_url": "https://www.instagram.com/alpha/"}]}
    assert client.get("/api/v1/accounts/beta", headers=headers(key)).status_code == 404
    profile = client.get("/api/v1/accounts/alpha", headers=headers(key)).json()
    assert profile["data"]["followers"] == 130
    assert profile["generated_at"] == "2026-10-09T18:00:15+00:00"
    assert profile["data_updated_at"] == {"profile": "2026-10-02T08:00:00+00:00", "engagement": "2026-10-08T12:00:00+00:00"}
    for suffix in ("", "/media-kit", "/posts", "/followers/history"):
        response = client.get("/api/v1/accounts/alpha" + suffix, headers=headers(key))
        assert response.status_code == 200, response.text
        assert "CANARY" not in response.text
        assert "no-store" in response.headers["cache-control"]
        assert response.headers["vary"] == "Authorization"
    media = client.get("/api/v1/accounts/alpha/media-kit", headers=headers(key)).json()
    assert media["data"]["summary"]["all_time"]["post_count"] == 3
    assert "last_30_days" not in media["data"]["summary"]


def test_posts_deduplicate_sources_filter_public_rows_and_paginate_local_dates(setup):
    client, _ = setup
    key = create(client)["key"]
    path = "/api/v1/accounts/alpha/posts"
    first = client.get(path, params={"limit": 1}, headers=headers(key)).json()
    assert first["data"][0]["shortcode"] == "PublicC"
    assert first["pagination"] == {"limit": 1, "offset": 0, "total": 3, "has_more": True, "next_offset": 1}
    local = client.get(path, params={"from": "2026-10-01", "to": "2026-10-01"}, headers=headers(key)).json()
    assert [post["shortcode"] for post in local["data"]] == ["PublicB", "PublicA"]
    assert local["data"][0]["likes"] is None and local["data"][0]["comments"] == 0
    assert local["data"][0]["caption"] is None
    assert local["data"][1]["likes"] == 100  # newer Dashboard reading, one published post
    assert local["data"][1]["permalink"] == "https://www.instagram.com/p/PublicA/"
    last = client.get(path, params={"limit": 2, "offset": 2}, headers=headers(key)).json()
    assert len(last["data"]) == 1 and last["pagination"]["has_more"] is False
    beyond = client.get(path, params={"offset": 100}, headers=headers(key)).json()
    assert beyond["data"] == [] and beyond["pagination"]["total"] == 3
    for params in ({"limit": 101}, {"offset": -1}, {"from": "bad"}, {"from": "2026-10-02", "to": "2026-10-01"}):
        assert client.get(path, params=params, headers=headers(key)).status_code == 422


def test_promo_manual_hashtag_and_negative_filter_before_pagination(setup):
    client, connect = setup
    key = create(client)["key"]
    path = "/api/v1/accounts/alpha/posts"
    with connect() as conn:
        conn.execute("UPDATE posts SET is_promo = 1 WHERE shortcode = 'PublicA'")
        conn.execute("UPDATE dashboard_posts SET caption = 'Published #AITOOLSENTIENT! text' WHERE shortcode = 'PublicB'")
        conn.execute("UPDATE dashboard_posts SET caption = '#aitoolsentientlabs' WHERE shortcode = 'PublicC'")
        # A mark on another account or an excluded post must never enter totals.
        conn.execute("UPDATE dashboard_posts SET is_promo = 1 WHERE shortcode IN ('Hidden','Deleted','Future','Other')")
        conn.execute("UPDATE posts SET is_promo = 1 WHERE shortcode = 'InternalDraft'")
    all_posts = client.get(path, headers=headers(key)).json()
    assert all_posts["schema_version"] == "1.0"
    assert {post["shortcode"]: post["is_promo"] for post in all_posts["data"]} == {"PublicA": True, "PublicB": True, "PublicC": False}
    first = client.get(path, params={"is_promo": "true", "limit": 1}, headers=headers(key)).json()
    assert [post["shortcode"] for post in first["data"]] == ["PublicB"]
    assert first["pagination"] == {"limit": 1, "offset": 0, "total": 2, "has_more": True, "next_offset": 1}
    second = client.get(path, params={"is_promo": "true", "limit": 1, "offset": 1}, headers=headers(key)).json()
    assert [post["shortcode"] for post in second["data"]] == ["PublicA"]
    assert second["pagination"] == {"limit": 1, "offset": 1, "total": 2, "has_more": False, "next_offset": None}
    negative = client.get(path, params={"is_promo": "false"}, headers=headers(key)).json()
    assert [post["shortcode"] for post in negative["data"]] == ["PublicC"]
    assert negative["pagination"]["total"] == 1
    empty = client.get(path, params={"is_promo": "true", "offset": 10}, headers=headers(key)).json()
    assert empty["data"] == [] and empty["pagination"]["total"] == 2
    dated = client.get(path, params={"is_promo": "true", "from": "2026-10-02", "to": "2026-10-02"}, headers=headers(key)).json()
    assert dated["data"] == [] and dated["pagination"]["total"] == 0
    assert client.get(path, params={"is_promo": "invalid"}, headers=headers(key)).status_code == 422
    kit = client.get("/api/v1/accounts/alpha/media-kit", headers=headers(key)).json()["data"]
    assert {post["shortcode"]: post["is_promo"] for post in kit["best_posts"]["all_time"]} == {"PublicA": True, "PublicB": True, "PublicC": False}
    assert kit["summary"]["all_time"]["post_count"] == 3
    assert kit["summary"]["all_time"]["metrics"]["likes"]["total"] == 150
    assert "_public_manual_promo" not in json.dumps(kit)


@pytest.mark.parametrize(("caption", "expected"), [
    ("#aitoolsentient", True), ("#AITOOLSENTIENT", True),
    ("Try #AiToolSentient, today.", True), ("(#aitoolsentient)", True),
    ("#aitoolsentientlabs", False), ("#aitoolsentient_", False),
    ("#aitoolsentient2", False), ("aitoolsentient", False),
    ("#aİtoolsentient", False), ("#aitoolsentienté", True),
    (None, False), ("Ordinary published caption", False),
])
def test_promo_hashtag_uses_research_case_and_word_boundaries(setup, caption, expected):
    client, connect = setup
    key = create(client)["key"]
    with connect() as conn:
        conn.execute("UPDATE dashboard_posts SET caption = ?, title = '#aitoolsentient', hook_text = '#aitoolsentient' WHERE shortcode = 'PublicC'", (caption,))
    posts = client.get("/api/v1/accounts/alpha/posts", headers=headers(key)).json()["data"]
    assert next(post for post in posts if post["shortcode"] == "PublicC")["is_promo"] is expected
    kit = client.get("/api/v1/accounts/alpha/media-kit", headers=headers(key)).json()["data"]
    assert next(post for post in kit["best_posts"]["all_time"] if post["shortcode"] == "PublicC")["is_promo"] is expected


@pytest.mark.parametrize(("canonical_flag", "dashboard_flag"), [(1, 0), (0, 1)])
def test_promo_manual_canonical_changes_survive_newer_duplicate_metrics(setup, canonical_flag, dashboard_flag):
    client, connect = setup
    key = create(client)["key"]
    with connect() as conn:
        conn.execute("UPDATE posts SET is_promo = ? WHERE shortcode = 'PublicA'", (canonical_flag,))
        conn.execute("UPDATE dashboard_posts SET is_promo = ? WHERE shortcode = 'PublicA'", (dashboard_flag,))
    result = client.get("/api/v1/accounts/alpha/posts", headers=headers(key)).json()
    exported = next(post for post in result["data"] if post["shortcode"] == "PublicA")
    assert exported["is_promo"] is bool(canonical_flag)
    assert exported["likes"] == 100  # Metrics still use the freshest duplicate.
    filtered = client.get("/api/v1/accounts/alpha/posts", params={"is_promo": str(bool(canonical_flag)).lower()}, headers=headers(key)).json()
    assert "PublicA" in {post["shortcode"] for post in filtered["data"]}
    kit = client.get("/api/v1/accounts/alpha/media-kit", headers=headers(key)).json()["data"]
    assert next(post for post in kit["best_posts"]["all_time"] if post["shortcode"] == "PublicA")["is_promo"] is bool(canonical_flag)
    # Public classification must not change historical internal promo counts.
    internal = account_media_kit.build_account_media_kit("alpha")
    assert internal["summary"]["all_time"]["promo_posts"] == dashboard_flag


def test_noncanonical_manual_promo_and_legacy_missing_canonical_column(setup):
    client, connect = setup
    key = create(client, account_handles=["alpha", "beta"])["key"]
    with connect() as conn:
        conn.execute("UPDATE dashboard_posts SET is_promo = 1 WHERE shortcode IN ('PublicA','Other')")
        conn.execute("ALTER TABLE posts DROP COLUMN is_promo")
    for handle, code in (("alpha", "PublicA"), ("beta", "Other")):
        result = client.get(f"/api/v1/accounts/{handle}/posts", params={"is_promo": "true"}, headers=headers(key)).json()
        assert [post["shortcode"] for post in result["data"]] == [code]
        assert result["data"][0]["is_promo"] is True
        kit = client.get(f"/api/v1/accounts/{handle}/media-kit", headers=headers(key)).json()["data"]
        assert next(post for post in kit["best_posts"]["all_time"] if post["shortcode"] == code)["is_promo"] is True


def test_promo_api_recent_showcase_classifies_before_caption_truncation(setup):
    client, connect = setup
    key = create(client, account_handles=["beta"])["key"]
    caption = "x" * 501 + " #AITOOLSENTIENT."
    with connect() as conn:
        conn.execute("UPDATE dashboard_posts SET caption = ? WHERE shortcode = 'Other'", (caption,))
        conn.execute("INSERT INTO dashboard_posts (account, shortcode, published_at, caption, likes, comments, hidden, is_deleted, is_promo) VALUES ('beta','History','2026-09-08T18:00:00Z','Historical public caption',1,0,0,0,1)")
        conn.execute("INSERT INTO account_snapshots (handle,captured_at,followers_count,posts_count,private) VALUES ('beta','2026-09-09T17:00:00Z',990,1,0)")
        conn.execute("INSERT INTO account_snapshots (handle,captured_at,followers_count,posts_count,private) VALUES ('beta','2026-10-09T17:00:00Z',1000,2,0)")
    posts = client.get("/api/v1/accounts/beta/posts", params={"is_promo": "true"}, headers=headers(key)).json()
    assert posts["pagination"]["total"] == 2
    assert next(post for post in posts["data"] if post["shortcode"] == "Other")["caption"] == caption
    kit = client.get("/api/v1/accounts/beta/media-kit", headers=headers(key)).json()["data"]
    assert kit["summary"]["all_time"]["post_count"] == 2
    assert kit["summary"]["last_30_days"]["post_count"] == 1
    assert {post["shortcode"]: post["is_promo"] for post in kit["best_posts"]["all_time"]} == {"Other": True, "History": True}
    recent = kit["best_posts"]["last_30_days"]
    assert len(recent) == 1 and recent[0]["shortcode"] == "Other"
    assert recent[0]["is_promo"] is True and recent[0]["public_caption"] == "x" * 500


def test_followers_history_final_costa_rica_daily_snapshots_and_filters(setup):
    client, _ = setup
    key = create(client)["key"]
    path = "/api/v1/accounts/alpha/followers/history"
    result = client.get(path, headers=headers(key)).json()
    assert result["timezone"] == "America/Costa_Rica"
    assert result["data"] == [
        {"date": "2026-10-01", "captured_at": "2026-10-02T03:00:00+00:00", "followers": 120},
        {"date": "2026-10-02", "captured_at": "2026-10-02T08:00:00+00:00", "followers": 130},
    ]
    result = client.get(path, params={"from": "2026-10-02", "to": "2026-10-02"}, headers=headers(key)).json()
    assert len(result["data"]) == 1 and result["data"][0]["followers"] == 130


def test_private_account_posts_and_showcases_are_not_exported(setup):
    client, connect = setup
    key = create(client)["key"]
    with connect() as conn:
        conn.execute("UPDATE account_snapshots SET private = 1 WHERE handle = 'alpha'")
        conn.execute("UPDATE dashboard_posts SET is_promo = 1, caption = '#aitoolsentient' WHERE account = 'alpha'")
    assert client.get("/api/v1/accounts/alpha/posts", headers=headers(key)).json()["data"] == []
    for value in ("true", "false"):
        result = client.get("/api/v1/accounts/alpha/posts", params={"is_promo": value}, headers=headers(key)).json()
        assert result["data"] == [] and result["pagination"]["total"] == 0
    kit = client.get("/api/v1/accounts/alpha/media-kit", headers=headers(key)).json()["data"]
    assert kit["best_posts"] == {"all_time": []}
    assert kit["summary"]["all_time"]["post_count"] == 0


@pytest.mark.parametrize("flag", ["hidden", "is_deleted"])
def test_older_canonical_privacy_flags_override_newer_unflagged_metrics_copies(setup, flag):
    client, connect = setup
    key = create(client)["key"]
    with connect() as conn:
        conn.execute(f"UPDATE posts SET {flag} = 1 WHERE shortcode = 'PublicA'")
        conn.execute("UPDATE posts SET is_promo = 1 WHERE shortcode = 'PublicA'")
    exported = client.get("/api/v1/accounts/alpha/posts", headers=headers(key)).json()
    assert [post["shortcode"] for post in exported["data"]] == ["PublicC", "PublicB"]
    assert exported["pagination"]["total"] == 2
    promos = client.get("/api/v1/accounts/alpha/posts", params={"is_promo": "true"}, headers=headers(key)).json()
    assert promos["data"] == [] and promos["pagination"]["total"] == 0
    kit = client.get("/api/v1/accounts/alpha/media-kit", headers=headers(key)).json()["data"]
    assert kit["summary"]["all_time"]["post_count"] == 2
    assert kit["summary"]["all_time"]["metrics"]["likes"]["total"] == 50
    assert {post["shortcode"] for post in kit["best_posts"]["all_time"]} == {"PublicC", "PublicB"}
    # The optional export boundary does not change historical internal reports.
    internal = account_media_kit.build_account_media_kit("alpha")
    assert internal["public_summary"]["all_time"]["post_count"] == 3


def test_live_account_status_owner_privilege_allowlist_expiry_and_revocation(setup):
    client, connect = setup
    created = create(client)
    key = created["key"]
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_active = 0 WHERE handle = 'alpha'")
    assert client.get("/api/v1/accounts/alpha", headers=headers(key)).status_code == 404
    assert client.get("/api/v1/accounts", headers=headers(key)).json()["data"] == []
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_active = 1 WHERE handle = 'alpha'")
        conn.execute("UPDATE dashboard_users SET is_admin = 0, role = 'viewer' WHERE email = 'ana@example.com'")
    assert client.get("/api/v1/accounts", headers=headers(key)).status_code == 403
    listed = client.get(api.MANAGEMENT_URL, headers=headers("ana@example.com")).json()
    assert listed["can_create"] is False and listed["available_accounts"] == []
    assert listed["keys"][0]["id"] == created["connection"]["id"]
    assert client.delete(api.MANAGEMENT_URL + "/" + created["connection"]["id"], headers=headers("ana@example.com")).status_code == 200
    assert client.get("/api/v1/accounts", headers=headers(key)).status_code == 401
    with connect() as conn:
        conn.execute("UPDATE dashboard_users SET is_admin = 1 WHERE email = 'ana@example.com'")
    key = create(client)["key"]
    with connect() as conn:
        conn.execute("UPDATE website_api_keys SET expires_at = '2020-01-01' WHERE revoked_at IS NULL")
    assert client.get("/api/v1/accounts", headers=headers(key)).status_code == 401
    key = create(client)["key"]
    with connect() as conn:
        conn.execute("DELETE FROM dashboard_users WHERE email = 'ana@example.com'")
    assert client.get("/api/v1/accounts", headers=headers(key)).status_code == 403


def test_fixed_window_limit_is_durable_and_atomic_across_concurrent_requests(setup):
    client, connect = setup
    key = create(client)["key"]

    def authenticate(_):
        try:
            api.authenticate(key, "/api/v1/accounts", "GET")
            return 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=12) as executor:
        statuses = list(executor.map(authenticate, range(80)))
    assert statuses.count(200) == 60 and statuses.count(429) == 20
    denied = client.get("/api/v1/accounts", headers=headers(key))
    assert denied.status_code == 429 and denied.headers["retry-after"] == "45"
    with connect() as conn:
        assert conn.execute("SELECT request_count FROM website_api_rate_windows").fetchone()["request_count"] == 60
        conn.execute("UPDATE website_api_rate_windows SET minute_window = minute_window - 1")
    assert client.get("/api/v1/accounts", headers=headers(key)).status_code == 200


def test_runtime_schema_is_idempotent(setup):
    _, connect = setup
    with connect() as conn:
        api.ensure_schema(conn)
        api.ensure_schema(conn)
        assert conn.execute("SELECT COUNT(*) AS n FROM website_api_keys").fetchone()["n"] == 0


def test_authentication_never_holds_a_connection_while_acquiring_another(setup, monkeypatch):
    client, connect = setup
    key = create(client)["key"]
    depth = ContextVar("website_api_connection_depth", default=0)
    acquisitions = []

    @contextmanager
    def tracked_connect():
        assert depth.get() == 0, "Nested pool acquisitions can deadlock a saturated worker pool"
        token = depth.set(1)
        acquisitions.append(True)
        try:
            with connect() as conn:
                yield conn
        finally:
            depth.reset(token)

    monkeypatch.setattr(db, "connect", tracked_connect)
    assert api.authenticate(key, "/api/v1/accounts", "GET")["owner_email"] == "ana@example.com"
    assert len(acquisitions) == 3  # credential read, owner permissions, credential recheck/quota


def test_revocation_during_owner_lookup_is_rechecked_before_admission(setup, monkeypatch):
    client, connect = setup
    key = create(client)["key"]
    original_access = db.get_dashboard_user_access

    def revoke_during_access_lookup(owner):
        with connect() as conn:
            conn.execute("UPDATE website_api_keys SET revoked_at = '2026-10-09T18:00:15+00:00'")
        return original_access(owner)

    monkeypatch.setattr(db, "get_dashboard_user_access", revoke_during_access_lookup)
    with pytest.raises(HTTPException) as error:
        api.authenticate(key, "/api/v1/accounts", "GET")
    assert error.value.status_code == 401
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM website_api_rate_windows").fetchone()["n"] == 0
