import asyncio
import json
from collections import defaultdict, deque
from types import SimpleNamespace

import httpx
import anyio
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
    filters = {"query": "Elon Musk", "min_likes": 5001, "min_comments": None, "accounts": [], "from_date": None, "to_date": None, "sort": "newest", "promos_only": False, "post_type": "All posts", "collabs_only": False}
    calls = []
    original = httpx.AsyncClient
    def provider(req):
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            return httpx.Response(200, json={"output": [{"type": "function_call", "call_id": "show", "name": "show_research_posts", "arguments": json.dumps(filters | {"min_likes": 5000, "likes_operator": "gt", "comments_operator": "gte"})}]})
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "Filtros aplicados."}]}]})
    def client(**kwargs):
        kwargs.setdefault('transport', httpx.MockTransport(provider))
        return original(**kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    result = asyncio.run(assistant.respond(request(app), assistant.ChatRequest(message='Buscá Elon Musk con más de 5000 likes usando los filtros disponibles'), 'test-key'))
    assert result['ui_actions'] == [{"type": "research_filters", "filters": filters}]
    assert result['answer'] == 'Filtros aplicados.' and len(calls) == 1
    assert assistant.research_filters(filters | {'min_likes': 5000, 'likes_operator': 'gte'})['min_likes'] == 5000
    assert assistant.research_filters(filters | {'accounts': ['@Evolving.AI']})['accounts'] == ['evolving.ai']
    with pytest.raises(ValueError): assistant.ResearchFilters.model_validate(filters | {'sort': 'javascript:alert(1)'})


def test_ui_filters_reject_nonexistent_controls():
    for unsupported in ({"text_scope": "caption"}, {"media": "static"}, {"max_likes": 5000}, {"post_type": "Reel"}, {"sort": "most-views"}, {"audio_id": "1234"}):
        with pytest.raises(ValueError): assistant.research_filters({"query": ""} | unsupported)
    assert assistant.research_filters({"query": "", "post_type": "Carousel", "sort": "hot-rate", "collabs_only": True})['post_type'] == 'Carousel'


def test_ui_action_cannot_select_an_account_absent_from_real_controls(app, monkeypatch):
    @app.get('/api/dashboard/accounts')
    def accounts(request: Request):
        assert request.headers.get('authorization') == 'Bearer signed-in-member'
        return {"accounts": [{"handle": "evolving.ai"}]}
    app.openapi_schema = None
    original = httpx.AsyncClient
    def provider(req):
        return httpx.Response(200, json={"output": [{"type": "function_call", "call_id": "show", "name": "show_research_posts", "arguments": json.dumps({"query": "", "accounts": ["does.not.exist"]})}]})
    def client(**kwargs):
        kwargs.setdefault('transport', httpx.MockTransport(provider))
        return original(**kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    result = asyncio.run(assistant.respond(request(app), assistant.ChatRequest(message='Filter does.not.exist', language='en'), 'test-key'))
    assert result['ui_actions'] == [] and 'not available' in result['answer']


def test_single_page_and_upstream_coverage_are_marked_partial(app):
    @app.get('/api/insights/fixture/page')
    def pages(offset: int = 0):
        return {"data": [{"likes": 1}], "pagination": {"has_more": True, "next_offset": offset + 1}}

    @app.get('/api/insights/fixture/coverage')
    def incomplete_coverage():
        return {"data": [], "coverage": {"complete": False}}

    app.openapi_schema = None
    entries = assistant.operations(app)
    for path in ('/api/insights/fixture/page', '/api/insights/fixture/coverage'):
        entry = next(e for e in entries.values() if e['path'] == path)
        result, source = asyncio.run(assistant.read_operation(request(app), entry, {}))
        assert result['complete'] is False and source['partial'] is True


def test_analytics_follows_opaque_promo_cursors_and_detects_cycles(app):
    calls = []

    @app.get('/api/admin/fixture/promos')
    def promos(request: Request, cursor: str | None = None, cycle: bool = False):
        assert request.headers.get('authorization') == 'Bearer signed-in-member'
        calls.append(cursor)
        if cursor is None:
            return {"items": [{"likes": 1}], "next_cursor": "2026-11-05|evolving.ai|Post1"}
        if cursor.endswith('Post1'):
            return {"items": [{"likes": 9}], "next_cursor": "2026-11-04|evolving.ai|Post2"}
        return {"items": [{"likes": 100}], "next_cursor": "2026-11-05|evolving.ai|Post1" if cycle else None}

    app.openapi_schema = None
    entry = next(e for e in assistant.operations(app).values() if e['path'] == '/api/admin/fixture/promos')
    analysis = {"items_path": "items", "aggregations": [{"op": "sum", "field": "likes", "as": "likes"}]}
    result, source = asyncio.run(assistant.read_operation(request(app), entry, {}, analysis))
    assert result['complete'] and result['data']['complete'] and not source['partial']
    assert result['data']['data'] == [{"row_count": 3, "likes": 110, "likes_known": 3}]
    assert len(calls) == 3
    calls.clear()
    result, source = asyncio.run(assistant.read_operation(request(app), entry, {"query": {"cycle": True}}, analysis))
    assert not result['complete'] and not result['data']['complete'] and source['partial']
    assert len(calls) == 3  # No duplicated rows or infinite cursor loop.


def test_analytics_budget_bounds_all_pages_and_flags_partial(app, monkeypatch):
    calls = []
    @app.get('/api/insights/fixture/page')
    def pages(offset: int = 0):
        calls.append(offset)
        return {"data": [{"text": "x" * 150}], "pagination": {"has_more": True, "next_offset": offset + 1}}
    app.openapi_schema = None
    entry = next(e for e in assistant.operations(app).values() if e['path'] == '/api/insights/fixture/page')
    monkeypatch.setattr(assistant, 'MAX_READ_BYTES', 400)
    result, source = asyncio.run(assistant.read_operation(request(app), entry, {}, {"items_path": "data"}))
    assert calls == [0, 1]
    assert result['data']['input_rows'] == 1 and not result['complete'] and source['partial']


def test_generated_links_are_included_in_model_context_budget(monkeypatch):
    monkeypatch.setattr(assistant, 'MAX_TOOL_BYTES', 100)
    result = assistant.bounded_data({"account": "evolving.ai", "shortcode": "Post1"})
    assert result['truncated'] and 'data' not in result


def test_cancelling_request_stops_work_and_releases_active_slot(app, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test')
    state = {"started": False, "cancelled": False}
    async def blocked(*args):
        state['started'] = True
        try:
            await anyio.sleep_forever()
        finally:
            state['cancelled'] = True
    async def disconnected():
        return state['started']
    monkeypatch.setattr(assistant, 'respond', blocked)
    req = request(app)
    req.is_disconnected = disconnected
    with pytest.raises(HTTPException) as error:
        asyncio.run(assistant.assistant_chat(req, assistant.ChatRequest(message='cancel this question')))
    assert error.value.status_code == 499
    assert state == {"started": True, "cancelled": True}
    assert not assistant._active


def test_completed_answer_stops_disconnect_watch_and_preserves_response(app, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test')
    expected = {"answer": "Hello.", "sources": [], "ui_actions": []}
    async def answer(*args):
        await anyio.sleep(0)
        return expected
    monkeypatch.setattr(assistant, 'respond', answer)
    with TestClient(app) as client:
        result = client.post('/api/dashboard/assistant/chat', headers={"Authorization": "Bearer signed-in-member"}, json={"message": "hello"})
    assert result.status_code == 200 and result.json() == expected
    assert not assistant._active
