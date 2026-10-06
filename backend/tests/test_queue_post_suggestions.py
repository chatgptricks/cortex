"""Suggestions require review before one atomic, retry-safe Queue assignment."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app import db, main


REAL_POST_SNAPSHOT = main._queue_v2_post_snapshot
REAL_ACCESS = main._queue_v2_access


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
    monkeypatch.setattr(main, "_queue_v2_access", lambda request, coordinator=False: ("vc@example.com", False, ["vc"]) if coordinator else ("pd@example.com", False, ["pd"]))
    monkeypatch.setattr(main, "_queue_v2_slack_log", lambda **kwargs: True)
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


def review(result, action="approve", **fields):
    return main.dashboard_queue_v2_review_ticket(ticket_id=result["ticket"]["id"], request=None, action=action, review_note=None, **fields)


def approved_suggestion(**fields):
    return review(suggest(**fields))


def add_request(conn, *, start, points=3, minutes=10, status="scheduled", date="2026-10-05"):
    cursor = conn.execute(
        """INSERT INTO queue_requests (post_account,post_shortcode,production_points,minutes_per_pp,status,designer_email,
           coordinator_email,scheduled_date,scheduled_start_minutes,created_at,updated_at)
           VALUES ('', ?, ?, ?, ?, 'pd@example.com','vc@example.com',?,?, '', '')""",
        (f"fixture-{start}-{status}", points, minutes, status, date, start),
    )
    return int(cursor.lastrowid)


def test_any_website_is_assigned_only_after_approval_to_original_user_and_destination(queue):
    pending = suggest()
    assert pending["request"] is None
    assert pending["ticket"]["status"] == "pending"
    assert pending["pendingTicketCount"] == 1
    assert pending["ticket"]["requestId"] is None
    assert pending["ticket"]["scheduledDate"] is None
    assert pending["ticket"]["scheduledStartMinutes"] is None
    assert pending["ticket"]["durationMinutes"] is None
    with queue() as conn:
        for table in ("queue_requests", "queue_schedule_drafts", "queue_request_events"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert main._queue_v2_time_occupied(conn, "pd@example.com", None) == []
    result = review(pending)
    request = result["request"]
    assert request["status"] == "scheduled"
    assert request["designerEmail"] == "pd@example.com"
    assert request["coordinatorEmail"] == "vc@example.com"
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
    assert result["pendingTicketCount"] == 0


def test_earliest_slot_honors_work_time_holds_drafts_and_duration(queue):
    with queue() as conn:
        add_request(conn, start=610)
        draft_id = add_request(conn, start=710, status="pool")
        conn.execute("INSERT INTO queue_schedule_drafts (request_id,coordinator_email,designer_email,scheduled_date,scheduled_start_minutes,production_points,minutes_per_pp,updated_at) VALUES (?, 'vc@example.com','pd@example.com','2026-10-05',710,2,20,'')", (draft_id,))
        conn.execute("""INSERT INTO queue_tickets (ticket_type,requester_email,status,block_category,title,scheduled_date,scheduled_start_minutes,duration_minutes,created_at,updated_at)
                     VALUES ('time_block','pd@example.com','pending','meeting','Standup','2026-10-05',650,50,'','')""")
    # Work 10:10–10:40 + buffer; meeting10:50–11:40; draft11:50–12:30 + buffer.
    result = approved_suggestion()
    assert result["request"]["scheduledStartMinutes"] == 760
    with queue() as conn:
        assert conn.execute("SELECT scheduled_start_minutes FROM queue_requests WHERE id = 1").fetchone()[0] == 610


def test_full_day_rolls_into_next_day_without_moving_existing_work(queue, monkeypatch):
    with queue() as conn:
        add_request(conn, start=610, points=83, minutes=10)
    result = approved_suggestion()
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
    first = approved_suggestion()
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
    pending = suggest(source_url="https://www.instagram.com/reel/SOURCE42/?igsh=test", source_account="research", source_shortcode="SOURCE42")
    # Approval uses the durable snapshot even if Research is removed meanwhile.
    monkeypatch.setattr(main, "_queue_v2_post_snapshot", lambda *args: {})
    first = review(pending)
    assert first["request"]["post"]["coverUrl"] == "/api/dashboard/covers/research/42"
    assert first["request"]["post"]["caption"] == "Original caption"
    assert first["request"]["post"]["isCustom"] is False
    second = suggest(source_url="https://instagram.com/p/SOURCE42/", idempotency_key="second")
    assert second["alreadyScheduled"] is True
    assert second["request"]["id"] == first["request"]["id"]
    monkeypatch.setattr(main, "_queue_v2_post_snapshot", lambda *args: {"id": 42})
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


def test_concurrent_submissions_and_approvals_are_exactly_once_and_do_not_overlap(queue):
    with ThreadPoolExecutor(max_workers=4) as pool:
        retries = list(pool.map(lambda _: suggest(), range(4)))
    assert len({row["ticket"]["id"] for row in retries}) == 1
    assert all(row["request"] is None for row in retries)
    with ThreadPoolExecutor(max_workers=4) as pool:
        approvals = list(pool.map(review, retries))
    assert len({row["request"]["id"] for row in approvals}) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda number: approved_suggestion(source_url=f"https://example.com/{number}", idempotency_key=f"parallel-{number}"), [2, 3]))
    assert sorted(result["request"]["scheduledStartMinutes"] for result in results) == [680, 750]
    with queue() as conn:
        assert conn.execute("SELECT COUNT(*) FROM queue_requests").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM queue_request_events").fetchone()[0] == 3


def test_postgres_placement_lock_is_shared_and_transaction_scoped():
    calls = []
    connection = SimpleNamespace(is_postgres=True, execute=lambda sql, params: calls.append((sql, params)))
    main._queue_v2_lock_schedule(connection)
    other_worker = []
    main._queue_v2_lock_schedule(SimpleNamespace(is_postgres=True, execute=lambda sql, params: other_worker.append((sql, params))))
    assert calls == other_worker
    assert calls[0][0] == "SELECT pg_advisory_xact_lock(?)"


def test_ledger_schema_idempotent_and_deletion_preserves_retry_record(queue):
    result = approved_suggestion()
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
    first = approved_suggestion(**fields)
    second = approved_suggestion(**fields, account="other", idempotency_key="different-destination")
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
    result = approved_suggestion(source_url="https://example.com/#/post/123")
    assert result["request"]["post"]["permalink"] == "https://example.com/#/post/123"
    assert result["request"]["references"] == ["https://example.com/#/post/123"]
    another = approved_suggestion(source_url="https://example.com/#/post/456", idempotency_key="another-hash-route")
    assert another["request"]["id"] != result["request"]["id"]


def test_concurrent_completion_and_suggestion_keep_timeline_collision_free(queue, monkeypatch):
    monkeypatch.setattr(main, "utc_now", lambda: "2026-10-05T16:03:20+00:00")
    with queue() as conn:
        active = add_request(conn, start=570, points=12, status="in_progress")
        conn.execute("UPDATE queue_requests SET actual_started_at = '2026-10-05T15:30:00+00:00' WHERE id = ?", (active,))
        add_request(conn, start=700)
    with ThreadPoolExecutor(max_workers=2) as pool:
        completed = pool.submit(main.dashboard_queue_v2_complete, request_id=active, request=None)
        created = pool.submit(approved_suggestion)
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


def test_pending_retries_keep_payload_and_do_not_reserve_time(queue):
    first = suggest()
    duplicate = suggest(idempotency_key="second-tab")
    assert duplicate["alreadySubmitted"] is True
    assert duplicate["alreadyScheduled"] is False
    assert duplicate["request"] is None
    assert duplicate["ticket"]["id"] == first["ticket"]["id"]
    assert duplicate["pendingTicketCount"] == 1
    expected = {"sourceUrl": "https://example.com/useful-post", "account": "target", "title": "Useful angle", "postType": "Carousel", "sourceAccount": "", "sourceShortcode": ""}
    assert first["ticket"]["suggestion"] == expected
    with queue() as conn:
        projected = main._queue_v2_ticket(main._queue_v2_ticket_rows(conn)[0])
        assert projected["suggestion"] == expected
        assert conn.execute("SELECT COUNT(*) FROM queue_requests").fetchone()[0] == 0
        assert main._queue_v2_time_occupied(conn, "pd@example.com", None) == []
    approved = review(first)
    assert suggest(idempotency_key="second-tab")["request"]["id"] == approved["request"]["id"]


def test_rejection_creates_no_assignment_and_explicit_new_attempt_can_resubmit(queue):
    first = suggest()
    rejected = review(first, "reject", account="ignored")
    assert rejected["ticket"]["status"] == "rejected"
    assert rejected["request"] is None
    assert rejected["pendingTicketCount"] == 0
    assert review(first, "reject")["ticket"]["status"] == "rejected"
    replay = suggest()
    assert replay["alreadySubmitted"] is True
    assert replay["ticket"]["id"] == first["ticket"]["id"]
    assert replay["ticket"]["status"] == "rejected"
    resubmitted = suggest(idempotency_key="explicit-resubmission")
    assert resubmitted["ticket"]["id"] != first["ticket"]["id"]
    assert resubmitted["ticket"]["status"] == "pending"
    with queue() as conn:
        assert conn.execute("SELECT COUNT(*) FROM queue_requests").fetchone()[0] == 0
    with pytest.raises(HTTPException) as error:
        review(first, "approve")
    assert error.value.status_code == 409


def test_approval_uses_approval_time_current_duration_and_work_added_while_pending(queue, monkeypatch):
    pending = suggest()
    class ApprovalClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 7, 12, 6, tzinfo=main.SCHEDULER_TIMEZONE).astimezone(tz)
    monkeypatch.setattr(main, "datetime", ApprovalClock)
    with queue() as conn:
        conn.execute("UPDATE dashboard_users SET operating_roles = '[\"trainee\"]', minutes_per_pp = NULL WHERE email = 'pd@example.com'")
        add_request(conn, start=730, date="2026-10-07")
    approved = review(pending)
    assert approved["request"]["scheduledDate"] == "2026-10-07"
    assert approved["request"]["scheduledStartMinutes"] == 770
    assert approved["request"]["minutesPerPP"] == main.QUEUE_V2_TRAINEE_MINUTES_PER_PP
    assert approved["request"]["durationMinutes"] == 48


@pytest.mark.parametrize("change", ["unmanaged", "inactive", "group_changed", "deleted_account", "removed_user"])
def test_approval_rechecks_current_owner_and_account_and_leaves_pending_on_drift(queue, change):
    pending = suggest()
    with queue() as conn:
        if change == "unmanaged":
            conn.execute("DELETE FROM queue_designer_accounts WHERE designer_email = 'pd@example.com'")
        elif change == "inactive":
            conn.execute("UPDATE accounts SET is_active = 0 WHERE handle = 'target'")
        elif change == "group_changed":
            conn.execute("UPDATE accounts SET group_name = 'competitors' WHERE handle = 'target'")
        elif change == "deleted_account":
            conn.execute("DELETE FROM accounts WHERE handle = 'target'")
        else:
            conn.execute("DELETE FROM dashboard_users WHERE email = 'pd@example.com'")
    with pytest.raises(HTTPException) as error:
        review(pending)
    assert error.value.status_code == 409
    with queue() as conn:
        assert conn.execute("SELECT COUNT(*) FROM queue_requests").fetchone()[0] == 0
        ticket = conn.execute("SELECT * FROM queue_tickets WHERE id = ?", (pending["ticket"]["id"],)).fetchone()
        assert ticket["status"] == "pending"
        assert ticket["reviewed_at"] is None
    # The pending receipt remains recoverable after an account-access change.
    assert suggest()["ticket"]["id"] == pending["ticket"]["id"]
    assert review(pending, "reject")["ticket"]["status"] == "rejected"


def test_suggester_cannot_approve_and_coordinator_cannot_change_selected_destination(queue, monkeypatch):
    pending = suggest()
    with queue() as conn:
        conn.execute("INSERT INTO queue_designer_accounts VALUES ('pd@example.com','other','')")
    with pytest.raises(HTTPException) as error:
        review(pending, account="other")
    assert error.value.status_code == 409
    monkeypatch.setattr(main, "_queue_v2_access", REAL_ACCESS)
    request = SimpleNamespace(state=SimpleNamespace(user_email="pd@example.com", is_admin=False, operating_roles=["pd"], operating_role="pd"))
    with pytest.raises(HTTPException) as error:
        main.dashboard_queue_v2_review_ticket(ticket_id=pending["ticket"]["id"], request=request, action="approve")
    assert error.value.status_code == 403
    with queue() as conn:
        assert conn.execute("SELECT COUNT(*) FROM queue_requests").fetchone()[0] == 0


def test_legacy_pending_suggestion_requires_managed_account_and_can_then_schedule(queue):
    with queue() as conn:
        ticket_id = conn.execute("""INSERT INTO queue_tickets
            (ticket_type,requester_email,status,block_category,title,reason,created_at,updated_at)
            VALUES ('time_block','pd@example.com','pending','post_suggestion','https://example.com/legacy','A good post','','')""").lastrowid
        projected = main._queue_v2_ticket(dict(conn.execute("SELECT * FROM queue_tickets WHERE id = ?", (ticket_id,)).fetchone()))
    assert projected["suggestion"]["account"] == ""
    legacy = {"ticket": projected}
    with pytest.raises(HTTPException) as error:
        review(legacy)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        review(legacy, account="other")
    assert error.value.status_code == 409
    approved = review(legacy, account="target")
    assert approved["request"]["recommendedAccounts"] == ["target"]
    assert approved["request"]["post"]["permalink"] == "https://example.com/legacy"
    assert approved["ticket"]["suggestion"]["account"] == "target"


def test_failed_approval_rolls_back_task_and_ledger_and_preserves_pending_payload(queue, monkeypatch):
    pending = suggest()
    monkeypatch.setattr(main, "_queue_v2_publish", lambda *args: (_ for _ in ()).throw(RuntimeError("transaction failed")))
    with pytest.raises(RuntimeError):
        review(pending)
    with queue() as conn:
        assert conn.execute("SELECT COUNT(*) FROM queue_requests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM queue_request_events").fetchone()[0] == 0
        assert conn.execute("SELECT request_id FROM queue_post_suggestions").fetchone()[0] is None
        ticket = conn.execute("SELECT * FROM queue_tickets").fetchone()
        assert ticket["status"] == "pending"
        assert ticket["request_id"] is None
        assert main._queue_v2_ticket(dict(ticket))["suggestion"] == pending["ticket"]["suggestion"]


def test_pending_suggestions_survive_history_retention(queue):
    pending = suggest()
    with queue() as conn:
        conn.execute("UPDATE queue_tickets SET created_at = '2020-01-01T00:00:00+00:00'")
        main._queue_v2_purge_expired(conn)
        assert conn.execute("SELECT status FROM queue_tickets WHERE id = ?", (pending["ticket"]["id"],)).fetchone()[0] == "pending"


def test_approved_retry_is_read_only_after_reassignment_and_access_change(queue):
    approved = approved_suggestion()
    with queue() as conn:
        conn.execute("UPDATE queue_requests SET designer_email = 'another@example.com', status = 'in_progress' WHERE id = ?", (approved["request"]["id"],))
        conn.execute("DELETE FROM queue_designer_accounts WHERE designer_email = 'pd@example.com'")
        before = dict(conn.execute("SELECT * FROM queue_requests WHERE id = ?", (approved["request"]["id"],)).fetchone())
    assert review(approved)["request"]["designerEmail"] == "another@example.com"
    assert suggest()["request"]["designerEmail"] == "another@example.com"
    with queue() as conn:
        assert dict(conn.execute("SELECT * FROM queue_requests WHERE id = ?", (approved["request"]["id"],)).fetchone()) == before
        assert conn.execute("SELECT COUNT(*) FROM queue_requests").fetchone()[0] == 1


def test_concurrent_approval_and_rejection_commit_one_consistent_result(queue):
    pending = suggest()
    def decide(action):
        try:
            return review(pending, action)
        except HTTPException as error:
            assert error.status_code == 409
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, ["approve", "reject"]))
    assert sum(result is not None for result in results) == 1
    with queue() as conn:
        ticket = conn.execute("SELECT * FROM queue_tickets").fetchone()
        count = conn.execute("SELECT COUNT(*) FROM queue_requests").fetchone()[0]
        assert count == int(ticket["status"] == "approved")
        assert bool(ticket["request_id"]) == (ticket["status"] == "approved")


def test_coordinator_who_suggests_also_requires_explicit_approval(queue, monkeypatch):
    monkeypatch.setattr(main, "_queue_v2_access", lambda request, coordinator=False: ("pd@example.com", True, ["pd", "vc"]))
    result = suggest()
    assert result["ticket"]["status"] == "pending"
    assert result["request"] is None
    assert result["pendingTicketCount"] == 1
    with queue() as conn:
        assert conn.execute("SELECT COUNT(*) FROM queue_requests").fetchone()[0] == 0


def test_additive_schema_preserves_preexisting_autoapproved_assignments(queue):
    approved = approved_suggestion()
    with queue() as conn:
        # Represent a pre-approval-release record and migrate its old schema.
        conn.execute("UPDATE queue_tickets SET reviewer_email = NULL, review_note = 'Automatically scheduled for the suggesting user.'")
        conn.execute("ALTER TABLE queue_tickets DROP COLUMN suggestion_payload")
        original_request = dict(conn.execute("SELECT * FROM queue_requests").fetchone())
        original_ticket = dict(conn.execute("SELECT * FROM queue_tickets").fetchone())
        db._ensure_queue_post_suggestions_schema(conn)
        db._ensure_queue_post_suggestions_schema(conn)
        migrated = dict(conn.execute("SELECT * FROM queue_tickets").fetchone())
        assert migrated.pop("suggestion_payload") == "{}"
        assert migrated == original_ticket
        assert dict(conn.execute("SELECT * FROM queue_requests").fetchone()) == original_request
    retried = review(approved)
    assert retried["request"]["id"] == approved["request"]["id"]
    assert retried["ticket"]["reviewerEmail"] is None
    with queue() as conn:
        assert dict(conn.execute("SELECT * FROM queue_requests").fetchone()) == original_request


def test_historical_approved_unlinked_ticket_replay_never_creates_assignment(queue):
    with queue() as conn:
        ticket_id = conn.execute("""INSERT INTO queue_tickets
            (ticket_type,requester_email,status,block_category,title,reason,created_at,updated_at)
            VALUES ('time_block','pd@example.com','approved','post_suggestion','https://example.com/old-approved','Old approved source','','')""").lastrowid
    result = review({"ticket": {"id": ticket_id}})
    assert result["ticket"]["status"] == "approved"
    assert result["request"] is None
    with queue() as conn:
        assert conn.execute("SELECT COUNT(*) FROM queue_requests").fetchone()[0] == 0
