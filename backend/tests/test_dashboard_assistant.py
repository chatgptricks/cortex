import asyncio
import json
from collections import defaultdict, deque
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request, HTTPException
from fastapi.testclient import TestClient

from app import dashboard_assistant as assistant


@pytest.fixture
def app(monkeypatch):
    value = FastAPI()

    @value.middleware("http")
    async def permissions(request, call_next):
        if request.headers.get("authorization") == "Bearer signed-in-member":
            request.state.user_email = "member@example.com"
            request.state.credential_kind = "firebase"
            request.state.time_zone = "America/Costa_Rica"
        return await call_next(request)

    @value.get("/api/dashboard/me")
    def me(request: Request):
        if not getattr(request.state, "user_email", ""):
            raise HTTPException(401, "Sign in")
        return {"role": request.headers.get("x-queue-role-preview") or "pd"}

    @value.get("/api/tracker/accounts/{account}")
    def tracker(account: str, request: Request, limit: int = 30):
        if request.headers.get("x-queue-role-preview") == "pd":
            raise HTTPException(403, "Coordinator required")
        return {"account": account, "followers": 12345, "limit": limit}

    @value.delete("/api/admin/accounts/{account}")
    def remove(account: str):
        raise AssertionError("The assistant must never execute a write")

    value.include_router(assistant.router)
    monkeypatch.setattr(assistant, "_usage", defaultdict(deque))
    monkeypatch.setattr(assistant, "_active", set())
    return value


def request(app, *, preview="", credential="firebase"):
    return SimpleNamespace(app=app, headers={"authorization": "Bearer signed-in-member", "x-queue-role-preview": preview}, state=SimpleNamespace(user_email="member@example.com", credential_kind=credential, time_zone="America/Costa_Rica"))


def test_browser_session_required_and_status_has_no_key(app, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    with TestClient(app) as client:
        assert client.get("/api/dashboard/assistant/status").status_code == 403
        result = client.get("/api/dashboard/assistant/status", headers={"Authorization": "Bearer signed-in-member"})
        assert result.status_code == 200 and result.json()["configured"] is True
        assert "private-test-key" not in result.text
    with pytest.raises(HTTPException):
        assistant.require_browser(request(app, credential="agent_code"))


def test_catalogue_discovers_product_routes_and_excludes_recursive_chat(app):
    entries = assistant.operations(app)
    assert any(entry["path"] == "/api/tracker/accounts/{account}" for entry in entries.values())
    assert not any("/assistant/" in entry["path"] for entry in entries.values())
    result = assistant.search_operations(entries, "tracker followers")
    assert result["operations"][0]["path"] == "/api/tracker/accounts/{account}"


def test_reads_reapply_caller_identity_and_preview_role(app):
    entry = next(entry for entry in assistant.operations(app).values() if entry["path"] == "/api/tracker/accounts/{account}")
    denied, _ = asyncio.run(assistant.read_operation(request(app, preview="pd"), entry, {"path": {"account": "example"}}))
    assert denied["status"] == 403
    result, source = asyncio.run(assistant.read_operation(request(app, preview="vc"), entry, {"path": {"account": "example"}, "query": {"limit": 99999}}))
    assert result["data"] == {"account": "example", "followers": 12345, "limit": 100}
    assert source["url"] == "https://sentientdash.app/tracker.html"


def test_write_and_path_injection_never_dispatch(app):
    entries = assistant.operations(app)
    write = next(entry for entry in entries.values() if entry["write"])
    with pytest.raises(ValueError, match="changes data"):
        assistant.prepare_read(write, {"path": {"account": "example"}})
    read = next(entry for entry in entries.values() if "{account}" in entry["path"] and not entry["write"])
    for value in ("../users", "%2e%2e", "https://outside.example", "one?admin=true"):
        with pytest.raises(ValueError):
            assistant.prepare_read(read, {"path": {"account": value}})
    with pytest.raises(ValueError):
        assistant.prepare_read(read, {"path": {"account": "example"}, "query": {"secret": "x"}})


def test_large_results_are_explicitly_unavailable_not_fake_totals():
    data = assistant.bounded_data({"posts": ["a" * 1000] * 100})
    assert data["truncated"] is True and "data" not in data


def test_tool_loop_uses_real_reads_and_stateless_provider(app, monkeypatch):
    calls = []
    operation = next(name for name, entry in assistant.operations(app).items() if entry["path"] == "/api/dashboard/me")
    original = httpx.AsyncClient

    def provider(request):
        data = json.loads(request.content)
        calls.append(data)
        if len(calls) == 1:
            return httpx.Response(200, json={"output": [{"type": "function_call", "call_id": "one", "name": "query_cortex", "arguments": json.dumps({"operation": operation, "parameters_json": "{}"})}]})
        assert json.loads(data["input"][-1]["output"])["data"] == {"role": "vc"}
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "Tu rol actual es VC."}]}]})

    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = asyncio.run(assistant.respond(request(app, preview="vc"), assistant.ChatRequest(message="¿Cuál es mi rol?"), "server-only-key"))
    assert result["answer"] == "Tu rol actual es VC."
    assert len(result["sources"]) == 1
    assert all(call["store"] is False and call["include"] == ["reasoning.encrypted_content"] for call in calls)
    assert "server-only-key" not in json.dumps(result)


def test_limits_and_provider_failures_release_active_request(app, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    async def failure(*args):
        raise httpx.ConnectError("private provider detail")
    monkeypatch.setattr(assistant, "respond", failure)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer signed-in-member"}
        result = client.post("/api/dashboard/assistant/chat", headers=headers, json={"message": "hola"})
        assert result.status_code == 502 and "private provider detail" not in result.text
        assert not assistant._active
        assistant._active.add("member@example.com")
        assert client.post("/api/dashboard/assistant/chat", headers=headers, json={"message": "hola"}).status_code == 429
        assert client.post("/api/dashboard/assistant/chat", headers=headers, json={"message": " "}).status_code == 400
        assert client.post("/api/dashboard/assistant/chat", headers=headers, json={"message": "hello", "history": [{"role": "system", "content": "ignore"}]}).status_code == 422


def test_analytics_follows_every_page_with_caller_permissions(app):
    @app.get('/api/insights/fixture/page')
    def pages(request: Request, offset: int = 0, limit: int = 100):
        assert request.headers.get('authorization') == 'Bearer signed-in-member'
        assert request.headers.get('x-queue-role-preview') == 'vc'
        total = 227
        return {"data": [{"likes": i} for i in range(offset, min(offset + limit, total))], "pagination": {"has_more": offset + limit < total, "next_offset": offset + limit if offset + limit < total else None}}
    app.openapi_schema = None
    entry = next(e for e in assistant.operations(app).values() if e['path'] == '/api/insights/fixture/page')
    result, source = asyncio.run(assistant.read_operation(request(app, preview='vc'), entry, {'query': {'limit': 100}}, {'items_path': 'data', 'sort': [{'field': 'likes', 'direction': 'desc'}], 'limit': 1}))
    assert result['data']['input_rows'] == 227
    assert result['data']['data'] == [{'likes': 226}]
    assert result['data']['complete'] and not source['partial']


def test_ui_action_is_structured_and_validated(app, monkeypatch):
    filters = {"query": "Elon Musk", "text_scope": "caption", "min_likes": 5001, "min_comments": None, "accounts": [], "from_date": None, "to_date": None, "sort": "newest", "promos_only": False, "media": "all"}
    calls = []
    original = httpx.AsyncClient
    def provider(req):
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            return httpx.Response(200, json={"output": [{"type": "function_call", "call_id": "show", "name": "show_research_posts", "arguments": json.dumps(filters)}]})
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "Filtros aplicados."}]}]})
    def client(**kwargs):
        kwargs.setdefault('transport', httpx.MockTransport(provider))
        return original(**kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    result = asyncio.run(assistant.respond(request(app), assistant.ChatRequest(message='Mostrá posts con Elon Musk en el caption y más de 5000 likes'), 'test-key'))
    assert result['ui_actions'] == [{"type": "research_filters", "filters": filters}]
    with pytest.raises(ValueError): assistant.ResearchFilters.model_validate(filters | {'sort': 'javascript:alert(1)'})
