from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from app import apify_sync, db
from app.db import _ensure_account_scope_schema


def test_leads_are_promos_only_and_hidden_from_research(monkeypatch, tmp_path) -> None:
    path = tmp_path / "account-scopes.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE accounts (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               handle TEXT NOT NULL UNIQUE,
               label TEXT NOT NULL,
               group_name TEXT NOT NULL CHECK(group_name IN ('sentient', 'competitors')),
               hot_threshold INTEGER NOT NULL DEFAULT 600,
               scrape_mode TEXT NOT NULL DEFAULT 'posts',
               is_canonical INTEGER NOT NULL DEFAULT 0,
               is_active INTEGER NOT NULL DEFAULT 1,
               avatar_path TEXT,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
        )"""
    )
    connection.row_factory = sqlite3.Row
    _ensure_account_scope_schema(connection)
    connection.commit()
    connection.close()

    @contextmanager
    def connect():
        value = sqlite3.connect(path)
        value.row_factory = sqlite3.Row
        try:
            yield value
            value.commit()
        finally:
            value.close()

    monkeypatch.setattr(db, "connect", connect)

    lead = apify_sync.create_account(
        "new.lead",
        "New Lead",
        "leads",
        600,
        "posts",
        "business_growth",
        True,
        False,
    )
    assert lead["group"] == "leads"
    assert lead["subcategory"] == "business_growth"
    assert lead["research_enabled"] is False
    assert lead["promos_enabled"] is True

    assert apify_sync.list_accounts(active_only=True, research_only=True) == []
    assert [item["handle"] for item in apify_sync.list_accounts(active_only=True, promos_only=True)] == ["new.lead"]

    with connect() as value:
        stored = value.execute(
            "SELECT group_name, category FROM accounts WHERE handle = 'new.lead'"
        ).fetchone()
    assert dict(stored) == {"group_name": "competitors", "category": "leads"}
