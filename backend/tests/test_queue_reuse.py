"""Explicit Create Post can reuse a source without reopening old Queue work."""
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app import db, main


SOURCE = "https://www.instagram.com/p/CaseSensitive42/"


@pytest.fixture
def queue(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "queue-reuse.sqlite3")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "ensure_directories", lambda: None)
    db.init_db()
    monkeypatch.setattr(main, "connect", db.connect)
    monkeypatch.setattr(main, "_queue_v2_access", lambda request, **kwargs: ("vc@example.com", False, ["vc", "pd"]))
    monkeypatch.setattr(main, "_queue_v2_publish", lambda *args: 1)
    monkeypatch.setattr(main, "_queue_v2_slack_log", lambda **kwargs: True)
    monkeypatch.setattr(main, "_queue_v2_fetch_source_preview", lambda *args: (_ for _ in ()).throw(HTTPException(422, "Preview blocked")))
    with db.connect() as conn:
        conn.execute("INSERT INTO accounts (handle,label,group_name,is_active,is_canonical,created_at,updated_at) VALUES ('research','Research','competitors',1,0,'','')")
        conn.execute("INSERT INTO accounts (handle,label,group_name,is_active,is_canonical,created_at,updated_at) VALUES ('target','Target','sentient',1,0,'','')")
    return db.connect


def original(conn, *, status="closed", url=SOURCE, copied=False):
    shortcode = "CaseSensitive42--copy-old" if copied else "CaseSensitive42"
    cursor = conn.execute(
        """INSERT INTO queue_requests (
             post_account,post_shortcode,post_title,post_permalink,post_caption,post_type,cover_url,
             production_points,status,designer_email,coordinator_email,recommended_accounts,
             scheduled_date,scheduled_start_minutes,actual_started_at,completed_at,closed_at,
             final_permalink,final_permalinks,attachments,brief,notes,created_at,updated_at
           ) VALUES ('research', ?, 'Successful original', ?, 'Original source caption', 'Reel',
             '/api/dashboard/covers/research/42', 5, ?, 'old-pd@example.com','other-vc@example.com',
             '["target"]','2026-10-02',600,'2026-10-02T16:00:00Z','2026-10-02T17:00:00Z','2026-10-02T18:00:00Z',
             'https://www.instagram.com/p/DeliveryOne/',
             '[{"account":"target","url":"https://www.instagram.com/p/DeliveryOne/"},{"account":"second","url":"https://www.instagram.com/p/DeliveryTwo/"}]',
             '[{"id":"original-file"}]','Previous brief','Previous notes','2026-10-01T16:00:00Z','2026-10-02T18:00:00Z')""",
        (shortcode, url, status),
    )
    request_id = int(cursor.lastrowid)
    main._queue_v2_log(conn, request_id, "old-pd@example.com", "completed", {"old": True})
    return request_id


def create(**fields):
    return main.dashboard_queue_v2_create(request=None, **{
        "title": "Use the successful angle again", "source_url": SOURCE,
        "post_type": "Carousel", "production_points": 3, "priority": "urgent",
        "brief": "New brief", "notes": "New notes", "idempotency_key": "attempt-one", **fields,
    })


def preview(url=SOURCE):
    return main.dashboard_queue_v2_source_preview(request=None, source_url=url)["preview"]


@pytest.mark.parametrize("status", ["pool", "scheduled", "in_progress", "completed", "closed", "cancelled"])
def test_reusing_source_creates_independent_pool_and_preserves_every_old_field(queue, status):
    with queue() as conn:
        old_id = original(conn, status=status)
        before = dict(conn.execute("SELECT * FROM queue_requests WHERE id = ?", (old_id,)).fetchone())
        events = [dict(row) for row in conn.execute("SELECT * FROM queue_request_events WHERE request_id = ?", (old_id,))]
    result = create()
    new = result["request"]
    assert new["id"] != old_id
    assert new["status"] == "pool"
    assert new["designerEmail"] is None
    assert new["recommendedAccounts"] == []
    assert new["scheduledDate"] is None
    assert new["actualStartedAt"] is None
    assert new["completedAt"] is None
    assert new["finalPermalink"] is None
    assert new["finalPermalinks"] == []
    assert new["attachments"] == []
    assert new["post"]["shortcode"].startswith("manual-")
    assert new["post"]["permalink"] == SOURCE
    assert new["post"]["caption"] == "Original source caption"
    assert new["post"]["coverUrl"] == "/api/dashboard/covers/research/42"
    assert new["post"]["type"] == "Carousel"
    assert new["post"]["title"] == "Use the successful angle again"
    assert new["productionPoints"] == 3
    assert new["priority"] == "urgent"
    assert new["brief"] == "New brief"
    assert new["notes"] == "New notes"
    assert result["alreadyCreated"] is False
    with queue() as conn:
        assert dict(conn.execute("SELECT * FROM queue_requests WHERE id = ?", (old_id,)).fetchone()) == before
        assert [dict(row) for row in conn.execute("SELECT * FROM queue_request_events WHERE request_id = ?", (old_id,))] == events
        details = json.loads(conn.execute("SELECT details FROM queue_request_events WHERE request_id = ?", (new["id"],)).fetchone()[0])
        assert details["reusedFromRequestId"] == old_id


@pytest.mark.parametrize("url", [
    "https://instagram.com/p/CaseSensitive42/", "http://m.instagram.com/reel/CaseSensitive42/?igsh=tracking#fragment",
    "https://www.instagram.com/reels/CaseSensitive42", "https://www.instagram.com/tv/CaseSensitive42/",
])
def test_preview_recognizes_instagram_variants_without_external_fetch(queue, url):
    with queue() as conn:
        old_id = original(conn, copied=True)
    result = preview(url)
    assert result["queueHistory"] == {"count": 1, "latestRequestId": old_id, "latestStatus": "closed", "lastUsedAt": "2026-10-02T18:00:00Z"}
    assert result["title"] == "Successful original"
    assert result["imageUrl"] == "/api/dashboard/covers/research/42"
    assert result["postType"] == "Reel"
    with pytest.raises(HTTPException):
        preview("https://www.instagram.com/p/casesensitive42/")


def test_recognizes_each_published_delivery_without_copying_inspiration_metadata(queue):
    with queue() as conn:
        old_id = original(conn)
    for code in ("DeliveryOne", "DeliveryTwo"):
        url = f"https://m.instagram.com/reel/{code}/?igsh=test"
        result = preview(url)
        assert result["queueHistory"]["latestRequestId"] == old_id
        assert result["imageUrl"] == ""
        assert result["description"] == ""
        new = create(source_url=url, idempotency_key=code)["request"]
        assert new["status"] == "pool"
        assert new["post"]["coverUrl"] == ""
        assert new["post"]["caption"] == "New brief"
    with queue() as conn:
        assert conn.execute("SELECT status FROM queue_requests WHERE id = ?", (old_id,)).fetchone()[0] == "closed"


def test_indexed_research_source_keeps_server_cover_and_honors_editable_fields(queue):
    with queue() as conn:
        old_id = original(conn)
        conn.execute("INSERT INTO dashboard_posts (id, account,shortcode,caption,post_type_label,permalink,created_at,updated_at) VALUES (99,'research','CaseSensitive42','Updated Research caption','Reel',?,'','')", (SOURCE,))
    result = preview()
    assert result["dashboardPost"] == {"account": "research", "shortcode": "CaseSensitive42"}
    assert result["imageUrl"] == "/api/dashboard/covers/research/99"
    new = create(source_description="Edited description", source_image_url=result["imageUrl"])["request"]
    assert new["post"]["caption"] == "Edited description"
    assert new["post"]["coverUrl"] == "/api/dashboard/covers/research/99"
    assert new["post"]["type"] == "Carousel"
    blank = create(source_description="", source_image_url="", idempotency_key="cleared")["request"]
    assert blank["post"]["caption"] == ""
    assert blank["post"]["coverUrl"] == ""
    assert new["id"] != old_id


def test_external_source_reuse_keeps_query_parameters_and_does_not_require_fetch(queue):
    url = "https://example.com/article?id=123&part=2#details"
    with queue() as conn:
        old_id = original(conn, url=url)
    assert preview(url)["queueHistory"]["latestRequestId"] == old_id
    first = create(source_url=url)
    second = create(source_url=url, idempotency_key="intentional-another")
    assert first["request"]["id"] != second["request"]["id"]
    assert second["request"]["post"]["permalink"] == url
    assert preview(url)["queueHistory"]["count"] == 3
    with pytest.raises(HTTPException):
        preview("https://example.com/article?id=123&part=3#details")


def test_same_attempt_retries_recover_task_even_after_progress_and_do_not_repeat_events(queue):
    with queue() as conn:
        conn.execute("INSERT INTO dashboard_posts (id,account,shortcode,caption,post_type_label,permalink,created_at,updated_at) VALUES (99,'research','CaseSensitive42','Original Research caption','Reel',?,'','')", (SOURCE,))
    first = create()["request"]
    with queue() as conn:
        conn.execute("UPDATE queue_requests SET status = 'in_progress', designer_email = 'reassigned@example.com' WHERE id = ?", (first["id"],))
        conn.execute("UPDATE dashboard_posts SET caption = 'New metadata after ingestion' WHERE id = 99")
    retry = create()
    assert retry["alreadyCreated"] is True
    assert retry["request"]["id"] == first["id"]
    assert retry["request"]["status"] == "in_progress"
    assert retry["request"]["designerEmail"] == "reassigned@example.com"
    assert retry["request"]["post"]["caption"] == "Original Research caption"
    with queue() as conn:
        assert conn.execute("SELECT COUNT(*) FROM queue_requests").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM queue_request_events").fetchone()[0] == 1
    with pytest.raises(HTTPException) as error:
        create(title="Different new content")
    assert error.value.status_code == 409


def test_concurrent_retries_create_once_and_distinct_attempts_create_independently(queue):
    with ThreadPoolExecutor(max_workers=4) as pool:
        retries = list(pool.map(lambda _: create(), range(4)))
    assert len({item["request"]["id"] for item in retries}) == 1
    assert sum(not item["alreadyCreated"] for item in retries) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        new = list(pool.map(lambda key: create(idempotency_key=key), ["second", "third"]))
    assert len({item["request"]["id"] for item in [*retries, *new]}) == 3


def test_ledger_is_additive_and_deleted_task_does_not_make_retry_create_again(queue):
    result = create()
    with queue() as conn:
        db._ensure_queue_create_attempts_schema(conn)
        db._ensure_queue_create_attempts_schema(conn)
        conn.execute("DELETE FROM queue_requests WHERE id = ?", (result["request"]["id"],))
        assert conn.execute("SELECT request_id FROM queue_create_attempts").fetchone()[0] is None
    with pytest.raises(HTTPException) as error:
        create()
    assert error.value.status_code == 410
    assert create(idempotency_key="new-after-retention")["request"]["status"] == "pool"


def test_failed_creation_rolls_back_request_event_and_attempt(queue, monkeypatch):
    monkeypatch.setattr(main, "_queue_v2_publish", lambda *args: (_ for _ in ()).throw(RuntimeError("failed publication")))
    with pytest.raises(RuntimeError):
        create()
    with queue() as conn:
        for table in ("queue_requests", "queue_request_events", "queue_create_attempts"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_create_does_not_change_research_send_to_pool_duplicate_guard(queue, monkeypatch):
    with queue() as conn:
        old_id = original(conn)
    monkeypatch.setattr(main, "_queue_post_exists", lambda account, shortcode: (account, shortcode))
    monkeypatch.setattr(main, "_queue_v2_post_snapshot", lambda *args: {"id": 42})
    with pytest.raises(HTTPException) as error:
        main.dashboard_queue_v2_pool(None, account="research", shortcode="CaseSensitive42", production_points=3)
    assert error.value.status_code == 409
    with queue() as conn:
        assert conn.execute("SELECT COUNT(*) FROM queue_requests").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM queue_requests WHERE id = ?", (old_id,)).fetchone()[0] == "closed"


def test_creation_permissions_and_private_history_remain_narrow(queue, monkeypatch):
    with queue() as conn:
        original(conn)
    monkeypatch.setattr(main, "_queue_v2_access", lambda request, **kwargs: ("pd@example.com", False, ["pd"]))
    request = SimpleNamespace(state=SimpleNamespace(can_self_assign=False))
    with pytest.raises(HTTPException) as error:
        main.dashboard_queue_v2_create(request, title="Forbidden", source_url=SOURCE)
    assert error.value.status_code == 403
    request.state.can_self_assign = True
    context = main._queue_v2_create_source_context(SOURCE, caller="pd@example.com", coordinator=False)
    assert "queueHistory" not in context
    allowed = main.dashboard_queue_v2_create(request, title="My own Pool", source_url=SOURCE)
    assert allowed["request"]["coordinatorEmail"] == "pd@example.com"
    assert allowed["request"]["status"] == "pool"
    monkeypatch.setattr(main, "_queue_v2_access", lambda request, **kwargs: ("admin@example.com", True, ["pd"]))
    assert main.dashboard_queue_v2_create(request, title="Admin Pool", source_url=SOURCE)["request"]["status"] == "pool"
