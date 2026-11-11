"""Provider outages retain ambiguous starts and never spend the poison budget."""
from contextlib import contextmanager
import json
import sqlite3

import httpx
import pytest

from app import apify_sync, db, ingestion_jobs as jobs


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "provider-retries.sqlite"

    @contextmanager
    def connect():
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(jobs, "RETRY_SECONDS", 2)
    monkeypatch.setattr(jobs, "MAX_PROVIDER_RETRY_SECONDS", 16)
    monkeypatch.setattr(jobs.time, "time", lambda: 2_000_000_000.0)
    return connect


def _job(database):
    with database() as conn:
        row = dict(conn.execute("SELECT * FROM ingestion_jobs").fetchone())
    row["state"] = json.loads(row["state"])
    return row


def _expire(database):
    with database() as conn:
        conn.execute("UPDATE ingestion_jobs SET lease_until = 0")


def _seed(database, state):
    with database() as conn:
        jobs.initialize(conn)
        conn.execute(
            "INSERT INTO ingestion_jobs VALUES ('short','01','retry','dead',0,?,NULL,'old')",
            (json.dumps(state),),
        )


def _http_error(status, host="api.apify.com"):
    request = httpx.Request("POST", f"https://{host}/v2/acts/actor/runs?token=secret")
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request,
        response=httpx.Response(status, request=request),
    )


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_classification_follows_wrapped_apify_http_errors(status):
    try:
        raise _http_error(status)
    except httpx.HTTPStatusError as exc:
        wrapped = apify_sync.ApifySyncError("Failed to start Apify run")
        wrapped.__cause__ = exc
    assert jobs.classify_apify_failure(wrapped) == "apify_unavailable"


@pytest.mark.parametrize("status", [401, 403])
def test_classification_distinguishes_rejected_apify_credentials(status):
    assert jobs.classify_apify_failure(_http_error(status)) == "apify_auth"


def test_classification_does_not_hide_unrelated_http_or_processing_failures():
    assert jobs.classify_apify_failure(_http_error(502, "other.example")) is None
    assert jobs.classify_apify_failure(_http_error(400)) is None
    assert jobs.classify_apify_failure(RuntimeError("bad row: HTTP 502")) is None
    request = httpx.Request("GET", "https://other.example/data")
    assert jobs.classify_apify_failure(httpx.ReadTimeout("lost", request=request)) is None


def test_classification_handles_apify_transport_and_pending_errors():
    request = httpx.Request("GET", "https://api.apify.com/v2/actor-runs/run")
    assert jobs.classify_apify_failure(httpx.ReadTimeout("lost", request=request)) == "apify_unavailable"
    assert jobs.classify_apify_failure(
        apify_sync.ApifyPendingError("Unconfirmed Apify start")
    ) == "apify_pending"


def test_provider_retries_preserve_journal_and_increase_bounded_backoff(database):
    initial = {
        "last_success_at": "2026-11-11T19:00:00+00:00",
        "now": "2026-11-11T19:45:00+00:00",
        "run:0": {"starting_at": "2026-11-11T19:44:55", "payload": {"directUrls": ["account"]}},
        "attempts": 1,
    }
    _seed(database, initial)

    def fail():
        raise apify_sync.ApifySyncError("provider unavailable") from _http_error(502)

    delays = []
    first_retry_at = None
    for attempt in range(1, 8):
        assert not jobs.run("short", "02", fail, max_attempts=3)
        row = _job(database)
        state = row["state"]
        assert row["status"] == "retry" and row["slot"] == "01"
        assert state["last_success_at"] == initial["last_success_at"]
        assert state["now"] == initial["now"]
        assert state["run:0"] == initial["run:0"]
        assert state["attempts"] == 1
        assert state["retry_reason"] == "apify_unavailable"
        assert state["transient_attempts"] == attempt
        first_retry_at = first_retry_at or state["retry_started_at"]
        assert state["retry_started_at"] == first_retry_at
        assert "abandoned" not in state
        delays.append(row["lease_until"] - 2_000_000_000)
        assert not jobs.run("short", "02", lambda: pytest.fail("unexpired lease claimed"))
        _expire(database)
    assert delays == [2, 4, 8, 16, 16, 16, 16]

    # The actual poison count remains 1, so two processing failures reach 3.
    def poison():
        raise RuntimeError("deterministic bad row")

    assert not jobs.run("short", "02", poison, max_attempts=3)
    assert _job(database)["status"] == "retry"
    _expire(database)
    assert not jobs.run("short", "02", poison, max_attempts=3)
    row = _job(database)
    assert row["status"] == "done"
    assert row["state"]["abandoned"]["attempts"] == 3
    assert row["state"]["last_success_at"] == initial["last_success_at"]


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_wait_for_repair_without_abandoning_the_slot(database, status):
    _seed(database, {"last_success_at": "2026-11-11T19:00:00+00:00"})

    def fail():
        raise apify_sync.ApifySyncError("rejected token") from _http_error(status)

    for _ in range(7):
        assert not jobs.run("short", "02", fail, max_attempts=5)
        row = _job(database)
        assert row["status"] == "retry"
        assert row["state"]["retry_reason"] == "apify_auth"
        assert row["state"].get("attempts", 0) == 0
        _expire(database)
    assert jobs.run("short", "02", lambda: None, max_attempts=5)
    assert not any(key in _job(database)["state"] for key in (
        "retry_reason", "transient_attempts", "retry_started_at", "abandoned",
    ))


def test_pending_runs_do_not_consume_poison_attempts_or_exponentially_delay_polling(database):
    _seed(database, {"run:0": {"run": {"id": "paid"}}, "attempts": 2})

    def pending():
        raise apify_sync.ApifyPendingError("run is still running")

    for _ in range(7):
        assert not jobs.run("short", "02", pending, max_attempts=3)
        row = _job(database)
        assert row["status"] == "retry"
        assert row["state"]["attempts"] == 2
        assert row["state"]["retry_reason"] == "apify_pending"
        assert row["lease_until"] - 2_000_000_000 == 2
        assert row["state"]["run:0"]["run"]["id"] == "paid"
        _expire(database)


def test_ambiguous_start_survives_many_reconciliation_failures_and_reuses_paid_run(database, monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "test")
    _seed(database, {
        "last_success_at": "2026-11-11T19:00:00+00:00",
        "now": "2026-11-11T19:45:00+00:00",
    })
    requests = []
    recovering = False

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            # A gateway error cannot tell us whether Apify started this run.
            return httpx.Response(502)
        if "/acts/" in request.url.path:
            if not recovering:
                return httpx.Response(502)
            return httpx.Response(200, json={"data": {"items": [{
                "id": "paid", "status": "SUCCEEDED", "startedAt": "2099-01-01",
                "defaultKeyValueStoreId": "input", "defaultDatasetId": "dataset",
            }]}})
        if request.url.path.endswith("/records/INPUT"):
            return httpx.Response(200, json={"directUrls": ["account"]})
        return httpx.Response(200, json=[{"shortCode": "already-paid"}])

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler)))
    output = []

    def work():
        output.append(apify_sync._run_apify_actor_and_fetch({"directUrls": ["account"]}))

    starting_at = None
    for _ in range(7):
        assert not jobs.run("short", "02", work, max_attempts=5)
        row = _job(database)
        assert row["status"] == "retry"
        starting_at = starting_at or row["state"]["run:0"]["starting_at"]
        assert row["state"]["run:0"]["starting_at"] == starting_at
        _expire(database)
    recovering = True
    assert jobs.run("short", "02", work, max_attempts=5)
    assert output == [[{"shortCode": "already-paid"}]]
    assert sum(method == "POST" for method, _ in requests) == 1
    row = _job(database)
    assert row["state"] == {"last_success_at": "2026-11-11T19:45:00+00:00"}
    assert row["slot"] == "01"
    assert jobs.run("short", "02", lambda: None, max_attempts=5)


@pytest.mark.parametrize("response_data", [{}, {"id": "paid", "status": "RUNNING"}])
def test_incomplete_start_response_keeps_reconcilable_or_running_journal(database, monkeypatch, response_data):
    monkeypatch.setenv("APIFY_TOKEN", "test")
    requests = []

    def handler(request):
        requests.append(request.method)
        return httpx.Response(200, json={"data": response_data})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler)))
    assert not jobs.run("short", "01", lambda: apify_sync._run_apify_actor_and_fetch(
        {"directUrls": ["account"]}, max_wait_seconds=0,
    ), max_attempts=1)
    row = _job(database)
    assert row["status"] == "retry"
    assert row["state"]["retry_reason"] == "apify_pending"
    assert row["state"]["run:0"]["starting_at"]
    if response_data:
        assert row["state"]["run:0"]["run"]["id"] == "paid"
    else:
        assert "run" not in row["state"]["run:0"]
    assert requests == ["POST"]


def test_unconfirmed_start_waits_for_history_before_starting_again(database, monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setenv("APIFY_TOKEN", "test")
    _seed(database, {"run:0": {
        "actor": apify_sync.APIFY_ACTOR_ID,
        "payload": {"directUrls": ["account"]},
        "starting_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
    }})
    requests = []

    def handler(request):
        requests.append(request.method)
        return httpx.Response(200, json={"data": {"items": []}})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler)))
    for _ in range(6):
        assert not jobs.run("short", "02", lambda: apify_sync._run_apify_actor_and_fetch(
            {"directUrls": ["account"]},
        ), max_attempts=1)
        row = _job(database)
        assert row["status"] == "retry"
        assert row["state"]["retry_reason"] == "apify_pending"
        assert row["state"]["run:0"]["starting_at"]
        _expire(database)
    assert requests == ["GET"] * 6

