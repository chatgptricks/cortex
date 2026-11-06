"""/next checks all stored destination history before editorial selection."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import sqlite3

import pytest

from app import dashboard_next_reuse as reuse, db


@pytest.fixture
def stored(tmp_path, monkeypatch):
    path = tmp_path / "reuse.sqlite"

    @contextmanager
    def connect(*, readonly=False):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            if readonly:
                conn.execute("PRAGMA query_only = ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    with connect() as conn:
        conn.executescript("""
            CREATE TABLE accounts(handle TEXT PRIMARY KEY, is_active INTEGER, is_canonical INTEGER,
                                  group_name TEXT, label TEXT, research_enabled INTEGER);
            INSERT INTO accounts VALUES ('alpha',1,1,'sentient','Alpha',1),
                                        ('beta',1,0,'sentient','Beta',1),
                                        ('rival',1,0,'competitors','Rival',1);
            CREATE TABLE posts(id INTEGER PRIMARY KEY, section TEXT DEFAULT 'historical', shortcode TEXT,
                               published_at TEXT, updated_at TEXT, caption TEXT, hook_text TEXT,
                               post_type_label TEXT, hidden INTEGER, is_deleted INTEGER);
            CREATE TABLE dashboard_posts(id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT,
                                         published_at TEXT, updated_at TEXT, caption TEXT, hook_text TEXT,
                                         post_type_label TEXT, hidden INTEGER, is_deleted INTEGER);
            CREATE TABLE topic_stack_members(post_key TEXT PRIMARY KEY, stack_id TEXT);
            CREATE TABLE queue_requests(id INTEGER PRIMARY KEY, post_account TEXT, post_shortcode TEXT,
                                        post_permalink TEXT, post_caption TEXT, final_permalink TEXT,
                                        final_permalinks TEXT, recommended_accounts TEXT, status TEXT,
                                        closed_at TEXT, completed_at TEXT, updated_at TEXT);
        """)
    # Any schema creation, topic initialization, or accidental write fails.
    monkeypatch.setattr(db, "connect", lambda: connect(readonly=True))
    return connect


def post(conn, code, *, account="beta", canonical=False, **extra):
    values = {"shortcode": code, "published_at": "2020-01-01T00:00:00Z", "updated_at": "2020-01-02T00:00:00Z",
              "caption": "Scientists discovered rare ancient fossils beneath volcanic mountains", "hook_text": "",
              "post_type_label": "Image", "hidden": 0, "is_deleted": 0, **extra}
    if not canonical:
        values["account"] = account
    table = "posts" if canonical else "dashboard_posts"
    conn.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()))


def queue(conn, source, *, target="beta", final="Delivered", status="closed", **extra):
    values = {"post_account": "rival", "post_shortcode": source,
              "post_permalink": f"https://www.instagram.com/p/{source}/",
              "post_caption": "Scientists announced magnetic superconductors under ambient pressure",
              "final_permalink": f"https://www.instagram.com/p/{final}/",
              "final_permalinks": json.dumps([{"account": target, "url": f"https://www.instagram.com/p/{final}/"}]),
              "recommended_accounts": json.dumps([target]), "status": status,
              "closed_at": "2025-10-01T00:00:00Z", "completed_at": "2025-09-30T00:00:00Z",
              "updated_at": "2025-10-01T00:00:00Z", **extra}
    conn.execute(f"INSERT INTO queue_requests ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()))


def candidate(code="New", *, account="rival", **extra):
    return {"account": account, "shortcode": code, "key": account + ":" + code,
            "caption": "Astronauts reveal plans for Europa research submarine expedition", "ocr_text": "",
            "likes": 12345, **extra}


@pytest.mark.parametrize("target,canonical", [("alpha", True), ("beta", False)])
def test_complete_history_all_formats_hidden_deleted_and_account_scope(stored, target, canonical):
    with stored() as conn:
        for index in range(220):
            post(conn, f"Historic{index}", account=target, canonical=canonical,
                 post_type_label=["Image", "Reel", "Carousel"][index % 3], hidden=1, is_deleted=1)
        post(conn, "Elsewhere", account="rival")
        post(conn, "Future", account=target, canonical=canonical,
             published_at=(datetime.now(UTC) + timedelta(days=10)).isoformat())
        post(conn, "Unpublished", account=target, canonical=canonical, published_at=None)
    history = reuse.load_history(target)
    assert history.post_count == 220
    assert history.filter_candidates([candidate("Historic0"), candidate("Historic219")]) == []
    assert [item["shortcode"] for item in history.filter_candidates([candidate("Elsewhere"), candidate("Future"), candidate("Unpublished")])] == ["Elsewhere", "Future", "Unpublished"]


def test_canonical_uses_enriched_dashboard_metadata_and_full_text(stored):
    full = "Ordinary introduction " * 100 + "Scientists identify extraordinary fossil traces beneath Antarctic volcanic mountains"
    with stored() as conn:
        post(conn, "Published", canonical=True, caption="", hook_text="")
        post(conn, "Published", account="alpha", caption=full, updated_at="2025-01-01", hook_text="Ancient minerals reveal unusual microbial ecosystems under polar glaciers")
    history = reuse.load_history("alpha")
    assert history.post_count == 1
    assert history.filter_candidates([candidate(caption=full.upper().replace(" ", "\n"))]) == []
    assert history.filter_candidates([candidate(ocr_text="Ancient minerals reveal unusual microbial ecosystems under polar glaciers")]) == []
    other = candidate(caption=full[:1200] + " A completely distinct final development")
    assert history.filter_candidates([other])[0]["shortcode"] == "New"


def test_persisted_same_event_stack_excludes_but_other_destination_does_not(stored):
    with stored() as conn:
        post(conn, "Published", account="beta")
        post(conn, "Elsewhere", account="alpha")
        conn.executemany("INSERT INTO topic_stack_members VALUES (?, ?)", [
            ("beta:Published", "same-event"), ("rival:Rewritten", "same-event"),
            ("alpha:Elsewhere", "other-event"), ("rival:Unrelated", "other-event")])
    history = reuse.load_history("beta")
    assert [item["shortcode"] for item in history.filter_candidates([candidate("Rewritten"), candidate("Unrelated")])] == ["Unrelated"]


def test_closed_queue_exact_destination_copies_and_not_yet_imported(stored):
    with stored() as conn:
        queue(conn, "Inspiration--copy-123", post_permalink="https://www.instagram.com/reel/Inspiration/?ref=source")
        queue(conn, "OtherDestination", target="alpha", final="OtherDelivered")
        queue(conn, "AssignedOnly", final="NotPublished", status="scheduled")
        queue(conn, "Legacy", final="LegacyDelivered", final_permalinks="[]", recommended_accounts='["beta","alpha"]')
        queue(conn, "OtherLegacy", final="OtherLegacyDelivered", final_permalinks="[]", recommended_accounts='["alpha","beta"]')
        queue(conn, "NoFinal", final_permalink="", final_permalinks="[]")
        conn.executemany("INSERT INTO topic_stack_members VALUES (?, ?)", [
            ("rival:Inspiration", "published-event"), ("rival:SameEvent", "published-event")])
    history = reuse.load_history("beta")
    assert history.post_count == 2
    codes = ["Inspiration", "Delivered", "SameEvent", "OtherDestination", "AssignedOnly", "Legacy", "OtherLegacy", "NoFinal"]
    assert [item["shortcode"] for item in history.filter_candidates([candidate(code) for code in codes])] == ["OtherDestination", "AssignedOnly", "OtherLegacy", "NoFinal"]
    assert history.filter_candidates([candidate(caption="Scientists announced magnetic superconductors under ambient pressure")]) == []


def test_multi_destination_final_links_are_exact_and_dedup_imported_posts(stored):
    with stored() as conn:
        post(conn, "Delivered", account="beta")
        queue(conn, "Both", final_permalinks=json.dumps([
            {"account": "alpha", "url": "https://instagram.com/p/AlphaDelivery/"},
            {"account": "@BETA", "url": "https://instagram.com/p/Delivered/"}]))
        queue(conn, "Substring", target="betamax", final="UnrelatedDelivery")
    history = reuse.load_history("beta")
    assert history.post_count == 1
    assert [item["shortcode"] for item in history.filter_candidates([candidate("Both"), candidate("Substring")])] == ["Substring"]


def test_semantic_context_retrieves_old_stories_and_tail_matching_snippets(stored):
    with stored() as conn:
        for index in range(230):
            post(conn, f"Routine{index}", published_at="2024-02-01", caption=f"Recipe number {index} presents healthy pasta dishes alongside seasonal vegetables")
        post(conn, "AncientEvent", published_at="2010-01-01",
             caption="Unrelated promotional introductory copy " * 80 + "Europa submarine expedition discovers crystalline oceans beside radioactive geysers",
             hook_text="Routine heading " * 90 + "Europa expedition crystalline oceans radioactive geysers submarine")
        for index in range(3):
            post(conn, f"Latest{index}", published_at=f"2025-01-0{index + 1}", caption="Modern painters design vibrant landscape galleries throughout the city")
    history = reuse.load_history("beta")
    fresh = candidate(caption="Europa submarine expedition reveals radioactive geysers beneath crystalline oceans")
    result = history.filter_candidates([fresh])[0]
    assert result["likes"] == fresh["likes"] and result["key"] == fresh["key"]
    assert result["reuse_uncertain"] is False
    assert result["prior_posts"][0]["key"] == "beta:AncientEvent"
    assert "crystalline oceans" in result["prior_posts"][0]["caption"]
    assert "radioactive geysers" in result["prior_posts"][0]["ocr_text"]
    assert {item["key"] for item in result["prior_posts"]} >= {"beta:Latest0", "beta:Latest1", "beta:Latest2"}
    assert len(result["prior_posts"]) <= 11
    assert [item["shortcode"] for item in history.examples] == ["Latest2", "Latest1", "Latest0"]


def test_uncertain_text_and_common_person_do_not_create_false_exact_matches(stored):
    with stored() as conn:
        post(conn, "Old", caption="Elon Musk", hook_text="Breaking news")
    history = reuse.load_history("beta")
    assert history.filter_candidates([candidate(caption="Elon Musk", ocr_text="")])[0]["reuse_uncertain"] is True
    assert history.filter_candidates([candidate(caption="", ocr_text="")])[0]["reuse_uncertain"] is True
    unrelated = candidate(caption="Elon Musk announces electric vehicle battery research breakthroughs")
    assert history.filter_candidates([unrelated])[0]["reuse_uncertain"] is False


def test_bilingual_terms_retrieve_old_history_without_proving_duplicate_or_text_quality(stored):
    old_spanish = "Cientificos descubrieron cristales gigantes dentro de cuevas submarinas cerca de volcanes"
    with stored() as conn:
        post(conn, "OldSpanish", published_at="2010-01-01", caption=old_spanish)
        for index in range(4):
            post(conn, f"Recent{index}", published_at=f"2025-01-0{index + 1}",
                 caption="New artists present futuristic portraits inspired by illuminated cities")
    history = reuse.load_history("beta")
    english = candidate(caption="Scientists found giant crystals within underwater caves near volcanoes")
    assert "beta:OldSpanish" not in {post["key"] for post in history.filter_candidates([english])[0]["prior_posts"]}
    translated = {**english, "reuse_search_text": old_spanish}
    result = history.filter_candidates([translated])[0]
    assert result["prior_posts"][0]["key"] == "beta:OldSpanish"
    assert result["reuse_uncertain"] is False
    # Generated search text matching a target exactly is not a hard exclusion,
    # and must never repair missing facts in the actual source caption/OCR.
    textless = candidate(caption="", reuse_search_text=old_spanish)
    assert history.filter_candidates([textless])[0]["reuse_uncertain"] is True


def test_fresh_read_only_snapshot_and_filtering_does_not_reread(stored, monkeypatch):
    first = reuse.load_history("beta")
    with stored() as conn:
        post(conn, "Fresh")
    second = reuse.load_history("beta")
    assert (first.post_count, second.post_count) == (0, 1)
    monkeypatch.setattr(db, "connect", lambda: pytest.fail("Filtering must use its loaded history"))
    assert first.filter_candidates([candidate("Fresh")])
    assert second.filter_candidates([candidate("Fresh")]) == []


def test_history_uses_one_snapshot_when_ingestion_finishes_during_read(stored, monkeypatch):
    with stored() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
    original = reuse.external_research._sources
    inserted = False

    def concurrent_ingestion(conn, handles):
        nonlocal inserted
        result = original(conn, handles)
        assert conn.in_transaction
        if not inserted:
            inserted = True
            with stored() as writer:
                post(writer, "ConcurrentPost")
                queue(writer, "ConcurrentSource", final="ConcurrentDelivery")
                writer.executemany("INSERT INTO topic_stack_members VALUES (?, ?)", [
                    ("beta:ConcurrentPost", "concurrent-event"), ("rival:Rewritten", "concurrent-event")])
        return result

    monkeypatch.setattr(reuse.external_research, "_sources", concurrent_ingestion)
    snapshot = reuse.load_history("beta")
    assert snapshot.post_count == 0
    assert len(snapshot.filter_candidates([candidate("ConcurrentSource"), candidate("Rewritten")])) == 2
    fresh = reuse.load_history("beta")
    assert fresh.post_count == 2
    assert fresh.filter_candidates([candidate("ConcurrentSource"), candidate("Rewritten")]) == []


def test_invalid_target_and_incomplete_history_fail_closed(stored, monkeypatch):
    for target in ("rival", "missing", "beta; DROP TABLE posts"):
        with pytest.raises(ValueError):
            reuse.load_history(target)
    with stored() as conn:
        conn.execute("DROP TABLE topic_stack_members")
    with pytest.raises(ValueError, match="Unable to verify"):
        reuse.load_history("beta")


def test_malformed_publication_metadata_is_not_silently_ignored(stored):
    with stored() as conn:
        post(conn, "UnknownDate", published_at="invalid timestamp")
    with pytest.raises(ValueError, match="Unable to verify"):
        reuse.load_history("beta")
    with stored() as conn:
        conn.execute("DELETE FROM dashboard_posts")
        queue(conn, "Source", final_permalinks="invalid json")
    with pytest.raises(ValueError, match="Unable to verify"):
        reuse.load_history("beta")
