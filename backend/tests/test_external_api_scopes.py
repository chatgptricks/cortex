"""Cross-router credential checks for the expanded, read-only integration API."""
import json

import pytest

from app import external_api as api, external_research
from test_external_api import create, headers, setup


def test_legacy_key_keeps_public_scope_and_can_discover_every_route(setup):
    client, _ = setup
    key = create(client)["key"]
    response = client.get("/api/v1/capabilities", headers=headers(key))
    assert response.status_code == 200
    body = response.json()
    assert body["data_scopes"] == ["accounts"]
    assert body["read_only"] and body["methods"] == ["GET"]
    assert body["account_handles"] == ["alpha"]
    operations = {item["path"]: item for item in body["operations"]}
    assert set(operations) == {route.path for route in api._versioned_routes()}
    assert operations["/api/v1/hot"]["available"] is True
    assert operations["/api/v1/queue/requests"]["available"] is False
    assert client.get("/api/v1/queue/requests", headers=headers(key)).status_code == 403
    assert response.headers["X-RateLimit-Remaining"] == "59"
    assert int(response.headers["X-RateLimit-Reset"]) > 0


def test_browser_owner_can_expand_scopes_without_rotating_or_broadening_accounts(setup):
    client, _ = setup
    created = create(client)
    before = created["connection"]
    response = client.patch(api.MANAGEMENT_URL + "/" + before["id"], headers=headers("ana@example.com"),
                            json={"data_scopes": ["queue", "accounts"]})
    assert response.status_code == 200, response.text
    after = response.json()["connection"]
    assert after["data_scopes"] == ["accounts", "queue"]
    assert {k: v for k, v in after.items() if k != "data_scopes"} == {k: v for k, v in before.items() if k != "data_scopes"}
    assert created["key"] not in response.text and "key_hash" not in response.text
    assert client.get("/api/v1/capabilities", headers=headers(created["key"])).json()["data_scopes"] == ["accounts", "queue"]
    assert client.get("/api/v1/accounts/beta", headers=headers(created["key"])).status_code == 404
    assert client.patch(api.MANAGEMENT_URL + "/" + before["id"], headers=headers(created["key"]),
                        json={"data_scopes": ["accounts"]}).status_code == 403


def test_other_owner_cannot_edit_key_scopes(setup):
    client, connect = setup
    created = create(client)
    with connect() as conn:
        conn.execute("UPDATE dashboard_users SET is_admin = 1 WHERE email = 'bob@example.com'")
    response = client.patch(api.MANAGEMENT_URL + "/" + created["connection"]["id"], headers=headers("bob@example.com"),
                            json={"data_scopes": ["accounts", "queue"]})
    assert response.status_code == 404


@pytest.mark.parametrize("scopes,status", [([], 422), (["unknown"], 422), (["hooks"], 403), (["vault"], 403), (["news"], 403)])
def test_scopes_cannot_exceed_current_owner_permissions(setup, scopes, status):
    client, _ = setup
    response = client.post(api.MANAGEMENT_URL, headers=headers("ana@example.com"),
                           json={"name": "limited", "account_handles": ["alpha"], "data_scopes": scopes})
    assert response.status_code == status


def test_current_permission_loss_disables_existing_grant_on_next_read(setup):
    client, connect = setup
    with connect() as conn:
        conn.execute("UPDATE dashboard_users SET operating_roles = '[\"vc\",\"pd\",\"dev\"]' WHERE email = 'ana@example.com'")
    created = create(client, data_scopes=list(api.DATA_SCOPES))
    before = client.get("/api/v1/capabilities", headers=headers(created["key"])).json()
    assert set(before["data_scopes"]) == set(api.DATA_SCOPES)
    with connect() as conn:
        conn.execute("UPDATE dashboard_users SET operating_roles = '[\"vc\",\"pd\"]' WHERE email = 'ana@example.com'")
    assert client.get("/api/v1/hooks", headers=headers(created["key"])).status_code == 403
    assert client.get("/api/v1/vault", headers=headers(created["key"])).status_code == 403
    after = client.get("/api/v1/capabilities", headers=headers(created["key"])).json()
    assert not set(after["data_scopes"]).intersection({"news", "hooks", "vault"})
    assert client.get("/api/v1/accounts/alpha", headers=headers(created["key"])).status_code == 200


def test_openapi_only_describes_supported_versioned_gets(setup):
    client, _ = setup
    key = create(client)["key"]
    response = client.get("/api/v1/openapi.json", headers=headers(key))
    assert response.status_code == 200
    spec = response.json()
    assert set(spec["paths"]) == {route.path for route in api._versioned_routes()}
    assert all(path.startswith("/api/v1/") and set(methods) == {"get"} for path, methods in spec["paths"].items())
    assert spec["paths"]["/api/v1/queue/requests"]["get"]["x-available"] is False
    assert spec["paths"]["/api/v1/hot"]["get"]["x-required-scopes"] == ["accounts", "research"]
    assert api.MANAGEMENT_URL not in json.dumps(spec)
    assert "key_hash" not in json.dumps(spec) and key not in response.text


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD"])
def test_expanded_routes_never_accept_writes_even_for_full_area_key(setup, method):
    client, connect = setup
    with connect() as conn:
        conn.execute("UPDATE dashboard_users SET operating_roles = '[\"dev\"]' WHERE email = 'ana@example.com'")
    key = create(client, data_scopes=list(api.DATA_SCOPES))["key"]
    for path in ("/api/v1/hot", "/api/v1/queue/requests", "/api/v1/settings/users", "/api/v1/news"):
        assert client.request(method, path, headers=headers(key)).status_code == 403


def test_database_pressure_returns_retryable_503_without_secrets(setup, monkeypatch):
    client, _ = setup
    key = create(client)["key"]

    class PoolTimeout(Exception):
        pass

    def busy(*args, **kwargs):
        raise PoolTimeout("CANARY_PRIVATE_DATABASE_ADDRESS")

    monkeypatch.setattr(external_research, "_posts_page", busy)
    response = client.get("/api/v1/accounts/alpha/posts", headers=headers(key))
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert "CANARY" not in response.text


def test_group_selection_metadata_and_legacy_scope_migration_are_available(setup):
    client, connect = setup
    with connect() as conn:
        conn.execute("UPDATE accounts SET group_name = 'competitors' WHERE handle = 'beta'")
    response = client.get(api.MANAGEMENT_URL, headers=headers("ana@example.com"))
    grouped = {account["handle"]: account for account in response.json()["available_accounts"]}
    assert grouped["alpha"]["group"] == "sentient"
    assert grouped["beta"]["group"] == "others" and grouped["beta"]["group_name"] == "competitors"
    assert response.json()["max_accounts_per_key"] == 1000
    assert {scope["id"] for scope in response.json()["available_scopes"]} >= {"accounts", "research", "queue", "tracker", "insights"}
    assert next(scope for scope in response.json()["available_scopes"] if scope["id"] == "queue")["global"] is True


def _other_owner_keys(client, connect):
    with connect() as conn:
        conn.execute("UPDATE dashboard_users SET is_admin = 1 WHERE email = 'bob@example.com'")
    return [client.post(api.MANAGEMENT_URL, headers=headers("bob@example.com"), json={
        "name": name, "account_handles": ["beta"], "expires_in_days": 30,
    }).json() for name in ("Other active", "Other revoked", "Other expired")]


def test_dev_can_inspect_all_active_keys_without_reading_their_secrets(setup):
    client, connect = setup
    own = create(client)
    active, revoked, expired = _other_owner_keys(client, connect)
    with connect() as conn:
        conn.execute("UPDATE dashboard_users SET operating_roles = '[\"dev\"]' WHERE email = 'ana@example.com'")
        conn.execute("UPDATE website_api_keys SET revoked_at = '2026-10-09T17:00:00+00:00' WHERE id = ?", (revoked["connection"]["id"],))
        conn.execute("UPDATE website_api_keys SET expires_at = '2026-10-08T17:00:00+00:00' WHERE id = ?", (expired["connection"]["id"],))
    response = client.get(api.MANAGEMENT_URL, headers=headers("ana@example.com"))
    body = response.json()
    assert body["can_manage_all"] is True
    keys = {key["id"]: key for key in body["keys"]}
    assert set(keys) == {own["connection"]["id"], active["connection"]["id"]}
    foreign = keys[active["connection"]["id"]]
    assert foreign["owner_email"] == "bob@example.com"
    assert foreign["can_revoke"] is True and foreign["can_edit"] is False
    assert foreign["account_handles"] == ["beta"] and foreign["data_scopes"] == ["accounts"]
    assert "key_hash" not in response.text
    assert all(value["key"] not in response.text for value in (own, active, revoked, expired))


def test_admin_keeps_own_key_visibility_and_cannot_revoke_another_owner(setup):
    client, connect = setup
    own = create(client)
    active, _, _ = _other_owner_keys(client, connect)
    response = client.get(api.MANAGEMENT_URL, headers=headers("ana@example.com"))
    assert response.json()["can_manage_all"] is False
    assert [key["id"] for key in response.json()["keys"]] == [own["connection"]["id"]]
    assert client.delete(api.MANAGEMENT_URL + "/" + active["connection"]["id"], headers=headers("ana@example.com")).status_code == 404
    assert client.get("/api/v1/accounts", headers=headers(active["key"])).status_code == 200


def test_dev_can_revoke_foreign_key_and_access_stops_immediately(setup):
    client, connect = setup
    active, _, _ = _other_owner_keys(client, connect)
    with connect() as conn:
        conn.execute("UPDATE dashboard_users SET operating_roles = '[\"dev\"]' WHERE email = 'ana@example.com'")
    path = api.MANAGEMENT_URL + "/" + active["connection"]["id"]
    assert client.get("/api/v1/accounts", headers=headers(active["key"])).status_code == 200
    assert client.patch(path, headers=headers("ana@example.com"), json={"data_scopes": ["accounts", "queue"]}).status_code == 404
    assert client.delete(path, headers=headers("ana@example.com")).status_code == 200
    assert client.get("/api/v1/accounts", headers=headers(active["key"])).status_code == 401
    with connect() as conn:
        row = conn.execute("SELECT revoked_at FROM website_api_keys WHERE id = ?", (active["connection"]["id"],)).fetchone()
    assert row["revoked_at"] is not None


def test_dev_role_loss_removes_global_key_control_on_next_request(setup):
    client, connect = setup
    active, _, _ = _other_owner_keys(client, connect)
    with connect() as conn:
        conn.execute("UPDATE dashboard_users SET operating_roles = '[\"dev\"]' WHERE email = 'ana@example.com'")
    assert client.get(api.MANAGEMENT_URL, headers=headers("ana@example.com")).json()["can_manage_all"] is True
    with connect() as conn:
        conn.execute("UPDATE dashboard_users SET operating_roles = '[\"vc\",\"pd\"]' WHERE email = 'ana@example.com'")
    assert client.get(api.MANAGEMENT_URL, headers=headers("ana@example.com")).json()["keys"] == []
    assert client.delete(api.MANAGEMENT_URL + "/" + active["connection"]["id"], headers=headers("ana@example.com")).status_code == 404
