"""Per-turn language and written/spoken conversational parity."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import dashboard_assistant as assistant
from test_dashboard_assistant import app  # noqa: F401 — shared authenticated fixture


def tool(name, value):
    return {"output": [{"type": "function_call", "call_id": "call", "name": name, "arguments": json.dumps(value)}]}


@pytest.mark.parametrize("language,ui,message,expected", [
    ("es", "en", "Quiero recrear un reel", "¿Para qué cuenta querés recrearlo?"),
    ("en", "es", "I want to recreate a reel", "Which account would you like to recreate it for?"),
])
def test_voice_next_clarification_uses_spoken_language_and_returns_it(app, monkeypatch, language, ui, message, expected):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    original = httpx.AsyncClient
    calls = []

    def provider(request):
        payload = json.loads(request.content)
        name = payload["tool_choice"]["name"]
        calls.append(name)
        if name == "resolve_reply_language":
            data = json.loads(payload["input"][0]["content"])
            assert data["current_message"] == message and data["fallback_language"] == ui
            assert "actually speaking/writing" in payload["instructions"]
            assert "Proper names, quoted captions" in payload["instructions"]
            return httpx.Response(200, json=tool(name, {"language": language}))
        assert name == "prepare_next_recommendation"
        return httpx.Response(200, json=tool(name, {"post_format": "reel", "target_account": None}))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(provider), **kwargs))
    with TestClient(app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"},
            json={"message": message, "mode": "next", "language": ui, "input_mode": "voice"})
    assert response.status_code == 200
    assert response.json()["answer"] == expected and response.json()["language"] == language
    assert response.json()["pending_next"] == {"post_format": "reel", "target_account": None}
    assert calls == ["resolve_reply_language", "prepare_next_recommendation"]


@pytest.mark.parametrize("language,ui,message,expected", [
    ("es", "en", "Filtra posts de Elon Musk", "Filtros aplicados."),
    ("en", "es", "Filter posts about Elon Musk", "Filters applied."),
])
def test_filter_confirmation_and_voice_prompt_use_same_resolved_language(app, monkeypatch, language, ui, message, expected):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    original = httpx.AsyncClient

    def provider(request):
        payload = json.loads(request.content)
        if isinstance(payload["tool_choice"], dict):
            return httpx.Response(200, json=tool("resolve_reply_language", {"language": language}))
        instructions = payload["instructions"]
        assert "using the SAME words" in instructions and "no separate narrator" in instructions
        assert "at most 80 words" in instructions and "overrides the link-only" in instructions
        assert json.loads(instructions.split("\nVIEW_CONTEXT=", 1)[1])["language"] == language
        return httpx.Response(200, json=tool("show_research_posts", {"query": "Elon Musk"}))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(provider), **kwargs))
    with TestClient(app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"},
            json={"message": message, "mode": "filter", "language": ui, "input_mode": "voice"})
    assert response.status_code == 200
    assert response.json()["answer"] == expected and response.json()["language"] == language
    assert response.json()["ui_actions"][0]["filters"]["query"] == "Elon Musk"


def test_language_neutral_followup_passes_bounded_history_and_no_dashboard_data(monkeypatch):
    observed = []

    async def classify(client, api_key, instructions, inputs, tool_spec, **kwargs):
        observed.append(json.loads(inputs[0]["content"]))
        assert tool_spec == assistant.LANGUAGE_TOOL
        assert "continue the latest established language" in instructions
        assert "explicitly request the reply in Spanish or English" in instructions
        return {"language": "es"}

    monkeypatch.setattr(assistant, "next_tool_call", classify)
    body = assistant.ChatRequest(message="chatgptricks", language="en", input_mode="voice", history=[
        {"role": "user", "content": "Quiero recrear un reel"},
        {"role": "assistant", "content": "¿Para qué cuenta querés recrearlo?"},
    ])
    assert asyncio.run(assistant.resolve_reply_language(body, "test")) == "es"
    assert observed == [{"current_message": "chatgptricks", "recent_conversation": [
        {"role": "user", "text": "Quiero recrear un reel"},
        {"role": "assistant", "text": "¿Para qué cuenta querés recrearlo?"}], "fallback_language": "en"}]


@pytest.mark.parametrize("value", [{"language": "fr"}, {"language": "es", "answer": "do something"}, {}, {"language": None}])
def test_invalid_language_result_is_rejected_before_answering(monkeypatch, value):
    async def classify(*args, **kwargs):
        return value
    monkeypatch.setattr(assistant, "next_tool_call", classify)
    with pytest.raises(ValueError):
        asyncio.run(assistant.resolve_reply_language(assistant.ChatRequest(message="hola"), "test"))


def test_typed_message_retains_compact_link_only_output_rules():
    assert assistant.response_instructions(assistant.ChatRequest(message="most liked post")) == assistant.INSTRUCTIONS
    assert "independently of the dashboard UI" in assistant.INSTRUCTIONS
