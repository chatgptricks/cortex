"""Behavioral coverage for dashboard-first /next clarification and recommendation."""
import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import dashboard_assistant as assistant
from test_dashboard_assistant import app, request  # noqa: F401 — shared auth fixture


EMPTY_CONTEXT = {"post_format": None, "target_account": None}


def function_call(name, arguments, call_id="call"):
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(arguments)}


def provider_mock(monkeypatch, answer):
    """Intercept provider calls without affecting internal authenticated reads."""
    original = httpx.AsyncClient
    calls = []

    def provider(req):
        assert str(req.url) == "https://api.openai.com/v1/responses"
        payload = json.loads(req.content)
        calls.append(payload)
        return httpx.Response(200, json={"output": answer(payload, len(calls))})

    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return calls


def candidate_stub(monkeypatch, value):
    calls = []

    async def bundle(req, slots, entries, read_operation):
        calls.append((req, slots, entries, read_operation))
        return value

    monkeypatch.setattr(assistant.dashboard_next, "candidate_bundle", bundle)
    return calls


def run(app, **body):
    return asyncio.run(assistant.respond(request(app), assistant.ChatRequest(**body), "test-key"))


@pytest.mark.parametrize("message, mode, expected", [
    (" /NeXt ", "auto", ("next", "")),
    ("/next reel for chatgptricks", "research", ("next", "reel for chatgptricks")),
    ("/research latest post", "next", ("research", "latest post")),
    ("/filter carousels", "next", ("filter", "carousels")),
    ("/nextweek", "auto", ("auto", "/nextweek")),
])
def test_next_prefix_has_exact_boundary_and_precedence(message, mode, expected):
    body = assistant.ChatRequest(message=message, mode=mode)
    assert assistant.request_mode(body) == expected


@pytest.mark.parametrize("language, expected", [
    ("es", "¿Reel, imagen o carrusel, y para qué cuenta?"),
    ("en", "Reel, image or carousel, and for which account?"),
])
def test_bare_next_asks_before_provider_or_data(app, monkeypatch, language, expected):
    async def no_data(*args, **kwargs):
        raise AssertionError("A bare /next must ask before reading post data")

    monkeypatch.setattr(assistant, "read_operation", no_data)
    calls = provider_mock(monkeypatch, lambda *_: pytest.fail("Bare /next needs no model call"))
    result = run(app, message="/next", language=language)
    assert result["answer"] == expected
    assert result["pending_next"] == EMPTY_CONTEXT
    assert result["sources"] == [] and result["ui_actions"] == [] and not calls


@pytest.mark.parametrize("context", [
    {"post_format": "video", "target_account": "chatgptricks"},
    {"post_format": "reel", "target_account": "x" * 65},
    {"post_format": None, "target_account": None, "trusted": True},
])
def test_invalid_next_context_is_rejected(app, context):
    with TestClient(app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"}, json={"message": "reel", "mode": "next", "next_context": context})
    assert response.status_code == 422


@pytest.mark.parametrize("slots, question_terms", [
    ({"post_format": "reel", "target_account": None}, ("cuenta",)),
    ({"post_format": None, "target_account": "chatgptricks"}, ("reel", "imagen", "carrusel")),
])
def test_partial_next_clarifies_before_candidate_search(app, monkeypatch, slots, question_terms):
    candidate_calls = candidate_stub(monkeypatch, {"status": "ready"})

    def answer(payload, turn):
        assert turn == 1
        assert {tool["name"] for tool in payload["tools"]} == {"prepare_next_recommendation"}
        assert payload["tool_choice"] == {"type": "function", "name": "prepare_next_recommendation"}
        return [function_call("prepare_next_recommendation", slots)]

    calls = provider_mock(monkeypatch, answer)
    result = run(app, message="/next reel" if slots["post_format"] else "/next para chatgptricks", language="es")
    assert result["pending_next"] == slots
    for term in question_terms:
        assert term in result["answer"].lower()
    assert result["ui_actions"] == [] and result["sources"] == []
    assert len(calls) == 1 and not candidate_calls


def ready_bundle():
    return {
        "status": "ready",
        "target": {"handle": "chatgptricks", "label": "ChatGPT Tricks", "group": "sentient", "subcategory": "ai_automation"},
        "post_format": "reel",
        "window_hours": 24,
        "candidates": [{
            "account": "evolving.ai", "shortcode": "VALID_123", "likes": 12450,
            "comments": 85, "published_at": "2026-11-05T12:00:00+00:00",
            "caption": "A new AI animation demo", "ocrText": "AI animation",
            "dashboard_url": "https://sentientdash.app/index.html?post=evolving.ai%3AVALID_123",
        }],
        "sources": [{"endpoint": "/api/insights/top-posts", "label": "Dashboard posts", "url": "https://sentientdash.app/insights.html", "partial": False}],
    }


def test_next_followup_preserves_explicit_slots_and_returns_verified_internal_post(app, monkeypatch):
    candidate_calls = candidate_stub(monkeypatch, ready_bundle())

    def answer(payload, turn):
        if turn == 1:
            assert {tool["name"] for tool in payload["tools"]} == {"prepare_next_recommendation"}
            assert "chatgptricks" in json.dumps(payload)
            return [function_call("prepare_next_recommendation", {"post_format": "reel", "target_account": "chatgptricks"})]
        assert {tool["name"] for tool in payload["tools"]} == {"choose_next_post"}
        assert "VALID_123" in json.dumps(payload)
        return [function_call("choose_next_post", {"account": "evolving.ai", "shortcode": "VALID_123", "reason": "Fits the account’s AI focus and has strong engagement."}, "selection")]

    calls = provider_mock(monkeypatch, answer)
    result = run(app, message="reel", mode="next", language="en", next_context={"post_format": None, "target_account": "chatgptricks"})
    assert len(candidate_calls) == 1 and len(calls) == 2
    assert candidate_calls[0][1] == {"post_format": "reel", "target_account": "chatgptricks"}
    assert candidate_calls[0][0].headers["authorization"] == "Bearer signed-in-member"
    assert "https://sentientdash.app/index.html?post=evolving.ai%3AVALID_123" in result["answer"]
    assert "instagram.com" not in result["answer"]
    assert "12450" in result["answer"].replace(",", "").replace(".", "")
    assert len(result["answer"].split()) <= 80
    assert result["ui_actions"] == [] and not result.get("pending_next")
    assert result["sources"] == ready_bundle()["sources"]


def test_invalid_target_clarifies_instead_of_recommending_a_competitor_as_destination(app, monkeypatch):
    candidate_calls = candidate_stub(monkeypatch, {"status": "invalid_target", "available_targets": ["chatgptricks", "tecnologia"]})
    provider_mock(monkeypatch, lambda *_: [function_call("prepare_next_recommendation", {"post_format": "carousel", "target_account": "evolving.ai"})])
    result = run(app, message="/next carousel for evolving.ai", language="es")
    assert len(candidate_calls) == 1
    assert result["pending_next"] == {"post_format": "carousel", "target_account": None}
    assert "cuenta" in result["answer"].lower()
    assert result["ui_actions"] == [] and "?post=" not in result["answer"]


@pytest.mark.parametrize("status", ["empty", "unavailable"])
def test_next_without_verified_candidates_cannot_invent_a_recommendation(app, monkeypatch, status):
    candidate_calls = candidate_stub(monkeypatch, {"status": status, "target": {"handle": "chatgptricks"}, "sources": []})
    calls = provider_mock(monkeypatch, lambda *_: [function_call("prepare_next_recommendation", {"post_format": "image", "target_account": "chatgptricks"})])
    result = run(app, message="/next image for chatgptricks", language="en")
    assert len(candidate_calls) == 1 and len(calls) == 1
    assert result["ui_actions"] == [] and "?post=" not in result["answer"]
    assert not result.get("pending_next")


def test_unknown_selected_post_fails_closed(app, monkeypatch):
    candidate_stub(monkeypatch, ready_bundle())

    def answer(payload, turn):
        if turn == 1:
            return [function_call("prepare_next_recommendation", {"post_format": "reel", "target_account": "chatgptricks"})]
        return [function_call("choose_next_post", {"account": "invented", "shortcode": "MADE_UP", "reason": "This is the winner."}, "fake")]

    provider_mock(monkeypatch, answer)
    try:
        result = run(app, message="/next reel for chatgptricks", language="en")
    except HTTPException as failure:
        assert failure.status_code in {502, 503}
    else:
        assert "MADE_UP" not in result["answer"] and "?post=" not in result["answer"]
        assert result["ui_actions"] == []


def test_unoffered_next_action_never_changes_filters_or_searches_before_clarifying(app, monkeypatch):
    candidate_calls = candidate_stub(monkeypatch, ready_bundle())

    def answer(payload, turn):
        if turn == 1:
            return [function_call("show_research_posts", {"query": "AI"}, "bad")]
        return [function_call("prepare_next_recommendation", EMPTY_CONTEXT, "clarify")]

    provider_mock(monkeypatch, answer)
    try:
        result = run(app, message="/next something", language="es")
    except HTTPException as failure:
        assert failure.status_code in {502, 503}
    else:
        assert result["ui_actions"] == []
        assert result.get("pending_next") in (None, EMPTY_CONTEXT)
        assert "?post=" not in result["answer"]
    assert not candidate_calls


def test_explicit_research_cancels_pending_next_context(app, monkeypatch):
    candidate_calls = candidate_stub(monkeypatch, ready_bundle())

    def answer(payload, _):
        assert "prepare_next_recommendation" not in {tool["name"] for tool in payload["tools"]}
        assert "choose_next_post" not in {tool["name"] for tool in payload["tools"]}
        assert "show_research_posts" not in {tool["name"] for tool in payload["tools"]}
        return [{"type": "message", "content": [{"type": "output_text", "text": "Your dashboard has no matching post."}]}]

    provider_mock(monkeypatch, answer)
    result = run(app, message="/research latest post", mode="next", next_context={"post_format": "reel", "target_account": "chatgptricks"})
    assert not result.get("pending_next") and not candidate_calls


def test_fresh_next_does_not_take_slots_from_old_history(app, monkeypatch):
    candidate_calls = candidate_stub(monkeypatch, ready_bundle())

    def answer(payload, _):
        # A fresh command cannot inherit previous destination or format merely
        # because an unrelated turn used them earlier in the conversation.
        serialized = json.dumps(payload["input"])
        assert "OLD_ACCOUNT" not in serialized
        assert "OLD_ACCOUNT" not in payload["instructions"]
        return [function_call("prepare_next_recommendation", {"post_format": "image", "target_account": None})]

    provider_mock(monkeypatch, answer)
    result = run(app, message="/next image", history=[{"role": "user", "content": "/next reel for OLD_ACCOUNT"}, {"role": "assistant", "content": "Previous recommendation"}], next_context={"post_format": "reel", "target_account": "OLD_ACCOUNT"})
    assert result["pending_next"] == {"post_format": "image", "target_account": None}
    assert not candidate_calls


def test_all_factual_queries_receive_dashboard_first_grounding_instructions(app, monkeypatch):
    def answer(payload, _):
        instructions = payload["instructions"].lower()
        assert "dashboard" in instructions and "first" in instructions
        assert "external" in instructions and "knowledge" in instructions
        assert "general" in instructions or "every" in instructions or "all" in instructions
        assert {tool["name"] for tool in payload["tools"]} >= {"search_cortex_endpoints", "query_cortex"}
        return [{"type": "message", "content": [{"type": "output_text", "text": "No matching information in the dashboard."}]}]

    provider_mock(monkeypatch, answer)
    result = run(app, message="What is happening with AI today?", language="en")
    assert result["ui_actions"] == []


@pytest.mark.parametrize("reason", [
    "See https://example.com/invented for another result.",
    "[Another post](https://sentientdash.app/index.html?post=madeup%3AX)",
    "word " * 41,
])
def test_selector_cannot_add_links_or_long_explanation_through_reason(app, monkeypatch, reason):
    candidate_stub(monkeypatch, ready_bundle())

    def answer(payload, turn):
        if turn == 1:
            return [function_call("prepare_next_recommendation", {"post_format": "reel", "target_account": "chatgptricks"})]
        return [function_call("choose_next_post", {"account": "evolving.ai", "shortcode": "VALID_123", "reason": reason})]

    provider_mock(monkeypatch, answer)
    result = run(app, message="/next reel for chatgptricks", language="en")
    assert result["answer"].count("https://") == 1
    assert "evolving.ai%3AVALID_123" in result["answer"]
    assert "madeup" not in result["answer"] and "example.com" not in result["answer"]
    assert len(result["answer"].split()) <= 20


def test_provider_free_text_cannot_skip_required_preparation(app, monkeypatch):
    candidate_calls = candidate_stub(monkeypatch, ready_bundle())
    provider_mock(monkeypatch, lambda *_: [{"type": "message", "content": [{"type": "output_text", "text": "Post this invented recommendation."}]}])
    result = run(app, message="/next something", language="en")
    assert "invented" not in result["answer"] and not candidate_calls
    assert result["ui_actions"] == [] and "?post=" not in result["answer"]


@pytest.mark.parametrize("language, expected", [
    ("es", "No pude consultar los datos del dashboard en este momento."),
    ("en", "Dashboard data is temporarily unavailable."),
])
@pytest.mark.parametrize("failure_kind", ["denied", "timeout", "unqueryable"])
def test_failed_data_reads_override_verbose_provider_errors_even_after_discovery(app, monkeypatch, language, expected, failure_kind):
    operation = next(name for name, entry in assistant.operations(app).items() if entry["path"] == "/api/dashboard/me")
    reads = []

    async def failed_read(req, entry, parameters, analysis=None):
        reads.append(entry["path"])
        if failure_kind == "timeout":
            raise TimeoutError("private connection diagnostics")
        if failure_kind == "unqueryable":
            # HTTP 200 does not establish a successful data read when the
            # endpoint returned a non-JSON page instead of usable data.
            return {"status": 200, "error": "This endpoint does not return queryable JSON."}, {}
        return {"status": 403, "error": "Technical role and permission explanation"}, {}

    monkeypatch.setattr(assistant, "read_operation", failed_read)

    def answer(payload, turn):
        if turn == 1:
            return [function_call("search_cortex_endpoints", {"query": "dashboard me"}, "discover")]
        if turn in (2, 3):
            return [function_call("query_cortex", {"operation": operation, "parameters_json": "{}"}, "read" + str(turn))]
        return [{"type": "message", "content": [{"type": "output_text", "text": "I could not query /api/dashboard/me because the internal role requires administrator access. Here are the technical steps and what you need to change..."}]}]

    calls = provider_mock(monkeypatch, answer)
    result = run(app, message="What posts should I consider?", language=language)
    assert len(calls) == 4 and len(reads) == 2
    assert result["answer"] == expected
    assert result["sources"] == [] and result["ui_actions"] == []
    assert not result.get("pending_next")


@pytest.mark.parametrize("success_first", [True, False])
def test_one_successful_data_read_preserves_grounded_response_after_another_read_fails(app, monkeypatch, success_first):
    operation = next(name for name, entry in assistant.operations(app).items() if entry["path"] == "/api/dashboard/me")
    reads = []
    source = {"endpoint": "/api/dashboard/me", "label": "Your dashboard", "url": "https://sentientdash.app/", "partial": False}

    async def mixed_read(req, entry, parameters, analysis=None):
        reads.append(entry["path"])
        success = len(reads) == (1 if success_first else 2)
        return ({"status": 200, "data": {"role": "pd"}, "complete": True}, source) if success else ({"status": 503, "error": "Unavailable"}, {})

    monkeypatch.setattr(assistant, "read_operation", mixed_read)

    def answer(payload, turn):
        if turn <= 2:
            return [function_call("query_cortex", {"operation": operation, "parameters_json": "{}"}, "read" + str(turn))]
        return [{"type": "message", "content": [{"type": "output_text", "text": "Your role is PD."}]}]

    provider_mock(monkeypatch, answer)
    result = run(app, message="What is my dashboard role?", language="en")
    assert len(reads) == 2
    assert result["answer"] == "Your role is PD."
    assert result["sources"] == [source] and result["ui_actions"] == []
