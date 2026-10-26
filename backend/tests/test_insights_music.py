import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import insights_music as music, main


ACCOUNTS = [
    {"handle": "alpha", "group": "sentient", "is_canonical": True, "label": "Alpha", "hot_threshold": 600},
    {"handle": "beta", "group": "competitors", "is_canonical": False, "label": "Beta", "hot_threshold": 900},
]
NOW = datetime(2026, 10, 25, tzinfo=UTC)


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
