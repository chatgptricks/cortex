from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main, slack_alerts
from test_queue_tickets import _ticket_database, _isolate


def request(*, admin=False, dev=False):
    return SimpleNamespace(state=SimpleNamespace(user_email="admin@example.com", is_admin=admin, is_dev=dev))


@pytest.fixture
def storage(monkeypatch, tmp_path):
    path = tmp_path / "requests.sqlite3"
    _ticket_database(path)
    connect = _isolate(monkeypatch, path)
    monkeypatch.setattr(slack_alerts, "notify_new_account_request", lambda **kwargs: True)
    return connect


def test_admin_requests_without_creating_account(storage):
    result = main.admin_request_new_account(request(admin=True), "@New.Account", "sentient", "New client")
    assert result["ticket"]["type"] == "new_account"
    assert result["ticket"]["requestedAccounts"] == ["new.account"]
    assert result["slackDelivered"] is True
    with storage() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    with pytest.raises(HTTPException) as error:
        main.admin_request_new_account(request(admin=True), "new.account")
    assert error.value.status_code == 409


def test_only_dev_can_create_even_with_password(monkeypatch):
    monkeypatch.setattr(main, "TRICKS_DASH_REFRESH_PASSWORD", "test")
    with pytest.raises(HTTPException) as error:
        main.admin_create_account(request(admin=True), "test", "new.account")
    assert error.value.status_code == 403
    monkeypatch.setattr(main, "create_account", lambda *args: {"handle": args[0]})
    assert main.admin_create_account(request(dev=True), "test", "new.account")["account"]["handle"] == "new.account"


def test_only_dev_reviews_and_account_must_exist(storage):
    ticket = main.admin_request_new_account(request(admin=True), "new.account")["ticket"]
    for action in ("approve", "reject"):
        with pytest.raises(HTTPException) as error:
            main.dashboard_queue_v2_review_ticket(ticket["id"], request(admin=True), action)
        assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        main.dashboard_queue_v2_review_ticket(ticket["id"], request(dev=True), "approve")
    assert error.value.status_code == 409
    with storage() as conn:
        conn.execute("INSERT INTO accounts VALUES ('new.account', 'competitors', 1)")
    result = main.dashboard_queue_v2_review_ticket(ticket["id"], request(dev=True), "approve")
    assert result["ticket"]["status"] == "approved"


def test_slack_failure_keeps_request(storage, monkeypatch):
    monkeypatch.setattr(slack_alerts, "notify_new_account_request", lambda **kwargs: False)
    result = main.admin_request_new_account(request(admin=True), "new.account")
    assert result["slackDelivered"] is False
    with storage() as conn:
        assert conn.execute("SELECT status FROM queue_tickets").fetchone()[0] == "pending"


def test_non_admin_and_invalid_handles_rejected(storage):
    with pytest.raises(HTTPException) as error:
        main.admin_request_new_account(request(), "new.account")
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        main.admin_request_new_account(request(admin=True), "https://instagram.com/test")
    assert error.value.status_code == 400


def test_pending_request_survives_retention(storage):
    main.admin_request_new_account(request(admin=True), "new.account")
    with storage() as conn:
        conn.execute("UPDATE queue_tickets SET created_at = '2020-01-01'")
        main._queue_v2_purge_expired(conn)
        assert conn.execute("SELECT COUNT(*) FROM queue_tickets").fetchone()[0] == 1


@pytest.mark.parametrize("correct_recipient", [True, False])
def test_slack_notification_is_private_to_dev(monkeypatch, correct_recipient):
    import httpx
    calls = []
    recipient = slack_alerts.slack_user_id_for_email("esteban@sentientagency.io")
    assert recipient
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append((url, kwargs["json"]))
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"ok": True, "channel": {"id": "DM"}})
        def get(self, url, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"ok": True, "channel": {"is_im": True, "user": recipient if correct_recipient else "OTHER"}})

    monkeypatch.setattr(httpx, "Client", Client)
    assert slack_alerts.notify_new_account_request(ticket_id=1, handle="brand", requester="admin@example.com", reason="test") is correct_recipient
    assert calls[0][1]["users"] == recipient
    assert len(calls) == (2 if correct_recipient else 1)
