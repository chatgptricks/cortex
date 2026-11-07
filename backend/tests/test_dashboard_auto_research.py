"""Default queries resolve semantically before any page-changing tool is available."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import dashboard_assistant as assistant
from test_dashboard_assistant import app, request  # noqa: F401 — authenticated fixture


def metadata(mode, language="es", *, shape=None, count=None, sufficient=None):
    return {"language": language,
            "result_shape": shape or ("all_matching_posts" if mode == "filter" else "selected_posts"),
            "requested_count": count, "page_filters_sufficient": mode == "filter" if sufficient is None else sufficient,
            "mode": mode, "continue_next": False, "needs_query": False}


def function_call(name, arguments):
    return {"output": [{"type": "function_call", "call_id": "call", "name": name,
                        "arguments": json.dumps(arguments)}]}


@pytest.mark.parametrize("input_mode", ["text", "voice"])
@pytest.mark.parametrize("message,mode", [
    ("¿Cuál es el último post de evolving.ai?", "research"),
    ("most liked post in the last 24 hours", "research"),
    ("best 3 reels ever", "research"),
    ("las últimas 10 promos", "research"),
    ("ideas parecidas a este post", "research"),
    ("filtra los posts con menos de 5000 likes", "research"),
    ("research posts about Elon Musk with more than 5000 likes", "research"),
    ("posts sobre Elon Musk con más de 5000 likes", "filter"),
])
def test_automatic_turn_routes_once_without_losing_full_query(app, monkeypatch, input_mode, message, mode):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    observed = []

    async def classify(client, key, instructions, inputs, tool, **kwargs):
        assert tool["name"] == ("resolve_voice_request" if input_mode == "voice" else "resolve_dashboard_request")
        context = json.loads(inputs[0]["content"])
        assert context["current_message"] == message and context["locked_mode"] is None
        # These are the actual routing instructions, before action tools exist.
        assert "DEFAULT: intelligent research of stored dashboard data" in instructions
        assert "ALL requested conditions" in instructions
        assert "No count limit, exact rolling-hour window" in instructions
        assert "semantic similarity" in instructions and "maximum likes" in instructions
        assert "Never use external/web research" in instructions
        observed.append("resolve")
        return metadata(mode)

    async def respond(req, body, key):
        observed.append("respond")
        assert body.message == message  # Keyword selection must not replace user intent.
        assert body.language == "es" and body.input_mode == input_mode
        assert assistant.request_mode(body) == (mode, message)
        return {"answer": "OK", "ui_actions": []}

    monkeypatch.setattr(assistant, "next_tool_call", classify)
    monkeypatch.setattr(assistant, "resolve_reply_language", lambda *args: pytest.fail("No duplicate language call"))
    monkeypatch.setattr(assistant, "respond", respond)
    with TestClient(app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"},
                               json={"message": message, "input_mode": input_mode, "language": "en"})
    assert response.status_code == 200
    assert response.json()["mode"] == mode and response.json()["language"] == "es"
    assert observed == ["resolve", "respond"] and not assistant._active


@pytest.mark.parametrize("input_mode", ["text", "voice"])
def test_resolved_filter_returns_real_page_action_and_effective_mode(app, monkeypatch, input_mode):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    original = httpx.AsyncClient
    calls = []
    message = "posts sobre Elon Musk con más de 5000 likes"

    def provider(req):
        payload = json.loads(req.content)
        calls.append(payload)
        if len(calls) == 1:
            name = "resolve_voice_request" if input_mode == "voice" else "resolve_dashboard_request"
            assert payload["tool_choice"] == {"type": "function", "name": name}
            return httpx.Response(200, json=function_call(name, metadata("filter")))
        assert payload["input"][-1]["content"] == message
        assert any(tool["name"] == "show_research_posts" for tool in payload["tools"])
        return httpx.Response(200, json=function_call("show_research_posts", {
            "query": "Elon Musk", "min_likes": 5000, "likes_operator": "gt",
        }))

    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    with TestClient(app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"},
                               json={"message": message, "input_mode": input_mode})
    assert response.status_code == 200 and len(calls) == 2
    data = response.json()
    assert data["mode"] == "filter" and data["answer"] == "Filtros aplicados."
    assert data["ui_actions"][0]["filters"]["query"] == "Elon Musk"
    assert data["ui_actions"][0]["filters"]["min_likes"] == 5001


@pytest.mark.parametrize("mode", ["auto", "research"])
def test_non_filter_mode_cannot_mutate_page_even_if_provider_emits_action(app, monkeypatch, mode):
    original = httpx.AsyncClient
    calls = []

    def provider(req):
        payload = json.loads(req.content)
        calls.append(payload)
        assert all(tool["name"] != "show_research_posts" for tool in payload["tools"])
        if len(calls) == 1:
            return httpx.Response(200, json=function_call("show_research_posts", {"query": "AI"}))
        assert "Only a resolved filter request" in json.loads(payload["input"][-1]["output"])["error"]
        return httpx.Response(200, json={"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "I can research dashboard posts."}]}]})

    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = asyncio.run(assistant.respond(request(app), assistant.ChatRequest(message="Find similar posts", mode=mode), "test"))
    assert result["ui_actions"] == [] and len(calls) == 2


@pytest.mark.parametrize("message,selected,expected", [
    ("/filter which posts mention AI?", "research", "filter"),
    ("/research filter posts about AI", "filter", "research"),
    ("posts about AI", "research", "research"),
    ("which posts mention AI?", "filter", "filter"),
    ("/next reel for evolving.ai", "research", "next"),
    ("reel for evolving.ai", "next", "next"),
    ("/researcher posts about AI", "auto", "auto"),
])
def test_explicit_typed_modes_keep_precedence_over_automatic_routing(app, monkeypatch, message, selected, expected):
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    async def language(*args):
        return "en"

    async def respond(req, body, key):
        assert assistant.request_mode(body)[0] == expected
        return {"answer": "OK", "ui_actions": []}

    monkeypatch.setattr(assistant, "resolve_reply_language", language)
    monkeypatch.setattr(assistant, "resolve_dashboard_request", lambda *args: pytest.fail("Explicit mode must not be inferred"))
    monkeypatch.setattr(assistant, "respond", respond)
    with TestClient(app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"},
                               json={"message": message, "mode": selected})
    assert response.status_code == 200 and response.json()["mode"] == expected


@pytest.mark.parametrize("shape,count,sufficient,expected", [
    ("all_matching_posts", 2, True, "research"),  # Count wins even if both other fields claim filtering works.
    ("selected_posts", None, True, "research"),  # Implicit best/latest selection cannot use a full page.
    ("analysis", None, True, "research"),
    ("all_matching_posts", None, False, "research"),  # Any unsupported criterion requires research.
    ("all_matching_posts", None, True, "filter"),
])
def test_independent_outcome_facts_override_automatic_filter_mode(monkeypatch, shape, count, sufficient, expected):
    async def classify(client, key, instructions, inputs, tool, **kwargs):
        properties = tool["parameters"]["properties"]
        assert set(tool["parameters"]["required"]) == set(properties)
        assert properties["requested_count"]["minimum"] == 1
        assert "NOT a result count" in properties["requested_count"]["description"]
        return metadata("filter", shape=shape, count=count, sufficient=sufficient)

    monkeypatch.setattr(assistant, "next_tool_call", classify)
    result = asyncio.run(assistant.resolve_dashboard_request(assistant.ChatRequest(message="Una consulta"), "test"))
    assert result["mode"] == expected


@pytest.mark.parametrize("message,selected", [("/filter Show the top two posts", "auto"), ("Show the top two posts", "filter")])
def test_explicit_filter_lock_survives_result_shape_guard(monkeypatch, message, selected):
    async def classify(*args, **kwargs):
        return metadata("research", shape="selected_posts", count=2, sufficient=False)

    monkeypatch.setattr(assistant, "next_tool_call", classify)
    body = assistant.ChatRequest(message=message, mode=selected, input_mode="voice")
    result = asyncio.run(assistant.resolve_voice_request(body, "test"))
    assert result["mode"] == "filter"


@pytest.mark.parametrize("input_mode,language,message", [
    ("text", "es", "Filtra los 2 posts con más likes de evolving.ai"),
    ("voice", "en", "Filter the two most liked posts from evolving.ai"),
    ("text", "en", "Filter to the most liked post from evolving.ai"),
])
def test_production_top_count_regression_queries_exact_results_without_changing_page(app, monkeypatch, input_mode, language, message):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    original = httpx.AsyncClient
    calls, reads = [], []
    count = 1 if message.startswith("Filter to") else 2

    @app.get("/api/insights/top-posts")
    def top_posts(account: str, limit: int):
        reads.append((account, limit))
        assert account == "evolving.ai" and limit == count
        return {"posts": [{"account": account, "shortcode": f"Post{i}", "likes": 9000 - i} for i in range(limit)]}

    app.openapi_schema = None
    operation = next(name for name, entry in assistant.operations(app).items() if entry["path"] == "/api/insights/top-posts")

    def provider(req):
        payload = json.loads(req.content)
        calls.append(payload)
        if len(calls) == 1:
            name = "resolve_voice_request" if input_mode == "voice" else "resolve_dashboard_request"
            assert payload["tool_choice"] == {"type": "function", "name": name}
            assert json.loads(payload["input"][0]["content"])["current_message"] == message
            # Reproduce a wrong mode decision; the independent semantic facts
            # must stop it from opening every post sorted by likes.
            return httpx.Response(200, json=function_call(name, metadata("filter", language,
                shape="selected_posts", count=count, sufficient=False)))
        assert all(tool["name"] != "show_research_posts" for tool in payload["tools"])
        assert json.loads(payload["instructions"].split("\nVIEW_CONTEXT=", 1)[1])["mode"] == "research"
        if len(calls) == 2:
            assert payload["input"][-1]["content"] == message
            # Even an unoffered page action from the answer model is blocked.
            return httpx.Response(200, json=function_call("show_research_posts", {"accounts": ["evolving.ai"], "sort": "likes-desc"}))
        if len(calls) == 3:
            assert "cannot change dashboard filters" in json.loads(payload["input"][-1]["output"])["error"]
            return httpx.Response(200, json=function_call("query_cortex", {"operation": operation,
                "parameters_json": json.dumps({"query": {"account": "evolving.ai", "limit": count}})}))
        posts = json.loads(payload["input"][-1]["output"])["data"]["posts"]
        answer = "\n".join(f"[Post {i + 1}]({post['dashboard_url']})" for i, post in enumerate(posts))
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": answer}]}]})

    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    with TestClient(app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"},
            json={"message": message, "language": "en", "input_mode": input_mode, "history": [], "page": "/"})
    assert response.status_code == 200
    result = response.json()
    assert result["mode"] == "research" and result["ui_actions"] == []
    assert result["language"] == language
    assert result["answer"].count("https://sentientdash.app/") == count
    assert reads == [("evolving.ai", count)] and len(calls) == 4
