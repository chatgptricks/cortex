import json
import sqlite3
from contextlib import contextmanager
import hashlib

import pytest

from app import apify_sync, db, engagement_refresh, main, research_collaboration as collab
from app.postgres import _sql
from starlette.requests import Request


@pytest.fixture
def catalogue(monkeypatch, tmp_path):
    path = tmp_path / "collaboration.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY, title TEXT, caption TEXT, hook_text TEXT, published_at TEXT,
                likes INTEGER, comments INTEGER, post_type_label TEXT, shortcode TEXT, image_path TEXT,
                is_animated INTEGER, source_row_number INTEGER, created_at TEXT, section TEXT,
                is_hot INTEGER, hot_rate_multiplier REAL, is_promo INTEGER, hidden INTEGER,
                is_deleted INTEGER, updated_at TEXT, coauthors TEXT, enriched_at TEXT, raw_json TEXT
            );
            CREATE TABLE dashboard_posts (
                id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT, published_at TEXT, likes INTEGER,
                comments INTEGER, caption TEXT, post_type_label TEXT, is_animated INTEGER, permalink TEXT,
                is_hot INTEGER, hot_rate_multiplier REAL, hook_text TEXT, music_song TEXT,
                music_artist TEXT, music_audio_id TEXT, uses_original_audio INTEGER, is_promo INTEGER,
                hidden INTEGER, is_deleted INTEGER, transcript TEXT, updated_at TEXT,
                coauthors TEXT, enriched_at TEXT, raw_json TEXT
            );
            CREATE TABLE engagement_observations (shortcode TEXT PRIMARY KEY, observed_at TEXT, raw_json TEXT);
            CREATE TABLE queue_requests (
                id INTEGER PRIMARY KEY, post_account TEXT, post_shortcode TEXT, status TEXT,
                designer_email TEXT, coordinator_email TEXT, production_points INTEGER,
                actual_started_at TEXT, completed_at TEXT, final_permalink TEXT, final_permalinks TEXT
            );
        """)

    @contextmanager
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(main, "connect", connect)
    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(main, "list_accounts", lambda active_only=False: [
        {"handle": "chatgptricks", "group": "sentient"},
        {"handle": "openai", "group": "competitors"},
    ])
    monkeypatch.setattr(main, "_dashboard_catalogue_decoration", lambda revision: {
        "group_by_handle": {"chatgptricks": "sentient", "openai": "competitors"},
        "canonical": {"handle": "chatgptricks"}, "queue_by_source": {}, "queue_by_final": {},
        "stack_by_post": {}, "stack_sizes": {},
    })
    return connect


def add(connect, code="Sample", *, table="posts", account="chatgptricks", raw=None,
        coauthors=None, updated="2026-10-01T00:00:00Z", enriched=None):
    fields = {"shortcode": code, "published_at": "2026-09-01", "likes": 10, "comments": 1,
              "caption": "Mentions @openai #aitoolsentient", "post_type_label": "Image",
              "is_promo": 1, "updated_at": updated, "enriched_at": enriched,
              "coauthors": coauthors, "raw_json": json.dumps(raw) if raw is not None else None}
    if table == "posts":
        fields.update(title="Sample", image_path="cover.jpg", section="historical")
    else:
        fields["account"] = account
    with connect() as conn:
        return conn.execute(f"INSERT INTO {table} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", list(fields.values())).lastrowid


def observe(connect, raw, code="Sample", at="2026-10-09T00:00:00Z"):
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO engagement_observations VALUES (?, ?, ?)", (code, at, json.dumps(raw)))


@pytest.mark.parametrize(("raw", "coauthors", "status", "partners"), [
    ({"coauthorProducers": [{"username": "@OpenAI", "id": "CANARY"}, "openai"]}, None, True, ["openai"]),
    ({"coauthorProducers": []}, "openai", False, []),
    ({"coauthorProducers": None}, "openai", None, []),
    ({"taggedUsers": [{"username": "openai"}], "mentions": ["openai"]}, None, None, []),
    ({"ownerUsername": "OpenAI", "coauthorProducers": ["chatgptricks"]}, None, True, ["openai"]),
    ({"ownerUsername": "chatgptricks", "coauthorProducers": ["chatgptricks"]}, None, False, []),
    ({"coauthorProducers": ["chatgptricks"]}, None, None, []),
    ({}, "@OPENAI, openai, other", True, ["openai", "other"]),
])
def test_source_pages_and_legacy_feed_share_sanitized_collaboration(catalogue, raw, coauthors, status, partners):
    add(catalogue, raw=raw, coauthors=coauthors)
    add(catalogue, table="dashboard_posts", account="openai", raw=raw, coauthors=coauthors)
    canonical = main._dashboard_catalogue_page("canonical", 0, 10, "test")
    dashboard = main._dashboard_catalogue_page("dashboard", 0, 10, "test")
    legacy = main._dashboard_posts_payload()["posts"]
    assert canonical[0]["isCollab"] is status
    assert canonical[0]["collaborators"] == partners
    by_key = {(post["account"], post["shortcode"]): post for post in legacy}
    for post in canonical + dashboard:
        other = by_key[(post["account"], post["shortcode"])]
        assert (other["isCollab"], other["collaborators"]) == (post["isCollab"], post["collaborators"])
        assert not any(key in post for key in ("raw_json", "coauthors", "_public_collaboration", "coauthorProducers"))
        assert "CANARY" not in json.dumps(post)


def test_duplicate_metadata_is_resolved_before_feed_deduplication_and_cursor_bounds(catalogue):
    first = add(catalogue, raw={"coauthorProducers": ["openai"]})
    add(catalogue, raw={"taggedUsers": ["foreign"]}, updated="2026-10-08T00:00:00Z")
    add(catalogue, table="dashboard_posts", raw={"coauthorProducers": None}, updated="2026-10-09T00:00:00Z")
    add(catalogue, table="dashboard_posts", account="openai", raw={"coauthorProducers": ["CANARY_FOREIGN_ACCOUNT"]}, updated="2026-10-10T00:00:00Z")
    page = main._dashboard_catalogue_page("canonical", 0, 1, "test", after_id=0, until_id=first, cursor_metadata=True)
    assert page[1] == first and len(page[0]) == 1
    assert page[0][0]["isCollab"] is True
    assert page[0][0]["collaborators"] == ["openai"]
    legacy = main._dashboard_posts_payload()["posts"]
    mine = [post for post in legacy if post["account"] == "chatgptricks"]
    assert len(mine) == 1 and mine[0]["collaborators"] == ["openai"]
    # New explicit metadata can override positives, including on another
    # matching storage copy, even when that copy is outside this ID page.
    add(catalogue, table="dashboard_posts", raw={"coauthorProducers": []}, enriched="2026-10-11T00:00:00Z")
    assert main._dashboard_catalogue_page("canonical", 0, 1, "test")[0]["isCollab"] is False


def test_equal_timestamp_duplicate_metadata_uses_public_api_quality_tiebreak(catalogue):
    add(catalogue, raw={"coauthorProducers": ["openai"]})
    add(catalogue, raw={"coauthorProducers": []})
    page = main._dashboard_catalogue_page("canonical", 0, 10, "test")
    assert all(post["isCollab"] is False for post in page)
    assert main._dashboard_posts_payload()["posts"][0]["isCollab"] is False


@pytest.mark.parametrize(("raw", "status", "partners"), [
    ({"shortCode": "Sample", "coauthorProducers": []}, False, []),
    ({"shortCode": "Foreign", "coauthorProducers": []}, True, ["openai"]),
    ({"shortcode": "Foreign", "coauthorProducers": []}, True, ["openai"]),
    ({"shortCode": "Sample", "coauthorProducers": None}, True, ["openai"]),
    ({"shortCode": "Sample", "likesCount": 200}, True, ["openai"]),
])
def test_newest_usable_observation_is_checked_against_selected_post(catalogue, raw, status, partners):
    add(catalogue, raw={"coauthorProducers": ["openai"]})
    observe(catalogue, raw)
    post = main._dashboard_catalogue_page("canonical", 0, 10, "test")[0]
    assert (post["isCollab"], post["collaborators"]) == (status, partners)


def test_compact_queries_preserve_semantics_and_exclude_unrelated_media(catalogue):
    add(catalogue, raw={"owner": {"username": "openai", "profileUrl": "CANARY"},
                        "coauthorProducers": ["chatgptricks"], "childPosts": ["CANARY" * 10000]})
    with catalogue() as conn:
        columns = collab._columns(conn, "posts")
        row = collab._metadata_rows(conn, "posts", columns, "shortcode = ?", ["Sample"])[0]
        assert len(row["raw_json"]) < 250 and "CANARY" not in row["raw_json"]
        assert collab.collaboration_by_post(conn, [{"account": "chatgptricks", "shortcode": "Sample"}])[
            ("chatgptricks", "Sample")] == {"isCollab": True, "collaborators": ["openai"]}
    with catalogue() as conn:
        conn.execute("UPDATE posts SET raw_json = 'malformed', coauthors = 'openai'")
    assert main._dashboard_catalogue_page("canonical", 0, 1, "test")[0]["collaborators"] == ["openai"]


def test_metadata_projection_never_mutates_rows_or_schema_and_batches_large_pages(catalogue):
    with catalogue() as conn:
        conn.executemany("INSERT INTO dashboard_posts (account, shortcode, coauthors) VALUES ('openai', ?, 'chatgptricks')",
                         [(f"Code{index}",) for index in range(405)])
        before = conn.total_changes
        statements = []
        conn.set_trace_callback(statements.append)
        posts = [{"account": "openai", "shortcode": f"Code{index}"} for index in range(405)]
        collab.attach_collaboration(conn, posts)
        assert conn.total_changes == before
        assert all(post["isCollab"] is True and post["collaborators"] == ["chatgptricks"] for post in posts)
        assert not any(statement.lstrip().upper().startswith(("CREATE", "ALTER", "INSERT", "UPDATE", "DELETE")) for statement in statements)
        assert all(" WHERE " in statement for statement in statements if statement.lstrip().upper().startswith("SELECT"))


def test_metrics_metadata_only_updates_are_account_relative_and_keep_cursor_shape(catalogue):
    add(catalogue)
    add(catalogue, table="dashboard_posts", account="openai")
    observe(catalogue, {"shortCode": "Sample", "likesCount": 10, "commentsCount": 1, "coauthorProducers": []}, at="2026-10-08T00:00:00Z")
    first = engagement_refresh.metric_updates(limit=1)
    assert first["updates"][0]["likes"] == 10 and first["hasMore"] is False
    assert all(post["isCollab"] is False for post in first["collaborationUpdates"])
    observe(catalogue, {"shortCode": "Sample", "likesCount": 10, "commentsCount": 1,
                        "ownerUsername": "openai", "coauthorProducers": ["chatgptricks"]})
    next_page = engagement_refresh.metric_updates(first["cursor"]["at"], first["cursor"]["code"], limit=1)
    assert next_page["updates"][0]["likes"] == 10
    assert next_page["collaborationUpdates"] == [
        {"account": "chatgptricks", "shortcode": "Sample", "isCollab": True, "collaborators": ["openai"]},
        {"account": "openai", "shortcode": "Sample", "isCollab": True, "collaborators": ["chatgptricks"]},
    ]
    assert next_page["cursor"] == {"at": "2026-10-09T00:00:00Z", "code": "Sample"}
    assert engagement_refresh.metric_updates(next_page["cursor"]["at"], next_page["cursor"]["code"])["collaborationUpdates"] == []


def test_postgres_projection_preserves_parameter_count_and_recovers_bad_json():
    class BadJson(ValueError):
        sqlstate = "22P02"

    class Connection:
        is_postgres = True

        def __init__(self):
            self.statements = []

        def execute(self, query, params=None):
            self.statements.append(query)
            if "jsonb_build_object" in query:
                assert _sql(query).count("%s") == len(params)
                raise BadJson("malformed legacy payload")
            return self

        def fetchall(self):
            return [{"shortcode": "Sample", "raw_json": "malformed"}]

    conn = Connection()
    rows = collab._metadata_rows(conn, "posts", {"shortcode", "raw_json"}, "shortcode = ?", ["Sample"])
    assert rows[0]["raw_json"] == "malformed"
    assert conn.statements[-1] == "RELEASE SAVEPOINT research_collaboration_json"
    assert any(query.startswith("ROLLBACK TO") for query in conn.statements)


def test_metrics_use_registered_canonical_handle(catalogue):
    add(catalogue, raw={"ownerUsername": "openai", "coauthorProducers": ["alternate"]})
    observe(catalogue, {"shortCode": "Sample", "ownerUsername": "openai", "coauthorProducers": ["alternate"]})
    with catalogue() as conn:
        conn.execute("CREATE TABLE accounts (handle TEXT, is_canonical INTEGER)")
        conn.execute("INSERT INTO accounts VALUES ('alternate', 1)")
        assert collab.collaboration_updates(conn, ["Sample"]) == [
            {"account": "alternate", "shortcode": "Sample", "isCollab": True, "collaborators": ["openai"]},
        ]


def test_projection_upgrade_invalidates_legacy_manifest_etag(catalogue):
    add(catalogue)
    manifest = main._dashboard_catalogue_manifest()
    assert manifest["projectionVersion"] == 2
    previous = {"sources": manifest["sources"], "queue": 0,
                "catalogue_generation": main._DASHBOARD_CATALOGUE_GENERATION}
    old_revision = hashlib.sha256(json.dumps(previous, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    response = main.dashboard_posts_manifest(Request({"type": "http", "headers": [(b"if-none-match", f'"{old_revision}"'.encode())]}))
    assert response.status_code == 200
    assert json.loads(response.body)["projectionVersion"] == 2
    assert response.headers["etag"] != f'"{old_revision}"'


@pytest.mark.parametrize(("function", "result", "invoke"), [
    ("enrich_from_existing_runs", {"updated": 1}, lambda: main._enrich_worker(1, 1)),
    ("enrich_from_run", {"updated": 1}, lambda: main.admin_enrich_from_run("stored-run", "test")),
    ("enrich_account_via_profile", {"updated_existing": 1, "inserted_new": 0}, lambda: main._profile_enrich_worker("chatgptricks", 1)),
    ("scrape_missing_enrichment", {"updated": 1}, lambda: main._scrape_missing_worker(1, "chatgptricks")),
])
def test_metadata_enrichment_invalidates_cached_catalogue_without_new_ids(catalogue, monkeypatch, function, result, invoke):
    add(catalogue)
    monkeypatch.setattr(main, "_DASHBOARD_CATALOGUE_GENERATION", 0)
    monkeypatch.setattr(main, "_DASHBOARD_POSTS_CACHE_CONTENT", b"old catalogue")
    monkeypatch.setattr(main, "_DASHBOARD_POSTS_CACHE_EXPIRES_AT", 10000)
    monkeypatch.setattr(main, "_require_admin", lambda password: None)
    for state in ("_ENRICH_RUN", "_PROFILE_ENRICH", "_SCRAPE_RUN"):
        monkeypatch.setattr(main, state, {"running": True, "result": None, "error": None})
    before = main._dashboard_catalogue_manifest()
    assert main._dashboard_catalogue_page("canonical", 0, 10, before["revision"])[0]["isCollab"] is None

    def enrich(*args, **kwargs):
        with catalogue() as conn:
            conn.execute("UPDATE posts SET raw_json = ?, enriched_at = '2026-10-10T00:00:00Z'",
                         (json.dumps({"coauthorProducers": ["openai"]}),))
        return result

    monkeypatch.setattr(apify_sync, function, enrich)
    invoke()
    after = main._dashboard_catalogue_manifest()
    assert after["sources"] == before["sources"]
    assert after["projectionGeneration"] == 1 and after["revision"] != before["revision"]
    assert main._DASHBOARD_POSTS_CACHE_CONTENT is None
    assert main._dashboard_catalogue_page("canonical", 0, 10, after["revision"])[0]["collaborators"] == ["openai"]


def test_empty_enrichment_does_not_force_a_catalogue_reload(catalogue, monkeypatch):
    monkeypatch.setattr(main, "_DASHBOARD_CATALOGUE_GENERATION", 0)
    monkeypatch.setattr(main, "_ENRICH_RUN", {"running": True, "result": None, "error": None})
    monkeypatch.setattr(apify_sync, "enrich_from_existing_runs", lambda **kwargs: {"updated": 0})
    before = main._dashboard_catalogue_manifest()
    main._enrich_worker(1, 1)
    assert main._dashboard_catalogue_manifest()["revision"] == before["revision"]


@pytest.mark.parametrize(("function", "state", "invoke"), [
    ("enrich_from_existing_runs", "_ENRICH_RUN", lambda: main._enrich_worker(1, 1)),
    ("scrape_missing_enrichment", "_SCRAPE_RUN", lambda: main._scrape_missing_worker(1, "chatgptricks")),
    ("enrich_account_via_profile", "_PROFILE_ENRICH", lambda: main._profile_enrich_worker("chatgptricks", 1)),
])
def test_partial_committed_enrichment_remains_visible_after_later_failure(catalogue, monkeypatch, function, state, invoke):
    add(catalogue)
    monkeypatch.setattr(main, "_DASHBOARD_CATALOGUE_GENERATION", 0)
    monkeypatch.setattr(main, "_DASHBOARD_POSTS_CACHE_CONTENT", b"old catalogue")
    monkeypatch.setattr(main, "_DASHBOARD_POSTS_CACHE_EXPIRES_AT", 10000)
    monkeypatch.setattr(main, state, {"running": True, "result": None, "error": None})
    before = main._dashboard_catalogue_manifest()

    def enrich(*args, **kwargs):
        with catalogue() as conn:
            conn.execute("UPDATE posts SET raw_json = ?, enriched_at = '2026-10-10T00:00:00Z'",
                         (json.dumps({"coauthorProducers": ["openai"]}),))
        raise RuntimeError("later batch unavailable")

    monkeypatch.setattr(apify_sync, function, enrich)
    invoke()
    after = main._dashboard_catalogue_manifest()
    assert after["projectionGeneration"] == 1 and after["revision"] != before["revision"]
    assert main._DASHBOARD_POSTS_CACHE_CONTENT is None
    assert main._dashboard_catalogue_page("canonical", 0, 10, after["revision"])[0]["collaborators"] == ["openai"]
    assert getattr(main, state)["error"] == "later batch unavailable"
    assert getattr(main, state)["running"] is False
