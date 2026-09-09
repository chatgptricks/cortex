from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import sqlite3
from types import SimpleNamespace

from app import main


def test_presence_status_expires_after_two_minutes():
    now = datetime(2026, 9, 8, 18, 0, tzinfo=UTC)

    assert main._queue_v2_presence_status("active", now - timedelta(seconds=30), now) == "active"
    assert main._queue_v2_presence_status("idle", now - timedelta(seconds=119), now) == "idle"
    assert main._queue_v2_presence_status("active", now - timedelta(seconds=121), now) == "offline"
    assert main._queue_v2_presence_status("unknown", now, now) == "offline"


def test_presence_heartbeat_persists_caller_and_returns_snapshot(monkeypatch, tmp_path):
    database = tmp_path / "queue-presence.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE queue_presence (
            email TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.commit()
    connection.close()

    @contextmanager
    def isolated_connect():
        value = sqlite3.connect(database)
        value.row_factory = sqlite3.Row
        try:
            yield value
            value.commit()
        finally:
            value.close()

    monkeypatch.setattr(main, "connect", isolated_connect)
    monkeypatch.setattr(
        main,
        "_queue_v2_access",
        lambda request: ("florez@example.com", False, ["pd"]),
    )
    request = SimpleNamespace()

    result = main.dashboard_queue_v2_presence_heartbeat(request, "idle")

    assert result["ok"] is True
    assert result["presence"]["florez@example.com"]["status"] == "idle"
    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT status FROM queue_presence WHERE email = ?", ("florez@example.com",)
    ).fetchone()[0] == "idle"
    connection.close()
