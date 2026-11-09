import asyncio
import json
from types import SimpleNamespace
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import pytest

from app import assistant_queue_actions as actions
from app.product_mcp import resolve


@pytest.fixture
def app():
    value = FastAPI()
    value.calls = []

    @value.middleware("http")
    async def authenticate(request, next_handler):
        token = request.headers.get("authorization")
        if token in {"Bearer pd", "Bearer admin"}:
            request.state.user_email = "person@example.com"
            request.state.credential_kind = "firebase"
            request.state.is_admin = token == "Bearer admin"
        return await next_handler(request)

    @value.post("/api/dashboard/queue/v2/tickets/time-block")
    def time_block(request: Request, category: Annotated[str, Form()], scheduled_date: Annotated[str, Form()],
                   scheduled_start_minutes: Annotated[int, Form()], duration_minutes: Annotated[int, Form()],
                   reschedule_conflicts: Annotated[bool, Form()] = False):
        assert not request.scope.get("sentient_assistant_read")
        assert not getattr(request.state, "assistant_read_only", False)
        assert request.headers.get("x-injected-role") is None
        value.calls.append({"kind": "time", "category": category, "date": scheduled_date,
                            "start": scheduled_start_minutes, "duration": duration_minutes,
                            "reflow": reschedule_conflicts, "preview": request.headers.get("x-queue-role-preview")})
        return JSONResponse({"ok": True, "ticket": {"id": 10, "status": "approved"}}, status_code=201)

    @value.post("/api/dashboard/queue/v2/requests/{request_id}/edit")
    def edit(request_id: int, request: Request, production_points: Annotated[int, Form()]):
        if not getattr(request.state, "is_admin", False):
            raise HTTPException(403, "Coordinator required")
        value.calls.append({"kind": "edit", "id": request_id, "pp": production_points})
        return {"ok": True, "request": {"id": request_id}}

    @value.post("/api/dashboard/queue/v2/requests/{request_id}/start")
    def start(request_id: int):
        value.calls.append({"kind": "start", "id": request_id})
        if request_id == 500:
            return JSONResponse({"private": "server stack"}, status_code=503)
        if request_id == 501:
            raise RuntimeError("private provider and database error")
        return {"ok": True}

    @value.post("/api/dashboard/queue/v2/tickets/{ticket_id}/review")
    def review(ticket_id: int, action: Annotated[str, Form()], _notify_slack: bool = True):
        value.calls.append({"kind": "review", "notify": _notify_slack})
        return {"ok": True}

    @value.post("/api/dashboard/queue/v2/requests/{request_id}/notify")
    def notify(request_id: int):
        value.calls.append({"kind": "notify", "id": request_id})
        return {"ok": True}

    class Create(BaseModel):
        title: str
        priority: str = "normal"

    @value.post("/api/dashboard/queue/v2/create")
    def create(body: Create):
        value.calls.append({"kind": "create", "body": body.model_dump()})
        return {"ok": True, "request": {"id": 20}}

    @value.post("/api/dashboard/queue/v2/presence")
    def presence():
        raise AssertionError("Presence is not a Queue request action")

    @value.post("/api/admin/queue/reset")
    def reset():
        raise AssertionError("Admin reset must never be available")

    return value


def request(app, token="pd"):
    return SimpleNamespace(app=app,
        headers={"authorization": "Bearer " + token, "x-queue-role-preview": "pd", "x-injected-role": "admin"},
        state=SimpleNamespace(user_email="person@example.com", credential_kind="firebase", is_admin=True))


def entry(app, suffix):
    return next(value for value in actions.queue_operations(app).values() if value["path"].endswith(suffix))


def execute(req, operation, params=None, state=None, **kwargs):
    return asyncio.run(actions.execute_queue_operation(req, operation, params or {}, authorized=True,
                                                     state={} if state is None else state, **kwargs))


def test_discovery_is_reviewed_queue_only(app):
    entries = actions.queue_operations(app)
    paths = {value["path"] for value in entries.values()}
    assert "/api/dashboard/queue/v2/presence" not in paths
    assert "/api/admin/queue/reset" not in paths
    result = actions.search_queue_operations(entries, "almuerzo personal")
    assert result["operations"][0]["path"].endswith("/tickets/time-block")
    assert result["operations"][0]["parameters"]["properties"]["body"]["required"]


def test_catalogue_preserves_real_property_names_while_removing_schema_annotations():
    schema = {"type": "object", "title": "Annotation", "properties": {
        "title": {"type": "string", "title": "Field annotation"},
        "discriminator": {"type": "string"},
        "nested": {"type": "object", "properties": {"title": {"type": "string"}}},
    }, "required": ["title", "discriminator"]}
    result = resolve(schema, {})
    assert "title" not in result
    assert set(result["properties"]) == {"title", "discriminator", "nested"}
    assert result["properties"]["title"] == {"type": "string"}
    assert "title" in result["properties"]["nested"]["properties"]


def test_form_encoding_real_authorization_and_same_turn_replay(app):
    req, state = request(app), {}
    op = entry(app, "/tickets/time-block")
    params = {"body": {"category": "break", "scheduled_date": "2026-11-09", "scheduled_start_minutes": 720,
                       "duration_minutes": 60, "reschedule_conflicts": True}}
    result = execute(req, op, params, state)
    assert result["status"] == 201 and result["applied"] and result["queue_changed"]
    assert app.calls == [{"kind": "time", "category": "break", "date": "2026-11-09", "start": 720,
                         "duration": 60, "reflow": True, "preview": "pd"}]
    repeated = execute(req, op, json.loads(json.dumps(params)), state)
    assert repeated["replayed"] and len(app.calls) == 1


def test_real_roles_cannot_be_replaced_by_outer_assistant_state(app):
    result = execute(request(app), entry(app, "/requests/{request_id}/edit"),
                     {"path": {"request_id": 12}, "body": {"production_points": 3}})
    assert result["status"] == 403 and not result["applied"]
    assert "Coordinator" in result["error"] and app.calls == []


def test_authorization_gate_prevents_any_http_dispatch(app):
    result = asyncio.run(actions.execute_queue_operation(request(app), entry(app, "/requests/{request_id}/start"),
        {"path": {"request_id": 12}}, authorized=False, state={}))
    assert result["status"] == 403 and app.calls == []


@pytest.mark.parametrize("params", [
    {"path": {"request_id": 2}, "headers": {"Authorization": "Bearer admin"}},
    {"path": {"request_id": "2/../../admin"}},
    {"path": {"request_id": 2, "unknown": 4}},
    {"path": {"request_id": True}},
    {"path": {"request_id": 2}, "body": {"unknown": "x"}},
    {"path": {"request_id": 2}, "confirm": False},
    {"path": {"request_id": 2}, "query": {"url": "https://other.example"}},
])
def test_unknown_or_wrongly_typed_parameters_fail_before_dispatch(app, params):
    with pytest.raises(ValueError):
        execute(request(app), entry(app, "/requests/{request_id}/start"), params)
    assert app.calls == []


def test_json_body_unknown_fields_and_defaults(app):
    req, state, op = request(app), {}, entry(app, "/create")
    with pytest.raises(ValueError):
        execute(req, op, {"body": {"title": "A", "arbitrary": "injected"}})
    assert execute(req, op, {"body": {"title": "A"}}, state)["applied"]
    assert execute(req, op, {"body": {"title": "A", "priority": "normal"}}, state)["replayed"]
    assert len(app.calls) == 1


def test_ticket_review_does_not_send_optional_slack_without_notify_intent(app):
    req, op = request(app), entry(app, "/tickets/{ticket_id}/review")
    params = {"path": {"ticket_id": 5}, "body": {"action": "approve"}, "query": {"_notify_slack": True}}
    assert execute(req, op, params)["applied"]
    assert app.calls[-1]["notify"] is False
    assert execute(req, op, params, allow_notifications=True)["applied"]
    assert app.calls[-1]["notify"] is True


def test_notify_only_action_requires_explicit_notification_authorization(app):
    req, op = request(app), entry(app, "/requests/{request_id}/notify")
    params = {"path": {"request_id": 12}}
    with pytest.raises(ValueError, match="explicit user notification request"):
        execute(req, op, params)
    assert app.calls == []
    assert execute(req, op, params, allow_notifications=True)["applied"]
    assert app.calls == [{"kind": "notify", "id": 12}]


@pytest.mark.parametrize("request_id", [500, 501])
def test_ambiguous_failures_are_not_retried_and_block_other_writes(app, request_id):
    req, state, op = request(app), {}, entry(app, "/requests/{request_id}/start")
    params = {"path": {"request_id": request_id}}
    result = execute(req, op, params, state)
    assert result["outcome"] == "unknown" and result["needs_state_check"]
    assert "private" not in json.dumps(result)
    assert execute(req, op, params, state)["replayed"]
    blocked = execute(req, op, {"path": {"request_id": 2}}, state)
    assert blocked["needs_state_check"] and len(app.calls) == 1
    assert not actions.record_queue_read(state, {"method": "get", "path": "/api/dashboard/queue/v2/summary"}, {"status": 200, "data": {}})
    detail = {"method": "get", "path": "/api/dashboard/queue/v2/requests/{request_id}"}
    assert not actions.record_queue_read(state, detail, {"status": 200, "data": {}}, parameters={"path": {"request_id": 2}})
    assert not actions.record_queue_read(state, detail, {"status": 200, "data": {}, "complete": False}, parameters={"path": {"request_id": request_id}})
    assert actions.record_queue_read(state, detail, {"status": 200, "data": {}}, parameters={"path": {"request_id": request_id}})
    assert execute(req, op, {"path": {"request_id": 2}}, state)["applied"]
    assert len(app.calls) == 2
    assert execute(req, op, params, state)["replayed"]  # Never replay the ambiguous original write.


def test_non_browser_credentials_and_fabricated_operations_fail_closed(app):
    req = request(app)
    req.state.credential_kind = "agent_code"
    with pytest.raises(HTTPException):
        execute(req, entry(app, "/create"), {"body": {"title": "A"}})
    req.state.credential_kind = "firebase"
    op = {**entry(app, "/create"), "path": "https://outside.example/write"}
    with pytest.raises(ValueError):
        execute(req, op, {"body": {"title": "A"}})
    assert app.calls == []


def test_transport_failure_is_sanitized_and_attempted_only_once(app, monkeypatch):
    calls = []
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def request(self, *args, **kwargs):
            calls.append(1)
            raise actions.httpx.ReadTimeout("private transport details")
    monkeypatch.setattr(actions.httpx, "AsyncClient", Client)
    req, state, op = request(app), {}, entry(app, "/requests/{request_id}/start")
    result = execute(req, op, {"path": {"request_id": 2}}, state)
    assert result["outcome"] == "unknown" and "private" not in json.dumps(result)
    assert execute(req, op, {"path": {"request_id": 2}}, state)["replayed"]
    assert calls == [1]
