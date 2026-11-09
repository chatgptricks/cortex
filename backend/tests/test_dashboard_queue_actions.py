"""Queue mutations go through both user intent and the real API boundary."""
import json
from typing import Annotated
from uuid import uuid4

import httpx
import pytest
from fastapi import Form, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import dashboard_assistant as assistant
from app import assistant_queue_actions as actions
from test_dashboard_assistant import app  # noqa: F401
from test_assistant_queue_receipts import database  # noqa: F401


def function(name, arguments):
    return {"output": [{"type": "function_call", "call_id": "call", "name": name,
                        "arguments": json.dumps(arguments)}]}


def metadata(queue_action):
    return {"language": "en", "result_shape": "conversation", "requested_count": None,
            "page_filters_sufficient": False, "mode": "auto", "continue_next": False,
            "needs_query": False, "queue_action": queue_action}


@pytest.fixture
def queue_app(app):
    app.writes = []

    @app.post("/api/dashboard/queue/v2/tickets/time-block")
    def lunch(request: Request, category: Annotated[str, Form()], title: Annotated[str, Form()],
              scheduled_date: Annotated[str, Form()], scheduled_start_minutes: Annotated[int, Form()],
              duration_minutes: Annotated[int, Form()]):
        assert request.state.operating_role == "pd"  # No assistant read elevation.
        assert not getattr(request.state, "assistant_read_only", False)
        assert "sentient_assistant_read" not in request.scope
        app.writes.append({"title": title, "category": category, "date": scheduled_date,
                           "start": scheduled_start_minutes, "duration": duration_minutes})
        if getattr(app, "unknown_outcome", False):
            return JSONResponse({"error": "Uncertain response"}, status_code=503)
        return JSONResponse({"ok": True, "ticket": {"id": 9, "status": "pending"}}, status_code=201)

    return app


def install_provider(monkeypatch, app, *, intent=True, permitted=True, fail_after_write=False, unknown_write=False):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    operation = next(name for name, entry in actions.queue_operations(app).items()
                     if entry["path"].endswith("/tickets/time-block"))
    parameters = {"confirm": True, "body": {"title": "Lunch", "category": "break", "scheduled_date": "2026-11-09",
                  "scheduled_start_minutes": 720, "duration_minutes": 60}}
    original, observed = httpx.AsyncClient, {"answer_calls": 0, "authorization_calls": 0}
    app.unknown_outcome = unknown_write

    def provider(req):
        payload = json.loads(req.content)
        tool_name = payload.get("tool_choice", {}).get("name") if isinstance(payload.get("tool_choice"), dict) else None
        if tool_name == "resolve_dashboard_request":
            assert "queue_action" in payload["tools"][0]["parameters"]["required"]
            return httpx.Response(200, json=function(tool_name, metadata(intent)))
        if tool_name == "authorize_queue_change":
            observed["authorization_calls"] += 1
            context = json.loads(payload["input"][0]["content"])
            assert context["actor_email"] == "member@example.com"
            assert context["proposed_operation"]["parameters"] == parameters
            return httpx.Response(200, json=function(tool_name, {"authorized": permitted, "notify_requested": False}))
        observed["answer_calls"] += 1
        if observed["answer_calls"] == 1:
            if not intent:
                assert all(tool["name"] != "execute_queue_action" for tool in payload["tools"])
            return httpx.Response(200, json=function("execute_queue_action", {"operation": operation, "parameters_json": json.dumps(parameters)}))
        output = json.loads(payload["input"][-1]["output"])
        if unknown_write:
            assert output["applied"] is False and output["needs_state_check"] is True
            return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "All changes saved."}]}]})
        if intent and permitted:
            assert output["applied"] is True and output["status"] == 201
            assert output["data"]["ticket"]["status"] == "pending"
            if fail_after_write:
                return httpx.Response(503, json={"error": "private provider response"})
            text = "Lunch approval requested."
        else:
            assert "error" in output
            text = "No changes were made."
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]})

    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return observed


def test_queue_action_executes_once_and_replays_lost_response_without_another_write(queue_app, database, monkeypatch):
    observed = install_provider(monkeypatch, queue_app)
    body = {"message": "Request my lunch from noon to one on November 9", "request_id": str(uuid4())}
    with TestClient(queue_app) as client:
        first = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"}, json=body)
        second = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"}, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["ui_actions"] == [{"type": "queue_updated"}]
    assert len(queue_app.writes) == 1 and observed["authorization_calls"] == 1


@pytest.mark.parametrize("intent,permitted", [(False, True), (True, False)])
def test_unoffered_or_unrequested_action_never_reaches_queue(queue_app, database, monkeypatch, intent, permitted):
    observed = install_provider(monkeypatch, queue_app, intent=intent, permitted=permitted)
    with TestClient(queue_app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"},
                               json={"message": "Tell me about personal lunch blocks", "request_id": str(uuid4())})
    assert response.status_code == 200 and response.json()["ui_actions"] == []
    assert queue_app.writes == []
    assert observed["authorization_calls"] == (1 if intent else 0)


def test_old_client_cannot_write_without_a_stable_turn_id(queue_app, database, monkeypatch):
    observed = install_provider(monkeypatch, queue_app)
    with TestClient(queue_app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"},
                               json={"message": "Reserve my lunch"})
    assert response.status_code == 200 and "Reload" in response.json()["answer"]
    assert queue_app.writes == [] and observed["answer_calls"] == 0


def test_saved_change_survives_provider_failure_and_retries_do_not_duplicate_it(queue_app, database, monkeypatch):
    install_provider(monkeypatch, queue_app, fail_after_write=True)
    body = {"message": "Request my lunch", "request_id": str(uuid4())}
    with TestClient(queue_app) as client:
        first = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"}, json=body)
        second = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"}, json=body)
    assert first.status_code == second.status_code == 200
    assert "Some Queue changes were saved" in first.json()["answer"]
    assert first.json()["ui_actions"] == [{"type": "queue_updated"}]
    assert first.json() == second.json() and len(queue_app.writes) == 1


def test_private_write_capability_cannot_be_injected_in_client_json(queue_app, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    with TestClient(queue_app) as client:
        response = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"},
                               json={"message": "Hello", "_queue_actions_allowed": True})
    assert response.status_code == 422 and queue_app.writes == []


def test_unknown_write_cannot_be_reported_as_success_or_retried(queue_app, database, monkeypatch):
    install_provider(monkeypatch, queue_app, unknown_write=True)
    body = {"message": "Request my lunch", "request_id": str(uuid4())}
    with TestClient(queue_app) as client:
        first = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"}, json=body)
        second = client.post("/api/dashboard/assistant/chat", headers={"Authorization": "Bearer signed-in-member"}, json=body)
    assert first.status_code == second.status_code == 200
    assert "could not confirm" in first.json()["answer"]
    assert first.json() == second.json() and len(queue_app.writes) == 1
    assert first.json()["ui_actions"] == [{"type": "queue_updated"}]
