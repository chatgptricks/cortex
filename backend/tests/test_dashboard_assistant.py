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
from app.assistant_access import apply_assistant_read_access


@pytest.fixture
def app(monkeypatch):
    value = FastAPI()

    @value.middleware("http")
    async def permissions(request, call_next):
        if request.headers.get("authorization") == "Bearer signed-in-member":
            request.state.user_email = "member@example.com"
            request.state.credential_kind = "firebase"
            request.state.time_zone = "America/Costa_Rica"
            request.state.operating_role = "pd"
            apply_assistant_read_access(request)
        return await call_next(request)

    @value.get("/api/dashboard/me")
    def me(request: Request):
        if not getattr(request.state, "user_email", ""):
            raise HTTPException(401, "Sign in")
        return {"role": request.state.operating_role}

    @value.get("/api/tracker/accounts/{handle}")
    def tracker(handle: str, request: Request, limit: int = 30):
        if not getattr(request.state, "assistant_read_only", False):
            raise HTTPException(403, "Coordinator required")
        assert request.headers.get("authorization") == "Bearer signed-in-member"
        assert "x-queue-role-preview" not in request.headers
        assert "x-sentient-time-zone" not in request.headers
        return {"account": handle, "followers": 12345, "limit": limit}

    @value.delete("/api/admin/accounts/{account}")
    def remove(account: str):
        raise AssertionError("The assistant must never execute a write")

    value.include_router(assistant.router)
    monkeypatch.setattr(assistant, "_usage", defaultdict(deque))
    monkeypatch.setattr(assistant, "_active", set())
    return value


def request(app, *, preview="", credential="firebase"):
    return SimpleNamespace(app=app, headers={"authorization": "Bearer signed-in-member", "x-queue-role-preview": preview, "x-sentient-time-zone": "Pacific/Honolulu"}, state=SimpleNamespace(user_email="member@example.com", credential_kind=credential, time_zone="America/Costa_Rica"))


@pytest.mark.parametrize("message,mode,expected", [
    (" /FiLtEr   Elon Musk ", "auto", ("filter", "Elon Musk")),
    ("/RESEARCH\nlatest posts", "auto", ("research", "latest posts")),
    ("/research latest posts", "filter", ("research", "latest posts")),
    ("/filter latest posts", "research", ("filter", "latest posts")),
    ("latest posts", "research", ("research", "latest posts")),
    ("/researcher latest posts", "auto", ("auto", "/researcher latest posts")),
    ("/filtering posts", "auto", ("auto", "/filtering posts")),
    ("/filter/posts", "auto", ("auto", "/filter/posts")),
    ("Explain /filter", "auto", ("auto", "Explain /filter")),
])
def test_shortcut_parsing_has_exact_boundary_and_prefix_precedence(message, mode, expected):
    assert assistant.request_mode(assistant.ChatRequest(message=message, mode=mode)) == expected


@pytest.mark.parametrize("message,mode,language,detail", [
    ("/filter", "auto", "es", "Escribí una consulta después del comando."),
    ("  /RESEARCH \n", "auto", "en", "Write a query after the command."),
    (" ", "research", "es", "Escribí una consulta después del comando."),
    (" ", "filter", "en", "Write a query after the command."),
])
def test_empty_shortcuts_fail_before_quota_or_provider(app, monkeypatch, message, mode, language, detail):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    async def unexpected_provider(*args):
        raise AssertionError("Empty shortcuts must never contact the provider")
    monkeypatch.setattr(assistant, "respond", unexpected_provider)
    with TestClient(app) as client:
        result = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"}, json={"message": message, "mode": mode, "language": language})
    assert result.status_code == 400 and result.json()["detail"] == detail
    assert not assistant._usage and not assistant._active


def test_unknown_shortcut_mode_is_rejected_by_request_schema(app):
    with TestClient(app) as client:
        result = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"}, json={"message": "hello", "mode": "write"})
    assert result.status_code == 422
    assert not assistant._usage and not assistant._active


@pytest.mark.parametrize("message,mode", [("/research filter posts about AI", "auto"), ("filter posts about AI", "research")])
def test_research_mode_enforces_no_ui_actions_and_preserves_authorized_reads(app, monkeypatch, message, mode):
    original = httpx.AsyncClient
    calls = []
    operation = next(name for name, entry in assistant.operations(app).items() if entry["path"] == "/api/dashboard/me")
    def provider(req):
        payload = json.loads(req.content)
        calls.append(payload)
        context = json.loads(payload["instructions"].split("\nVIEW_CONTEXT=", 1)[1])
        assert context["mode"] == "research"
        assert {tool["name"] for tool in payload["tools"]} == {"search_cortex_endpoints", "query_cortex", "analyze_cortex"}
        if len(calls) == 1:
            assert payload["input"][-1] == {"role": "user", "content": "filter posts about AI"}
            # A provider can emit an unoffered tool; the server still enforces the mode.
            return httpx.Response(200, json={"output": [{"type": "function_call", "call_id": "blocked", "name": "show_research_posts", "arguments": '{"query":"AI"}'}]})
        if len(calls) == 2:
            assert "cannot change dashboard filters" in json.loads(payload["input"][-1]["output"])["error"]
            return httpx.Response(200, json={"output": [{"type": "function_call", "call_id": "read", "name": "query_cortex", "arguments": json.dumps({"operation": operation, "parameters_json": "{}"})}]})
        assert json.loads(payload["input"][-1]["output"])["data"] == {"role": "pd"}
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "Your role is PD."}]}]})
    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = asyncio.run(assistant.respond(request(app, preview="vc"), assistant.ChatRequest(message=message, mode=mode, language="en"), "test-key"))
    assert result["ui_actions"] == [] and result["answer"] == "Your role is PD."
    assert len(result["sources"]) == 1 and len(calls) == 3
    assert any(tool["name"] == "show_research_posts" for tool in assistant.TOOLS)


def test_filter_shortcut_applies_existing_controls_even_for_a_question(app, monkeypatch):
    original = httpx.AsyncClient
    def provider(req):
        payload = json.loads(req.content)
        context = json.loads(payload["instructions"].split("\nVIEW_CONTEXT=", 1)[1])
        assert context["mode"] == "filter"
        assert "even if the query is phrased as a question" in payload["instructions"]
        assert payload["input"][-1]["content"] == "which carousels mention Elon Musk with more than 5000 likes?"
        action_tool = next(tool for tool in payload["tools"] if tool["name"] == "show_research_posts")
        assert "limit" not in action_tool["parameters"]["properties"]
        return httpx.Response(200, json={"output": [{"type": "function_call", "call_id": "show", "name": "show_research_posts", "arguments": json.dumps({"query": "Elon Musk", "min_likes": 5000, "likes_operator": "gt", "post_type": "Carousel"})}]})
    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = asyncio.run(assistant.respond(request(app), assistant.ChatRequest(message="/filter which carousels mention Elon Musk with more than 5000 likes?", language="en"), "test-key"))
    assert result["answer"] == "Filters applied."
    filters = result["ui_actions"][0]["filters"]
    assert filters["query"] == "Elon Musk" and filters["min_likes"] == 5001 and filters["post_type"] == "Carousel"


@pytest.mark.parametrize("previous_mode", ["filter", "research"])
def test_history_shortcuts_do_not_select_current_request_mode(app, monkeypatch, previous_mode):
    original = httpx.AsyncClient
    def provider(req):
        payload = json.loads(req.content)
        context = json.loads(payload["instructions"].split("\nVIEW_CONTEXT=", 1)[1])
        assert context["mode"] == "auto"
        assert "Do not carry a completed /filter or /research mode over" in payload["instructions"]
        assert all(tool["name"] != "show_research_posts" for tool in payload["tools"])
        assert payload["input"][-1] == {"role": "user", "content": "What can you help me with?"}
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "I can help with the dashboard."}]}]})
    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    body = assistant.ChatRequest(message="What can you help me with?", history=[{"role": "user", "content": f"/{previous_mode} Elon Musk"}, {"role": "assistant", "content": "Done."}])
    result = asyncio.run(assistant.respond(request(app), body, "test-key"))
    assert result["ui_actions"] == []


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
    assert any(entry["path"] == "/api/tracker/accounts/{handle}" for entry in entries.values())
    assert not any("/assistant/" in entry["path"] for entry in entries.values())
    result = assistant.search_operations(entries, "tracker followers")
    assert result["operations"][0]["path"] == "/api/tracker/accounts/{handle}"


def test_reads_reauthenticate_same_caller_without_forwarding_preview_headers(app):
    entry = next(entry for entry in assistant.operations(app).values() if entry["path"] == "/api/tracker/accounts/{handle}")
    with TestClient(app) as client:
        direct = client.get("/api/tracker/accounts/example", headers={"Authorization": "Bearer signed-in-member", "x-queue-role-preview": "vc"})
    assert direct.status_code == 403
    for preview in ("pd", "vc"):
        result, source = asyncio.run(assistant.read_operation(request(app, preview=preview), entry, {"path": {"handle": "example"}, "query": {"limit": 99999}}))
        assert result["data"] == {"account": "example", "followers": 12345, "limit": 100}
        assert source["url"] == "https://sentientdash.app/tracker.html"
    original = request(app)
    original.headers["authorization"] = "Bearer invalid-user"
    denied, _ = asyncio.run(assistant.read_operation(original, entry, {"path": {"handle": "example"}}))
    assert denied["status"] == 403


def test_write_and_path_injection_never_dispatch(app):
    entries = assistant.operations(app)
    write = next(entry for entry in assistant.catalogue(app.openapi()).values() if entry["write"])
    with pytest.raises(ValueError, match="changes data"):
        assistant.prepare_read(write, {"path": {"account": "example"}})
    read = next(entry for entry in entries.values() if "{handle}" in entry["path"] and not entry["write"])
    for value in ("../users", "%2e%2e", "https://outside.example", "one?admin=true"):
        with pytest.raises(ValueError):
            assistant.prepare_read(read, {"path": {"handle": value}})
    with pytest.raises(ValueError):
        assistant.prepare_read(read, {"path": {"handle": "example"}, "query": {"secret": "x"}})


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
        assert json.loads(data["input"][-1]["output"])["data"] == {"role": "pd"}
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "Tu rol actual es PD."}]}]})

    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = asyncio.run(assistant.respond(request(app, preview="vc"), assistant.ChatRequest(message="¿Cuál es mi rol?"), "server-only-key"))
    assert result["answer"] == "Tu rol actual es PD."
    assert len(result["sources"]) == 1
    assert all(call["store"] is False and call["include"] == ["reasoning.encrypted_content"] for call in calls)
    assert "server-only-key" not in json.dumps(result)


def test_limits_and_provider_failures_release_active_request(app, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    async def failure(*args):
        raise httpx.ConnectError("private provider detail")
    monkeypatch.setattr(assistant, "respond", failure)
    monkeypatch.setattr(assistant, "resolve_reply_language", failure)
    monkeypatch.setattr(assistant, "resolve_dashboard_request", failure)
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
    @app.get('/api/insights/music/page')
    def pages(request: Request, offset: int = 0, limit: int = 100):
        assert request.headers.get('authorization') == 'Bearer signed-in-member'
        assert request.headers.get('x-queue-role-preview') is None
        assert request.headers.get('x-sentient-time-zone') is None
        assert getattr(request.state, 'assistant_read_only', False)
        total = 227
        return {"data": [{"likes": i} for i in range(offset, min(offset + limit, total))], "pagination": {"has_more": offset + limit < total, "next_offset": offset + limit if offset + limit < total else None}}
    app.openapi_schema = None
    entry = next(e for e in assistant.operations(app).values() if e['path'] == '/api/insights/music/page')
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
    result = asyncio.run(assistant.respond(request(app), assistant.ChatRequest(message='Buscá Elon Musk con más de 5000 likes usando los filtros disponibles', mode='filter'), 'test-key'))
    assert result['ui_actions'] == [{"type": "research_filters", "filters": filters}]
    assert result['answer'] == 'Filtros aplicados.' and len(calls) == 1
    assert assistant.research_filters(filters | {'min_likes': 5000, 'likes_operator': 'gte'})['min_likes'] == 5000
    assert assistant.research_filters(filters | {'accounts': ['@Evolving.AI']})['accounts'] == ['evolving.ai']
    with pytest.raises(ValueError): assistant.ResearchFilters.model_validate(filters | {'sort': 'javascript:alert(1)'})


def test_ui_filters_reject_nonexistent_controls():
    for unsupported in ({"text_scope": "caption"}, {"limit": 3}, {"media": "static"}, {"max_likes": 5000}, {"post_type": "Reel"}, {"sort": "most-views"}, {"audio_id": "1234"}):
        with pytest.raises(ValueError): assistant.research_filters({"query": ""} | unsupported)
    assert assistant.research_filters({"query": "", "post_type": "Carousel", "sort": "hot-rate", "collabs_only": True})['post_type'] == 'Carousel'


def test_caption_mentions_and_top_n_page_request_use_existing_controls(app, monkeypatch):
    original = httpx.AsyncClient
    calls = []
    def provider(req):
        payload = json.loads(req.content)
        calls.append(payload)
        assert 'do not over-interpret a casual caption reference' in payload['instructions']
        action_tool = next(tool for tool in payload['tools'] if tool['name'] == 'show_research_posts')
        assert 'limit' not in action_tool['parameters']['properties']
        assert 'text_scope' not in action_tool['parameters']['properties']
        return httpx.Response(200, json={"output": [{"type": "function_call", "call_id": "show", "name": "show_research_posts", "arguments": json.dumps({"query": "Elon Musk", "sort": "newest"})}]})
    def client(**kwargs):
        kwargs.setdefault('transport', httpx.MockTransport(provider))
        return original(**kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    result = asyncio.run(assistant.respond(request(app), assistant.ChatRequest(message='/filter Show the 3 latest posts whose caption mentions Elon Musk.', language='en'), 'test-key'))
    assert result['ui_actions'] == [{"type": "research_filters", "filters": assistant.research_filters({"query": "Elon Musk", "sort": "newest"})}]
    assert result['answer'] == 'Filters applied.'
    assert len(calls) == 1


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
    result = asyncio.run(assistant.respond(request(app), assistant.ChatRequest(message='/filter does.not.exist', language='en'), 'test-key'))
    assert result['ui_actions'] == [] and 'not available' in result['answer']


def test_single_page_and_upstream_coverage_are_marked_partial(app):
    @app.get('/api/insights/music/page')
    def pages(offset: int = 0):
        return {"data": [{"likes": 1}], "pagination": {"has_more": True, "next_offset": offset + 1}}

    @app.get('/api/insights/posts')
    def incomplete_coverage():
        return {"data": [], "coverage": {"complete": False}}

    app.openapi_schema = None
    entries = assistant.operations(app)
    for path in ('/api/insights/music/page', '/api/insights/posts'):
        entry = next(e for e in entries.values() if e['path'] == path)
        result, source = asyncio.run(assistant.read_operation(request(app), entry, {}))
        assert result['complete'] is False and source['partial'] is True


def test_analytics_follows_opaque_promo_cursors_and_detects_cycles(app):
    calls = []

    @app.get('/api/admin/promos')
    def promos(request: Request, cursor: str | None = None, cycle: bool = False):
        assert request.headers.get('authorization') == 'Bearer signed-in-member'
        calls.append(cursor)
        if cursor is None:
            return {"items": [{"likes": 1}], "next_cursor": "2026-11-05|evolving.ai|Post1"}
        if cursor.endswith('Post1'):
            return {"items": [{"likes": 9}], "next_cursor": "2026-11-04|evolving.ai|Post2"}
        return {"items": [{"likes": 100}], "next_cursor": "2026-11-05|evolving.ai|Post1" if cycle else None}

    app.openapi_schema = None
    entry = next(e for e in assistant.operations(app).values() if e['path'] == '/api/admin/promos')
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
    @app.get('/api/insights/music/page')
    def pages(offset: int = 0):
        calls.append(offset)
        return {"data": [{"text": "x" * 150}], "pagination": {"has_more": True, "next_offset": offset + 1}}
    app.openapi_schema = None
    entry = next(e for e in assistant.operations(app).values() if e['path'] == '/api/insights/music/page')
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
    monkeypatch.setattr(assistant, 'resolve_dashboard_request', blocked)
    monkeypatch.setattr(assistant, 'respond', lambda *args: pytest.fail('Cancelled language resolution must not start chat'))
    req = request(app)
    req.is_disconnected = disconnected
    with pytest.raises(HTTPException) as error:
        asyncio.run(assistant.assistant_chat(req, assistant.ChatRequest(message='cancel this question')))
    assert error.value.status_code == 499
    assert state == {"started": True, "cancelled": True}
    assert not assistant._active


def test_completed_answer_stops_disconnect_watch_and_preserves_response(app, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test')
    expected = {"answer": "Hello.", "sources": [], "ui_actions": [], "language": "en", "mode": "auto"}
    async def classify(*args):
        return {"language": "en", "mode": "auto", "continue_next": False, "needs_query": False}
    monkeypatch.setattr(assistant, "resolve_dashboard_request", classify)
    async def answer(*args):
        await anyio.sleep(0)
        return expected
    monkeypatch.setattr(assistant, 'respond', answer)
    with TestClient(app) as client:
        result = client.post('/api/dashboard/assistant/chat', headers={"Authorization": "Bearer signed-in-member"}, json={"message": "hello"})
    assert result.status_code == 200 and result.json() == expected
    assert not assistant._active
