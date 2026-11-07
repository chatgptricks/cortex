"""Voice intent routes through the same bounded dashboard command workflows."""
import asyncio
import json

import anyio
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import dashboard_assistant as assistant
from test_dashboard_assistant import app, request  # noqa: F401 — shared authenticated fixture


def voice_metadata(mode="auto", language="es", *, continuation=False, needs_query=False):
    shape = "clarification" if needs_query else {"filter": "all_matching_posts", "research": "selected_posts", "next": "next_recommendation", "auto": "conversation"}[mode]
    return {"language": language, "result_shape": shape, "requested_count": None,
            "page_filters_sufficient": mode == "filter" and not needs_query,
            "mode": mode, "continue_next": continuation, "needs_query": needs_query}


def provider_tool(name, values):
    return {"output": [{"type": "function_call", "call_id": "route", "name": name, "arguments": json.dumps(values)}]}


@pytest.mark.parametrize("message,mode,language", [
    ("Filtrá posts sobre Elon Musk con más de cinco mil likes", "filter", "es"),
    ("Apply filters for carousels with over five thousand likes", "filter", "en"),
    ("Investiga las últimas diez promos", "research", "es"),
    ("Busca en el dashboard posts que digan next", "research", "es"),
    ("Research the latest posts from evolving.ai", "research", "en"),
    ("No filtres, investiga las promos", "research", "es"),
    ("Filtra los posts cuyo caption diga research", "filter", "es"),
    ("Quiero el próximo post para recrear", "next", "es"),
    ("What should I post next?", "next", "en"),
    ("¿Qué hace el filtro?", "auto", "es"),
    ("What does research do?", "auto", "en"),
])
def test_one_metadata_call_routes_voice_without_rewriting_transcript(app, monkeypatch, message, mode, language):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    calls, routed = [], []
    original = httpx.AsyncClient

    def provider(req):
        data = json.loads(req.content)
        calls.append(data)
        assert data["tool_choice"]["name"] == "resolve_voice_request"
        context = json.loads(data["input"][0]["content"])
        assert context["current_message"] == message
        assert context["locked_mode"] is None
        assert context["pending_next"] is None
        return httpx.Response(200, json=provider_tool("resolve_voice_request", voice_metadata(mode, language)))

    async def respond(req, body, key):
        routed.append(body)
        return {"answer": "OK", "sources": [], "ui_actions": []}

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(provider), **kwargs))
    monkeypatch.setattr(assistant, "respond", respond)
    with TestClient(app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"},
            json={"message": message, "input_mode": "voice", "language": "en" if language == "es" else "es"})
    assert response.status_code == 200
    assert response.json()["mode"] == mode and response.json()["language"] == language
    assert len(calls) == 1 and len(routed) == 1
    assert routed[0].message == message
    assert assistant.request_mode(routed[0]) == (mode, message)
    assert not assistant._active


@pytest.mark.parametrize("message,requested,inferred,expected", [
    ("/research filtra los posts de Elon Musk", "auto", "filter", "research"),
    ("/filter investiga Elon Musk", "research", "research", "filter"),
    ("filtra los posts de Elon Musk", "research", "filter", "research"),
    ("/next", "auto", "filter", "next"),
    ("/nextish post", "auto", "next", "auto"),
])
def test_explicit_command_is_authoritative_even_if_classifier_ignores_lock(monkeypatch, message, requested, inferred, expected):
    async def classify(client, key, instructions, inputs, tool, **kwargs):
        context = json.loads(inputs[0]["content"])
        assert context["locked_mode"] == expected
        return voice_metadata(inferred)
    monkeypatch.setattr(assistant, "next_tool_call", classify)
    body = assistant.ChatRequest(message=message, mode=requested, input_mode="voice")
    result = asyncio.run(assistant.resolve_voice_request(body, "test"))
    assert result["mode"] == expected
    assert result["continue_next"] is False


@pytest.mark.parametrize("input_mode", ["text", "voice"])
@pytest.mark.parametrize("message,mode,continuation,retained", [
    ("reel", "next", True, True),
    ("mejor imagen", "next", True, True),
    ("mejor investiga las últimas promos", "research", False, False),
    ("filtra los posts con más de cinco mil likes", "filter", False, False),
    ("quiero el próximo post", "next", False, False),
    ("¿qué hace el filtro?", "auto", False, False),
])
def test_followup_only_retains_next_slots_for_actual_continuation(app, monkeypatch, input_mode, message, mode, continuation, retained):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    slots = assistant.NextContext(post_format=None, target_account="chatgptricks")
    observed = []

    async def classify(client, key, instructions, inputs, tool, **kwargs):
        assert json.loads(inputs[0]["content"])["pending_next"] == slots.model_dump()
        return voice_metadata(mode, continuation=continuation)
    async def respond(req, body, key):
        observed.append(body)
        return {"answer": "OK", "ui_actions": []}
    monkeypatch.setattr(assistant, "next_tool_call", classify)
    monkeypatch.setattr(assistant, "respond", respond)
    with TestClient(app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"},
            json={"message": message, "input_mode": input_mode, "next_context": slots.model_dump()})
    assert response.status_code == 200 and response.json()["mode"] == mode
    assert observed[0].next_context == (slots if retained else None)
    assert assistant.request_mode(observed[0])[0] == mode


@pytest.mark.parametrize("mode,message,language", [("filter", "filtro", "es"), ("research", "investiga", "es"), ("research", "research", "en")])
def test_bare_spoken_action_clarifies_without_querying_or_clearing_page(app, monkeypatch, mode, message, language):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    async def classify(*args, **kwargs):
        return voice_metadata(mode, language, needs_query=True)
    monkeypatch.setattr(assistant, "next_tool_call", classify)
    monkeypatch.setattr(assistant, "respond", lambda *args: pytest.fail("No dashboard tools should run before criteria are supplied"))
    with TestClient(app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"},
            json={"message": message, "input_mode": "voice"})
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == mode and data["language"] == language
    assert data["ui_actions"] == [] and data["sources"] == []
    assert data["answer"].endswith("?")
    assert "pending_next" not in data


def test_spoken_next_uses_existing_required_slot_workflow(app, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    calls = []
    async def classify(client, key, instructions, inputs, tool, **kwargs):
        calls.append(tool["name"])
        if tool["name"] == "resolve_voice_request":
            return voice_metadata("next")
        assert tool["name"] == "prepare_next_recommendation"
        assert inputs[0]["content"] == "el siguiente"
        return {"post_format": None, "target_account": None}
    monkeypatch.setattr(assistant, "next_tool_call", classify)
    with TestClient(app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"},
            json={"message": "el siguiente", "input_mode": "voice", "language": "en"})
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "next" and data["language"] == "es"
    assert data["answer"] == "¿Reel, imagen o carrusel, y para qué cuenta?"
    assert data["pending_next"] == {"post_format": None, "target_account": None}
    assert calls == ["resolve_voice_request", "prepare_next_recommendation"]


@pytest.mark.parametrize("bad", [
    {"mode": "delete"}, {"language": "fr"}, {"mode": ["filter"]}, {"language": []},
    {"continue_next": 1}, {"needs_query": "false"}, {"unexpected": "secret"},
    {"result_shape": "filter"}, {"result_shape": []}, {"requested_count": True},
    {"requested_count": 0}, {"requested_count": -1}, {"requested_count": 2.5},
    {"page_filters_sufficient": "true"}, {"page_filters_sufficient": 1},
])
def test_invalid_voice_metadata_rejected_before_dispatch(monkeypatch, bad):
    async def classify(*args, **kwargs):
        return {**voice_metadata(), **bad}
    monkeypatch.setattr(assistant, "next_tool_call", classify)
    with pytest.raises(ValueError):
        asyncio.run(assistant.resolve_voice_request(assistant.ChatRequest(message="hola", input_mode="voice"), "test"))


def test_cancelling_during_voice_intent_resolution_never_dispatches(app, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    state = {"started": False, "cancelled": False}
    async def classify(*args):
        state["started"] = True
        try:
            await anyio.sleep_forever()
        finally:
            state["cancelled"] = True
    async def disconnected():
        return state["started"]
    monkeypatch.setattr(assistant, "resolve_voice_request", classify)
    monkeypatch.setattr(assistant, "respond", lambda *args: pytest.fail("Cancelled voice must not query dashboard"))
    req = request(app)
    req.is_disconnected = disconnected
    with pytest.raises(HTTPException) as error:
        asyncio.run(assistant.assistant_chat(req, assistant.ChatRequest(message="investiga promos", input_mode="voice")))
    assert error.value.status_code == 499
    assert state == {"started": True, "cancelled": True} and not assistant._active


def test_typed_natural_next_uses_semantic_routing(app, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    async def classify(client, key, instructions, inputs, tool, **kwargs):
        assert tool["name"] == "resolve_dashboard_request"
        return voice_metadata("next", "en")
    async def respond(req, body, key):
        assert assistant.request_mode(body)[0] == "next"
        return {"answer": "OK", "ui_actions": []}
    monkeypatch.setattr(assistant, "next_tool_call", classify)
    monkeypatch.setattr(assistant, "resolve_reply_language", lambda *args: pytest.fail("Automatic turns resolve intent and language together"))
    monkeypatch.setattr(assistant, "respond", respond)
    with TestClient(app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"}, json={"message": "next"})
    assert response.status_code == 200 and response.json()["mode"] == "next"


@pytest.mark.parametrize("mode,previous,clarification,message", [
    ("filter", "filtro", "¿Qué posts querés filtrar y con qué criterios?", "Elon Musk con más de 5000 likes"),
    ("research", "research", "What would you like to search for in the dashboard?", "latest promos"),
])
def test_subject_answer_keeps_unanswered_clarification_available_to_router(app, monkeypatch, mode, previous, clarification, message):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    history = [{"role": "user", "content": previous}, {"role": "assistant", "content": clarification}]
    async def classify(client, key, instructions, inputs, tool, **kwargs):
        context = json.loads(inputs[0]["content"])
        assert context["recent_conversation"] == [{"role": item["role"], "text": item["content"]} for item in history]
        assert context["current_message"] == message
        assert "UNANSWERED clarification" in instructions and "not to revive completed commands" in instructions
        return voice_metadata(mode)
    async def respond(req, body, key):
        assert body.mode == mode and body.message == message
        return {"answer": "OK", "ui_actions": []}
    monkeypatch.setattr(assistant, "next_tool_call", classify)
    monkeypatch.setattr(assistant, "respond", respond)
    with TestClient(app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"},
            json={"message": message, "history": history, "input_mode": "voice"})
    assert response.status_code == 200 and response.json()["mode"] == mode
