from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import sqlite3
import threading

import pytest

from app import apify_sync as sync, db, engagement_checkpoints as checkpoints


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "checkpoints.sqlite"

    @contextmanager
    def connect():
        conn = sqlite3.connect(path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    with connect() as conn:
        conn.execute("""CREATE TABLE dashboard_posts (
            id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT, published_at TEXT,
            likes INTEGER, comments INTEGER, updated_at TEXT,
            hot_checked INTEGER NOT NULL DEFAULT 0, is_hot INTEGER NOT NULL DEFAULT 0,
            likes_at_1h INTEGER, hot_rate_multiplier REAL, hot_marked_at TEXT,
            likes_at_8h INTEGER, comments_at_8h INTEGER, refreshed_8h INTEGER NOT NULL DEFAULT 0
        )""")
        checkpoints.ensure_schema(conn)
    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(sync, "_slack_alerts_cover", lambda group: False)
    return connect


NOW = datetime(2026, 11, 6, 1, tzinfo=UTC)
RECEIVED = "2026-11-06T01:02:00+00:00"


def add(database, *, account="a", code="code", age=1, checked=0):
    with database() as conn:
        conn.execute("""INSERT INTO dashboard_posts
            (account, shortcode, published_at, hot_checked) VALUES (?, ?, ?, ?)""",
            (account, code, (NOW - timedelta(hours=age)).isoformat(), checked))


def process(account, item, *, now=NOW, threshold=600, **kwargs):
    return sync._process_short_term_items(account,
        {"table": "dashboard_posts", "hot_threshold": threshold, "group": "sentient"},
        [item], now, lookback_hours=168, insert_new=False, hot_check_window_hours=8, **kwargs)


def rows(database):
    with database() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM engagement_checkpoints ORDER BY account, checkpoint")]


def test_cold_first_check_records_threshold_and_two_clocks(database, monkeypatch):
    monkeypatch.setattr(db, "utc_now", lambda: RECEIVED)
    add(database)
    process("a", {"shortCode": "code", "likesCount": 6, "commentsCount": 0, "videoViewCount": 0})
    assert rows(database) == [{
        "account": "a", "shortcode": "code", "checkpoint": "first_hot_check",
        "likes": 6, "comments": 0, "views": 0, "observed_at": RECEIVED,
        "evaluated_at": NOW.isoformat(), "age_hours": 1.0,
        "hot_threshold": 600, "is_hot": 0, "rate_multiplier": 0.01,
        "source": "apify_ingestion",
    }]
    # Current metrics and configured thresholds may change without rewriting
    # the historical first check that supplied the calibration sample.
    process("a", {"shortCode": "code", "likesCount": 999}, threshold=100)
    assert rows(database)[0]["likes"] == 6
    assert rows(database)[0]["hot_threshold"] == 600


def test_checkpoint_storage_preserves_explicit_zero(database):
    with database() as conn:
        assert checkpoints.record(conn, account="a", shortcode="zero", checkpoint="24h",
            item={"likesCount": 0, "commentsCount": 0, "videoViewCount": 0},
            observed_at=RECEIVED, evaluated_at=NOW.isoformat(), age_hours=24)
    assert (rows(database)[0]["likes"], rows(database)[0]["comments"], rows(database)[0]["views"]) == (0, 0, 0)


def test_hidden_likes_do_not_claim_check_or_create_false_sample(database):
    add(database)
    process("a", {"shortCode": "code", "likesCount": -1, "commentsCount": 7})
    assert rows(database) == []
    with database() as conn:
        post = conn.execute("SELECT hot_checked, likes_at_1h FROM dashboard_posts").fetchone()
    assert tuple(post) == (0, None)


def test_eight_hour_snapshot_and_account_identity_are_immutable(database, monkeypatch):
    monkeypatch.setattr(db, "utc_now", lambda: RECEIVED)
    add(database, age=9, checked=1)
    add(database, account="b", age=9, checked=1)
    for account, likes in [("a", 90), ("b", 180)]:
        process(account, {"shortCode": "code", "likesCount": likes, "commentsCount": 3}, finalize_after_hours=8)
    process("a", {"shortCode": "code", "likesCount": 900}, finalize_after_hours=8)
    assert [(r["account"], r["checkpoint"], r["likes"], r["hot_threshold"]) for r in rows(database)] == [
        ("a", "8h", 90, None), ("b", "8h", 180, None)]
    with database() as conn:
        post = conn.execute("SELECT likes, likes_at_8h FROM dashboard_posts WHERE account='a'").fetchone()
    assert tuple(post) == (900, 90)


def test_later_checkpoints_use_existing_readings_only_inside_the_windows(database):
    for age, code in [(24, "at24"), (29.9, "near30"), (30, "late30"), (48, "at48"), (54, "late54")]:
        add(database, code=code, age=age, checked=1)
        process("a", {"shortCode": code, "likesCount": 42})
    assert {(r["shortcode"], r["checkpoint"]) for r in rows(database)} == {
        ("at24", "24h"), ("near30", "24h"), ("at48", "48h")}


@pytest.mark.parametrize("likes", [0, 1, 2, 3, -1, None, True])
@pytest.mark.parametrize("age", [24, 48])
def test_later_apify_checkpoints_exclude_pipeline_unknown_counts(database, likes, age):
    add(database, age=age, checked=1)
    process("a", {"shortCode": "code", "likesCount": likes})
    assert rows(database) == []


def test_manual_refresh_retains_received_measurement_without_an_extra_scrape(database, monkeypatch):
    add(database, age=25, checked=1)
    with database() as conn:
        for field in ("permalink TEXT", "cover_image_path TEXT", "cover_source_url TEXT", "is_deleted INTEGER"):
            conn.execute(f"ALTER TABLE dashboard_posts ADD COLUMN {field}")
    monkeypatch.setattr(db, "utc_now", lambda: NOW.isoformat())
    monkeypatch.setattr(sync, "get_account_config", lambda _: {"table": "dashboard_posts"})
    monkeypatch.setattr(sync, "_refresh_cover_from_item", lambda *args: False)
    monkeypatch.setattr("app.engagement_refresh.cached", lambda _: None)
    calls = []
    monkeypatch.setattr(sync, "_fetch_apify_items", lambda *args, **kwargs: calls.append(args) or [
        {"shortCode": "code", "likesCount": 123, "commentsCount": 5}])
    sync.refresh_single_post("a", "code")
    assert len(calls) == 1
    assert [(r["checkpoint"], r["likes"], r["source"]) for r in rows(database)] == [("24h", 123, "manual_refresh")]


def test_daily_cycle_retains_existing_paid_48h_measurement(database, monkeypatch):
    add(database, age=49, checked=1)
    with database() as conn:
        conn.execute("ALTER TABLE dashboard_posts ADD COLUMN refreshed_30d INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE dashboard_posts ADD COLUMN refreshed_120d INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE dashboard_posts ADD COLUMN permalink TEXT")
    monkeypatch.setattr(db, "utc_now", lambda: NOW.isoformat())
    monkeypatch.setattr("app.ingestion_jobs.now", lambda: NOW)
    monkeypatch.setattr(sync, "get_account_config", lambda _: {"table": "dashboard_posts"})
    calls = []
    monkeypatch.setattr(sync, "_run_apify_actor_and_fetch", lambda *args, **kwargs: calls.append(args) or [
        {"shortCode": "code", "likesCount": 234, "commentsCount": 6}])
    sync.run_daily_cycle("a")
    assert len(calls) == 1
    assert [(r["checkpoint"], r["likes"], r["age_hours"]) for r in rows(database)] == [("48h", 234, 49.0)]


def test_concurrent_passes_keep_one_first_check_matching_the_post(database, monkeypatch):
    add(database)
    barrier = threading.Barrier(2)
    original_connect = db.connect

    class FrozenRows:
        def __init__(self, values):
            self.values = values

        def fetchall(self):
            barrier.wait(timeout=10)
            return self.values

    class Connection:
        def __init__(self, raw):
            self.raw = raw

        def execute(self, sql, params=()):
            result = self.raw.execute(sql, params)
            if sql.startswith("SELECT id, shortcode, published_at, hot_checked, refreshed_8h"):
                return FrozenRows(result.fetchall())
            return result

    @contextmanager
    def synchronized_connect():
        with original_connect() as conn:
            yield Connection(conn)

    monkeypatch.setattr(db, "connect", synchronized_connect)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda likes: process("a", {"shortCode": "code", "likesCount": likes}), [1000, 2000]))
    assert sum(result["engagement"]["hot_marked"] for result in results) == 1
    snapshots = rows(database)
    assert len(snapshots) == 1
    with database() as conn:
        post = conn.execute("SELECT likes_at_1h FROM dashboard_posts").fetchone()
    assert post["likes_at_1h"] == snapshots[0]["likes"]


def test_schema_creation_is_idempotent_on_a_minimal_database(tmp_path):
    with sqlite3.connect(tmp_path / "minimal.sqlite") as conn:
        checkpoints.ensure_schema(conn)
        checkpoints.ensure_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM engagement_checkpoints").fetchone()[0] == 0
