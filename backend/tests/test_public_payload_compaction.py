from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from app import account_media_kit as kit, db
from app.postgres import _sql
from app.public_media_kit import project_public_media_kit


@pytest.fixture
def stored(tmp_path, monkeypatch):
    path = tmp_path / "public-compact.sqlite3"
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
        conn.executescript("""
            CREATE TABLE accounts (handle TEXT PRIMARY KEY, is_canonical INTEGER, label TEXT,
                is_active INTEGER, group_name TEXT, created_at TEXT, scrape_mode TEXT);
            INSERT INTO accounts VALUES ('alpha',1,'Alpha',1,'sentient','2026-01-01T00:00:00Z','posts');
            CREATE TABLE dashboard_posts (id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT,
                published_at TEXT, updated_at TEXT, enriched_at TEXT, caption TEXT,
                post_type_label TEXT, product_type TEXT, likes INTEGER, comments INTEGER,
                is_promo INTEGER, hidden INTEGER, is_deleted INTEGER, raw_json TEXT);
            CREATE TABLE posts (id INTEGER PRIMARY KEY, section TEXT, shortcode TEXT,
                published_at TEXT, updated_at TEXT, caption TEXT, likes INTEGER, comments INTEGER,
                is_promo INTEGER, hidden INTEGER, is_deleted INTEGER, raw_json TEXT);
            CREATE TABLE engagement_observations (shortcode TEXT PRIMARY KEY, observed_at TEXT, raw_json TEXT);
            CREATE TABLE account_snapshots (handle TEXT, captured_at TEXT, followers_count INTEGER,
                posts_count INTEGER, full_name TEXT, private INTEGER, verified INTEGER);
            INSERT INTO account_snapshots VALUES ('alpha','2026-10-01T12:00:00Z',1000,3,'Alpha',0,1);
        """)
    monkeypatch.setattr(db, "connect", connect)
    return connect, statements


def payload():
    heavy = "CANARY_UNUSED_MEDIA" * 5000
    return {
        "shortCode": "Example", "type": "Sidecar", "productType": "carousel_container",
        "ownerUsername": "alpha", "owner": {"username": "alpha"},
        "coauthorProducers": [{"username": "alpha"}, {"username": "openai"}],
        "likesCount": 50, "commentsCount": None, "videoViewCount": 700,
        "customWatchTime": 4.5, "metrics": {"sharesCount": 12, "video": {"retentionRate": 0.4}},
        "insights": {"reachCount": 888, "statistics": {"savesCount": 11}},
        "musicInfo": {"song_name": "Song", "artist_name": "Artist", "uses_original_audio": False},
        "hashtags": ["ai", "chatgpt"], "mentions": ["openai"], "taggedUsers": [{"username": "someone"}],
        "isPaidPartnership": True, "displayUrl": heavy, "images": [heavy],
        "displayResources": [{"src": heavy}], "latestComments": [{"text": heavy}],
        "childPosts": [
            {"type": "Video", "likesCount": 3, "commentsCount": 1, "videoViewCount": 20,
             "videoDuration": 1.5, "metrics": {"completionRate": 0.5},
             "displayUrl": heavy, "videoUrl": heavy, "video_versions": [{"url": heavy}],
             "comments": [{"text": heavy}], "latestComments": [{"text": heavy}]},
            {"type": "Image", "like_count": 8, "videoPlayCount": 30, "videoDuration": 2.5,
             "statistics": {"completionRate": 0.9}, "images": [heavy],
             # A nested child remains fully intact: no supported recursive
             # measurement can disappear because of the transport projection.
             "childPosts": [{"type": "Video", "likesCount": 1, "videoViewCount": 2}]},
            None, "legacy-entry", True, False, 7, ["legacy-array"],
        ],
    }


def add(conn, code="Example", raw=None, likes=None, at="2026-10-08T12:00:00Z"):
    conn.execute("""INSERT INTO dashboard_posts (account, shortcode, published_at, updated_at, enriched_at,
        caption, post_type_label, product_type, likes, comments, is_promo, hidden, is_deleted, raw_json)
        VALUES ('alpha', ?, '2026-10-01T12:00:00Z', ?, ?, 'Caption #aitoolsentient', 'Carousel',
            'carousel_container', ?, 5, 0, 0, 0, ?)""", (code, at, at, likes, json.dumps(raw) if raw is not None else None))


def test_public_compaction_removes_heavy_media_and_preserves_all_normalized_facts(stored):
    connect, statements = stored
    raw = payload()
    with connect() as conn:
        add(conn, raw=raw)
        original = kit._post_rows(conn, "dashboard_posts", "alpha", True)
        compact = kit._post_rows(conn, "dashboard_posts", "alpha", True, compact_public=True)
        normalized, _ = kit._normalize_posts(original, {}, "alpha")
        projected, _ = kit._normalize_posts(compact, {}, "alpha")
        assert projected == normalized
        assert kit._raw_metrics(json.loads(compact[0]["raw_json"])) == kit._raw_metrics(raw)
        assert len(compact[0]["raw_json"]) < len(original[0]["raw_json"]) / 100
        assert "CANARY_UNUSED_MEDIA" not in compact[0]["raw_json"]
        assert projected[0]["metrics"]["slide_count"] == 8
        assert projected[0]["metrics"]["carousel_slide_likes"] == 11
        assert projected[0]["_public_collaboration"]["collaborators"] == ["openai"]
    assert all(not query.lstrip().upper().startswith(("UPDATE", "DELETE", "ALTER", "CREATE")) for query in statements[statements.index(next(query for query in statements if "json_remove" in query)):])


@pytest.mark.parametrize("raw", [None, {}, [], [1], "text", 4, True])
def test_unusable_or_empty_raw_payloads_keep_original_normalization(stored, raw):
    connect, _ = stored
    with connect() as conn:
        add(conn, raw=raw, likes=7)
        original = kit._post_rows(conn, "dashboard_posts", "alpha", True)
        compact = kit._post_rows(conn, "dashboard_posts", "alpha", True, compact_public=True)
    assert kit._normalize_posts(compact, {}, "alpha") == kit._normalize_posts(original, {}, "alpha")


def test_malformed_serialized_json_remains_unknown_without_breaking_compact_sql(stored):
    connect, _ = stored
    with connect() as conn:
        add(conn, likes=7)
        conn.execute("UPDATE dashboard_posts SET raw_json = '{broken JSON' WHERE shortcode = 'Example'")
        original = kit._post_rows(conn, "dashboard_posts", "alpha", True)
        compact = kit._post_rows(conn, "dashboard_posts", "alpha", True, compact_public=True)
    assert kit._normalize_posts(compact, {}, "alpha") == kit._normalize_posts(original, {}, "alpha")


def test_compact_observations_preserve_newer_hidden_counts_nested_aliases_and_collabs(stored):
    connect, _ = stored
    with connect() as conn:
        add(conn, raw=payload(), likes=55)
        observation = {**payload(), "likesCount": None, "commentsCount": 44, "metrics": {"videoViewCount": 3333},
                       "coauthorProducers": [], "childPosts": []}
        conn.execute("INSERT INTO engagement_observations VALUES ('Example','2026-10-09T12:00:00Z',?)", (json.dumps(observation),))
        rows = kit._post_rows(conn, "dashboard_posts", "alpha", True)
        plain = dict(conn.execute("SELECT * FROM engagement_observations").fetchone())
        compact = kit._public_observation_rows(conn, ["Example"])[0]
        assert kit._normalize_posts(rows, {"Example": plain}, "alpha") == kit._normalize_posts(rows, {"Example": compact}, "alpha")
        result = kit._normalize_posts(rows, {"Example": compact}, "alpha")[0][0]
        assert result["metrics"]["likes"] is None and result["metrics"]["comments"] == 44
        assert result["_public_collaboration"]["is_collab"] is False


def test_strict_public_report_matches_uncompacted_projection_and_internal_path_stays_raw(stored, monkeypatch):
    connect, statements = stored
    with connect() as conn:
        add(conn, raw=payload(), likes=70)
        conn.execute("INSERT INTO engagement_observations VALUES ('Example','2026-10-09T12:00:00Z',?)", (json.dumps({**payload(), "likesCount": 90}),))
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    optimized = kit.build_account_media_kit("alpha", now=now, strict_public_exclusions=True)
    original_reader = kit._compact_public_rows

    def plain(conn, table, selected, where, parameters, *, order):
        return [dict(row) for row in conn.execute(f"SELECT {','.join(selected)} FROM {table}{where} ORDER BY {order}", parameters).fetchall()]

    monkeypatch.setattr(kit, "_compact_public_rows", plain)
    baseline = kit.build_account_media_kit("alpha", now=now, strict_public_exclusions=True)
    assert optimized == baseline
    assert project_public_media_kit(optimized) == project_public_media_kit(baseline)
    monkeypatch.setattr(kit, "_compact_public_rows", original_reader)
    statements.clear()
    kit.build_account_media_kit("alpha", now=now)
    assert not any("json_remove" in query or "public_payload" in query for query in statements)


def test_opt_in_compaction_batches_without_skipping_or_changing_internal_reader(stored):
    connect, _ = stored
    with connect() as conn:
        for index in range(405):
            add(conn, code=f"Code{index}", raw={"likesCount": index, "images": ["UNUSED" * 100]})
        original = kit._post_rows(conn, "dashboard_posts", "alpha", True)
        compact = kit._post_rows(conn, "dashboard_posts", "alpha", True, compact_public=True)
    assert len(original) == len(compact) == 405
    assert kit._normalize_posts(original, {}, "alpha") == kit._normalize_posts(compact, {}, "alpha")
    assert "UNUSED" in original[0]["raw_json"] and "UNUSED" not in compact[0]["raw_json"]


def test_postgres_bad_json_isolated_without_transferring_unbounded_payloads():
    class BadJSON(ValueError):
        sqlstate = "22P02"

    class Connection:
        is_postgres = True

        def __init__(self):
            self.statements = []
            self.rows = []

        def execute(self, query, params=None):
            self.statements.append((query, params))
            if params is None:
                return self
            assert _sql(query).count("%s") == len(params)
            take, offset = params[-2:]
            # One malformed legacy row between two valid payloads.
            selected = [{"shortcode": "GoodA", "raw_json": '{"likesCount":2}'},
                        {"shortcode": "Bad", "raw_json": None},
                        {"shortcode": "GoodB", "raw_json": '{"likesCount":3}'}][offset:offset + take]
            if "public_payload" in query and any(row["shortcode"] == "Bad" for row in selected):
                raise BadJSON("invalid serialized JSON")
            self.rows = selected
            return self

        def fetchall(self):
            return self.rows

    conn = Connection()
    result = kit._compact_public_rows(conn, "posts", ["shortcode", "raw_json"], " WHERE shortcode <> ?", [""], order="shortcode")
    assert [row["shortcode"] for row in result] == ["GoodA", "Bad", "GoodB"]
    fallback = [(query, params) for query, params in conn.statements if "NULL AS raw_json" in query]
    assert len(fallback) == 1 and fallback[0][1][-2] == 1
    assert all("LIMIT ? OFFSET ?" in query for query, params in conn.statements if params is not None)
    assert conn.statements[-1][0] == "RELEASE SAVEPOINT public_payload_json"
