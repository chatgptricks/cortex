"""Suggestion creation, permissions, scheduling and retry regression coverage."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import sqlite3
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app import db, main


REAL_POST_SNAPSHOT = main._queue_v2_post_snapshot


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 5, 10, 3, 20, tzinfo=main.SCHEDULER_TIMEZONE).astimezone(tz)


@pytest.fixture
def queue(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "suggestions.sqlite3")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "ensure_directories", lambda: None)
    db.init_db()
    monkeypatch.setattr(main, "connect", db.connect)
    monkeypatch.setattr(main, "datetime", FixedClock)
    monkeypatch.setattr(main, "_queue_v2_access", lambda request: ("pd@example.com", False, ["pd"]))
    monkeypatch.setattr(main, "_queue_v2_post_snapshot", lambda *args: {})
    monkeypatch.setattr(main, "_queue_v2_publish", lambda *args: 1)
    monkeypatch.setattr(main, "_queue_v2_fetch_source_preview", lambda *args: pytest.fail("Suggestions must not fetch source websites"))
    with db.connect() as conn:
        conn.execute("INSERT INTO dashboard_users (email, operating_role, operating_roles, time_zone, minutes_per_pp, created_at, updated_at) VALUES ('pd@example.com','pd','[\"pd\"]','America/Bogota',20,'','')")
        for handle, group, active in [("target", "sentient", 1), ("other", "sentient", 1), ("competitor", "competitors", 1), ("inactive", "sentient", 0)]:
            conn.execute("INSERT INTO accounts (handle, label, group_name, is_active, created_at, updated_at) VALUES (?, ?, ?, ?, '', '')", (handle, handle, group, active))
        conn.executemany("INSERT INTO queue_designer_accounts VALUES ('pd@example.com', ?, '')", [("target",), ("competitor",), ("inactive",)])
    return db.connect


def suggest(**fields):
    return main.dashboard_queue_v2_create_post_suggestion(request=None, **{
        "source_url": "https://example.com/useful-post", "account": "target", "reason": "A useful new angle for our audience.",
        "title": "Useful angle", "post_type": "Carousel", "idempotency_key": "test-suggestion-1", **fields,
    })


def add_request(conn, *, start, points=3, minutes=10, status="scheduled", date="2026-10-05"):
    cursor = conn.execute(
        """INSERT INTO queue_requests (post_account,post_shortcode,production_points,minutes_per_pp,status,designer_email,
           coordinator_email,scheduled_date,scheduled_start_minutes,created_at,updated_at)
           VALUES ('', ?, ?, ?, ?, 'pd@example.com','vc@example.com',?,?, '', '')""",
        (f"fixture-{start}-{status}", points, minutes, status, date, start),
    )
    return int(cursor.lastrowid)


def test_any_website_is_assigned_to_authenticated_user_with_destination(queue):
    result = suggest()
    request = result["request"]
    assert request["status"] == "scheduled"
    assert request["designerEmail"] == "pd@example.com"
    assert request["coordinatorEmail"] == "pd@example.com"
    assert request["recommendedAccounts"] == ["target"]
    assert request["post"]["permalink"] == "https://example.com/useful-post"
    assert request["post"]["isCustom"] is True
    assert request["productionPoints"] == 3
    assert request["minutesPerPP"] == 20
    assert request["durationMinutes"] == 60
    # Store the Costa Rica Queue timeline even for a Colombian user.
    assert request["scheduledDate"] == "2026-10-05"
    assert request["scheduledStartMinutes"] == 610
    assert result["ticket"]["status"] == "approved"
    assert result["ticket"]["requestId"] == request["id"]
    assert result["ticket"]["requestedAccounts"] == ["target"]
    assert result["alreadyScheduled"] is False


def test_earliest_slot_honors_work_time_holds_drafts_and_duration(queue):
    with queue() as conn:
        add_request(conn, start=610)
        draft_id = add_request(conn, start=710, status="pool")
        conn.execute("INSERT INTO queue_schedule_drafts (request_id,coordinator_email,designer_email,scheduled_date,scheduled_start_minutes,production_points,minutes_per_pp,updated_at) VALUES (?, 'vc@example.com','pd@example.com','2026-10-05',710,2,20,'')", (draft_id,))
        conn.execute("""INSERT INTO queue_tickets (ticket_type,requester_email,status,block_category,title,scheduled_date,scheduled_start_minutes,duration_minutes,created_at,updated_at)
                     VALUES ('time_block','pd@example.com','pending','meeting','Standup','2026-10-05',650,50,'','')""")
    # Work 10:10–10:40 + buffer; meeting10:50–11:40; draft11:50–12:30 + buffer.
    result = suggest()
    assert result["request"]["scheduledStartMinutes"] == 760
    with queue() as conn:
        assert conn.execute("SELECT scheduled_start_minutes FROM queue_requests WHERE id = 1").fetchone()[0] == 610


def test_full_day_rolls_into_next_day_without_moving_existing_work(queue, monkeypatch):
    with queue() as conn:
        add_request(conn, start=610, points=83, minutes=10)
    result = suggest()
    assert result["request"]["scheduledDate"] == "2026-10-06"
    assert result["request"]["scheduledStartMinutes"] == 10


@pytest.mark.parametrize("account", ["other", "competitor", "inactive", "missing"])
def test_rejects_unmanaged_inactive_or_non_sentient_destination(queue, account):
    with pytest.raises(HTTPException) as error:
        suggest(account=account)
    assert error.value.status_code == 403
    with queue() as conn:
        assert conn.execute("SELECT COUNT(*) FROM queue_requests").fetchone()[0] == 0


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///tmp/test", "https://user:password@example.com/", "http://127.0.0.1/", "http://localhost/", "https://example.com/\npost", "https://example.com:bad/post"])
def test_rejects_unsafe_source_links_without_writes(queue, url):
    with pytest.raises(HTTPException) as error:
        suggest(source_url=url)
    assert error.value.status_code == 400


def test_retry_and_duplicate_source_reuse_original_after_progress(queue):
    first = suggest()
    with queue() as conn:
        conn.execute("UPDATE queue_requests SET status = 'in_progress' WHERE id = ?", (first["request"]["id"],))
    repeated = suggest()
    duplicate = suggest(idempotency_key="another-key")
    assert repeated["alreadyScheduled"] is True
    assert duplicate["alreadyScheduled"] is True
    assert repeated["request"]["id"] == duplicate["request"]["id"] == first["request"]["id"]
    assert repeated["request"]["status"] == "in_progress"
    with queue() as conn:
        assert conn.execute("SELECT COUNT(*) FROM queue_requests").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM queue_tickets").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM queue_request_events").fetchone()[0] == 1
    with pytest.raises(HTTPException) as error:
        suggest(source_url="https://example.com/different-post")
    assert error.value.status_code == 409


def test_same_research_post_canonicalizes_permalink_variants(queue, monkeypatch):
    monkeypatch.setattr(main, "_queue_post_exists", lambda account, shortcode: (account, shortcode))
    monkeypatch.setattr(main, "_queue_v2_post_snapshot", lambda *args: {"id": 42, "caption": "Original caption", "type": "Reel", "permalink": "https://www.instagram.com/p/SOURCE42/"})
    first = suggest(source_url="https://www.instagram.com/reel/SOURCE42/?igsh=test", source_account="research", source_shortcode="SOURCE42")
    assert first["request"]["post"]["coverUrl"] == "/api/dashboard/covers/research/42"
    assert first["request"]["post"]["caption"] == "Original caption"
    assert first["request"]["post"]["isCustom"] is False
    second = suggest(source_url="https://instagram.com/p/SOURCE42/", idempotency_key="second")
    assert second["alreadyScheduled"] is True
    assert second["request"]["id"] == first["request"]["id"]
    with pytest.raises(HTTPException) as error:
        suggest(source_url="https://evilinstagram.com/p/SOURCE42/", source_account="research", source_shortcode="SOURCE42", idempotency_key="fake-host")
    assert error.value.status_code == 400


def test_creation_failure_rolls_back_request_ticket_and_retry_key(queue, monkeypatch):
    monkeypatch.setattr(main, "_queue_v2_publish", lambda *args: (_ for _ in ()).throw(RuntimeError("transaction failed")))
    with pytest.raises(RuntimeError):
        suggest()
    with queue() as conn:
        for table in ("queue_requests", "queue_tickets", "queue_post_suggestions"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_concurrent_retries_create_one_request_and_concurrent_posts_do_not_overlap(queue):
    with ThreadPoolExecutor(max_workers=4) as pool:
        retries = list(pool.map(lambda _: suggest(), range(4)))
    assert len({row["request"]["id"] for row in retries}) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda number: suggest(source_url=f"https://example.com/{number}", idempotency_key=f"parallel-{number}"), [2, 3]))
    assert sorted(result["request"]["scheduledStartMinutes"] for result in results) == [680, 750]


def test_postgres_placement_lock_is_shared_and_transaction_scoped():
    calls = []
    connection = SimpleNamespace(is_postgres=True, execute=lambda sql, params: calls.append((sql, params)))
    main._queue_v2_lock_schedule(connection)
    other_worker = []
    main._queue_v2_lock_schedule(SimpleNamespace(is_postgres=True, execute=lambda sql, params: other_worker.append((sql, params))))
    assert calls == other_worker
    assert calls[0][0] == "SELECT pg_advisory_xact_lock(?)"


def test_ledger_schema_idempotent_and_deletion_preserves_retry_record(queue):
    result = suggest()
    with queue() as conn:
        db._ensure_queue_post_suggestions_schema(conn)
        db._ensure_queue_post_suggestions_schema(conn)
        conn.execute("DELETE FROM queue_requests WHERE id = ?", (result["request"]["id"],))
        assert conn.execute("SELECT request_id FROM queue_post_suggestions").fetchone()[0] is None
    with pytest.raises(HTTPException) as error:
        suggest()
    assert error.value.status_code == 410


def test_research_source_can_be_suggested_to_another_owned_account_without_reassigning(queue, monkeypatch):
    monkeypatch.setattr(main, "_queue_post_exists", lambda account, shortcode: (account, shortcode))
    monkeypatch.setattr(main, "_queue_v2_post_snapshot", lambda *args: {"id": 42, "caption": "Original caption"})
    with queue() as conn:
        conn.execute("INSERT INTO queue_designer_accounts VALUES ('pd@example.com','other','')")
    fields = {"source_url": "https://www.instagram.com/p/SOURCE42/", "source_account": "research", "source_shortcode": "SOURCE42"}
    first = suggest(**fields)
    second = suggest(**fields, account="other", idempotency_key="different-destination")
    assert first["request"]["id"] != second["request"]["id"]
    assert second["request"]["post"]["shortcode"].startswith("SOURCE42--copy-")
    assert second["request"]["scheduledStartMinutes"] == 680
    assert second["request"]["recommendedAccounts"] == ["other"]
    with queue() as conn:
        original = conn.execute("SELECT recommended_accounts FROM queue_requests WHERE id = ?", (first["request"]["id"],)).fetchone()
        assert json.loads(original[0]) == ["target"]


def test_suggestion_capability_does_not_bypass_role_or_destination(queue, monkeypatch):
    with pytest.raises(HTTPException) as error:
        suggest(account=None)
    assert error.value.status_code == 400
    monkeypatch.setattr(main, "_queue_v2_access", lambda request: ("unknown@example.com", False, []))
    with pytest.raises(HTTPException) as error:
        suggest()
    assert error.value.status_code == 403


def test_external_hash_route_preserves_exact_suggested_content(queue):
    result = suggest(source_url="https://example.com/#/post/123")
    assert result["request"]["post"]["permalink"] == "https://example.com/#/post/123"
    assert result["request"]["references"] == ["https://example.com/#/post/123"]
    another = suggest(source_url="https://example.com/#/post/456", idempotency_key="another-hash-route")
    assert another["alreadyScheduled"] is False
    assert another["request"]["id"] != result["request"]["id"]


def test_concurrent_completion_and_suggestion_keep_timeline_collision_free(queue, monkeypatch):
    monkeypatch.setattr(main, "utc_now", lambda: "2026-10-05T16:03:20+00:00")
    with queue() as conn:
        active = add_request(conn, start=570, points=12, status="in_progress")
        conn.execute("UPDATE queue_requests SET actual_started_at = '2026-10-05T15:30:00+00:00' WHERE id = ?", (active,))
        add_request(conn, start=700)
    with ThreadPoolExecutor(max_workers=2) as pool:
        completed = pool.submit(main.dashboard_queue_v2_complete, request_id=active, request=None)
        created = pool.submit(suggest)
        completed.result()
        created.result()
    with queue() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM queue_requests ORDER BY scheduled_start_minutes").fetchall()]
    for left, right in zip(rows, rows[1:]):
        assert not main.intervals_conflict(left["scheduled_start_minutes"], main._queue_v2_duration(left), right["scheduled_start_minutes"], main._queue_v2_duration(right))


def test_source_helpers_reuse_scheduler_connection_without_nested_pool_lease(queue, monkeypatch):
    with queue() as conn:
        conn.execute("INSERT INTO accounts (handle,label,group_name,is_active,created_at,updated_at) VALUES ('research','Research','competitors',1,'','')")
        conn.execute("INSERT INTO dashboard_posts (account,shortcode,caption,post_type_label,permalink,created_at,updated_at) VALUES ('research','SOURCE1','A caption','Reel','https://www.instagram.com/p/SOURCE1/','','')")
        monkeypatch.setattr(main, "connect", lambda: pytest.fail("Nested connection lease under schedule lock"))
        snapshot = REAL_POST_SNAPSHOT("research", "SOURCE1", conn)
        assert snapshot["caption"] == "A caption"
        assert main._queue_post_id("research", "SOURCE1", conn) == snapshot["id"]
