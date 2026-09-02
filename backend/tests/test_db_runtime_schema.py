from __future__ import annotations

import sqlite3

from app.db import _ensure_runtime_schema_extensions, _has_internal_self_assign


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

    _ensure_runtime_schema_extensions(connection)
    _ensure_runtime_schema_extensions(connection)

    columns = {
        row["name"]: row
        for row in connection.execute("PRAGMA table_info(dashboard_users)").fetchall()
    }
    assert columns["time_zone"]["dflt_value"] == "''"
    assert columns["can_self_assign"]["dflt_value"] == "0"
    account_columns = {
        row["name"]: row
        for row in connection.execute("PRAGMA table_info(accounts)").fetchall()
    }
    assert account_columns["scrape_mode"]["dflt_value"] == "'posts'"

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
