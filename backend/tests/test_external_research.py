from __future__ import annotations

import json
import sqlite3
import io
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app import db, external_api as api, external_research as reads


class FixedNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 15, 18, tzinfo=UTC).astimezone(tz or UTC)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    database = tmp_path / "research-read.sqlite3"
    queries = []

    @contextmanager
    def connect():
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        conn.set_trace_callback(queries.append)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    with connect() as conn:
        conn.executescript("""
            CREATE TABLE accounts (handle TEXT PRIMARY KEY, is_active INTEGER, is_canonical INTEGER,
                hot_threshold INTEGER, label TEXT, group_name TEXT, research_enabled INTEGER);
            INSERT INTO accounts VALUES ('alpha',1,1,600,'Alpha','sentient',1);
            INSERT INTO accounts VALUES ('beta',1,0,300,'Beta','competitors',1);
            INSERT INTO accounts VALUES ('a_b',1,0,100,'Underscore','competitors',1);
            INSERT INTO accounts VALUES ('axb',1,0,100,'Must not match underscore','competitors',1);
            CREATE TABLE posts (id INTEGER PRIMARY KEY, section TEXT, shortcode TEXT, published_at TEXT,
                updated_at TEXT, caption TEXT, title TEXT, likes INTEGER, comments INTEGER,
                is_hot INTEGER, hot_rate_multiplier REAL, hot_marked_at TEXT, likes_at_1h INTEGER,
                is_promo INTEGER, hidden INTEGER, is_deleted INTEGER, hook_text TEXT, raw_json TEXT);
            INSERT INTO posts VALUES (1,'historical','Recent','2026-10-15T12:00:00+00:00',
                '2026-10-15T17:00:00+00:00','Public caption #aitoolsentient','Internal title',
                250,9,1,2.5,'2026-10-15T13:00:00+00:00',150,0,0,0,'OCR is useful',
                '{"coauthorProducers":[{"username":"alpha"},{"username":"openai"}],"secret":"CANARY_SECRET"}');
            INSERT INTO posts VALUES (2,'historical','Historic','2026-10-01T12:00:00+00:00',
                '2026-10-15T17:00:00+00:00','Older hot',NULL,900,12,1,7.2,NULL,600,0,0,0,NULL,NULL);
            INSERT INTO posts VALUES (3,'historical','Unknown','2026-10-15T11:00:00+00:00',
                '2026-10-15T17:00:00+00:00','Likes are unavailable',NULL,-1,NULL,0,NULL,NULL,NULL,0,0,0,NULL,
                '{"likesCount":9999,"taggedUsers":[{"username":"openai"}]}');
            INSERT INTO posts VALUES (4,'historical','Hidden','2026-10-15T10:00:00+00:00',
                '2026-10-15T17:00:00+00:00','Hidden post',NULL,5000,99,1,9,NULL,NULL,1,1,0,NULL,NULL);
            INSERT INTO posts VALUES (5,'ab','InternalAB','2026-10-15T13:00:00+00:00',
                '2026-10-15T17:00:00+00:00','Internal draft',NULL,5000,99,1,9,NULL,NULL,1,0,0,NULL,NULL);
            CREATE TABLE dashboard_posts (id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT,
                published_at TEXT, updated_at TEXT, caption TEXT, likes INTEGER, comments INTEGER,
                video_views INTEGER, video_plays INTEGER, is_hot INTEGER, hot_rate_multiplier REAL,
                hot_marked_at TEXT, is_promo INTEGER, hidden INTEGER, is_deleted INTEGER,
                raw_json TEXT, coauthors TEXT, transcript TEXT, alt_text TEXT, hashtags TEXT,
                analysis_summary TEXT, llm_report TEXT, post_type_label TEXT);
            INSERT INTO dashboard_posts VALUES (1,'beta','Foreign','2026-10-15T13:00:00+00:00',
                '2026-10-15T17:00:00+00:00','CANARY_FOREIGN',99999,99,100000,200000,1,99,NULL,0,0,0,
                '{"coauthorProducers":[{"username":"beta"},{"username":"openai"}]}',NULL,
                'CANARY_FOREIGN_TRANSCRIPT',NULL,NULL,NULL,NULL,'Reel');
            INSERT INTO dashboard_posts VALUES (2,'alpha','Recent','2026-10-15T12:00:00+00:00',
                '2026-10-15T18:00:00+00:00','Public caption #aitoolsentient',400,99,700,800,1,2.5,NULL,0,0,0,NULL,NULL,
                'Saved transcript','Saved alt text','["chatgpt"]','{"metrics":{"virality":0.7}}','Saved report','Reel');
            CREATE TABLE account_snapshots (handle TEXT, captured_at TEXT, followers_count INTEGER,
                posts_count INTEGER, full_name TEXT, private INTEGER, verified INTEGER);
            INSERT INTO account_snapshots VALUES ('alpha','2026-10-01T12:00:00+00:00',100,20,'Public Alpha',0,1);
            INSERT INTO account_snapshots VALUES ('alpha','2026-10-08T12:00:00+00:00',120,23,'Public Alpha',0,1);
            INSERT INTO account_snapshots VALUES ('alpha','2026-10-14T12:00:00+00:00',130,24,'Public Alpha',0,1);
            INSERT INTO account_snapshots VALUES ('alpha','2026-10-15T12:00:00+00:00',150,25,'Public Alpha',0,1);
            INSERT INTO account_snapshots VALUES ('alpha','2026-10-15T16:00:00+00:00',NULL,25,'Public Alpha',0,1);
            INSERT INTO account_snapshots VALUES ('beta','2026-10-15T12:00:00+00:00',99999,555,'CANARY_PROFILE',0,1);
            CREATE TABLE topic_stack_members (post_key TEXT PRIMARY KEY, stack_id TEXT, words TEXT, posted_at REAL);
            INSERT INTO topic_stack_members VALUES ('alpha:Recent','shared','["useful"]',1792065600);
            INSERT INTO topic_stack_members VALUES ('beta:Foreign','shared','["CANARY_FOREIGN_WORD"]',1792069200);
            INSERT INTO topic_stack_members VALUES ('a_b:Own','wildcard','[]',1792065600);
            INSERT INTO topic_stack_members VALUES ('axb:Foreign','wildcard','[]',1792065600);
        """)
    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(reads, "datetime", FixedNow)

    def require_scope(request, *scopes):
        identity = request.state.website_api_key
        if not set(scopes) & set(identity["data_scopes"]):
            raise HTTPException(403, "Scope not available")
        return identity

    monkeypatch.setattr(api, "require_scope", require_scope, raising=False)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.website_api_key = {"account_handles": request.headers.get("x-accounts", "alpha").split(","),
                                         "data_scopes": request.headers.get("x-scopes", "accounts,research,tracker,insights").split(",")}
        return await call_next(request)

    app.include_router(reads.router)
    queries.clear()
    return TestClient(app), connect, queries


def test_hot_matches_current_research_window_and_persisted_history(setup):
    client, _, queries = setup
    current = client.get("/api/v1/hot")
    assert current.status_code == 200
    value = current.json()
    assert [post["shortcode"] for post in value["data"]] == ["Recent"]
    assert value["data"][0]["hot"]["score"] == 2.5
    assert value["data"][0]["hot"]["account_threshold"] == 600
    assert value["data"][0]["is_collab"] is True
    assert value["data"][0]["collaborators"] == ["openai"]
    assert value["criteria"]["recent_window_hours"] == 30
    assert current.headers["cache-control"] == "private, no-store"
    assert "ocr_text" not in value["data"][0] and "Internal title" not in current.text
    assert "CANARY" not in current.text
    assert [post["shortcode"] for post in client.get("/api/v1/hot?history=true").json()["data"]] == ["Recent", "Historic"]
    assert not [query for query in queries if re_mutation(query)]


def re_mutation(query):
    return query.strip().split(" ", 1)[0].upper() in {"UPDATE", "INSERT", "DELETE", "CREATE", "ALTER", "DROP", "REPLACE"}


def test_account_scope_is_before_pagination_and_search(setup):
    client, _, _ = setup
    page = client.get("/api/v1/posts?limit=1").json()
    assert page["data"][0]["shortcode"] == "Recent"
    assert page["pagination"]["total"] == 3
    assert client.get("/api/v1/posts?account=beta").status_code == 404
    assert client.get("/api/v1/posts?q=CANARY_FOREIGN").json()["data"] == []
    assert "CANARY_FOREIGN" not in client.get("/api/v1/research/stacks").text
    assert client.get("/api/v1/research/stacks/shared/posts").json()["pagination"]["total"] == 1
    assert client.get("/api/v1/research/stacks/shared/posts").json()["data"][0]["account"] == "alpha"


def test_null_counts_are_not_replaced_by_stale_raw_and_unknown_is_not_false(setup):
    client, _, _ = setup
    post = client.get("/api/v1/posts?q=unavailable").json()["data"][0]
    assert post["likes"] is None and post["comments"] is None
    assert post["is_collab"] is None and post["collaborators"] == []
    confirmed = client.get("/api/v1/posts?is_collab=true").json()
    assert [post["shortcode"] for post in confirmed["data"]] == ["Recent"]
    assert confirmed["pagination"]["total"] is None
    assert confirmed["pagination"]["offset_type"] == "candidate"
    assert confirmed["pagination"]["scanned_count"] == 3
    assert client.get("/api/v1/posts?is_collab=false").json()["data"] == []


def test_promo_filter_and_candidate_pagination_never_skip_matches(setup):
    client, _, _ = setup
    first = client.get("/api/v1/posts?is_promo=true&limit=1").json()
    assert first["data"][0]["shortcode"] == "Recent"
    assert first["pagination"]["next_offset"] == 1
    second = client.get("/api/v1/posts?is_promo=true&limit=1&offset=1").json()
    assert second["data"] == []
    assert second["pagination"]["next_offset"] is None
    assert client.get("/api/v1/posts?q=%25").json()["data"] == []


def test_research_detail_has_saved_text_without_provider_payload_or_other_accounts(setup):
    client, _, _ = setup
    response = client.get("/api/v1/research/posts/alpha/Recent")
    assert response.status_code == 200
    value = response.json()["data"]
    assert value["ocr_text"] == "OCR is useful"
    assert value["transcript"] == "Saved transcript"
    assert value["analysis_summary"] == {"metrics": {"virality": 0.7}}
    assert value["alt_text"] == "Saved alt text"
    assert value["transcript_available"] is True and value["alt_text_available"] is True
    assert value["hashtags"] == ["chatgpt"]
    assert "CANARY" not in response.text and "raw_json" not in response.text
    assert client.get("/api/v1/research/posts/beta/Foreign").status_code == 404
    assert client.get("/api/v1/research/posts/alpha/InternalAB").status_code == 404


def test_grants_enforced_and_public_hot_excludes_private_accounts(setup):
    client, connect, _ = setup
    assert client.get("/api/v1/posts", headers={"x-scopes": "accounts"}).status_code == 403
    assert client.get("/api/v1/hot", headers={"x-scopes": "research"}).status_code == 200
    assert client.get("/api/v1/tracker/accounts", headers={"x-scopes": "research"}).status_code == 403
    assert client.get("/api/v1/insights/posts", headers={"x-scopes": "research"}).status_code == 403
    with connect() as conn:
        conn.execute("UPDATE account_snapshots SET private = 1 WHERE handle = 'alpha'")
    assert client.get("/api/v1/hot?history=true").json()["data"] == []
    assert client.get("/api/v1/posts").json()["data"]


def test_tracker_latest_usable_count_and_history_isolation(setup):
    client, _, queries = setup
    response = client.get("/api/v1/tracker/accounts")
    value = response.json()["data"][0]
    assert value["handle"] == "alpha" and value["followers"] == 150
    assert value["deltas"]["1d"]["delta"] == 20
    assert value["deltas"]["7d"]["delta"] == 30
    assert value["deltas"]["30d"] is None
    assert "CANARY" not in response.text
    history = client.get("/api/v1/tracker/accounts/alpha/history?limit=2")
    assert len(history.json()["data"]) == 2
    assert history.json()["pagination"]["total"] == 5
    assert history.json()["data"][0]["followers"] is None
    assert client.get("/api/v1/tracker/accounts/beta/history").status_code == 404
    assert not [query for query in queries if re_mutation(query)]


def test_tracker_batches_accounts_and_compares_local_day_final_readings(setup):
    client, connect, queries = setup
    with connect() as conn:
        conn.execute("INSERT INTO account_snapshots VALUES ('alpha','2026-10-15T01:00:00+00:00',140,24,'Public Alpha',0,1)")
    queries.clear()
    response = client.get("/api/v1/tracker/accounts", headers={"x-accounts": "alpha,beta"})
    assert response.status_code == 200
    values = response.json()["data"]
    assert values[0]["deltas"]["1d"]["delta"] == 10
    assert values[1]["followers"] == 99999
    assert len([query for query in queries if query.startswith("WITH latest") or query.startswith("WITH targets")]) == 2


def test_tracker_exports_following_engagement_and_scoped_rankings_before_page(setup):
    client, connect, _ = setup
    with connect() as conn:
        conn.execute("ALTER TABLE account_snapshots ADD COLUMN following_count INTEGER")
        conn.execute("UPDATE account_snapshots SET following_count = 42 WHERE handle = 'alpha'")
        conn.execute("INSERT INTO dashboard_posts (id,account,shortcode,published_at,updated_at,likes) VALUES (50,'alpha','Previous','2026-09-01T12:00:00+00:00','2026-10-15T17:00:00+00:00',200)")
    page = client.get("/api/v1/tracker/overview?limit=1", headers={"x-accounts": "alpha,beta"}).json()
    value = page["data"][0]
    assert value["handle"] == "alpha" and value["following_count"] == 42
    assert value["rank_by_followers"] == 2 and value["rank_by_growth_7d"] == 1
    assert value["avg_likes_30d"] == 400 and value["posts_30d"] == 1
    assert value["engagement_trend_pct"] == 100
    assert value["delta_7d"] == value["deltas"]["7d"]
    # An excluded account cannot affect this key's rank or leak in metadata.
    assert client.get("/api/v1/tracker/accounts").json()["data"][0]["rank_by_followers"] == 1
    history = client.get("/api/v1/tracker/accounts/alpha/history").json()["data"]
    assert history[0]["following_count"] == 42


def test_tracker_unknown_likes_do_not_lower_averages_but_posts_still_count(setup):
    client, connect, _ = setup
    with connect() as conn:
        conn.execute("INSERT INTO dashboard_posts (id,account,shortcode,published_at,updated_at,likes) VALUES (60,'alpha','HiddenLikes','2026-10-15T12:00:00+00:00','2026-10-15T17:00:00+00:00',-1)")
    value = client.get("/api/v1/tracker/accounts").json()["data"][0]
    assert value["avg_likes_30d"] == 400 and value["posts_30d"] == 2
    assert value["likes_known_30d"] == 1 and value["likes_known_previous_30d"] == 0


def test_insights_aggregates_ignore_unknown_and_excluded_posts(setup):
    client, _, _ = setup
    value = client.get("/api/v1/insights/summary").json()["data"]
    assert value["post_count"] == 3
    assert value["likes_known"] == 2 and value["likes_total"] == 1300
    assert value["likes_average"] == 650
    assert value["hot_posts"] == 2
    assert client.get("/api/v1/insights/summary?account=beta").status_code == 404
    assert client.get("/api/v1/insights/summary?from=2026-10-16&to=2026-10-01").status_code == 422


def test_stack_account_underscore_is_not_sql_wildcard(setup):
    client, _, _ = setup
    response = client.get("/api/v1/research/stacks", headers={"x-accounts": "a_b"})
    assert response.json()["data"][0]["post_count"] == 1


def test_candidate_paging_has_hard_scan_bound(setup):
    client, connect, _ = setup
    with connect() as conn:
        conn.executemany("""INSERT INTO posts (id, section, shortcode, published_at, updated_at, caption,
            is_hot, is_promo, hidden, is_deleted) VALUES (?, 'historical', ?, '2026-10-15T15:00:00+00:00',
            '2026-10-15T17:00:00+00:00', 'Many unknown posts', 0, 0, 0, 0)""",
                         [(100 + index, f"Item{index:04d}") for index in range(510)])
    response = client.get("/api/v1/posts?is_collab=true").json()
    assert response["data"] == []
    assert response["pagination"]["scanned_count"] == 500
    assert response["pagination"]["next_offset"] == 500
    next_page = client.get("/api/v1/posts?is_collab=true&offset=500").json()
    assert [post["shortcode"] for post in next_page["data"]] == ["Recent"]


def test_newer_observation_null_and_views_use_existing_metric_precedence(setup):
    client, connect, _ = setup
    with connect() as conn:
        conn.execute("CREATE TABLE engagement_observations (shortcode TEXT, observed_at TEXT, raw_json TEXT)")
        conn.execute("INSERT INTO engagement_observations VALUES (?, ?, ?)", ("Recent", "2026-10-15T18:00:00+00:00", json.dumps({
            "shortCode": "Recent", "likesCount": None, "commentsCount": 42, "videoViewCount": 900,
            "credentials": "CANARY_SECRET", "childPosts": [{"displayUrl": "CANARY_HEAVY_MEDIA"}]})))
    response = client.get("/api/v1/posts?q=Public%20caption")
    value = response.json()["data"][0]
    assert value["likes"] is None and value["comments"] == 42 and value["views"] == 900
    assert value["metrics_updated_at"] == "2026-10-15T18:00:00+00:00"
    assert "CANARY" not in response.text


def test_nested_performance_container_is_kept_in_compact_metric_projection(setup):
    client, connect, _ = setup
    with connect() as conn:
        conn.execute("CREATE TABLE engagement_observations (shortcode TEXT, observed_at TEXT, raw_json TEXT)")
        conn.execute("INSERT INTO engagement_observations VALUES (?, ?, ?)", ("Recent", "2026-10-15T18:00:00+00:00", json.dumps({
            "shortCode": "Recent", "metrics": {"likesCount": 1234, "videoViewCount": 5678}})))
    value = client.get("/api/v1/posts?q=Public%20caption").json()["data"][0]
    assert value["likes"] == 1234 and value["views"] == 5678


def test_public_projection_keeps_merged_format_caption_and_date_parity(setup):
    client, connect, _ = setup
    with connect() as conn:
        conn.execute("ALTER TABLE dashboard_posts ADD COLUMN product_type TEXT")
        conn.execute("UPDATE posts SET caption = NULL, published_at = NULL WHERE shortcode = 'Recent'")
        conn.execute("UPDATE dashboard_posts SET product_type = 'clips' WHERE account = 'alpha' AND shortcode = 'Recent'")
    value = client.get("/api/v1/hot").json()["data"][0]
    assert value["format"] == "Reel"
    assert value["published_at"] == "2026-10-15T12:00:00+00:00"
    assert value["caption"] == "Public caption #aitoolsentient"


def test_research_includes_all_stored_performance_facts_without_expanding_public_hot(setup):
    client, connect, _ = setup
    with connect() as conn:
        conn.execute("ALTER TABLE dashboard_posts ADD COLUMN shares INTEGER")
        conn.execute("ALTER TABLE dashboard_posts ADD COLUMN reach INTEGER")
        conn.execute("UPDATE dashboard_posts SET shares = 88, reach = 9000 WHERE account = 'alpha' AND shortcode = 'Recent'")
    research = client.get("/api/v1/posts?q=Public%20caption").json()["data"][0]
    assert research["metrics"]["shares"] == 88 and research["metrics"]["reach"] == 9000
    assert "metrics" not in client.get("/api/v1/hot").json()["data"][0]


def test_promoted_hashtags_csv_and_nullable_audio_metadata_survive_canonical_copy(setup):
    client, connect, _ = setup
    with connect() as conn:
        conn.execute("ALTER TABLE dashboard_posts ADD COLUMN mentions TEXT")
        conn.execute("ALTER TABLE dashboard_posts ADD COLUMN uses_original_audio INTEGER")
        conn.execute("ALTER TABLE dashboard_posts ADD COLUMN paid_partnership INTEGER")
        conn.execute("UPDATE dashboard_posts SET hashtags = 'ai, chatgpt', mentions = 'openai, alpha', uses_original_audio = 0, paid_partnership = 1 WHERE account = 'alpha' AND shortcode = 'Recent'")
    value = client.get("/api/v1/posts?q=Public%20caption").json()["data"][0]
    assert value["hashtags"] == ["ai", "chatgpt"]
    assert value["mentions"] == ["openai", "alpha"]
    assert value["uses_original_audio"] is False and value["paid_partnership"] is True


def test_stack_filters_dates_before_limit_and_reports_filtered_total(setup):
    client, connect, _ = setup
    with connect() as conn:
        conn.execute("INSERT INTO topic_stack_members VALUES ('alpha:Historic','shared','[]',1790856000)")
    response = client.get("/api/v1/research/stacks/shared/posts?to=2026-10-02&limit=1").json()
    assert [post["shortcode"] for post in response["data"]] == ["Historic"]
    assert response["pagination"]["total"] == 1
    assert response["pagination"]["next_offset"] is None


def test_stack_id_pagination_returns_all_permitted_members_across_accounts(setup):
    client, connect, queries = setup
    stack_id = '0123456789abcdef0123456789abcdef'
    with connect() as conn:
        conn.execute("UPDATE topic_stack_members SET stack_id = ? WHERE stack_id = 'shared'", (stack_id,))
        conn.execute("INSERT INTO topic_stack_members VALUES ('alpha:Historic',?,'[]',1790856000)", (stack_id,))
    queries.clear()
    offset = 0
    members = []
    while offset is not None:
        response = client.get(f'/api/v1/research/stacks/{stack_id}/posts?limit=1&offset={offset}',
                              headers={'x-accounts': 'alpha,beta'})
        assert response.status_code == 200
        page = response.json()
        assert page['stack_id'] == stack_id
        assert page['pagination']['total'] == 3
        members.extend((post['account'], post['shortcode']) for post in page['data'])
        offset = page['pagination']['next_offset']
    assert set(members) == {('alpha', 'Recent'), ('alpha', 'Historic'), ('beta', 'Foreign')}
    assert len(members) == 3
    assert not [query for query in queries if re_mutation(query)]


def test_tracker_monthly_costa_rica_boundary_and_missing_month_is_unknown(setup):
    client, connect, _ = setup
    with connect() as conn:
        conn.execute("INSERT INTO account_snapshots VALUES ('alpha', '2026-10-01T02:00:00+00:00', 90, 20, 'Public Alpha', 0, 1)")
        conn.execute("INSERT INTO account_snapshots VALUES ('alpha', '2026-07-31T12:00:00+00:00', 50, 20, 'Public Alpha', 0, 1)")
    value = client.get("/api/v1/tracker/accounts/alpha/monthly").json()["data"]
    assert [month["month"] for month in value] == ["2026-10", "2026-09", "2026-07"]
    assert value[0]["closing_followers"] == 150 and value[0]["net_followers"] == 60
    assert value[1]["net_followers"] is None
    assert client.get("/api/v1/tracker/accounts/beta/monthly").status_code == 404
    engagement = client.get("/api/v1/tracker/accounts/alpha/engagement?granularity=day").json()
    assert engagement["data"][0]["period"] == "2026-10-15"
    assert engagement["data"][0]["post_count"] == 2


def test_top_posts_and_follower_insights_are_scoped_bounded_reads(setup):
    client, _, queries = setup
    top = client.get("/api/v1/insights/top-posts?limit=1").json()
    assert top["data"][0]["shortcode"] == "Historic"
    response = client.get("/api/v1/insights/follower-growth?account=alpha")
    assert response.status_code == 200
    assert response.json()["coverage"]["lookback_days"] == 120
    assert response.json()["data"]["accounts"][0]["handle"] == "alpha"
    assert "CANARY" not in response.text
    assert client.get("/api/v1/insights/follower-growth?account=beta").status_code == 404
    assert client.get("/api/v1/insights/follower-growth?account=alpha,beta", headers={"x-accounts": "alpha,beta"}).status_code == 422
    assert not [query for query in queries if re_mutation(query)]


def test_media_exports_only_existing_safe_refs_and_streams_stored_bytes(setup, monkeypatch):
    client, connect, queries = setup
    from app import media_storage
    with connect() as conn:
        conn.execute("ALTER TABLE posts ADD COLUMN image_path TEXT")
        conn.execute("UPDATE posts SET image_path = 'r2://uploads/saved-cover.jpg', raw_json = ? WHERE shortcode = 'Recent'", (json.dumps({
            "shortCode": "Recent", "childPosts": [
                {"displayUrl": "https://cdn.example.com/slide.jpg"},
                {"videoUrl": "https://cdn.example.com/slide.mp4", "displayUrl": "https://cdn.example.com/poster.jpg"},
                {"displayUrl": "https://username:password@cdn.example.com/private.jpg"},
                {"displayUrl": "file:///CANARY_PRIVATE_PATH"}], "credentials": "CANARY_SECRET"}),))
    calls = []

    class ReadOnlyStorage:
        def get_object(self, **kwargs):
            calls.append(kwargs)
            return {"Body": io.BytesIO(b"SAVED_JPEG")}

        def __getattr__(self, name):
            pytest.fail(f"Read-only export attempted storage action {name}")

    monkeypatch.setattr(media_storage, "r2_enabled", lambda: True)
    monkeypatch.setattr(media_storage, "_client", lambda: ReadOnlyStorage())
    monkeypatch.setattr(media_storage, "R2_BUCKET", "test-bucket")
    queries.clear()
    response = client.get("/api/v1/research/posts/alpha/Recent/media")
    assert response.status_code == 200
    values = response.json()["data"]
    assert len(values) == 3 and values[0]["stored"] is True
    assert values[1]["source_url"] == "https://cdn.example.com/slide.jpg"
    assert values[2]["kind"] == "video" and values[2]["stored"] is False
    assert "CANARY" not in response.text and "r2://" not in response.text
    assert "username:password" not in response.text
    assert calls == []
    content = client.get(values[0]["download_url"])
    assert content.content == b"SAVED_JPEG" and content.headers["cache-control"] == "private, no-store"
    assert calls == [{"Bucket": "test-bucket", "Key": "uploads/saved-cover.jpg"}]
    assert client.get("/api/v1/research/posts/alpha/Recent/media/2").status_code == 404
    assert client.get("/api/v1/research/posts/beta/Foreign/media").status_code == 404
    assert not [query for query in queries if re_mutation(query)]
