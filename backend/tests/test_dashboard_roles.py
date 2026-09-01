import json
import sqlite3
from contextlib import contextmanager

from app import db


def _role_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE dashboard_users (
            email TEXT PRIMARY KEY,
            display_name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'viewer',
            operating_role TEXT NOT NULL DEFAULT 'pd',
            operating_roles TEXT NOT NULL DEFAULT '[]',
            is_admin INTEGER NOT NULL DEFAULT 0,
            slack_user_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()


def _isolated_connect(monkeypatch, path):
    @contextmanager
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(db, "connect", connect)
    return connect


def test_unrelated_user_edit_preserves_multi_role_access(monkeypatch, tmp_path):
    database = tmp_path / "roles.sqlite3"
    _role_database(database)
    connect = _isolated_connect(monkeypatch, database)
    with connect() as connection:
        connection.execute(
            "INSERT INTO dashboard_users VALUES (?, ?, 'admin', 'vc', ?, 1, ?, '', '')",
            (
                "ivan@sentientagency.io", "Ivan", json.dumps(["vc", "pd", "sales", "trainee"]),
                "U0516SU09J9",
            ),
        )

    db.upsert_dashboard_user(
        "ivan@sentientagency.io", role="admin", operating_role="vc", is_admin=True,
        display_name="Ivan Updated", slack_user_id="U0516SU09J9",
    )

    with connect() as connection:
        row = connection.execute(
            "SELECT display_name, operating_roles FROM dashboard_users WHERE email = ?",
            ("ivan@sentientagency.io",),
        ).fetchone()
    assert row["display_name"] == "Ivan Updated"
    assert json.loads(row["operating_roles"]) == ["vc", "pd", "sales", "trainee"]


def test_every_new_user_gets_pd_baseline(monkeypatch, tmp_path):
    database = tmp_path / "new-role.sqlite3"
    _role_database(database)
    connect = _isolated_connect(monkeypatch, database)

    db.upsert_dashboard_user(
        "sales@example.com", role="viewer", operating_role="sales",
        display_name="Sales User",
    )

    with connect() as connection:
        row = connection.execute(
            "SELECT operating_role, operating_roles FROM dashboard_users WHERE email = ?",
            ("sales@example.com",),
        ).fetchone()
    assert row["operating_role"] == "sales"
    assert json.loads(row["operating_roles"]) == ["sales", "pd"]
