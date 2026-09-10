from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from app import db
from app.db import _ensure_runtime_schema_extensions, _has_internal_self_assign, seed_queue_role_roster


def test_runtime_schema_extensions_add_post_cutover_fields_idempotently() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE dashboard_users (
               email TEXT PRIMARY KEY,
               updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE accounts (
               handle TEXT PRIMARY KEY,
               updated_at TEXT NOT NULL
           )"""
    )
    connection.execute(
        """CREATE TABLE queue_requests (
               id INTEGER PRIMARY KEY,
               updated_at TEXT NOT NULL
           )"""
    )

    _ensure_runtime_schema_extensions(connection)
    _ensure_runtime_schema_extensions(connection)

    for table in ("promo_scans", "promo_opportunities", "promo_jobs"):
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'queue_presence'"
    ).fetchone()

    columns = {
        row["name"]: row
        for row in connection.execute("PRAGMA table_info(dashboard_users)").fetchall()
    }
    assert columns["time_zone"]["dflt_value"] == "''"
    assert columns["can_self_assign"]["dflt_value"] == "0"
    assert columns["can_access_promos"]["dflt_value"] == "0"
    assert "minutes_per_pp" in columns
    account_columns = {
        row["name"]: row
        for row in connection.execute("PRAGMA table_info(accounts)").fetchall()
    }
    assert account_columns["scrape_mode"]["dflt_value"] == "'posts'"
    queue_columns = {
        row["name"]: row
        for row in connection.execute("PRAGMA table_info(queue_requests)").fetchall()
    }
    assert queue_columns["final_permalinks"]["dflt_value"] == "'[]'"

    connection.execute(
        "INSERT INTO queue_scheduler_preferences (viewer_email, updated_at) VALUES (?, ?)",
        ("esteban@sentientagency.io", "2026-09-01T09:00:00+00:00"),
    )
    row = connection.execute(
        "SELECT hidden_users, row_order FROM queue_scheduler_preferences"
    ).fetchone()
    assert dict(row) == {"hidden_users": "[]", "row_order": "[]"}
def test_gabo_is_not_an_internal_self_assignment_exception() -> None:
    assert not _has_internal_self_assign("gabo@sentientagency.io")


def test_legacy_profile_schema_cannot_break_authentication_access(monkeypatch, tmp_path) -> None:
    path = tmp_path / "legacy-dashboard-users.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE dashboard_users (email TEXT PRIMARY KEY, role TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO dashboard_users (email, role) VALUES (?, ?)",
            ("esteban@sentientagency.io", "admin"),
        )

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
    access = db.get_dashboard_user_access("esteban@sentientagency.io")
    assert access is not None
    assert access["is_admin"] is True
    assert access["operating_role"] == "sales"
    assert access["time_zone"] == "America/Costa_Rica"
    assert access["minutes_per_pp"] is None
    assert access["can_access_promos"] is False

    # The incoming browser clock is an optional preference, not a reason to
    # make every authenticated request fail while the old schema is upgraded.
    db.set_dashboard_user_time_zone("esteban@sentientagency.io", "America/Bogota")


def test_victor_receives_promos_without_admin_access(monkeypatch, tmp_path) -> None:
    path = tmp_path / "promos-access.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE dashboard_users (
                email TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'viewer',
                operating_role TEXT NOT NULL DEFAULT 'sales',
                operating_roles TEXT NOT NULL DEFAULT '[]',
                is_admin INTEGER NOT NULL DEFAULT 0,
                slack_user_id TEXT NOT NULL DEFAULT '',
                can_self_assign INTEGER NOT NULL DEFAULT 0,
                can_access_promos INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE scheduler_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
            CREATE TABLE queue_designer_accounts (
                designer_email TEXT NOT NULL,
                account_handle TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (designer_email, account_handle)
            );
            INSERT INTO dashboard_users (email, created_at, updated_at)
            VALUES ('victor@sentientagency.io', 'now', 'now');
            """
        )

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
    seed_queue_role_roster()
    access = db.get_dashboard_user_access("victor@sentientagency.io")
    assert access is not None
    assert access["can_access_promos"] is True
    assert access["is_admin"] is False


def test_runtime_migration_adds_soft_delete_to_both_post_tables():
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    for table in ('dashboard_users', 'accounts', 'posts', 'dashboard_posts'):
        connection.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
    _ensure_runtime_schema_extensions(connection)
    _ensure_runtime_schema_extensions(connection)
    for table in ('posts', 'dashboard_posts'):
        connection.execute(f'INSERT INTO {table} (id) VALUES (1)')
        assert connection.execute(f'SELECT is_deleted FROM {table}').fetchone()[0] == 0


def test_runtime_migration_repairs_canonical_account_registry():
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE dashboard_users (email TEXT PRIMARY KEY, updated_at TEXT NOT NULL)")
    connection.execute(
        "CREATE TABLE accounts (handle TEXT PRIMARY KEY, is_canonical INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1)"
    )
    connection.execute("CREATE TABLE queue_requests (id INTEGER PRIMARY KEY, updated_at TEXT NOT NULL)")
    connection.executemany(
        "INSERT INTO accounts (handle, is_canonical, is_active) VALUES (?, ?, ?)",
        [('chatgptricks', 0, 0), ('competitor', 1, 1)],
    )

    _ensure_runtime_schema_extensions(connection)

    rows = {
        row['handle']: (row['is_canonical'], row['is_active'])
        for row in connection.execute('SELECT handle, is_canonical, is_active FROM accounts').fetchall()
    }
    assert rows == {'chatgptricks': (1, 1), 'competitor': (0, 1)}
