from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest

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


@pytest.mark.parametrize("correction", [
    {"review_status": "reviewed"},
    {"review_status": "dismissed"},
    {"client": "Corrected brand"},
    {"classification": "likely", "product": "Acme Pro"},
])
def test_negative_reanalysis_preserves_human_decision_and_detector_disagreement(monkeypatch, correction):
    connection = _connection()

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(promos, "connect", connect)
    base = {"account": "competitor", "shortcode": "reviewed", "published_at": "2026-09-01T12:00:00+00:00"}
    first = promos.analyze_post({**base, "caption": "Sponsored by @higgsfield"})
    reviewed = promos.update_opportunity("competitor", "reviewed", correction, "reviewer@example.com")
    assert reviewed["detector_classification"] == "disclosed"
    assert reviewed["reviewer"] == "reviewer@example.com"

    promos.analyze_post({**base, "caption": "Independent editorial update without a commercial offer."})
    result = promos.get_opportunity("competitor", "reviewed")
    assert result is not None
    assert result["classification"] == correction.get("classification", "disclosed")
    assert result["detector_classification"] == "not_promo"
    assert result["review_status"] == correction.get("review_status", "new")
    assert result["first_detected_at"] == first["first_detected_at"]
    assert result["reviewer"] == reviewed["reviewer"]
    assert result["reviewed_at"] == reviewed["reviewed_at"]
    for key in ("client", "product"):
        if key in correction:
            assert result[key] == correction[key]


def test_semantic_assessment_alone_does_not_protect_stale_opportunity(monkeypatch):
    connection = _connection()

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(promos, "connect", connect)
    base = {"account": "competitor", "shortcode": "automatic"}
    promos.analyze_post({**base, "caption": "Sponsored by @higgsfield"})
    promos.update_opportunity("competitor", "automatic", {"jev_review": {"semanticPromo": 0.2, "relationshipConfidence": 0.8}}, "automatic@example.com")
    promos.analyze_post({**base, "caption": "A regular editorial update."})
    assert promos.get_opportunity("competitor", "automatic") is None


def test_list_filters_match_effective_manual_corrections_after_rescan(monkeypatch):
    connection = _connection()

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(promos, "connect", connect)
    base = {"account": "competitor", "shortcode": "corrected"}
    promos.analyze_post({**base, "caption": "Sponsored by @wrongbrand"})
    promos.update_opportunity("competitor", "corrected", {"client": "Acme", "classification": "likely", "review_status": "reviewed"}, "reviewer@example.com")
    promos.analyze_post({**base, "caption": "Sponsored by @wrongbrand. #ad"})
    result = promos.list_opportunities(client="ACME", classification="likely", review="reviewed", account="competitor")
    assert len(result["items"]) == 1
    assert result["items"][0]["client"] == "Acme"
    assert result["items"][0]["classification"] == "likely"
    assert result["items"][0]["detector_classification"] == "disclosed"
    assert not promos.list_opportunities(classification="disclosed")["items"]
    assert not promos.list_opportunities(client="wrongbrand")["items"]


@pytest.mark.parametrize("peer_client,classification,review,evidence,expected", [
    ("Other brand", "disclosed", "new", [], "needs_review"),
    ("Higgsfield", "disclosed", "dismissed", [], "needs_review"),
    ("Higgsfield", "likely", "new", [], "needs_review"),
    ("Higgsfield", "likely", "new", [{"family": "stack", "rule": "promo cluster support"}], "needs_review"),
    (None, "disclosed", "new", [], "needs_review"),
    (" @HIGGSFIELD ", "disclosed", "new", [], "likely"),
    ("Higgsfield", "likely", "new", [{"family": "affiliate", "rule": "use code"}], "likely"),
    ("Higgsfield", "likely", "reviewed", [], "likely"),
])
def test_stack_corroboration_needs_same_brand_and_credible_live_signal(monkeypatch, peer_client, classification, review, evidence, expected):
    connection = _connection()

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(promos, "connect", connect)
    promos._initialize_topic_stacks(connection)
    connection.executemany(
        "INSERT INTO topic_stack_members(post_key, stack_id, words, posted_at) VALUES (?, 'stack', '[]', 0)",
        [("competitor:peer",), ("competitor:target",)],
    )
    connection.execute(
        "INSERT INTO promo_opportunities(account, shortcode, classification, client, analysis_json, review_status) VALUES ('competitor', 'peer', ?, ?, ?, ?)",
        (classification, peer_client, promos._json({"classification": classification, "evidence": evidence}), review),
    )
    result = promos.analyze_post({"account": "competitor", "shortcode": "target", "caption": "Partner @higgsfield"})
    assert result["classification"] == expected
    assert result["stack_support_count"] == (1 if expected == "likely" else 0)


def test_dismissed_manual_override_cannot_support_a_peer(monkeypatch):
    connection = _connection()

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(promos, "connect", connect)
    promos._initialize_topic_stacks(connection)
    connection.executemany(
        "INSERT INTO topic_stack_members(post_key, stack_id, words, posted_at) VALUES (?, 'stack', '[]', 0)",
        [("competitor:peer",), ("competitor:target",)],
    )
    connection.execute(
        "INSERT INTO promo_opportunities(account, shortcode, classification, client, analysis_json, review_status, review_override_json) VALUES ('competitor', 'peer', 'disclosed', 'higgsfield', '{}', 'reviewed', ?)",
        (promos._json({"classification": "not_promo"}),),
    )
    result = promos.analyze_post({"account": "competitor", "shortcode": "target", "caption": "Partner @higgsfield"})
    assert result["classification"] == "needs_review"


def test_stack_cannot_resolve_a_contradictory_disclosure(monkeypatch):
    connection = _connection()

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(promos, "connect", connect)
    promos._initialize_topic_stacks(connection)
    connection.executemany(
        "INSERT INTO topic_stack_members(post_key, stack_id, words, posted_at) VALUES (?, 'stack', '[]', 0)",
        [("competitor:peer",), ("competitor:target",)],
    )
    promos.analyze_post({"account": "competitor", "shortcode": "peer", "caption": "Sponsored by @higgsfield"})
    result = promos.analyze_post({"account": "competitor", "shortcode": "target", "caption": "Not sponsored. Partner @higgsfield #higgsfieldpartner"})
    assert result["classification"] == "needs_review"


def test_semantic_rescan_skips_field_correction_and_preserves_human_attribution(monkeypatch):
    connection = _connection()

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(promos, "connect", connect)
    post = {"account": "competitor", "shortcode": "corrected", "caption": "Sponsored by @brand"}
    promos.analyze_post(post)
    manual = promos.update_opportunity("competitor", "corrected", {"client": "Corrected brand"}, "human@example.com")
    assessed = promos.update_opportunity("competitor", "corrected", {"jev_review": {"semanticPromo": 0.2, "relationshipConfidence": 0.8}}, "ai-review@example.com")
    assert assessed["reviewer"] == manual["reviewer"]
    assert assessed["reviewed_at"] == manual["reviewed_at"]
    monkeypatch.setattr(promos, "_post_rows", lambda conn, **kwargs: [post])
    monkeypatch.setattr(promos, "discover_promos", lambda posts: pytest.fail("A reviewed correction must not be rescanned"))
    assert promos.process_jev_posts()["processed"] == 0
    assert promos.get_opportunity("competitor", "corrected")["client"] == "Corrected brand"
