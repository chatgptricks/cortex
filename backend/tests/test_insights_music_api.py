from __future__ import annotations

import json

import pytest

from app import agent_connections, external_api as api, insights_music, main
from test_external_api import create, headers, setup  # noqa: F401


def seed_music(connect):
    with connect() as conn:
        conn.executescript("""
            ALTER TABLE dashboard_posts ADD COLUMN music_song TEXT;
            ALTER TABLE dashboard_posts ADD COLUMN music_artist TEXT;
            ALTER TABLE dashboard_posts ADD COLUMN music_audio_id TEXT;
            ALTER TABLE dashboard_posts ADD COLUMN uses_original_audio INTEGER;
            ALTER TABLE posts ADD COLUMN raw_json TEXT;
            CREATE TABLE engagement_observations (shortcode TEXT PRIMARY KEY, observed_at TEXT, raw_json TEXT);
        """)
        conn.execute("UPDATE dashboard_posts SET music_song = ?, music_artist = ?, music_audio_id = ?, uses_original_audio = 0 WHERE shortcode = 'PublicA'",
                     ("Named song", "Known artist", "9876543210987654321"))
        conn.execute("UPDATE dashboard_posts SET music_audio_id = '0', uses_original_audio = 0 WHERE shortcode = 'PublicB'")
        conn.execute("INSERT INTO engagement_observations VALUES (?, ?, ?)", (
            "PublicC", "2026-10-08T15:00:00+00:00", json.dumps({"shortCode": "PublicC", "musicInfo": {
                "song_name": "Original audio - exact name", "artist_name": "creator", "audio_id": "1234567890123456789", "uses_original_audio": True}})))
        conn.execute("UPDATE dashboard_posts SET raw_json = ? WHERE shortcode = 'Other'", (json.dumps({
            "musicInfo": {"song_name": "CANARY_OTHER_SONG", "artist_name": "CANARY_OTHER_ARTIST", "audio_id": "999"}}),))


def test_music_api_returns_bounded_readable_music_and_exact_originals(setup, monkeypatch):
    client, connect = setup
    seed_music(connect)
    key = create(client, data_scopes=["insights"])["key"]
    calls = []
    reader = insights_music._read_source

    def tracked(conn, table, columns, where="1 = 1", params=None):
        calls.append((table, where, params))
        return reader(conn, table, columns, where, params)

    monkeypatch.setattr(insights_music, "_read_source", tracked)
    monkeypatch.setattr(insights_music, "build_music_catalogue", lambda *a, **k: pytest.fail("Paged API must never load the complete music catalogue"))
    response = client.get("/api/v1/insights/music?limit=1", headers=headers(key))
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    first = response.json()
    assert first["pagination"] == {"limit": 1, "offset": 0, "total": 3, "has_more": True, "next_offset": 1}
    post = first["data"][0]
    assert post == {
        "account": "alpha", "accounts": ["alpha"], "shortcode": "PublicA",
        "published_at": "2026-10-01T23:00:00+00:00", "permalink": "https://www.instagram.com/p/PublicA/",
        "format": "Reel", "likes": 100, "comments": 10, "views": 500,
        "metrics_updated_at": "2026-10-08T12:00:00+00:00", "song": "Named song", "artist": "Known artist",
        "audio_id": "9876543210987654321", "is_original_audio": False,
        "audio_url": "https://www.instagram.com/reels/audio/9876543210987654321/",
    }
    assert first["coverage"] == {"scope": "returned_page", "posts": 1, "with_music_metadata": 1, "unknown_music": 0, "with_audio_id": 1}
    assert first["ordering"]["basis"] == "stored_post_index" and first["ordering"]["sort"] == "likes"
    assert len(calls) == 3
    assert all("shortcode IN (?)" in where and "PublicA" in params for _, where, params in calls)
    assert all("PublicB" not in params and "Other" not in params for _, _, params in calls)
    assert "LOWER(account) IN (?)" in calls[0][1] and "alpha" in calls[0][2]
    assert "CANARY" not in response.text and "raw_json" not in response.text
    second = client.get("/api/v1/insights/music?limit=1&offset=1", headers=headers(key)).json()
    assert second["data"][0]["song"] == "Original audio - exact name"
    assert second["data"][0]["artist"] == "creator"
    assert second["data"][0]["audio_id"] == "1234567890123456789"
    assert second["data"][0]["is_original_audio"] is True
    third = client.get("/api/v1/insights/music?limit=1&offset=2", headers=headers(key)).json()
    assert third["data"][0]["likes"] is None
    assert third["data"][0]["audio_id"] is None and third["data"][0]["audio_url"] is None
    assert third["data"][0]["song"] is None and third["data"][0]["is_original_audio"] is False
    assert third["coverage"]["unknown_music"] == 1 and third["pagination"]["next_offset"] is None


def test_music_api_recovers_canonical_raw_music_and_keeps_missing_unknown(setup):
    client, connect = setup
    seed_music(connect)
    with connect() as conn:
        conn.execute("UPDATE dashboard_posts SET music_song = NULL, music_artist = NULL, music_audio_id = NULL, uses_original_audio = NULL WHERE shortcode = 'PublicA'")
        conn.execute("UPDATE posts SET raw_json = ? WHERE shortcode = 'PublicA'", (json.dumps({"musicInfo": {
            "song_name": "Historic song", "artist_name": "Historic artist", "audio_id": "123", "uses_original_audio": False}}),))
        conn.execute("UPDATE dashboard_posts SET uses_original_audio = NULL, raw_json = '{malformed' WHERE shortcode = 'PublicB'")
    key = create(client, data_scopes=["insights"])["key"]
    values = client.get("/api/v1/insights/music", headers=headers(key)).json()["data"]
    assert values[0]["song"] == "Historic song" and values[0]["audio_id"] == "123"
    assert values[-1]["song"] is None and values[-1]["artist"] is None
    assert values[-1]["audio_id"] is None and values[-1]["is_original_audio"] is None


def test_music_api_filters_and_validates_before_page_hydration(setup):
    client, connect = setup
    seed_music(connect)
    key = create(client, data_scopes=["insights"], account_handles=["alpha", "beta"])["key"]
    response = client.get("/api/v1/insights/music?account=alpha&from=2026-10-02&to=2026-10-02&sort=date", headers=headers(key))
    assert response.status_code == 200
    assert [post["shortcode"] for post in response.json()["data"]] == ["PublicC"]
    assert response.json()["pagination"]["total"] == 1
    for query in ("limit=101", "limit=0", "offset=-1", "sort=nope", "from=not-a-date", "from=2026-10-03&to=2026-10-02"):
        assert client.get("/api/v1/insights/music?" + query, headers=headers(key)).status_code == 422
    empty = client.get("/api/v1/insights/music?offset=999", headers=headers(key)).json()
    assert empty["data"] == [] and empty["coverage"]["posts"] == 0


def test_music_api_tie_break_matches_canonical_catalogue_and_blocks_cross_account_metadata(setup):
    client, connect = setup
    seed_music(connect)
    with connect() as conn:
        conn.execute("UPDATE posts SET likes = 100, updated_at = '2026-10-08T12:00:00+00:00', raw_json = ? WHERE shortcode = 'PublicA'", (json.dumps({
            "musicInfo": {"song_name": "Canonical song", "audio_id": "111", "uses_original_audio": False}}),))
        conn.execute("UPDATE dashboard_posts SET shortcode = 'PublicA', updated_at = '2026-10-09T00:00:00+00:00' WHERE shortcode = 'Other'")
    key = create(client, data_scopes=["insights"])["key"]
    response = client.get("/api/v1/insights/music?limit=1", headers=headers(key))
    assert response.json()["data"][0]["song"] == "Canonical song"
    assert response.json()["data"][0]["audio_id"] == "111"
    assert "CANARY" not in response.text and response.json()["data"][0]["accounts"] == ["alpha"]


def test_music_api_marks_index_ordering_when_returning_newer_observations(setup):
    client, connect = setup
    seed_music(connect)
    with connect() as conn:
        conn.execute("UPDATE engagement_observations SET raw_json = ? WHERE shortcode = 'PublicC'", (json.dumps({
            "shortCode": "PublicC", "likesCount": 50000,
            "musicInfo": {"song_name": "Newly recovered song", "audio_id": "222", "uses_original_audio": False}}),))
    key = create(client, data_scopes=["insights"])["key"]
    response = client.get("/api/v1/insights/music?sort=likes", headers=headers(key)).json()
    assert [p["likes"] for p in response["data"]] == [100, 50000, None]
    assert response["data"][1]["song"] == "Newly recovered song"
    assert response["data"][1]["metrics_updated_at"] == "2026-10-08T15:00:00+00:00"
    assert "re-sort returned values" in response["ordering"]["note"]


@pytest.mark.parametrize("latest_likes", [None, -1])
def test_music_api_does_not_resurrect_older_visible_likes(setup, latest_likes):
    client, connect = setup
    seed_music(connect)
    with connect() as conn:
        conn.execute("INSERT INTO engagement_observations VALUES (?, ?, ?)", (
            "PublicA", "2026-10-08T16:00:00+00:00", json.dumps({"shortCode": "PublicA", "likesCount": latest_likes})))
    key = create(client, data_scopes=["insights"])["key"]
    response = client.get("/api/v1/insights/music?limit=1", headers=headers(key)).json()
    assert response["data"][0]["shortcode"] == "PublicA"
    assert response["data"][0]["likes"] is None
    assert response["data"][0]["metrics_updated_at"] == "2026-10-08T16:00:00+00:00"


def test_music_api_requires_insights_and_current_owner_and_account_grants(setup):
    client, connect = setup
    key = create(client, data_scopes=["insights"])["key"]
    assert client.get("/api/v1/insights/music", headers=headers(key)).status_code == 200
    assert client.get("/api/v1/insights/music?account=beta", headers=headers(key)).status_code == 404
    account_key = create(client, data_scopes=["accounts"])["key"]
    assert client.get("/api/v1/insights/music", headers=headers(account_key)).status_code == 403
    assert client.get("/api/insights/music/page", headers=headers(key)).status_code == 403
    for method in ("POST", "PUT", "DELETE"):
        assert client.request(method, "/api/v1/insights/music", headers=headers(key)).status_code == 403
    with connect() as conn:
        conn.execute("UPDATE dashboard_users SET role = 'viewer', is_admin = 0, operating_roles = '[\"pd\"]' WHERE email = 'ana@example.com'")
    assert client.get("/api/v1/insights/music", headers=headers(key)).status_code == 403


def test_music_api_discovery_includes_insights_scope_and_readable_schema(setup):
    client, _ = setup
    key = create(client, data_scopes=["insights"])["key"]
    response = client.get("/api/v1/openapi.json", headers=headers(key))
    spec = response.json()
    operation = spec["paths"]["/api/v1/insights/music"]["get"]
    assert operation["x-required-scopes"] == ["insights"] and operation["x-available"] is True
    assert {param["name"] for param in operation["parameters"]} == {"account", "sort", "limit", "offset", "from", "to"}
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/MusicPostPage"
    props = spec["components"]["schemas"]["ReadMusicPost"]["properties"]
    assert props["audio_id"]["type"] == ["string", "null"]
    assert props["is_original_audio"]["type"] == ["boolean", "null"]
    capabilities = client.get("/api/v1/capabilities", headers=headers(key)).json()
    assert "/api/v1/insights/music" in json.dumps(capabilities)


def test_music_page_alias_accepts_read_agent_and_rechecks_current_access(setup, monkeypatch):
    client, connect = setup
    seed_music(connect)
    client.app.add_api_route("/api/insights/music/page", main.insights_music_page, methods=["GET"])
    client.app.include_router(agent_connections.router)
    monkeypatch.setattr(main, "list_accounts", lambda **kwargs: [{"handle": "alpha"}])
    path = "/api/insights/music/page?limit=1&from=2026-10-01&to=2026-10-01"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers("not-allowed@example.com")).status_code == 403
    created = client.post(agent_connections.URL, headers=headers("ana@example.com"), json={"name": "Music reader", "access_mode": "read"})
    assert created.status_code == 201, created.text
    key = created.json()["key"]
    response = client.get(path, headers=headers(key))
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.json()["data"][0]["song"] == "Named song"
    assert client.get("/api/insights/music/page?account=beta", headers=headers(key)).status_code == 404
    assert client.post("/api/insights/music/page", headers=headers(key)).status_code == 403
    with connect() as conn:
        conn.execute("DELETE FROM dashboard_users WHERE email = 'ana@example.com'")
    assert client.get(path, headers=headers(key)).status_code == 403


def test_hosted_mcp_discovers_and_executes_bounded_music_for_read_connection(setup, monkeypatch):
    from app import product_mcp

    client, connect = setup
    seed_music(connect)
    client.app.add_api_route("/api/insights/music/page", main.insights_music_page, methods=["GET"])
    client.app.include_router(agent_connections.router)
    monkeypatch.setattr(main, "list_accounts", lambda **kwargs: [{"handle": "alpha"}])
    key = client.post(agent_connections.URL, headers=headers("ana@example.com"), json={"name": "Music MCP", "access_mode": "read"}).json()["key"]
    product_mcp.install(client.app)

    def rpc(method, params=None):
        response = client.post("/mcp", headers={**headers(key), "Accept": "application/json, text/event-stream"},
                               json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        assert response.status_code == 200, response.text
        return response.json()["result"]

    with client:
        guide = json.loads(rpc("tools/call", {"name": "product_guide", "arguments": {}})["content"][0]["text"])
        assert "get_insights_music_page" in guide["instructions"]
        assert "re-sort returned values" in guide["instructions"]
        tools = {tool["name"]: tool for tool in rpc("tools/list")["tools"]}
        tool = tools["get_insights_music_page"]
        assert tool["annotations"]["readOnlyHint"] is True
        params = tool["inputSchema"]["properties"]["query"]["properties"]
        assert set(params) == {"account", "from", "to", "limit", "offset", "sort"}
        assert params["limit"]["maximum"] == 100
        result = rpc("tools/call", {"name": "get_insights_music_page", "arguments": {"query": {"limit": 1}}})
        assert not result.get("isError"), result
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == 200
        assert payload["data"]["data"][0]["song"] == "Named song"
        assert payload["data"]["data"][0]["audio_id"] == "9876543210987654321"
