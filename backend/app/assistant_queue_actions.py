"""Reviewed Queue mutations for the browser companion, using the caller's real roles.

This dispatcher deliberately does not use the assistant's broad read capability.
Every mutation re-enters normal Firebase authentication and Queue authorization.
The caller must separately authorize each proposed action against trusted user
instructions; captions, retrieved records and tool results cannot authorize it.
"""
from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import quote

import anyio
from fastapi import HTTPException
import httpx
from jsonschema import Draft202012Validator

from .product_mcp import catalogue


_QUEUE = "/api/dashboard/queue/v2"
_WRITES = frozenset(_QUEUE + suffix for suffix in (
    "/create", "/pool", "/drafts", "/drafts/clear", "/submit", "/pick",
    "/scheduler-preferences",
    "/requests/{request_id}/start", "/requests/{request_id}/not-started",
    "/requests/{request_id}/edit", "/requests/{request_id}/duplicate",
    "/requests/{request_id}/assign-accounts", "/requests/{request_id}/complete",
    "/requests/{request_id}/close", "/requests/{request_id}/cancel",
    "/requests/{request_id}/notify",
    "/requests/batch-close",
    "/tickets/post-suggestion", "/tickets/time-block",
    "/tickets/time-block/{ticket_id}", "/tickets/pp-revision",
    "/tickets/cancellation", "/tickets/trainee-review", "/tickets/move",
    "/tickets/{ticket_id}/review",
))
_RECONCILIATION_READS = frozenset(_QUEUE + suffix for suffix in (
    "", "/tickets", "/requests/{request_id}", "/requests/{request_id}/history",
))
MAX_ACTION_BYTES = 64_000
MAX_RESULT_BYTES = 96_000


def queue_operation_allowed(entry: dict) -> bool:
    """Only explicitly reviewed current Queue state changes are writable."""
    return (entry.get("write") is True and entry.get("method", "").lower() == "post"
            and entry.get("path") in _WRITES
            and entry.get("media") in {None, "application/json", "application/x-www-form-urlencoded", "multipart/form-data"})


def queue_operations(app) -> dict:
    return {name: entry for name, entry in catalogue(app.openapi()).items()
            if queue_operation_allowed(entry)}


def search_queue_operations(entries: dict, query: str) -> dict:
    words = re.findall(r"[a-z0-9]+", str(query).lower())[:12]
    aliases = {"almuerzo": "time block", "comida": "time block", "personal": "time block",
               "mover": "edit submit move", "asignar": "assign submit", "cancelar": "cancel",
               "terminar": "complete close", "horario": "schedule submit"}
    words += [word for token in list(words) for word in aliases.get(token, "").split()]
    scored = []
    for name, entry in entries.items():
        if not queue_operation_allowed(entry):
            continue
        haystack = (name + " " + entry["tool"].description).lower()
        score = sum(3 if word in entry["path"].lower() else 1 for word in words if word in haystack)
        if words and not score:
            continue
        scored.append((score, name, entry))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return {"total_matches": len(scored), "operations": [
        {"operation": name, "method": entry["method"].upper(), "path": entry["path"],
         "description": entry["tool"].description, "parameters": entry["tool"].inputSchema,
         "uses_current_user_permissions": True}
        for _, name, entry in scored[:8]]}


def _strict_schema(schema):
    value = deepcopy(schema)
    if isinstance(value, list):
        return [_strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    value = {key: _strict_schema(item) for key, item in value.items()}
    if value.get("type") == "object" and "properties" in value:
        value.setdefault("additionalProperties", False)
    return value


def _apply_defaults(value, schema):
    if not isinstance(value, dict) or not isinstance(schema, dict):
        return
    for key, child in schema.get("properties", {}).items():
        if key not in value and "default" in child:
            value[key] = deepcopy(child["default"])
        if key in value:
            _apply_defaults(value[key], child)


def prepare_queue_write(entry: dict, parameters: dict, *, allow_notifications=False) -> tuple[str, dict]:
    if not queue_operation_allowed(entry):
        raise ValueError("This operation is not an available Queue action.")
    if entry["path"] == _QUEUE + "/requests/{request_id}/notify" and not allow_notifications:
        raise ValueError("Sending a Queue notification requires an explicit user notification request.")
    if not isinstance(parameters, dict):
        raise ValueError("Queue parameters must be an object.")
    try:
        encoded = json.dumps(parameters, allow_nan=False, ensure_ascii=False)
    except (ValueError, TypeError):
        raise ValueError("Queue parameters must contain valid JSON values.") from None
    if len(encoded.encode()) > MAX_ACTION_BYTES:
        raise ValueError("Queue action is too large; use a bounded batch or the scheduling action.")
    values = json.loads(encoded)
    # confirm is the catalogue's action marker, not a second approval prompt.
    # The independent trusted-user authorization gate runs before dispatch.
    values.setdefault("confirm", True)
    schema = _strict_schema(entry["tool"].inputSchema)
    _apply_defaults(values, schema)
    query_schema = schema.get("properties", {}).get("query", {}).get("properties", {})
    if "_notify_slack" in query_schema and not allow_notifications and isinstance(values.get("query", {}), dict):
        values.setdefault("query", {})["_notify_slack"] = False
    errors = sorted(Draft202012Validator(schema).iter_errors(values), key=lambda error: str(list(error.path)))
    if errors:
        location = ".".join(str(part) for part in errors[0].path) or "parameters"
        raise ValueError("Invalid Queue action parameters at " + location + ". Use the discovered schema exactly.")
    path = entry["path"]
    for key, value in values.get("path", {}).items():
        text = str(value)
        if text in {".", ".."} or any(char in text for char in ("/", "\\", "?", "#", "%")):
            raise ValueError("Invalid Queue path parameter.")
        path = path.replace("{" + key + "}", quote(text, safe=""))
    if "{" in path:
        raise ValueError("Required Queue path parameter is missing.")
    kwargs = {"params": {key: value for key, value in values.get("query", {}).items() if value is not None}}
    if "body" in values:
        if entry["media"] == "application/json":
            kwargs["json"] = values["body"]
        else:
            form = {key: json.dumps(value, separators=(",", ":")) if isinstance(value, (list, dict, bool)) else str(value)
                    for key, value in values["body"].items() if value is not None}
            kwargs["files" if entry["media"] == "multipart/form-data" else "data"] = (
                {key: (None, value) for key, value in form.items()}
                if entry["media"] == "multipart/form-data" else form)
    return path, kwargs


def _has_record(data, wanted, *, request_record=False):
    if isinstance(data, list):
        return any(_has_record(item, wanted, request_record=request_record) for item in data)
    if not isinstance(data, dict):
        return False
    if (type(data.get("id")) is int and data["id"] == wanted
            and (not request_record or "productionPoints" in data or "production_points" in data)):
        return True
    return any(_has_record(item, wanted, request_record=request_record) for item in data.values()
               if isinstance(item, (dict, list)))


def record_queue_read(state: dict, entry: dict, result: dict, *, parameters=None) -> bool:
    """Permit a new plan only after successful Queue state reconciliation.

    An ambiguous action's identical fingerprint is still not replayed in this
    turn. The model must inspect this fresh state before proposing a new action.
    Badge summaries and presence are insufficient evidence of a mutation.
    """
    if (entry.get("method", "").lower() == "get" and entry.get("path") in _RECONCILIATION_READS
            and isinstance(result, dict) and result.get("status") == 200
            and "data" in result and not result.get("error") and not result.get("truncated")
            and result.get("complete") is not False):
        pending = state.get("queue_write_uncertain_action", {})
        path_values = pending.get("path", {})
        read_values = parameters.get("path", {}) if isinstance(parameters, dict) else {}
        if "request_id" in path_values:
            wanted = path_values["request_id"]
            if entry["path"] == _QUEUE:
                if not _has_record(result["data"], wanted, request_record=True):
                    return False
            elif (entry["path"] not in {_QUEUE + "/requests/{request_id}", _QUEUE + "/requests/{request_id}/history"}
                  or read_values.get("request_id") != wanted):
                return False
        elif "ticket_id" in path_values:
            if entry["path"] != _QUEUE + "/tickets" or not _has_record(result["data"], path_values["ticket_id"]):
                return False
        elif pending and "/tickets/" in pending.get("template", ""):
            if entry["path"] != _QUEUE + "/tickets":
                return False
        elif pending and entry["path"] != _QUEUE:
            return False
        state["queue_write_uncertain"] = False
        state.pop("queue_write_uncertain_action", None)
        return True
    return False


def _bounded_result(data):
    if len(json.dumps(data, ensure_ascii=False, default=str).encode()) <= MAX_RESULT_BYTES:
        return data, False
    if not isinstance(data, dict):
        return {}, True
    # The mutation has already happened. Keep identities/result flags without
    # putting an unbounded Queue response into the model context.
    kept = {key: data[key] for key in ("ok", "id", "deleted", "rescheduled_count", "changed_request_ids",
                                      "deferred", "movedToNow", "detail") if key in data}
    for key in ("request", "ticket"):
        if isinstance(data.get(key), dict):
            kept[key] = {field: data[key][field] for field in ("id", "status", "scheduledDate", "scheduledStartMinutes")
                         if field in data[key]}
    if isinstance(kept.get("changed_request_ids"), list) and len(kept["changed_request_ids"]) > 1000:
        kept["changed_request_ids"] = kept["changed_request_ids"][:1000]
    return kept, True


async def execute_queue_operation(request, entry: dict, parameters: dict, *, authorized: bool,
                                  state: dict, allow_notifications: bool = False) -> dict:
    """Execute at most once, with normal roles and no automatic write retries."""
    if not authorized:
        return {"status": 403, "applied": False,
                "error": "This Queue action was not authorized by the current user's request."}
    if not getattr(request.state, "user_email", "") or getattr(request.state, "credential_kind", "") != "firebase":
        raise HTTPException(403, "Use your signed-in Sentient Dash session.")
    # Re-resolve from the application's real catalogue, never a model-provided
    # method, path or fabricated schema.
    name = getattr(entry.get("tool"), "name", None)
    trusted = queue_operations(request.app).get(name)
    if not trusted or trusted["path"] != entry.get("path") or trusted["method"] != entry.get("method"):
        raise ValueError("Unknown Queue action. Discover its current schema first.")
    path, kwargs = prepare_queue_write(trusted, parameters, allow_notifications=allow_notifications)
    fingerprint = hashlib.sha256(json.dumps([trusted["method"], path, kwargs], sort_keys=True,
                                            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    attempts = state.setdefault("queue_write_attempts", {})
    if fingerprint in attempts:
        return {**deepcopy(attempts[fingerprint]), "replayed": True}
    if state.get("queue_write_uncertain"):
        return {"status": 409, "applied": False, "needs_state_check": True,
                "error": "A previous Queue write has an unknown outcome. Read the affected Queue state before another action; do not retry it blindly."}
    uncertain = {"status": 503, "applied": False, "outcome": "unknown", "needs_state_check": True,
                 "error": "The Queue action's outcome is unknown. Read the affected Queue state before proceeding; do not repeat this write."}
    attempts[fingerprint] = uncertain
    state["queue_write_uncertain"] = True
    state["queue_write_uncertain_action"] = {"template": trusted["path"], "path": deepcopy(parameters.get("path", {}))}
    headers = {"Accept": "application/json", "Authorization": request.headers.get("authorization", "")}
    # These come solely from the browser invocation, never from tool arguments;
    # normal middleware checks any active role preview and current time zone.
    for header in ("x-queue-role-preview", "x-sentient-time-zone"):
        if request.headers.get(header):
            headers[header] = request.headers[header]
    try:
        with anyio.fail_after(40):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=request.app, raise_app_exceptions=False),
                                         base_url="https://cortex.internal", follow_redirects=False, timeout=35) as client:
                response = await client.request(trusted["method"].upper(), path, headers=headers, **kwargs)
        if response.status_code >= 500:
            result = {**uncertain, "status": response.status_code}
        else:
            try:
                data = response.json()
            except ValueError:
                data = None
            if response.is_success:
                bounded, truncated = _bounded_result(data)
                result = {"status": response.status_code, "applied": True, "data": bounded,
                          "truncated": truncated, "queue_changed": True}
            else:
                # Only normal product validation/permission details are public.
                detail = data.get("detail") if isinstance(data, dict) else None
                result = {"status": response.status_code, "applied": False,
                          "error": str(detail)[:500] if detail is not None else "The Queue action was rejected. Read its current state and check the requested parameters."}
            state["queue_write_uncertain"] = False
            state.pop("queue_write_uncertain_action", None)
    except (TimeoutError, httpx.HTTPError):
        result = uncertain
    attempts[fingerprint] = deepcopy(result)
    return result
