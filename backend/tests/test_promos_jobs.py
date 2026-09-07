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
