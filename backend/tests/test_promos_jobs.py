from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from app import promos


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE promo_scans (
            account TEXT NOT NULL, shortcode TEXT NOT NULL, input_hash TEXT,
            detector_version TEXT, status TEXT, attempts INTEGER DEFAULT 0,
            error TEXT, updated_at TEXT, PRIMARY KEY (account, shortcode)
        );
        CREATE TABLE promo_opportunities (
            account TEXT NOT NULL, shortcode TEXT NOT NULL, classification TEXT,
            client TEXT, product TEXT, analysis_json TEXT, review_status TEXT,
            review_override_json TEXT, published_at TEXT, first_detected_at TEXT,
            last_analyzed_at TEXT, PRIMARY KEY (account, shortcode)
        );
        CREATE TABLE dashboard_posts (
            id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT,
            cover_image_path TEXT, cover_source_url TEXT, permalink TEXT,
            caption TEXT, raw_json TEXT
        );
        """
    )
    return connection


def test_analyze_post_is_idempotent_and_keeps_review_override(monkeypatch):
    connection = _connection()

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(promos, "connect", connect)
    post = {
        "account": "competitor",
        "shortcode": "abc123",
        "caption": "Sponsored by @higgsfield. Comment VIDEO for the link",
        "published_at": "2026-09-01T12:00:00+00:00",
    }
    first = promos.analyze_post(post)
    assert first["classification"] == "disclosed"
    promos.update_opportunity("competitor", "abc123", {"review_status": "reviewed", "client": "Higgsfield"}, "admin@example.com")
    second = promos.analyze_post({**post, "caption": post["caption"] + " #video"})
    assert second["review_status"] == "reviewed"
    row = connection.execute("SELECT COUNT(*) FROM promo_opportunities").fetchone()
    assert row[0] == 1


def test_reanalysis_removes_opportunity_when_signal_disappears(monkeypatch):
    connection = _connection()

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(promos, "connect", connect)
    base = {"account": "competitor", "shortcode": "gone1", "published_at": "2026-09-01T12:00:00+00:00"}
    promos.analyze_post({**base, "caption": "Sponsored by @higgsfield"})
    promos.analyze_post({**base, "caption": "A regular editorial update about technology"})
    assert connection.execute("SELECT COUNT(*) FROM promo_opportunities").fetchone()[0] == 0


def test_stack_confirmation_boosts_relationship_signal_without_creating_one(monkeypatch):
    connection = _connection()
    connection.execute("CREATE TABLE topic_stack_members (post_key TEXT PRIMARY KEY, stack_id TEXT NOT NULL, words TEXT NOT NULL, posted_at REAL NOT NULL)")
    connection.executemany(
        "INSERT INTO topic_stack_members(post_key, stack_id, words, posted_at) VALUES (?, ?, '[]', 0)",
        [("competitor:confirmed", "stack-a"), ("competitor:review", "stack-a")],
    )
    connection.execute(
        "INSERT INTO promo_opportunities(account, shortcode, classification, client, analysis_json, review_status, first_detected_at, last_analyzed_at) VALUES ('competitor', 'confirmed', 'disclosed', 'Higgsfield', '{}', 'new', '2026-09-01', '2026-09-01')"
    )

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(promos, "connect", connect)
    result = promos.analyze_post({
        "account": "competitor",
        "shortcode": "review",
        "caption": "Partner @higgsfield",
        "published_at": "2026-09-01T12:00:00+00:00",
    })
    assert result["classification"] == "likely"
    assert result["stack_size"] == 2
    assert result["stack_support_count"] == 1
    assert "promo cluster support" in result["signals"]


def test_list_opportunities_exposes_stack_metadata(monkeypatch):
    connection = _connection()
    connection.execute("CREATE TABLE topic_stack_members (post_key TEXT PRIMARY KEY, stack_id TEXT NOT NULL, words TEXT NOT NULL, posted_at REAL NOT NULL)")
    connection.executemany(
        "INSERT INTO topic_stack_members(post_key, stack_id, words, posted_at) VALUES (?, ?, '[]', 0)",
        [("competitor:one", "stack-b"), ("competitor:two", "stack-b")],
    )
    connection.execute(
        "INSERT INTO promo_opportunities(account, shortcode, classification, client, analysis_json, review_status, first_detected_at, last_analyzed_at) VALUES ('competitor', 'one', 'disclosed', 'Higgsfield', '{}', 'new', '2026-09-01', '2026-09-01')"
    )

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(promos, "connect", connect)
    result = promos.list_opportunities()
    assert result["items"][0]["stack_id"] == "stack-b"
    assert result["items"][0]["stack_size"] == 2
