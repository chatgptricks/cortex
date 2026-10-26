import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import insights_music as music, main


ACCOUNTS = [
    {"handle": "alpha", "group": "sentient", "is_canonical": True, "label": "Alpha", "hot_threshold": 600},
    {"handle": "beta", "group": "competitors", "is_canonical": False, "label": "Beta", "hot_threshold": 900},
]
NOW = datetime(2026, 10, 25, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_music_cache(monkeypatch):
    monkeypatch.setattr(music, "_CACHE_ENTRY", None)
    monkeypatch.setattr(music, "_INFLIGHT", {})


@pytest.fixture
def stored(tmp_path, monkeypatch):
    path = tmp_path / "music.sqlite3"
    statements = []

    @contextmanager
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.set_trace_callback(statements.append)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    with connect() as conn:
        common = """id INTEGER PRIMARY KEY, shortcode TEXT, published_at TEXT, updated_at TEXT,
            enriched_at TEXT, likes INTEGER, comments INTEGER, post_type_label TEXT,
            product_type TEXT, video_views INTEGER, video_plays INTEGER, video_duration REAL,
            slide_count INTEGER, hashtags TEXT, hook_text TEXT, paid_partnership INTEGER,
            music_song TEXT, music_artist TEXT, music_audio_id TEXT,
            uses_original_audio INTEGER, is_hot INTEGER, hot_rate_multiplier REAL, permalink TEXT,
            hidden INTEGER DEFAULT 0, is_deleted INTEGER DEFAULT 0, is_animated INTEGER,
            likes_at_24h INTEGER, likes_at_48h INTEGER, raw_json TEXT"""
        conn.execute(f"CREATE TABLE dashboard_posts ({common}, account TEXT)")
        # The actual canonical table has no promoted music columns.
        conn.execute("""CREATE TABLE posts (id INTEGER PRIMARY KEY, section TEXT, shortcode TEXT,
            published_at TEXT, updated_at TEXT, likes INTEGER, comments INTEGER,
            post_type_label TEXT, hidden INTEGER DEFAULT 0, is_deleted INTEGER DEFAULT 0)""")
        conn.execute("CREATE TABLE engagement_observations (shortcode TEXT PRIMARY KEY, observed_at TEXT, raw_json TEXT)")
    monkeypatch.setattr(main, "connect", connect)
    monkeypatch.setattr(main, "list_accounts", lambda **kwargs: ACCOUNTS)
    statements.clear()
    return connect, statements


def add(conn, code, *, table="dashboard_posts", raw=None, **fields):
    values = {"shortcode": code, "published_at": "2026-10-01T12:00:00Z", "updated_at": "2026-10-02T12:00:00Z",
              "likes": 100, "comments": 5, "post_type_label": "Image"}
    values.update({"account": "beta", "raw_json": json.dumps(raw) if raw is not None else None} if table == "dashboard_posts" else {"section": "historical"})
    values.update(fields)
    conn.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()))


def observation(conn, code, raw, at="2026-10-03T12:00:00Z"):
    conn.execute("INSERT INTO engagement_observations VALUES (?, ?, ?)", (code, at, json.dumps(raw)))


def test_recovery_covers_canonical_carousel_and_exact_original_names(stored):
    connect, _ = stored
    with connect() as conn:
        add(conn, "canonical", table="posts", post_type_label="Carousel (carousel_container)")
        observation(conn, "canonical", {"shortCode": "canonical", "musicInfo": {
            "song_name": "Original audio — My exact title", "artist_name": "alpha",
            "audio_id": "99999999999999999999", "uses_original_audio": True},
            "likesCount": 760, "commentsCount": 30, "productType": "carousel_container"})
        add(conn, "photo", raw={"musicInfo": {"song_name": "Instant Crush", "artist_name": "Daft Punk",
                                              "audio_id": 1234, "uses_original_audio": False}})
        report = music.build_music_catalogue(conn, ACCOUNTS, now=NOW)
    posts = {p["sc"]: p for p in report["posts"]}
    assert posts["canonical"]["ms"] == "Original audio — My exact title"
    assert posts["canonical"]["ai"] == "99999999999999999999"
    assert posts["canonical"]["oa"] is True
    assert posts["canonical"]["t"] == "Carousel"
    assert posts["canonical"]["l"] == 760
    assert posts["canonical"]["aa"] == ["alpha"]
    assert posts["photo"]["ai"] == "1234" and posts["photo"]["oa"] is False
    assert posts["photo"]["ma"] == "Daft Punk"
    assert report["coverage"]["metadata_posts"] == 2


def test_unknown_metadata_remains_unknown_and_hidden_likes_do_not_resurrect(stored):
    connect, _ = stored
    with connect() as conn:
        add(conn, "unknown", likes=900)
        observation(conn, "unknown", {"shortCode": "unknown", "likesCount": None})
        add(conn, "original", music_song="Original audio", music_audio_id="101", uses_original_audio=1)
        add(conn, "other-original", music_song="Original audio", music_audio_id="102", uses_original_audio=1)
        add(conn, "false-only", uses_original_audio=0)
        report = music.build_music_catalogue(conn, ACCOUNTS, now=NOW)
        posts = {p["sc"]: p for p in report["posts"]}
    assert posts["unknown"]["oa"] is None and posts["unknown"]["l"] is None
    assert posts["unknown"]["ai"] is None
    assert posts["original"]["ai"] != posts["other-original"]["ai"]
    assert posts["original"]["ms"] == posts["other-original"]["ms"] == "Original audio"
    assert posts["false-only"]["oa"] is False
    assert report["coverage"]["unknown_audio_posts"] == 2


def test_duplicates_count_once_keep_all_accounts_and_propagate_curation(stored):
    connect, _ = stored
    with connect() as conn:
        add(conn, "collab", table="posts", likes=300, updated_at="2026-10-04T00:00:00Z")
        add(conn, "collab", music_song="Song", music_artist="Artist", music_audio_id="333")
        add(conn, "collab", account="alpha", likes=200)
        add(conn, "hidden", table="posts", hidden=1)
        add(conn, "hidden", music_audio_id="444")
        add(conn, "deleted", is_deleted=1)
        add(conn, "future", published_at="2027-01-01")
        add(conn, "ab", table="posts", section="ab")
        add(conn, "outside-scope", account="not-visible")
        result = music.build_music_catalogue(conn, ACCOUNTS, now=NOW)
    assert len(result["posts"]) == 1
    post = result["posts"][0]
    assert post["sc"] == "collab" and post["aa"] == ["alpha", "beta"]
    assert post["l"] == 300 and post["ai"] == "333"


def test_all_time_keeps_undated_real_posts_but_not_unpublished_uploads(stored):
    connect, _ = stored
    with connect() as conn:
        add(conn, "undated", table="posts", published_at=None)
        add(conn, None, table="posts", section="single", published_at=None)
        add(conn, "malformed", raw=None)
        conn.execute("UPDATE dashboard_posts SET raw_json = '{bad JSON' WHERE shortcode = 'malformed'")
        result = music.build_music_catalogue(conn, ACCOUNTS, now=NOW)
    posts = {p["sc"]: p for p in result["posts"]}
    assert set(posts) == {"undated", "malformed"}
    assert posts["undated"]["d"] is None
    assert posts["malformed"]["oa"] is None


def test_audio_identity_changes_never_mix_old_title_and_new_id(stored):
    connect, _ = stored
    with connect() as conn:
        add(conn, "changed", music_song="Old song", music_artist="Old artist", music_audio_id="old")
        observation(conn, "changed", {"shortCode": "changed", "musicInfo": {"audio_id": "new", "uses_original_audio": True}})
        post = music.build_music_catalogue(conn, ACCOUNTS, now=NOW)["posts"][0]
    assert post["ai"] == "new" and post["ms"] is None and post["ma"] is None


def test_new_title_without_id_cannot_inherit_an_older_exact_audio(stored):
    connect, _ = stored
    with connect() as conn:
        add(conn, "changed", music_song="Old song", music_artist="Old artist", music_audio_id="old")
        observation(conn, "changed", {"shortCode": "changed", "musicInfo": {"song_name": "Replacement", "artist_name": "New artist", "uses_original_audio": False}})
        post = music.build_music_catalogue(conn, ACCOUNTS, now=NOW)["posts"][0]
    assert post["ms"] == "Replacement" and post["ma"] == "New artist"
    assert post["ai"] is None and post["oa"] is False


@pytest.mark.parametrize("placeholder", [0, "0", " 0 ", "000", "0.0", "-1", -5, None])
def test_zero_audio_ids_are_unknown_not_a_shared_sound(stored, placeholder):
    connect, _ = stored
    with connect() as conn:
        add(conn, "placeholder", music_audio_id=placeholder,
            raw={"musicInfo": {"audio_id": placeholder, "uses_original_audio": False}})
        report = music.build_music_catalogue(conn, ACCOUNTS, now=NOW)
    assert report["posts"][0]["ai"] is None
    assert report["coverage"]["audio_id_posts"] == 0
    assert report["coverage"]["unknown_audio_posts"] == 1


@pytest.mark.parametrize("placeholder", ["0", "000", "-1", "null"])
def test_zero_promoted_audio_id_can_recover_real_id_from_stored_payload(stored, placeholder):
    connect, _ = stored
    with connect() as conn:
        add(conn, "recover", music_song="Song", music_artist="Artist", music_audio_id=placeholder,
            uses_original_audio=0, product_type="clips", raw={"musicInfo": {"audio_id": "12345"}})
        post = music.build_music_catalogue(conn, ACCOUNTS, now=NOW)["posts"][0]
    assert post["ai"] == "12345"


def test_read_is_batched_read_only_and_never_transfers_media_payload(stored):
    connect, statements = stored
    with connect() as conn:
        for i in range(450):
            add(conn, f"code-{i}", raw={"musicInfo": {"song_name": "Song"}, "displayUrl": "CANARY_HEAVY_MEDIA" * 500})
        statements.clear()
        rows = music._read_source(conn, "dashboard_posts", music._columns(conn, "dashboard_posts"))
        assert "CANARY_HEAVY_MEDIA" not in json.dumps(rows)
        result = music.build_music_catalogue(conn, ACCOUNTS, now=NOW)
    assert len(result["posts"]) == 450
    queries = [q for q in statements if q.lstrip().upper().startswith("SELECT")]
    assert len(queries) <= 6
    assert not any(q.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for q in statements)


def test_scope_filters_precede_large_json_projection(stored):
    connect, statements = stored
    with connect() as conn:
        add(conn, "visible", raw={"musicInfo": {"song_name": "Song"}})
        add(conn, "excluded", account="outside", raw={"musicInfo": {"song_name": "Secret"}})
        add(conn, "ab", table="posts", section="ab")
        statements.clear()
        result = music.build_music_catalogue(conn, ACCOUNTS, now=NOW)
    assert [p["sc"] for p in result["posts"]] == ["visible"]
    source_queries = [q for q in statements if q.startswith("SELECT") and "music_rows" in q]
    assert any("FROM dashboard_posts WHERE LOWER(account) IN (" in q for q in source_queries)
    assert any("FROM posts WHERE COALESCE(section, '') <> 'ab'" in q for q in source_queries)


def test_dashboard_parse_skip_keeps_all_recoverable_music_and_metric_fields(stored, monkeypatch):
    connect, _ = stored
    with connect() as conn:
        add(conn, "plain", raw={"displayUrl": "HEAVY" * 1000, "childPosts": []}, product_type="feed")
        add(conn, "raw-metrics", raw={"productType": "clips", "videoViewCount": 300,
                                      "videoPlayCount": 400, "videoDuration": 1.5})
        add(conn, "escaped")
        conn.execute("UPDATE dashboard_posts SET raw_json = ? WHERE shortcode = 'escaped'", ('{"\\u006dusicInfo":{"song_name":"Recovered"}}',))
        rows = {r["shortcode"]: r for r in music._read_source(conn, "dashboard_posts", music._columns(conn, "dashboard_posts"))}
        optimized = music.build_music_catalogue(conn, ACCOUNTS, now=NOW)
        monkeypatch.setattr(music, "_dashboard_raw_needed", lambda *args: "1 = 1")
        assert music.build_music_catalogue(conn, ACCOUNTS, now=NOW) == optimized
        report = {p["sc"]: p for p in optimized["posts"]}
    assert rows["plain"]["r_song"] is None and rows["plain"]["r_product"] is None
    assert report["raw-metrics"]["pt"] == "clips" and report["raw-metrics"]["v"] == 300
    assert report["raw-metrics"]["pl"] == 400 and report["raw-metrics"]["dur"] == 1.5
    assert report["escaped"]["ms"] == "Recovered"


def test_route_requires_auth_and_does_not_change_overview_route(stored, monkeypatch):
    connect, _ = stored
    with connect() as conn:
        add(conn, "Sample", music_song="Song", music_artist="Artist", music_audio_id="123")
    monkeypatch.setattr(main, "FIREBASE_APP", object())
    monkeypatch.setattr(main.firebase_auth, "verify_id_token", lambda token: {"email": "admin@example.com", "uid": "test"})
    monkeypatch.setattr(main, "get_dashboard_user_access", lambda email: {"is_admin": True, "operating_role": "admin", "operating_roles": '["admin"]'})
    monkeypatch.setattr(main, "log_usage_event", lambda *args: None)
    app = FastAPI()
    app.middleware("http")(main._require_firebase_user)
    app.add_api_route("/api/insights/music", main.insights_music)
    client = TestClient(app)
    overview_before = main.insights_posts()
    assert client.get("/api/insights/music").status_code == 401
    response = client.get("/api/insights/music", headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    assert response.json()["posts"][0]["ai"] == "123"
    assert main.insights_posts() == overview_before
    assert "ai" not in overview_before["posts"][0]
    monkeypatch.setattr(main, "get_dashboard_user_access", lambda email: None)
    assert client.get("/api/insights/music", headers={"Authorization": "Bearer test"}).status_code == 403


def test_malformed_postgres_json_isolated_without_fetching_full_payloads():
    class BadJSON(Exception):
        sqlstate = "22P02"

    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Postgres:
        is_postgres = True

        def __init__(self):
            self.queries = []

        def execute(self, query, params=()):
            self.queries.append(query)
            if not query.startswith("SELECT"):
                return Cursor([])
            take, offset = params[-2:]
            if offset > 1:
                return Cursor([])
            if offset == 0 and "music_json" in query:
                raise BadJSON()
            if offset == 0:
                return Cursor([{"id": 1, "shortcode": "bad", "r_song": None}])
            return Cursor([{"id": 2, "shortcode": "good", "r_song": "Song"}][:take])

    conn = Postgres()
    result = music._read_source(conn, "dashboard_posts", {"id", "shortcode", "raw_json"})
    assert [r["shortcode"] for r in result] == ["bad", "good"]
    assert result[0]["r_song"] is None and result[1]["r_song"] == "Song"
    assert "ROLLBACK TO SAVEPOINT insights_music_json" in conn.queries
    assert all("raw_json" not in q.split(" FROM ")[0] for q in conn.queries if q.startswith("SELECT"))


def test_cache_ttl_preserves_source_timestamp_and_recomputes_after_expiry(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(music, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []
    def load():
        calls.append(1)
        return {"posts": [], "coverage": {"generated_at": f"read-{len(calls)}"}}
    first = music.cached_music_catalogue(ACCOUNTS, load, database_identity="example-db")
    clock[0] += 299
    assert music.cached_music_catalogue(ACCOUNTS, load, database_identity="example-db") is first
    assert first["coverage"]["generated_at"] == "read-1" and len(calls) == 1
    clock[0] += 1
    second = music.cached_music_catalogue(ACCOUNTS, load, database_identity="example-db")
    assert second["coverage"]["generated_at"] == "read-2" and len(calls) == 2


def test_cache_invalidates_for_roster_and_database_identity():
    calls = []
    def load():
        calls.append(1)
        return {"revision": len(calls)}
    assert music.cached_music_catalogue(ACCOUNTS, load, database_identity="first-db")["revision"] == 1
    changed = [{**a, "label": "Changed"} for a in ACCOUNTS]
    assert music.cached_music_catalogue(changed, load, database_identity="first-db")["revision"] == 2
    assert music.cached_music_catalogue(changed, load, database_identity="second-db")["revision"] == 3
    assert len(calls) == 3
    assert "second-db" not in repr(music._CACHE_ENTRY[0])


def test_concurrent_cache_reads_share_one_calculation():
    ready = threading.Barrier(9)
    started, release = threading.Event(), threading.Event()
    calls = []
    def load():
        calls.append(1)
        started.set()
        assert release.wait(3)
        return {"posts": [], "coverage": {"generated_at": "same-read"}}
    def read():
        ready.wait(timeout=3)
        return music.cached_music_catalogue(ACCOUNTS, load, database_identity="shared-db")
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(read) for _ in range(8)]
        ready.wait(timeout=3)
        assert started.wait(3)
        release.set()
        results = [future.result(timeout=3) for future in pending]
    assert len(calls) == 1
    assert all(result is results[0] for result in results)
    assert music._INFLIGHT == {}


def test_failed_cache_calculation_does_not_poison_next_read():
    def fail():
        raise RuntimeError("temporary storage failure")
    with pytest.raises(RuntimeError, match="temporary storage failure"):
        music.cached_music_catalogue(ACCOUNTS, fail, database_identity="shared-db")
    assert music._INFLIGHT == {} and music._CACHE_ENTRY is None
    result = music.cached_music_catalogue(ACCOUNTS, lambda: {"posts": []}, database_identity="shared-db")
    assert result == {"posts": []}


def test_music_route_cache_rechecks_roster_and_avoids_reopening_catalogue_connection(stored, monkeypatch):
    connect, _ = stored
    with connect() as conn:
        add(conn, "first", music_song="Song")
    first = main.insights_music()
    def forbidden_connection():
        raise AssertionError("A warm music catalogue should not reopen its data connection")
    monkeypatch.setattr(main, "connect", forbidden_connection)
    assert main.insights_music() is first
    monkeypatch.setattr(main, "list_accounts", lambda **kwargs: ACCOUNTS[:1])
    with pytest.raises(AssertionError, match="warm music catalogue"):
        main.insights_music()
