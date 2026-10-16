from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import promos_engagement as engagement

NOW = datetime(2026, 10, 16, 18, tzinfo=UTC)


@pytest.fixture
def engagement_db(monkeypatch, tmp_path):
    path = tmp_path / "promos-engagement.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE accounts (handle TEXT PRIMARY KEY, label TEXT, group_name TEXT,
            category TEXT, subcategory TEXT, is_canonical INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1, avatar_path TEXT, research_enabled INTEGER DEFAULT 1,
            promos_enabled INTEGER DEFAULT 1, hot_threshold INTEGER, scrape_mode TEXT);
        CREATE TABLE posts (id INTEGER PRIMARY KEY, section TEXT, shortcode TEXT,
            published_at TEXT, likes INTEGER, comments INTEGER, raw_json TEXT,
            updated_at TEXT, hidden INTEGER, is_deleted INTEGER);
        CREATE TABLE dashboard_posts (id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT,
            published_at TEXT, likes INTEGER, comments INTEGER, raw_json TEXT,
            video_views INTEGER, video_plays INTEGER, slide_count INTEGER,
            post_type_label TEXT, product_type TEXT, updated_at TEXT, enriched_at TEXT,
            hidden INTEGER, is_deleted INTEGER);
        CREATE TABLE account_snapshots (id INTEGER PRIMARY KEY, handle TEXT,
            followers_count INTEGER, captured_at TEXT);
        CREATE TABLE engagement_observations (shortcode TEXT PRIMARY KEY,
            observed_at TEXT, raw_json TEXT);
        INSERT INTO accounts (handle, label, category) VALUES ('sample', 'Sample', 'competitors');
    """)
    conn.commit()
    conn.close()
    statements = []

    @contextmanager
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.set_trace_callback(statements.append)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(engagement.db, "connect", connect)
    return connect, statements


def insert(conn, table, **values):
    names = list(values)
    conn.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", list(values.values()))


def post(conn, code, **values):
    insert(conn, "dashboard_posts", account="sample", shortcode=code,
           **{"published_at": "2026-10-15T12:00:00Z", **values})


def test_paired_core_sample_and_full_sample_do_not_mix_missing_with_zero(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "paired", likes=100, comments=10,
             raw_json=json.dumps({"sharesCount": 20, "savesCount": 0, "impressionsCount": 1000}))
        post(conn, "missing_comments", likes=200, comments=None)
        post(conn, "hidden_likes", likes=3, comments=30, raw_json='{"likesCount":999}')
        post(conn, "negative_likes", likes=-1, comments=50)
        post(conn, "known_zero", likes=20, comments=0)
        insert(conn, "account_snapshots", handle="sample", followers_count=1000, captured_at="2026-10-14T12:00:00Z")
        insert(conn, "account_snapshots", handle="sample", followers_count=None, captured_at="2026-10-15T12:00:00Z")
        insert(conn, "account_snapshots", handle="sample", followers_count=9999, captured_at="2026-10-17T12:00:00Z")
    report = engagement.calculate_promos_engagement(now=NOW)
    account = report["accounts"][0]
    assert account["post_count"] == 5
    assert account["engagement_post_count"] == 2
    assert account["average_engagements"] == 65
    assert account["engagement_rate_pct"] == 6.5
    assert account["engagement_coverage_pct"] == 40
    assert account["followers"] == 1000
    assert account["followers_captured_at"] == "2026-10-14T12:00:00Z"
    assert account["metrics"]["likes"] == {"count": 3, "total": 320, "average": 320 / 3, "coverage_pct": 60}
    assert account["metrics"]["comments"] == {"count": 4, "total": 90, "average": 22.5, "coverage_pct": 80}
    assert account["metrics"]["saves"]["average"] == 0
    assert account["metrics"]["impressions"]["average"] == 1000
    assert account["metrics"]["video_views"]["average"] is None
    assert account["complete_engagement_post_count"] == 1
    assert account["average_complete_engagements"] == 130


def test_latest_observation_overrides_stale_payload_and_duplicate_canonical_is_one_post(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_canonical = 1")
        insert(conn, "posts", shortcode="same", section="historical", likes=100, comments=10,
               published_at="2026-10-15T12:00:00Z", updated_at="2026-10-15T13:00:00Z")
        post(conn, "same", likes=80, comments=8, video_views=400, video_plays=600,
             updated_at="2026-10-15T11:00:00Z", raw_json='{"sharesCount":1,"savesCount":2}')
        insert(conn, "engagement_observations", shortcode="same", observed_at="2026-10-16T12:00:00Z",
               raw_json=json.dumps({"shortCode": "same", "likesCount": 200, "commentsCount": 20,
                                    "sharesCount": 5, "savesCount": 10, "impressionsCount": 2000}))
        insert(conn, "posts", shortcode="experiment", section="ab", likes=10000, comments=100,
               published_at="2026-10-15T12:00:00Z")
        insert(conn, "posts", section="historical", likes=10000, comments=100)
    account = engagement.calculate_promos_engagement(now=NOW)["accounts"][0]
    assert account["post_count"] == 1
    assert account["average_engagements"] == 220
    assert account["average_complete_engagements"] == 235
    assert account["metrics"]["video_views"]["average"] == 400
    assert account["metrics"]["video_plays"]["average"] == 600
    assert account["metrics_updated_at"] == "2026-10-16T12:00:00+00:00"


def test_publication_window_format_and_active_dashboard_roster_include_leads(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        for code, date, kind in (("recent_reel", "2026-10-15T12:00:00Z", "clips"),
                                 ("older", "2026-09-01T12:00:00Z", None),
                                 ("old", "2026-01-01T12:00:00Z", None),
                                 ("future", "2026-10-17T12:00:00Z", None),
                                 ("undated", None, None)):
            post(conn, code, likes=100, comments=0, published_at=date, product_type=kind)
        post(conn, "carousel", likes=100, comments=0, post_type_label="Carousel (carousel_container)")
        post(conn, "hidden", likes=500, comments=50, hidden=1)
        post(conn, "deleted", likes=500, comments=50, is_deleted=1)
        for handle, active, enabled, research in (("lead", 1, 1, 0), ("inactive", 0, 1, 1), ("disabled", 1, 0, 1)):
            insert(conn, "accounts", handle=handle, label=handle, category="leads", is_active=active,
                   promos_enabled=enabled, research_enabled=research)
    recent = engagement.calculate_promos_engagement(now=NOW)
    assert {account["handle"] for account in recent["accounts"]} == {"sample", "lead", "disabled"}
    assert recent["methodology"]["account_scope"] == "active_dashboard_accounts"
    sample = next(account for account in recent["accounts"] if account["handle"] == "sample")
    assert sample["post_count"] == 2
    assert sample["first_post_at"] == "2026-10-15T12:00:00+00:00"
    assert sample["engagement_rate_pct"] is None
    assert engagement.calculate_promos_engagement(days=90, now=NOW)["accounts"][0]["post_count"] == 3
    stored = engagement.calculate_promos_engagement(days=0, now=NOW)["accounts"][0]
    assert stored["post_count"] == 5
    assert stored["undated_post_count"] == 1
    reel = engagement.calculate_promos_engagement(format="reel", now=NOW)["accounts"][0]
    assert reel["post_count"] == 1
    assert engagement.calculate_promos_engagement(format="carousel", now=NOW)["accounts"][0]["post_count"] == 1
    assert engagement.calculate_promos_engagement(format="image", now=NOW)["accounts"][0]["post_count"] == 0
    empty = next(account for account in recent["accounts"] if account["handle"] == "lead")
    assert empty["average_engagements"] is None
    assert empty["metrics"]["shares"]["total"] is None
    assert recent["period"]["from"] == "2026-09-16T18:00:00+00:00"


def test_hidden_flag_on_either_duplicate_removes_post_and_counts_are_fresh_read_only(engagement_db, monkeypatch):
    from app import apify_sync
    connect, statements = engagement_db
    monkeypatch.setattr(apify_sync, "_run_apify_actor_and_fetch", lambda *a, **k: pytest.fail("Paid scrape during benchmark"))
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_canonical = 1")
        insert(conn, "posts", shortcode="hidden_copy", section="historical", hidden=1,
               likes=9999, comments=99, published_at="2026-10-15T12:00:00Z")
        post(conn, "hidden_copy", likes=9999, comments=99, updated_at="2026-10-16T12:00:00Z")
        post(conn, "visible", likes=10, comments=0)
    statements.clear()
    first = engagement.calculate_promos_engagement(now=NOW)["accounts"][0]
    assert first["post_count"] == 1
    assert first["average_engagements"] == 10
    assert all(statement.lstrip().startswith(("SELECT", "PRAGMA")) for statement in statements)
    with connect() as conn:
        conn.execute("UPDATE dashboard_posts SET likes = 30 WHERE shortcode = 'visible'")
    assert engagement.calculate_promos_engagement(now=NOW)["accounts"][0]["average_engagements"] == 30


def test_metadata_edits_cannot_revert_observed_counts_or_claim_metric_freshness(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_canonical = 1")
        insert(conn, "posts", shortcode="same", section="historical", likes=300, comments=30,
               published_at="2026-10-15T12:00:00Z", updated_at="2026-10-15T12:00:00Z")
        # A transcript/OCR repair touched a stale duplicate's generic timestamp.
        post(conn, "same", likes=100, comments=10, updated_at="2026-10-16T12:00:00Z")
        insert(conn, "engagement_observations", shortcode="same", observed_at="2026-10-15T12:00:00Z",
               raw_json='{"shortCode":"same","likesCount":300,"commentsCount":30}')
    account = engagement.calculate_promos_engagement(now=NOW)["accounts"][0]
    assert account["average_engagements"] == 330
    assert account["metrics_updated_at"] == "2026-10-15T12:00:00+00:00"
    with connect() as conn:
        # Reverse the tables: a canonical cover repair has the same outcome.
        conn.execute("UPDATE posts SET likes = 100, comments = 10, updated_at = '2026-10-16T13:00:00Z'")
        conn.execute("UPDATE dashboard_posts SET likes = 300, comments = 30, updated_at = '2026-10-15T12:00:00Z'")
    account = engagement.calculate_promos_engagement(now=NOW)["accounts"][0]
    assert account["average_engagements"] == 330
    assert account["metrics_updated_at"] == "2026-10-15T12:00:00+00:00"


def test_unstamped_promoted_metrics_remain_available_without_claiming_update_time(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "fallback", likes=100, comments=10, updated_at="2026-10-16T12:00:00Z")
    account = engagement.calculate_promos_engagement(now=NOW)["accounts"][0]
    assert account["average_engagements"] == 110
    assert account["metrics_updated_at"] is None


@pytest.mark.parametrize("hidden_likes", [None, -1, 0, 3])
def test_older_visible_observation_does_not_resurrect_newer_unavailable_likes(engagement_db, hidden_likes):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "hidden", likes=hidden_likes, comments=10, updated_at="2026-10-16T12:00:00Z")
        insert(conn, "engagement_observations", shortcode="hidden", observed_at="2026-10-15T12:00:00Z",
               raw_json='{"shortCode":"hidden","likesCount":300,"commentsCount":30}')
    account = engagement.calculate_promos_engagement(now=NOW)["accounts"][0]
    assert account["metrics"]["likes"]["average"] is None
    assert account["engagement_post_count"] == 0


def test_newer_observation_can_decrease_counts_and_newer_enrichment_can_replace_it(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "changing", likes=300, comments=30, updated_at="2026-10-16T12:00:00Z")
        insert(conn, "engagement_observations", shortcode="changing", observed_at="2026-10-15T12:00:00Z",
               raw_json='{"shortCode":"changing","likesCount":200,"commentsCount":20}')
    account = engagement.calculate_promos_engagement(now=NOW)["accounts"][0]
    assert account["average_engagements"] == 220
    with connect() as conn:
        conn.execute("UPDATE dashboard_posts SET enriched_at = '2026-10-16T10:00:00Z', raw_json = ?",
                     ('{"likesCount":400,"commentsCount":40,"sharesCount":2}',))
    account = engagement.calculate_promos_engagement(now=NOW)["accounts"][0]
    assert account["average_engagements"] == 440
    assert account["metrics"]["shares"]["average"] == 2
    assert account["metrics_updated_at"] == "2026-10-16T10:00:00+00:00"


def test_recent_cohorts_filter_before_provider_json_and_keep_every_duplicate_copy(engagement_db, monkeypatch):
    connect, statements = engagement_db
    monkeypatch.setattr(engagement, "_BLOCK_SIZE", 2)
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_canonical = 1")
        conn.executescript("CREATE INDEX account_date ON dashboard_posts(account,published_at); CREATE INDEX account_code ON dashboard_posts(account,shortcode);")
        for index in range(20):
            post(conn, f"old{index}", likes=99999, comments=999,
                 published_at="2025-01-01T12:00:00Z", raw_json='{"likesCount":99999}')
        for index in range(5):
            post(conn, f"recent{index}", likes=100, comments=10)
        # An undated canonical copy must still contribute its current metrics.
        insert(conn, "posts", shortcode="recent0", section="historical", likes=300, comments=30,
               updated_at="2026-10-16T12:00:00Z")
        # A hidden older copy must remove a recent duplicate, even outside the window.
        insert(conn, "posts", shortcode="recent1", section="historical", likes=200, comments=20,
               published_at="2025-01-01T12:00:00Z", hidden=1)
    seen = []
    measured = engagement._measured_posts

    def observe(records, *args):
        seen.append([row.get("shortcode") for row in records])
        return measured(records, *args)

    monkeypatch.setattr(engagement, "_measured_posts", observe)
    statements.clear()
    account = engagement.calculate_promos_engagement(now=NOW)["accounts"][0]
    assert account["post_count"] == 4
    assert account["average_engagements"] == 165
    assert all(not str(code).startswith("old") for block in seen for code in block)
    assert max(map(len, seen)) <= 4
    assert all(" OFFSET " not in statement.upper() for statement in statements)
    # All-time traverses the same small keyset blocks and includes old samples.
    assert engagement.calculate_promos_engagement(days=0, now=NOW)["accounts"][0]["post_count"] == 24


def test_parent_projection_keeps_nested_metrics_and_formats_without_summing_children(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "carousel", likes=100, comments=10, enriched_at="2026-10-15T12:00:00Z",
             raw_json=json.dumps({"Likes": 100, "commentsCount": 10,
                 "Metrics": {"sharesCount": 5, "savedCount": 3, "impressionsCount": 1000},
                 "childPosts": [{"likesCount": 9000, "sharesCount": 9999}, {"likesCount": 8000}],
                 "caption": "large ignored text", "images": ["unused preview"]}))
        post(conn, "reel", likes=200, comments=20, post_type_label="Reel")
    account = engagement.calculate_promos_engagement(format="carousel", now=NOW)["accounts"][0]
    assert account["post_count"] == 1
    assert account["average_engagements"] == 110
    assert account["average_complete_engagements"] == 118
    assert account["metrics"]["shares"]["average"] == 5
    assert engagement.calculate_promos_engagement(format="reel", now=NOW)["accounts"][0]["post_count"] == 1


def test_shortcode_less_imports_keep_independent_provider_counters(engagement_db, monkeypatch):
    connect, _ = engagement_db
    monkeypatch.setattr(engagement, "_BLOCK_SIZE", 2)
    with connect() as conn:
        for index, likes in enumerate((100, 200, 300)):
            post(conn, None, likes=likes, comments=likes // 10,
                 enriched_at=f"2026-10-{13 + index}T12:00:00Z",
                 raw_json=json.dumps({"likesCount": likes, "commentsCount": likes // 10}))
    account = engagement.calculate_promos_engagement(now=NOW)["accounts"][0]
    assert account["post_count"] == 3
    assert account["average_engagements"] == 220
    assert account["metrics"]["likes"]["average"] == 200


def test_malformed_provider_json_keeps_promoted_counters(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "malformed", likes=100, comments=10, raw_json="not valid JSON")
    account = engagement.calculate_promos_engagement(now=NOW)["accounts"][0]
    assert account["average_engagements"] == 110
    assert account["metrics"]["shares"]["average"] is None


def test_latest_observed_carousel_structure_overrides_stale_promoted_slide_count(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "changed", likes=100, comments=10, slide_count=1,
             updated_at="2026-10-14T12:00:00Z")
        insert(conn, "engagement_observations", shortcode="changed", observed_at="2026-10-15T12:00:00Z",
               raw_json='{"shortCode":"changed","likesCount":100,"commentsCount":10,"childPosts":[{},{}]}')
    assert engagement.calculate_promos_engagement(format="carousel", now=NOW)["accounts"][0]["post_count"] == 1


@pytest.mark.parametrize("age", [timedelta(days=6, hours=23, minutes=59), timedelta(days=7)])
def test_saved_benchmarks_are_reused_through_exactly_seven_days(engagement_db, monkeypatch, age):
    connect, statements = engagement_db
    with connect() as conn:
        post(conn, "visible", likes=100, comments=10)
        insert(conn, "account_snapshots", handle="sample", followers_count=1000, captured_at="2026-10-15T12:00:00Z")
    first = engagement.build_promos_engagement(now=NOW)
    assert first["cache"] == {"ttl_days": 7, "cached_count": 0, "recalculated_count": 1}
    with connect() as conn:
        conn.execute("UPDATE dashboard_posts SET likes = 999")
        conn.execute("UPDATE account_snapshots SET followers_count = 9999")
    statements.clear()
    monkeypatch.setattr(engagement, "calculate_promos_engagement", lambda **kwargs: pytest.fail("Recalculated a valid saved benchmark"))
    second = engagement.build_promos_engagement(now=NOW + age)
    account = second["accounts"][0]
    assert second["cache"] == {"ttl_days": 7, "cached_count": 1, "recalculated_count": 0}
    assert account["average_engagements"] == 110
    assert account["followers"] == 1000
    assert account["from_cache"] is True
    assert account["calculated_at"] == NOW.isoformat()
    assert account["cache_expires_at"] == (NOW + timedelta(days=7)).isoformat()
    assert account["calculation_period"] == {"days": 30, "from": (NOW - timedelta(days=30)).isoformat(), "to": NOW.isoformat()}
    assert not any(table in statement.lower() for statement in statements for table in ("dashboard_posts", "account_snapshots", "engagement_observations"))


def test_expired_benchmark_recalculates_after_seven_days_and_persists_across_connections(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "visible", likes=100, comments=10)
    engagement.build_promos_engagement(now=NOW)
    with connect() as conn:
        conn.execute("UPDATE dashboard_posts SET likes = 200")
    later = NOW + timedelta(days=7, microseconds=1)
    report = engagement.build_promos_engagement(now=later)
    assert report["cache"]["recalculated_count"] == 1
    assert report["accounts"][0]["average_engagements"] == 210
    with connect() as conn:
        row = conn.execute("SELECT calculated_at, payload_json FROM promos_engagement_cache").fetchone()
        assert row["calculated_at"] == later.isoformat()
        assert json.loads(row["payload_json"])["average_engagements"] == 210
    # The application has no process-local cache; a separate connection/run
    # reads the same durable row, even after source counters change again.
    with connect() as conn:
        conn.execute("UPDATE dashboard_posts SET likes = 300")
    assert engagement.build_promos_engagement(now=later + timedelta(seconds=1))["accounts"][0]["average_engagements"] == 210


def test_cache_keys_separate_account_window_and_format_and_never_store_prices(engagement_db, monkeypatch):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "visible", likes=100, comments=10, product_type="clips")
        insert(conn, "accounts", handle="lead", label="Lead", category="leads")
    calculate = engagement.calculate_promos_engagement

    def priced(**kwargs):
        report = calculate(**kwargs)
        for account in report["accounts"]:
            account.update(price=2500, currency="USD", post_price=99)
        return report

    monkeypatch.setattr(engagement, "calculate_promos_engagement", priced)
    for days, format, handles in ((30, "all", ["sample"]), (90, "all", ["sample"]),
                                  (30, "reel", ["sample"]), (30, "all", ["lead"])):
        assert engagement.build_promos_engagement(days=days, format=format, accounts=handles, now=NOW)["cache"]["recalculated_count"] == 1
    with connect() as conn:
        rows = conn.execute("SELECT account_handle, days, format, payload_json FROM promos_engagement_cache").fetchall()
    assert len(rows) == 4
    assert all(not any(word in row["payload_json"] for word in ("price", "currency")) for row in rows)


@pytest.mark.parametrize("damage", ["json", "shape", "future", "timestamp", "number", "version"])
def test_damaged_or_future_saved_benchmarks_recalculate(engagement_db, damage):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "visible", likes=100, comments=10)
    engagement.build_promos_engagement(now=NOW)
    with connect() as conn:
        if damage == "json":
            conn.execute("UPDATE promos_engagement_cache SET payload_json = 'broken JSON'")
        elif damage == "shape":
            conn.execute("UPDATE promos_engagement_cache SET payload_json = '{}'")
        elif damage == "future":
            conn.execute("UPDATE promos_engagement_cache SET calculated_at = ?", ((NOW + timedelta(days=1)).isoformat(),))
        elif damage == "timestamp":
            conn.execute("UPDATE promos_engagement_cache SET calculated_at = 'not a date'")
        elif damage == "version":
            conn.execute("UPDATE promos_engagement_cache SET schema_version = 0")
        else:
            payload = json.loads(conn.execute("SELECT payload_json FROM promos_engagement_cache").fetchone()["payload_json"])
            payload["metrics"]["likes"]["coverage_pct"] = "invalid"
            conn.execute("UPDATE promos_engagement_cache SET payload_json = ?", (json.dumps(payload),))
        conn.execute("UPDATE dashboard_posts SET likes = 200")
    report = engagement.build_promos_engagement(now=NOW)
    assert report["cache"]["recalculated_count"] == 1
    assert report["accounts"][0]["average_engagements"] == 210
    assert engagement.build_promos_engagement(now=NOW)["cache"]["cached_count"] == 1


def test_roster_changes_are_live_while_only_new_or_expired_accounts_recalculate(engagement_db, monkeypatch):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "visible", likes=100, comments=10)
        insert(conn, "accounts", handle="expired", label="Expired", category="leads")
    engagement.build_promos_engagement(now=NOW)
    with connect() as conn:
        conn.execute("UPDATE accounts SET label = 'New label', category = 'leads', avatar_path = 'new-avatar' WHERE handle = 'sample'")
        conn.execute("UPDATE promos_engagement_cache SET calculated_at = ? WHERE account_handle = 'expired'", ((NOW - timedelta(days=8)).isoformat(),))
        insert(conn, "accounts", handle="new", label="New", category="leads")
    seen = []
    calculate = engagement.calculate_promos_engagement

    def track(**kwargs):
        seen.extend(account["handle"] for account in kwargs["_roster"])
        return calculate(**kwargs)

    monkeypatch.setattr(engagement, "calculate_promos_engagement", track)
    report = engagement.build_promos_engagement(now=NOW)
    assert set(seen) == {"expired", "new"}
    sample = next(account for account in report["accounts"] if account["handle"] == "sample")
    assert sample["label"] == "New label" and sample["group"] == "leads" and sample["has_avatar"] is True
    assert sample["from_cache"] is True
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_active = 0 WHERE handle = 'sample'")
        conn.execute("UPDATE accounts SET promos_enabled = 0 WHERE handle = 'expired'")
    assert {account["handle"] for account in engagement.build_promos_engagement(now=NOW)["accounts"]} == {"expired", "new"}
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_active = 0 WHERE handle = 'expired'")
    assert [account["handle"] for account in engagement.build_promos_engagement(now=NOW)["accounts"]] == ["new"]


def test_selection_limits_calculation_and_cold_cache_to_chosen_accounts(engagement_db, monkeypatch):
    connect, statements = engagement_db
    with connect() as conn:
        post(conn, "visible", likes=100, comments=10)
        insert(conn, "accounts", handle="other", label="Other", category="leads")
    seen = []
    benchmark = engagement._account_benchmark

    def track(conn, account, *args):
        seen.append(account["handle"])
        return benchmark(conn, account, *args)

    monkeypatch.setattr(engagement, "_account_benchmark", track)
    report = engagement.build_promos_engagement(accounts=["@SAMPLE", "sample"], now=NOW)
    assert seen == ["sample"]
    assert [account["handle"] for account in report["accounts"]] == ["sample"]
    with connect() as conn:
        assert [row["account_handle"] for row in conn.execute("SELECT account_handle FROM promos_engagement_cache").fetchall()] == ["sample"]
    statements.clear()
    assert engagement.build_promos_engagement(accounts=[], now=NOW)["accounts"] == []
    assert not any("dashboard_posts" in statement for statement in statements)


def test_unknown_or_unavailable_selection_is_rejected_before_any_cache_or_metric_work(engagement_db):
    from fastapi import HTTPException
    connect, statements = engagement_db
    with connect() as conn:
        insert(conn, "accounts", handle="inactive", label="Inactive", category="leads", is_active=0)
    for handles in (["unknown"], ["sample", "inactive"], [""]):
        statements.clear()
        with pytest.raises(HTTPException) as failure:
            engagement.build_promos_engagement(accounts=handles, now=NOW)
        assert failure.value.status_code == 422
        assert all("dashboard_posts" not in statement and "promos_engagement_cache" not in statement for statement in statements)


def test_roster_read_never_opens_metrics_or_cache_and_schema_is_idempotent(engagement_db, monkeypatch):
    connect, statements = engagement_db
    with connect() as conn:
        insert(conn, "accounts", handle="lead", label="Lead", category="leads", research_enabled=0)
    monkeypatch.setattr(engagement, "calculate_promos_engagement", lambda **kwargs: pytest.fail("Roster calculated metrics"))
    statements.clear()
    roster = engagement.engagement_accounts(now=NOW)
    assert {account["handle"] for account in roster["accounts"]} == {"sample", "lead"}
    assert all(set(account) == {"handle", "label", "group", "subcategory", "has_avatar"} for account in roster["accounts"])
    assert all("promos_engagement_cache" not in statement and "dashboard_posts" not in statement for statement in statements)
    with connect() as conn:
        engagement.benchmark_cache.ensure_schema(conn)
        engagement.benchmark_cache.ensure_schema(conn)


@pytest.mark.parametrize("promos,research,group", [(0, 1, "competitors"), (1, 0, "sentient"), (0, 0, "leads")])
def test_all_active_dashboard_accounts_can_be_listed_calculated_and_cached_regardless_of_tool_flags(
    engagement_db, monkeypatch, promos, research, group
):
    connect, statements = engagement_db
    with connect() as conn:
        insert(conn, "accounts", handle="dashboard_only", label="Dashboard only", category=group,
               promos_enabled=promos, research_enabled=research)
        insert(conn, "accounts", handle="archived", label="Archived", category=group,
               promos_enabled=promos, research_enabled=research, is_active=0)
        insert(conn, "dashboard_posts", account="dashboard_only", shortcode="included", likes=100,
               comments=10, published_at="2026-10-15T12:00:00Z")
    statements.clear()
    roster = engagement.engagement_accounts(now=NOW)
    assert {account["handle"] for account in roster["accounts"]} == {"sample", "dashboard_only"}
    assert not any("dashboard_posts" in statement or "promos_engagement_cache" in statement for statement in statements)
    direct = engagement.calculate_promos_engagement(now=NOW)
    account = next(account for account in direct["accounts"] if account["handle"] == "dashboard_only")
    assert account["average_engagements"] == 110
    assert account["group"] == group
    selected = engagement.build_promos_engagement(accounts=["@DASHBOARD_ONLY"], now=NOW)
    assert [account["handle"] for account in selected["accounts"]] == ["dashboard_only"]
    assert selected["accounts"][0]["average_engagements"] == 110
    assert selected["cache"]["recalculated_count"] == 1
    with connect() as conn:
        rows = conn.execute("SELECT account_handle, payload_json FROM promos_engagement_cache").fetchall()
        assert [row["account_handle"] for row in rows] == ["dashboard_only"]
        assert "price" not in rows[0]["payload_json"] and "currency" not in rows[0]["payload_json"]
        conn.execute("UPDATE dashboard_posts SET likes = 999 WHERE account = 'dashboard_only'")
        conn.execute("UPDATE accounts SET promos_enabled = 0, research_enabled = 0 WHERE handle = 'dashboard_only'")
    monkeypatch.setattr(engagement, "calculate_promos_engagement", lambda **kwargs: pytest.fail("Recalculated a valid cached dashboard account"))
    cached = engagement.build_promos_engagement(accounts=["dashboard_only"], now=NOW + timedelta(days=7))
    assert cached["accounts"][0]["average_engagements"] == 110
    assert cached["accounts"][0]["from_cache"] is True
    assert cached["cache"]["cached_count"] == 1


def test_canonical_and_empty_dashboard_accounts_ignore_tool_flags_but_honor_archiving(engagement_db, monkeypatch):
    from fastapi import HTTPException
    connect, _ = engagement_db
    with connect() as conn:
        conn.execute("UPDATE accounts SET category = 'sentient', is_canonical = 1, promos_enabled = 0, research_enabled = 0 WHERE handle = 'sample'")
        insert(conn, "posts", shortcode="canonical", section="historical", likes=200, comments=20,
               published_at="2026-10-15T12:00:00Z")
        insert(conn, "accounts", handle="empty", label="Empty", category="competitors", promos_enabled=0, research_enabled=0)
    direct = engagement.calculate_promos_engagement(now=NOW)
    by_handle = {account["handle"]: account for account in direct["accounts"]}
    assert by_handle["sample"]["average_engagements"] == 220
    assert by_handle["empty"]["post_count"] == 0
    assert by_handle["empty"]["average_engagements"] is None
    assert by_handle["empty"]["metrics"]["likes"]["average"] is None
    engagement.build_promos_engagement(accounts=["sample"], now=NOW)
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_active = 0 WHERE handle = 'sample'")
    assert [account["handle"] for account in engagement.engagement_accounts(now=NOW)["accounts"]] == ["empty"]
    with pytest.raises(HTTPException) as failure:
        engagement.build_promos_engagement(accounts=["sample"], now=NOW)
    assert failure.value.status_code == 422
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_active = 1 WHERE handle = 'sample'")
    monkeypatch.setattr(engagement, "calculate_promos_engagement", lambda **kwargs: pytest.fail("Recalculated after reactivation despite valid cache"))
    restored = engagement.build_promos_engagement(accounts=["sample"], now=NOW + timedelta(days=1))
    assert restored["accounts"][0]["average_engagements"] == 220
    assert restored["accounts"][0]["from_cache"] is True


def test_older_concurrent_calculation_cannot_replace_a_newer_saved_result(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "visible", likes=100, comments=10)
    payload = engagement.calculate_promos_engagement(now=NOW)["accounts"][0]
    with connect() as conn:
        engagement.benchmark_cache.ensure_schema(conn)
        engagement.benchmark_cache.save(conn, "sample", 30, "all", NOW + timedelta(seconds=1), payload)
        older = {**payload, "average_engagements": 1}
        engagement.benchmark_cache.save(conn, "sample", 30, "all", NOW, older)
    report = engagement.build_promos_engagement(now=NOW + timedelta(seconds=2))
    assert report["accounts"][0]["average_engagements"] == 110


@pytest.fixture
def endpoint_client(monkeypatch):
    from app import main
    state = {"calls": [], "role": "sales", "roster_calls": 0}

    def build(**params):
        state["calls"].append(params)
        return {"accounts": [], "period": {"days": params["days"]}}

    monkeypatch.setattr(main, "build_promos_engagement", build)
    def roster():
        state["roster_calls"] += 1
        return {"accounts": [{"handle": "sample", "label": "Sample", "group": "competitors", "subcategory": "other", "has_avatar": False}], "generated_at": NOW.isoformat()}
    monkeypatch.setattr(main, "engagement_accounts", roster)
    monkeypatch.setattr(main, "FIREBASE_APP", object())
    monkeypatch.setattr(main.firebase_auth, "verify_id_token", lambda token: {"email": "member@example.com", "uid": "test"})
    monkeypatch.setattr(main, "get_dashboard_user_access", lambda email: {
        "is_admin": state["role"] == "admin", "operating_role": state["role"], "operating_roles": f'["{state["role"]}"]',
    })
    monkeypatch.setattr(main, "log_usage_event", lambda *args: None)
    app = FastAPI()
    app.middleware("http")(main._require_firebase_user)
    app.add_api_route("/api/admin/promos/engagement/accounts", main.admin_promos_engagement_accounts)
    app.add_api_route("/api/admin/promos/engagement", main.admin_promos_engagement)
    return TestClient(app), state


@pytest.mark.parametrize("role", ["sales", "pd", "vc", "admin"])
def test_promos_members_can_read_endpoint_without_settings_or_tracker_permission(endpoint_client, role):
    client, state = endpoint_client
    state["role"] = role
    response = client.get("/api/admin/promos/engagement?days=90&format=reel", headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    assert state["calls"] == [{"days": 90, "format": "reel", "accounts": None}]
    assert "no-store" in response.headers["cache-control"]


def test_engagement_endpoint_rejects_unauthenticated_and_invalid_queries(endpoint_client):
    client, state = endpoint_client
    assert client.get("/api/admin/promos/engagement").status_code == 401
    for query in ("days=15", "days=-1", "days=garbage", "format=stories"):
        assert client.get(f"/api/admin/promos/engagement?{query}", headers={"Authorization": "Bearer test"}).status_code == 422
    assert state["calls"] == []


def test_default_and_all_stored_windows_and_real_route_order(endpoint_client):
    from app import main
    client, state = endpoint_client
    headers = {"Authorization": "Bearer test"}
    assert client.get("/api/admin/promos/engagement", headers=headers).status_code == 200
    assert client.get("/api/admin/promos/engagement?days=0", headers=headers).status_code == 200
    assert state["calls"] == [{"days": 30, "format": "all", "accounts": None}, {"days": 0, "format": "all", "accounts": None}]
    paths = [route.path for route in main.app.routes if hasattr(route, "path")]
    assert paths.index("/api/admin/promos/engagement") < paths.index("/api/admin/promos/{account}/{shortcode}")
    assert paths.index("/api/admin/promos/engagement/accounts") < paths.index("/api/admin/promos/{account}/{shortcode}")


@pytest.mark.parametrize("role", ["sales", "pd", "vc", "admin"])
def test_roster_endpoint_shares_promos_auth_without_running_calculation(endpoint_client, role):
    client, state = endpoint_client
    state["role"] = role
    assert client.get("/api/admin/promos/engagement/accounts").status_code == 401
    assert state["roster_calls"] == 0
    response = client.get("/api/admin/promos/engagement/accounts", headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    assert response.json()["accounts"][0]["handle"] == "sample"
    assert state["calls"] == [] and state["roster_calls"] == 1


def test_account_selection_query_repeats_as_array(endpoint_client):
    client, state = endpoint_client
    response = client.get("/api/admin/promos/engagement?accounts=sample&accounts=lead", headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    assert state["calls"] == [{"days": 30, "format": "all", "accounts": ["sample", "lead"]}]
