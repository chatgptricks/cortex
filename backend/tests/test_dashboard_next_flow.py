"""Behavioral coverage for dashboard-first /next clarification and recommendation."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import dashboard_assistant as assistant
from app.dashboard_next_semantic import retrieve_prior_posts as real_retrieve_prior_posts
from test_dashboard_assistant import app, request  # noqa: F401 — shared auth fixture


EMPTY_CONTEXT = {"post_format": None, "target_account": None}


@pytest.fixture(autouse=True)
def prepared_history_evidence(monkeypatch):
    # Retrieval has its own tests; these flow fixtures already supply the
    # candidate/history evidence whose semantic verdict is under test.
    async def retrieved(_client, _api_key, bundle, _tool_call):
        return bundle["candidates"]
    monkeypatch.setattr(assistant.dashboard_next_semantic, "retrieve_prior_posts", retrieved)


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

    async def bundle(req, slots, entries, read_operation, *, excluded_keys=None, state=None):
        calls.append((req, slots, entries, read_operation))
        result = {**value}
        if "candidates" in value:
            result["candidates"] = [row for row in value["candidates"] if row["key"] not in (excluded_keys or ())]
            if value.get("status") == "ready" and excluded_keys and not result["candidates"]:
                result.update(status="empty", reason="no_unpublished_candidates")
        return result

    monkeypatch.setattr(assistant.dashboard_next, "candidate_bundle", bundle)
    return calls


def run(app, **body):
    return asyncio.run(assistant.respond(request(app), assistant.ChatRequest(**body), "test-key"))


def direct_tool_call(responses, *, max_output_tokens=1200):
    requests = []
    def provider(req):
        requests.append(json.loads(req.content))
        status, payload = responses[min(len(requests) - 1, len(responses) - 1)]
        return httpx.Response(status, json=payload)
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            return await assistant.next_tool_call(client, "test-key", "Prepare only.",
                [{"role": "user", "content": "reel for chatgptricks"}], assistant.NEXT_PREPARE_TOOL,
                max_output_tokens=max_output_tokens)
    return invoke, requests


@pytest.mark.parametrize("initial_budget, retry_budget", [(1200, 2400), (2400, 4800), (4000, 8000), (5000, 8000)])
def test_budget_incomplete_tool_call_retries_once_with_more_room(initial_budget, retry_budget):
    arguments = {"post_format": "reel", "target_account": "chatgptricks"}
    partial = function_call("prepare_next_recommendation", {})
    partial["arguments"] = '{"post_format":'
    invoke, requests = direct_tool_call([
        (200, {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": [partial]}),
        (200, {"status": "completed", "output": [function_call("prepare_next_recommendation", arguments)]}),
    ], max_output_tokens=initial_budget)
    assert asyncio.run(invoke()) == arguments
    assert [row["max_output_tokens"] for row in requests] == [initial_budget, retry_budget]
    assert {key: value for key, value in requests[0].items() if key != "max_output_tokens"} == {
        key: value for key, value in requests[1].items() if key != "max_output_tokens"}


def test_repeated_budget_exhaustion_stays_fail_closed_after_two_calls():
    invoke, requests = direct_tool_call([(200, {
        "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "reasoning", "summary": []}],
    })])
    with pytest.raises(ValueError) as failure:
        asyncio.run(invoke())
    assert failure.value.code == "provider_incomplete"
    assert [row["max_output_tokens"] for row in requests] == [1200, 2400]


@pytest.mark.parametrize("payload", [
    {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}, "output": []},
    {"status": "incomplete", "output": []},
    {"status": "failed", "output": []},
    {"status": "completed", "output": [function_call("unknown_tool", {})]},
    {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
     "output": [function_call("unknown_tool", {})]},
    {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
     "output": [function_call("prepare_next_recommendation", {}), function_call("prepare_next_recommendation", {})]},
])
def test_non_budget_or_invalid_tool_failures_are_not_retried(payload):
    invoke, requests = direct_tool_call([(200, payload)])
    with pytest.raises(ValueError) as failure:
        asyncio.run(invoke())
    assert failure.value.code in {"provider_incomplete", "provider_invalid_tool"}
    assert len(requests) == 1


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_provider_failures_are_not_retried(status):
    invoke, requests = direct_tool_call([(status, {"error": {"message": "provider failure"}})])
    with pytest.raises(HTTPException if status == 429 else httpx.HTTPStatusError):
        asyncio.run(invoke())
    assert len(requests) == 1


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
        "status": "ready", "history_complete": True, "history_posts_checked": 0,
        "target": {"handle": "chatgptricks", "label": "ChatGPT Tricks", "group": "sentient", "subcategory": "ai_automation"},
        "post_format": "reel",
        "window_hours": 24,
        "candidates": [{
            "key": "evolving.ai:VALID_123", "account": "evolving.ai", "shortcode": "VALID_123", "likes": 12450,
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


def history_bundle():
    bundle = ready_bundle()
    bundle["history_posts_checked"] = 12000
    candidate = bundle["candidates"][0]
    candidate["prior_posts"] = [{"key": "chatgptricks:OldStory", "caption": "Same demonstration previously published", "published_at": "2023-01-01"}]
    bundle["candidates"].append({**candidate, "key": "evolving.ai:NEW_456", "shortcode": "NEW_456", "caption": "A materially new announcement",
                                 "dashboard_url": assistant.dashboard_next.dashboard_post_url("evolving.ai", "NEW_456")})
    return bundle


def test_semantic_history_review_excludes_rewritten_story_before_selection(app, monkeypatch):
    candidate_stub(monkeypatch, history_bundle())
    def answer(payload, turn):
        if turn == 1:
            return [function_call("prepare_next_recommendation", {"post_format": "reel", "target_account": "chatgptricks"})]
        if turn == 2:
            assert payload["tools"][0]["name"] == "check_next_publication_history"
            assert payload["max_output_tokens"] == 4000
            assert "OldStory" in json.dumps(payload)
            return [function_call("check_next_publication_history", {"checks": [
                {"candidate_key": "evolving.ai:VALID_123", "status": "already_posted", "matched_post_key": "chatgptricks:OldStory"},
                {"candidate_key": "evolving.ai:NEW_456", "status": "new", "matched_post_key": None} ]})]
        assert "VALID_123" not in json.dumps(payload["input"])
        return [function_call("choose_next_post", {"account": "evolving.ai", "shortcode": "NEW_456", "reason": "A fresh relevant announcement."})]
    calls = provider_mock(monkeypatch, answer)
    result = run(app, message="/next reel for chatgptricks", language="en")
    assert "evolving.ai%3ANEW_456" in result["answer"] and "VALID_123" not in result["answer"]
    assert len(calls) == 3


@pytest.mark.parametrize("checks, expected_code", [([], "check_count"),
    ([{"candidate_key": "evolving.ai:VALID_123", "status": "new", "matched_post_key": None}] * 2, "candidate_identity"),
    ([{"candidate_key": "invented", "status": "new", "matched_post_key": None}] * 2, "candidate_identity"),
    ([{"candidate_key": "evolving.ai:VALID_123", "status": "already_posted", "matched_post_key": "invented"},
      {"candidate_key": "evolving.ai:NEW_456", "status": "new", "matched_post_key": None}], "prior_match"),
    ([{"candidate_key": "evolving.ai:VALID_123", "status": "new"},
      {"candidate_key": "evolving.ai:NEW_456", "status": "new", "matched_post_key": None}], "check_shape"),
    ([{"candidate_key": "evolving.ai:VALID_123", "status": "new", "matched_post_key": "chatgptricks:OldStory"},
      {"candidate_key": "evolving.ai:NEW_456", "status": "new", "matched_post_key": None}], "prior_match"),
])
def test_incomplete_or_forged_reuse_checks_cannot_allow_recommendation(app, monkeypatch, checks, expected_code):
    candidate_stub(monkeypatch, history_bundle())
    def answer(payload, turn):
        if turn == 1:
            return [function_call("prepare_next_recommendation", {"post_format": "reel", "target_account": "chatgptricks"})]
        assert turn in (2, 3)
        if turn == 3:
            assert expected_code in payload["instructions"]
        return [function_call("check_next_publication_history", {"checks": checks})]
    calls = provider_mock(monkeypatch, answer)
    result = run(app, message="/next reel for chatgptricks")
    assert "?post=" not in result["answer"]
    assert result["error_code"] == "next_history_review_" + expected_code
    assert len(calls) == 3


def test_missing_review_check_is_repaired_once_without_erasing_a_prior_veto(app, monkeypatch):
    candidate_stub(monkeypatch, history_bundle())
    review_inputs = []
    def answer(payload, turn):
        if turn == 1:
            return [function_call("prepare_next_recommendation", {"post_format": "reel", "target_account": "chatgptricks"})]
        if turn in (2, 3):
            assert payload["tools"][0]["name"] == "check_next_publication_history"
            review_inputs.append(payload["input"])
            if turn == 2:
                checks = [{"candidate_key": "evolving.ai:VALID_123", "status": "already_posted", "matched_post_key": "chatgptricks:OldStory"}]
            else:
                assert "check_count" in payload["instructions"] and "REQUIRED_CANDIDATE_KEYS=" in payload["instructions"]
                checks = [{"candidate_key": key, "status": "new", "matched_post_key": None}
                          for key in ("evolving.ai:VALID_123", "evolving.ai:NEW_456")]
            return [function_call("check_next_publication_history", {"checks": checks})]
        assert turn == 4 and payload["tools"][0]["name"] == "choose_next_post"
        assert "VALID_123" not in json.dumps(payload["input"]), "a valid earlier duplicate verdict must remain a veto"
        return [function_call("choose_next_post", {"account": "evolving.ai", "shortcode": "NEW_456", "reason": "A fresh relevant story."})]
    calls = provider_mock(monkeypatch, answer)
    result = run(app, message="/next reel for chatgptricks")
    assert "evolving.ai%3ANEW_456" in result["answer"] and not result.get("error_code")
    assert len(calls) == 4 and review_inputs[0] == review_inputs[1]


@pytest.mark.parametrize("missing, expected_code", [("history", "history_missing"), ("prior", "prior_missing")])
def test_missing_review_evidence_never_retries_the_model(monkeypatch, missing, expected_code):
    bundle = history_bundle()
    if missing == "history":
        bundle["history_complete"] = False
    else:
        bundle["candidates"][0]["prior_posts"] = []
    async def no_provider(*args, **kwargs):
        raise AssertionError("Missing evidence cannot be repaired by a provider")
    monkeypatch.setattr(assistant, "next_tool_call", no_provider)
    with pytest.raises(ValueError) as failure:
        asyncio.run(assistant.unpublished_next_candidates(None, "test-key", bundle))
    assert failure.value.code == expected_code


def test_reuse_review_can_veto_using_verified_history_retrieved_for_another_candidate(monkeypatch):
    bundle = history_bundle()
    bundle["candidates"][1]["prior_posts"] = [{"key": "chatgptricks:SharedStory", "caption": "Earlier publication of the first candidate's story"}]
    calls = []
    async def review(client, api_key, instructions, inputs, tool, **kwargs):
        calls.append(tool)
        properties = tool["parameters"]["properties"]["checks"]["items"]["properties"]
        assert properties["candidate_key"]["enum"] == ["evolving.ai:VALID_123", "evolving.ai:NEW_456"]
        assert set(properties["matched_post_key"]["enum"]) == {None, "chatgptricks:OldStory", "chatgptricks:SharedStory"}
        assert "another candidate is also valid evidence" in instructions
        return {"checks": [
            {"candidate_key": "evolving.ai:VALID_123", "status": "already_posted", "matched_post_key": "chatgptricks:SharedStory"},
            {"candidate_key": "evolving.ai:NEW_456", "status": "new", "matched_post_key": None},
        ]}
    monkeypatch.setattr(assistant, "next_tool_call", review)
    result = asyncio.run(assistant.unpublished_next_candidates(None, "test-key", bundle))
    assert [row["key"] for row in result] == ["evolving.ai:NEW_456"]
    assert len(calls) == 1, "a valid duplicate match must not trigger a repair or fail the complete request"
    original_properties = assistant.NEXT_REUSE_TOOL["parameters"]["properties"]["checks"]["items"]["properties"]
    assert "enum" not in original_properties["candidate_key"], "per-request IDs must not leak into another user's tool schema"
    assert "enum" not in original_properties["matched_post_key"]


def test_uncertain_or_already_posted_candidates_never_reach_selector(app, monkeypatch):
    candidate_stub(monkeypatch, history_bundle())
    def answer(payload, turn):
        if turn == 1:
            return [function_call("prepare_next_recommendation", {"post_format": "reel", "target_account": "chatgptricks"})]
        assert turn == 2
        return [function_call("check_next_publication_history", {"checks": [
            {"candidate_key": "evolving.ai:VALID_123", "status": "already_posted", "matched_post_key": "chatgptricks:OldStory"},
            {"candidate_key": "evolving.ai:NEW_456", "status": "uncertain", "matched_post_key": None}]})]
    provider_mock(monkeypatch, answer)
    result = run(app, message="/next reel for chatgptricks", language="es")
    assert "sin repetir" in result["answer"] and "?post=" not in result["answer"]


def test_unchecked_history_is_not_a_usable_bundle(app, monkeypatch):
    bundle = ready_bundle(); bundle.pop("history_complete")
    candidate_stub(monkeypatch, bundle)
    calls = provider_mock(monkeypatch, lambda *_: [function_call("prepare_next_recommendation", {"post_format": "reel", "target_account": "chatgptricks"})])
    assert "?post=" not in run(app, message="/next reel for chatgptricks")["answer"]
    assert len(calls) == 1


def test_semantic_veto_continues_to_thirteenth_candidate(app, monkeypatch):
    template = history_bundle()
    base = template["candidates"][0]
    rows = [{**base, "key": f"evolving.ai:P{i:02}", "shortcode": f"P{i:02}",
             "dashboard_url": assistant.dashboard_next.dashboard_post_url("evolving.ai", f"P{i:02}")} for i in range(13)]
    batch_calls, states = [], []
    async def next_batch(req, slots, entries, read_operation, *, excluded_keys=None, state=None):
        batch_calls.append(set(excluded_keys or ()))
        states.append(state)
        return {**template, "_history": object(), "candidates": [row for row in rows if row["key"] not in excluded_keys][:12]}
    monkeypatch.setattr(assistant.dashboard_next, "candidate_bundle", next_batch)
    def answer(payload, turn):
        tool = payload["tools"][0]["name"]
        if tool == "prepare_next_recommendation":
            return [function_call(tool, {"post_format": "reel", "target_account": "chatgptricks"})]
        if tool == "check_next_publication_history":
            supplied = json.loads(payload["input"][0]["content"])["candidates"]
            return [function_call(tool, {"checks": [{"candidate_key": row["key"],
                "status": "new" if row["shortcode"] == "P12" else "already_posted",
                "matched_post_key": None if row["shortcode"] == "P12" else "chatgptricks:OldStory"} for row in supplied]})]
        assert tool == "choose_next_post" and turn == 4
        assert "_history" not in json.dumps(payload["input"])
        assert "evolving.ai:P00" not in json.dumps(payload["input"])
        return [function_call(tool, {"account": "evolving.ai", "shortcode": "P12", "reason": "A new relevant story."})]
    calls = provider_mock(monkeypatch, answer)
    result = run(app, message="/next reel for chatgptricks")
    assert "evolving.ai%3AP12" in result["answer"]
    assert len(calls) == 4 and len(batch_calls) == 2
    assert not batch_calls[0] and len(batch_calls[1]) == 12
    assert states[0] is states[1]


def test_semantic_review_budget_never_falls_back_to_unreviewed_candidate(app, monkeypatch):
    template = history_bundle()
    base = template["candidates"][0]
    batches = []
    async def next_batch(req, slots, entries, read_operation, *, excluded_keys=None, state=None):
        index = len(batches)
        batches.append(set(excluded_keys or ()))
        return {**template, "candidates": [{**base, "key": f"evolving.ai:B{index}", "shortcode": f"B{index}"}]}
    monkeypatch.setattr(assistant.dashboard_next, "candidate_bundle", next_batch)
    def answer(payload, _):
        tool = payload["tools"][0]["name"]
        if tool == "prepare_next_recommendation":
            return [function_call(tool, {"post_format": "reel", "target_account": "chatgptricks"})]
        assert tool == "check_next_publication_history", "unverified candidates must never reach selection"
        rows = json.loads(payload["input"][0]["content"])["candidates"]
        return [function_call(tool, {"checks": [{"candidate_key": row["key"], "status": "uncertain", "matched_post_key": None} for row in rows]})]
    calls = provider_mock(monkeypatch, answer)
    result = run(app, message="/next reel for chatgptricks", language="es")
    assert "sin repetir" in result["answer"] and "?post=" not in result["answer"]
    assert len(batches) == assistant.MAX_NEXT_REUSE_BATCHES == 4
    assert len(calls) == assistant.MAX_NEXT_REUSE_BATCHES + 1
    assert [len(keys) for keys in batches] == [0, 1, 2, 3]


def test_full_next_flow_retrieves_then_reviews_then_selects(app, monkeypatch):
    monkeypatch.setattr(assistant.dashboard_next_semantic, "retrieve_prior_posts", real_retrieve_prior_posts)
    bundle = history_bundle()
    searches = []
    def retrieve(rows):
        searches.extend(row["reuse_search_text"] for row in rows)
        return rows
    bundle["_history"] = SimpleNamespace(filter_candidates=retrieve)
    candidate_stub(monkeypatch, bundle)
    def answer(payload, turn):
        tool = payload["tools"][0]["name"]
        if turn == 1:
            assert tool == "prepare_next_recommendation"
            return [function_call(tool, {"post_format": "reel", "target_account": "chatgptricks"})]
        if turn == 2:
            assert tool == "prepare_publication_history_search" and payload["max_output_tokens"] == 2400
            return [function_call(tool, {"queries": [{"candidate_key": row["key"], "search_text": "Specific demonstration. Demostración específica."} for row in bundle["candidates"]]})]
        if turn == 3:
            assert tool == "check_next_publication_history"
            assert "reuse_search_text" not in json.dumps(payload["input"])
            return [function_call(tool, {"checks": [
                {"candidate_key": "evolving.ai:VALID_123", "status": "already_posted", "matched_post_key": "chatgptricks:OldStory"},
                {"candidate_key": "evolving.ai:NEW_456", "status": "new", "matched_post_key": None}]})]
        assert turn == 4 and tool == "choose_next_post"
        assert "_history" not in json.dumps(payload["input"])
        assert "VALID_123" not in json.dumps(payload["input"])
        return [function_call(tool, {"account": "evolving.ai", "shortcode": "NEW_456", "reason": "A new relevant announcement."})]
    calls = provider_mock(monkeypatch, answer)
    result = run(app, message="/next reel for chatgptricks")
    assert "evolving.ai%3ANEW_456" in result["answer"]
    assert len(searches) == 2 and len(calls) == 4
