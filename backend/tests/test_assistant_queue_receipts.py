from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import assistant_queue_receipts as receipts


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "receipts.sqlite"
    @contextmanager
    def connect():
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    monkeypatch.setattr(receipts, "connect", connect)
    return connect


def request(email="person@example.com"):
    return SimpleNamespace(state=SimpleNamespace(user_email=email, credential_kind="firebase"))


def test_completed_receipt_replays_only_the_public_response(database):
    req, key = request(), str(uuid4())
    assert receipts.claim_turn(req, key, "a" * 64) == {"status": "claimed"}
    receipts.complete_turn(req, key, {"answer": "Lunch reserved.", "language": "en", "token": "private",
        "headers": {"Authorization": "secret"}, "sources": [{"label": "Queue", "token": "secret"}],
        "ui_actions": [{"type": "queue_updated", "authorization": "private"}]})
    other_worker = request()
    result = receipts.claim_turn(other_worker, key, "a" * 64)
    assert result == {"status": "completed", "response": {"answer": "Lunch reserved.", "language": "en",
        "sources": [{"label": "Queue"}], "ui_actions": [{"type": "queue_updated"}]}}
    with database() as conn:
        row = dict(conn.execute("SELECT * FROM assistant_queue_turns").fetchone())
    assert row["fingerprint"] == "a" * 64
    assert "secret" not in json.dumps(row) and "private" not in json.dumps(row)


def test_pending_uncertain_and_conflicting_ids_never_reclaim(database):
    req, key = request(), str(uuid4())
    assert receipts.claim_turn(req, key, "a" * 64)["status"] == "claimed"
    assert receipts.claim_turn(request(), key, "a" * 64)["status"] == "pending"
    assert receipts.claim_turn(request(), key, "b" * 64)["status"] == "conflict"
    receipts.uncertain_turn(req, key)
    assert receipts.claim_turn(request(), key, "a" * 64)["status"] == "pending"
    receipts.release_turn(req, key)
    assert receipts.claim_turn(request(), key, "a" * 64)["status"] == "pending"


def test_owner_scope_and_claim_nonce_prevent_cross_invocation_finishing(database):
    req, key = request(), str(uuid4())
    receipts.claim_turn(req, key, "a" * 64)
    assert receipts.claim_turn(request("other@example.com"), key, "a" * 64)["status"] == "claimed"
    with pytest.raises(ValueError):
        receipts.complete_turn(request(), key, {"answer": "Injected"})
    receipts.release_turn(request(), key)
    assert receipts.claim_turn(request(), key, "a" * 64)["status"] == "pending"
    receipts.complete_turn(req, key, {"answer": "Done"})
    assert receipts.claim_turn(request(), key, "a" * 64)["response"]["answer"] == "Done"


def test_known_pre_write_failure_can_release_its_own_claim(database):
    req, key = request(), str(uuid4())
    receipts.claim_turn(req, key, "a" * 64)
    receipts.release_turn(req, key)
    assert receipts.claim_turn(request(), key, "a" * 64)["status"] == "claimed"


def test_competing_workers_claim_once(database):
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(lambda _: receipts.claim_turn(request(), key, "a" * 64)["status"], range(4)))
    assert statuses.count("claimed") == 1 and statuses.count("pending") == 3


@pytest.mark.parametrize("request_id,fingerprint", [("not-a-uuid", "a" * 64), (None, "a" * 64), (str(uuid4()), "raw request"), (str(uuid4()), "A" * 64)])
def test_invalid_receipt_keys_are_rejected_before_storage(database, request_id, fingerprint):
    with pytest.raises(ValueError):
        receipts.claim_turn(request(), request_id, fingerprint)


def test_invalid_or_oversized_response_does_not_make_claim_replayable(database):
    req, key = request(), str(uuid4())
    receipts.claim_turn(req, key, "a" * 64)
    for value in ({"answer": "x" * 100_000}, {"answer": float("nan")}, {"answer": object()}):
        with pytest.raises(ValueError):
            receipts.complete_turn(req, key, value)
    assert receipts.claim_turn(request(), key, "a" * 64)["status"] == "pending"
