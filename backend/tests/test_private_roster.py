import json
import sqlite3
from contextlib import contextmanager

import pytest

from app import db, private_roster, slack_alerts


def test_explicit_missing_roster_never_falls_back_to_deployment_identities(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTIENT_ROSTER_FILE", str(tmp_path / "missing.json"))
    assert private_roster.roster() == {}
    assert private_roster.dev_emails() == ()
    assert not private_roster.user_flags("developer@example.com").get("is_dev")
    assert private_roster.user_flags("stored-dev@example.com", ["pd", "dev"])["is_dev"]
    assert slack_alerts.slack_user_id_for_email("developer@example.com") == ""


def test_roster_reload_keeps_private_flags_and_slack_overrides(monkeypatch, tmp_path):
    path = tmp_path / "private.json"
    value = {"version": 1, "dev_emails": ["developer@example.com"],
             "user_flags": {"coordinator@example.com": {"can_access_news": True, "can_role_switch": True}},
             "slack_users_by_email": {"developer@example.com": "U10000000"},
             "queue_notification_slack_overrides": {"trainee@example.com": "U10000000"}}
    path.write_text(json.dumps(value))
    monkeypatch.setenv("SENTIENT_ROSTER_FILE", str(path))
    assert private_roster.user_flags("developer@example.com")["is_dev"]
    assert private_roster.user_flags("coordinator@example.com")["can_access_news"]
    assert not private_roster.user_flags("coordinator@example.com")["is_dev"]
    assert slack_alerts.queue_notification_slack_user_id("trainee@example.com") == "U10000000"
    value["user_flags"] = {}
    path.write_text(json.dumps(value))
    assert not private_roster.user_flags("coordinator@example.com").get("can_access_news")


@pytest.mark.parametrize("content", ["not-json", '[]', '{"version": 2}'])
def test_invalid_private_roster_does_not_grant_capabilities(monkeypatch, tmp_path, content):
    path = tmp_path / "invalid.json"
    path.write_text(content)
    monkeypatch.setenv("SENTIENT_ROSTER_FILE", str(path))
    with pytest.raises(RuntimeError, match="Private roster configuration"):
        private_roster.roster()


def test_private_migrations_preserve_aliases_and_later_settings_edits(monkeypatch, tmp_path):
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"version": 1,
        "dashboard_email_aliases": {"old@example.com": "designer@example.com"},
        "display_names": {"designer@example.com": "Designer"},
        "slack_user_ids": {"designer@example.com": "U10000001"},
        "seed_migrations": [{"marker": "example-review-v1", "users": [
            {"email": "designer@example.com", "fields": {"operating_role": "vc", "operating_roles": ["vc", "pd"], "is_admin": 0}},
            {"email": "unlisted@example.com", "fields": {"is_admin": 1}},
        ], "account_assignments": [{"email": "designer@example.com", "handles": ["example-account"]}]}]}))
    monkeypatch.setenv("SENTIENT_ROSTER_FILE", str(path))
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE dashboard_users (email TEXT PRIMARY KEY, display_name TEXT DEFAULT '',
            role TEXT DEFAULT 'viewer', operating_role TEXT DEFAULT 'pd', operating_roles TEXT DEFAULT '[]',
            is_admin INTEGER DEFAULT 0, slack_user_id TEXT DEFAULT '', updated_at TEXT);
        CREATE TABLE scheduler_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        CREATE TABLE queue_designer_accounts (designer_email TEXT, account_handle TEXT, created_at TEXT,
            PRIMARY KEY (designer_email, account_handle));
        INSERT INTO dashboard_users(email) VALUES ('designer@example.com');
    """)

    @contextmanager
    def connect():
        yield conn

    monkeypatch.setattr(db, "connect", connect)
    assert db._dashboard_email_aliases()["old@example.com"] == "designer@example.com"
    db.seed_queue_role_roster()
    row = dict(conn.execute("SELECT * FROM dashboard_users").fetchone())
    assert row["display_name"] == "Designer" and row["slack_user_id"] == "U10000001"
    assert json.loads(row["operating_roles"]) == ["vc", "pd"] and not row["is_admin"]
    assert conn.execute("SELECT COUNT(*) FROM dashboard_users").fetchone()[0] == 1
    conn.execute("UPDATE dashboard_users SET operating_role = 'sales', operating_roles = '[\"sales\",\"pd\"]'")
    db.seed_queue_role_roster()
    assert conn.execute("SELECT operating_role FROM dashboard_users").fetchone()[0] == "sales"
    assert conn.execute("SELECT COUNT(*) FROM scheduler_state").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM queue_designer_accounts").fetchone()[0] == 1


@pytest.mark.parametrize("invalid", [
    {"version": True},
    {"version": 1, "dev_emails": "dev@example.com"},
    {"version": 1, "user_flags": {"dev@example.com": {"is_dev": "false"}}},
    {"version": 1, "upsert_roles": {"dev@example.com": {"roles": ["unknown"]}}},
    {"version": 1, "slack_users_by_email": []},
])
def test_file_roster_rejects_unvalidated_capability_configuration(monkeypatch, tmp_path, invalid):
    path = tmp_path / "private.json"
    path.write_text(json.dumps(invalid))
    monkeypatch.setenv("SENTIENT_ROSTER_FILE", str(path))
    with pytest.raises(RuntimeError, match="Private roster configuration"):
        private_roster.user_flags("dev@example.com")
