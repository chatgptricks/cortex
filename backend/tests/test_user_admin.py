import asyncio
from contextlib import contextmanager

import httpx

from app import main


def test_form_can_explicitly_clear_optional_fields(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "FIREBASE_APP", None)
    monkeypatch.setattr(main, "upsert_dashboard_user", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(main, "admin_list_users", lambda: {"users": [{"email": "pd@example.com", "avatar_url": "avatar"}]})
    monkeypatch.setattr(main, "_queue_v2_publish", lambda *args: None)

    @contextmanager
    def connect():
        yield object()

    monkeypatch.setattr(main, "connect", connect)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.post("/api/admin/users", data={
                "email": "pd@example.com", "role": "viewer", "operating_role": "pd",
                "display_name": "Designer", "clear_fields": "slack_user_id,time_zone,minutes_per_pp",
            })
            assert response.status_code == 200
            assert response.json()["users"][0]["avatar_url"] == "avatar"
            response = await client.post("/api/admin/users", data={"email": "pd@example.com", "clear_fields": "role"})
            assert response.status_code == 400

    asyncio.run(scenario())
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[4] == ""
    assert args[6] == ""
    assert kwargs["clear_minutes_per_pp"] is True
