"""Assistant keyword limits preserve query intent and the full search criteria."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Query, Request

from app import dashboard_assistant as assistant
from app.assistant_access import apply_assistant_read_access


@pytest.fixture
def research_app():
    app = FastAPI()
    reads = []

    @app.middleware("http")
    async def permissions(request, call_next):
        if request.headers.get("authorization") == "Bearer signed-in-member":
            request.state.user_email = "member@example.com"
            request.state.credential_kind = "firebase"
            apply_assistant_read_access(request)
        return await call_next(request)

    @app.get("/api/dashboard/posts/search")
    def posts(request: Request, q: str = "", account: str = "", min_likes: int = 0,
              start: str = Query("", alias="from"), end: str = Query("", alias="to"),
              text_scope: str = "all", sort: str = "newest", limit: int = 20):
        assert request.state.assistant_read_only
        reads.append(dict(request.query_params))
        return {"data": [
            {"account": "evolving.ai", "shortcode": "match", "likes": 6000,
             "caption": "Elon Musk unveils a new Tesla vehicle"},
            {"account": "evolving.ai", "shortcode": "other", "likes": 7000,
             "caption": "Elon Musk discusses something unrelated"},
        ], "pagination": {"has_more": False}}

    request = SimpleNamespace(
        app=app, headers={"authorization": "Bearer signed-in-member"},
        state=SimpleNamespace(user_email="member@example.com", credential_kind="firebase",
                              time_zone="America/Costa_Rica"),
    )
    operation, entry = next(iter(assistant.operations(app).items()))
    return request, operation, entry, reads


def provider_client(monkeypatch, provider):
    original = httpx.AsyncClient

    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


def function_call(name, arguments, call_id):
    return httpx.Response(200, json={"output": [{
        "type": "function_call", "name": name, "call_id": call_id,
        "arguments": json.dumps(arguments),
    }]})


@pytest.mark.parametrize("query", [
    "", "  ", "Tesla", "Elon Musk", "inteligencia artificial generativa",
    "  Elon\nMusk  ", '"Elon Musk"', "GPT-5.1", "evolving.ai", "AI/ML",
])
def test_short_keyword_queries_are_preserved(research_app, query):
    _, _, entry, _ = research_app
    assistant.validate_research_query(query)
    assert assistant.prepare_read(entry, {"query": {"q": query}})[1]["q"] == query
    assert assistant.research_filters({"query": query})["query"] == query


@pytest.mark.parametrize("query", [
    "busca todos los posts sobre Elon Musk",
    "Elon Musk unveils Tesla",
    "inteligencia\nartificial\tgenerativa avanzada",
    "Elon,Musk,Tesla,SpaceX",
    '"one two three four"',
])
def test_long_keyword_queries_are_rejected_instead_of_truncated(research_app, query):
    _, _, entry, reads = research_app
    with pytest.raises(assistant.ResearchQueryTooLong):
        assistant.validate_research_query(query)
    with pytest.raises(assistant.ResearchQueryTooLong):
        assistant.prepare_read(entry, {"query": {"q": query}})
    with pytest.raises(assistant.ResearchQueryTooLong):
        assistant.research_filters({"query": query})
    assert reads == []


@pytest.mark.parametrize("query", [None, 42, ["Elon", "Musk"], {"topic": "Tesla"}])
def test_keyword_validation_does_not_coerce_non_strings(query):
    with pytest.raises(ValueError):
        assistant.validate_research_query(query)


@pytest.mark.parametrize("tool", ["query_cortex", "analyze_cortex"])
@pytest.mark.parametrize("input_mode", ["text", "voice"])
def test_oversized_query_is_retried_before_reading_and_keeps_full_criteria(
        research_app, monkeypatch, tool, input_mode):
    request, operation, _, reads = research_app
    message = "Busca los posts de evolving.ai sobre Elon Musk que tengan más de 5000 likes en octubre."
    phrase = "Elon Musk unveils a new Tesla vehicle"
    criteria = {"account": "evolving.ai", "min_likes": 5001, "from": "2026-10-01",
                "to": "2026-10-31", "text_scope": "caption", "sort": "likes", "limit": 10}
    analysis = {"items_path": "data", "filters": [{"field": "caption", "op": "contains", "value": phrase}],
                "fields": ["shortcode", "likes", "dashboard_url"], "limit": 10}
    calls = []

    def provider(req):
        payload = json.loads(req.content)
        calls.append(payload)
        assert next(item for item in payload["input"] if item.get("role") == "user")["content"] == message
        assert all(item["name"] != "show_research_posts" for item in payload["tools"])
        if len(calls) <= 2:
            if len(calls) == 2:
                error = json.loads(payload["input"][-1]["output"])["error"]
                assert "3" in error or "three" in error.lower()
                assert reads == [], "The invalid query must never reach the endpoint"
            query = {**criteria, "q": message if len(calls) == 1 else "Elon Musk"}
            arguments = {"operation": operation, "parameters_json": json.dumps({"query": query})}
            if tool == "analyze_cortex":
                arguments["analysis_json"] = json.dumps(analysis)
            return function_call(tool, arguments, str(len(calls)))
        result = json.loads(payload["input"][-1]["output"])
        assert result["status"] == 200
        rows = result["data"]["data"]
        assert [row["shortcode"] for row in rows] == (["match"] if tool == "analyze_cortex" else ["match", "other"])
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{
            "type": "output_text", "text": "[Resultado](https://sentientdash.app/index.html?post=evolving.ai%3Amatch)",
        }]}]})

    provider_client(monkeypatch, provider)
    body = assistant.ChatRequest(message=message, mode="research", input_mode=input_mode)
    result = asyncio.run(assistant.respond(request, body, "test-key"))
    assert len(calls) == 3
    assert len(reads) == 1
    assert reads[0] == {key: str(value) for key, value in {**criteria, "q": "Elon Musk"}.items()}
    assert result["ui_actions"] == []
    assert len(result["sources"]) == 1
    assert body.message == message


def test_filter_query_retry_emits_only_corrected_action(research_app, monkeypatch):
    request, _, _, reads = research_app
    calls = []
    message = "Filtra los posts sobre Elon Musk con más de 5000 likes."

    def provider(req):
        payload = json.loads(req.content)
        calls.append(payload)
        assert next(item for item in payload["input"] if item.get("role") == "user")["content"] == message
        if len(calls) == 2:
            error = json.loads(payload["input"][-1]["output"])["error"]
            assert "3" in error or "three" in error.lower()
        return function_call("show_research_posts", {
            "query": message if len(calls) == 1 else "Elon Musk",
            "min_likes": 5000, "likes_operator": "gt", "post_type": "Carousel", "sort": "likes-desc",
        }, str(len(calls)))

    provider_client(monkeypatch, provider)
    result = asyncio.run(assistant.respond(request, assistant.ChatRequest(message=message, mode="filter"), "test-key"))
    assert len(calls) == 2
    assert reads == []
    assert len(result["ui_actions"]) == 1
    filters = result["ui_actions"][0]["filters"]
    assert filters["query"] == "Elon Musk"
    assert filters["min_likes"] == 5001
    assert filters["post_type"] == "Carousel" and filters["sort"] == "likes-desc"


def test_endpoint_discovery_is_not_restricted_to_content_keyword_limit(research_app):
    request, operation, _, _ = research_app
    result = assistant.search_operations(assistant.operations(request.app), "research dashboard posts search caption")
    assert any(item["operation"] == operation for item in result["operations"])


def test_unrelated_read_parameters_are_not_treated_as_research_keywords():
    entry = {"path": "/api/dashboard/news", "write": False,
             "tool": SimpleNamespace(inputSchema={"properties": {"query": {"properties": {"q": {}}}}})}
    query = "a complete news title with many words"
    assert assistant.prepare_read(entry, {"query": {"q": query}})[1]["q"] == query
