import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from app import db, ingestion_jobs, ingestion_monitor as monitor, scheduler, slack_alerts


@pytest.fixture
def incident(tmp_path, monkeypatch):
    path = tmp_path / "monitor.sqlite"

    @contextmanager
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(db, "connect", connect)
    with connect() as conn:
        ingestion_jobs.initialize(conn)
        conn.execute("CREATE TABLE scheduler_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    alerts = []
    monkeypatch.setattr(slack_alerts, "notify_devs", lambda title, body: alerts.append((title, body)) or 1)

    def record(*, reason="apify_auth", success="2026-11-11T13:00:00+00:00",
               started="2026-11-11T14:00:00+00:00", frozen="2026-11-11T14:00:00+00:00",
               status="retry", error="HTTP 401", slot="2026-11-11T08:00"):
        state = {"last_success_at": success, "now": frozen,
                 "retry_reason": reason, "retry_started_at": started}
        with connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ingestion_jobs VALUES (?, ?, ?, 'worker', 0, ?, ?, ?)",
                (monitor.DISCOVERY_JOB_KEY, slot, status, json.dumps(state), error, started),
            )

    return record, alerts


def at(hour, minute=0):
    return datetime(2026, 11, 11, hour, minute, tzinfo=UTC)


def test_monitor_reads_provider_retry_metadata(incident):
    record, _ = incident
    record()
    job = monitor._discovery_job()
    assert job["retry_reason"] == "apify_auth"
    assert job["retry_started_at"] == "2026-11-11T14:00:00+00:00"


def test_auth_failure_alerts_immediately_with_actionable_service_names(incident):
    record, alerts = incident
    record(frozen=None)

    assert monitor.check_once(at(14)) == "alerted"
    assert len(alerts) == 1
    title, body = alerts[0]
    assert title == "Apify rejected the ingestion credentials"
    assert all(value in body for value in ("401/403", "APIFY_TOKEN", "cortex-ingestion-worker", "cortex-api"))
    assert monitor.check_once(at(14, 1)) == "ok"
    assert len(alerts) == 1


def test_auth_failure_is_reported_before_the_first_success(incident):
    record, alerts = incident
    record(success=None, frozen=None)

    assert monitor.check_once(at(14)) == "alerted"
    assert monitor.check_once(at(14, 10)) == "ok"
    assert len(alerts) == 1


def test_temporary_provider_failure_uses_retry_start_and_suppresses_stale_duplicates(incident):
    record, alerts = incident
    record(reason="apify_unavailable", error="HTTP 502", frozen="2026-11-11T10:00:00+00:00")

    assert monitor.check_once(at(14, 10)) == "ok"
    assert alerts == []
    assert monitor.check_once(at(14, 20)) == "alerted"
    assert alerts[0][0] == "New-post discovery is waiting for Apify"
    assert monitor.check_once(at(16)) == "ok"
    assert len(alerts) == 1
    assert scheduler._state_get(monitor.PROVIDER_ALERT_KEY)
    assert not scheduler._state_get(monitor.ALERT_STATE_KEY)


def test_provider_incident_deduplicates_across_slots_and_changed_errors(incident):
    record, alerts = incident
    record()
    monitor.check_once(at(14))
    record(reason="apify_unavailable", error="HTTP 503", slot="2026-11-11T08:45")

    assert monitor.check_once(at(14, 45)) == "ok"
    assert len(alerts) == 1


def test_pending_start_explains_reconciliation_without_claiming_free_work(incident):
    record, alerts = incident
    record(reason="apify_pending", error="Unconfirmed Apify start")

    assert monitor.check_once(at(14, 20)) == "alerted"
    assert alerts[0][0] == "New-post discovery is reconciling an Apify run"
    assert "before starting another" in alerts[0][1]
    assert "costs nothing" not in alerts[0][1]


def test_recovery_waits_for_a_fresh_success_and_is_sent_once(incident):
    record, alerts = incident
    record(success="2026-11-11T11:00:00+00:00")
    monitor.check_once(at(14))
    record(reason=None, status="done", error=None, success="2026-11-11T11:45:00+00:00")

    assert monitor.check_once(at(14, 5)) == "ok"
    assert scheduler._state_get(monitor.PROVIDER_ALERT_KEY)
    record(success="2026-11-11T11:45:00+00:00", started="2026-11-11T14:05:00+00:00")
    assert monitor.check_once(at(14, 10)) == "ok"
    assert len(alerts) == 1

    record(reason=None, status="done", error=None, success="2026-11-11T14:10:00+00:00")
    assert monitor.check_once(at(14, 15)) == "recovered"
    assert monitor.check_once(at(14, 20)) == "ok"
    assert [title for title, _ in alerts] == [
        "Apify rejected the ingestion credentials", "New-post discovery recovered",
    ]
    assert scheduler._state_get(monitor.PROVIDER_ALERT_KEY) == ""

    record(success="2026-11-11T14:10:00+00:00", started="2026-11-11T14:20:00+00:00")
    assert monitor.check_once(at(14, 20)) == "alerted"
    assert len(alerts) == 3


def test_abandonment_cannot_be_mistaken_for_provider_recovery(incident):
    record, alerts = incident
    record()
    monitor.check_once(at(14))
    record(reason=None, status="done", error="processing failed")

    assert monitor.check_once(at(14, 10)) == "ok"
    assert len(alerts) == 1
    assert scheduler._state_get(monitor.PROVIDER_ALERT_KEY)
