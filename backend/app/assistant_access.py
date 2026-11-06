"""An in-process, read-only product capability for the dashboard companion.

It belongs to a single authenticated invocation, never to a user's persisted
roles or an HTTP header. New API routes fail closed until their GET behavior is
reviewed here; mutation, credential, file and operational-log routes are absent.
"""
from functools import lru_cache
import re

from fastapi import HTTPException

_MARKER = object()
_SCOPE_KEY = "sentient_assistant_read"
_PRODUCT_READS = frozenset({
    "/api/dashboard/me", "/api/dashboard/me/preferences",
    "/api/dashboard/accounts", "/api/dashboard/posts/manifest",
    "/api/dashboard/posts/page", "/api/dashboard/posts", "/api/dashboard/posts/search",
    "/api/dashboard/posts/{account}/{shortcode}/detail", "/api/dashboard/posts/metrics",
    "/api/dashboard/stacks/{account}/{shortcode}", "/api/dashboard/lists",
    "/api/dashboard/golden-nuggets", "/api/dashboard/news", "/api/dashboard/vault",
    "/api/dashboard/hooks", "/api/dashboard/hooks/drafts",
    "/api/dashboard/queue", "/api/dashboard/queue/summary", "/api/dashboard/queue/users",
    "/api/dashboard/queue/tasks/{task_id}/history", "/api/dashboard/queue/v2",
    "/api/dashboard/queue/v2/requests/{request_id}", "/api/dashboard/queue/v2/summary",
    "/api/dashboard/queue/v2/admin-report", "/api/dashboard/queue/v2/tickets",
    "/api/dashboard/queue/v2/requests/{request_id}/history", "/api/dashboard/queue/v2/presence",
    "/api/insights/posts", "/api/insights/music", "/api/insights/music/page",
    "/api/insights/follower-growth", "/api/insights/top-posts",
    "/api/tracker/summary", "/api/tracker/accounts/{handle}",
    "/api/tracker/refresh-allowance",
    "/api/admin/accounts", "/api/admin/users", "/api/admin/queue/designer-accounts",
    "/api/admin/usage", "/api/admin/engagement/checkpoints",
    "/api/admin/promos", "/api/admin/promos/engagement", "/api/admin/promos/engagement/accounts",
    "/api/admin/promos/{account}/{shortcode}",
})
_IDENTITY_READS = frozenset({"/api/dashboard/me", "/api/dashboard/me/preferences"})


def read_operation_allowed(entry: dict) -> bool:
    return (entry.get("method", "").lower() == "get" and entry.get("write") is False
            and entry.get("path") in _PRODUCT_READS)


@lru_cache(maxsize=128)
def _path_pattern(template: str):
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", r"[^/]+", re.escape(template)) + "$")


def assistant_dispatch(request, entry: dict, concrete_path: str):
    """ASGI wrapper for one catalogue read; keep the caller's Authorization.

    The normal Firebase middleware reauthenticates that bearer and allowlist on
    every internal read. The marker is useful only after that succeeds with the
    same original email. No token or role header is invented by this wrapper.
    """
    email = str(getattr(request.state, "user_email", "") or "").strip().lower()
    if not email or getattr(request.state, "credential_kind", "") != "firebase":
        raise HTTPException(403, "Use your signed-in Sentient Dash session.")
    if not read_operation_allowed(entry) or not _path_pattern(entry["path"]).fullmatch(concrete_path):
        raise ValueError("This operation is not a stored dashboard read.")

    async def dispatch(scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "GET" or scope.get("path") != concrete_path:
            from starlette.responses import JSONResponse
            await JSONResponse({"detail": "Read capability does not match this route."}, status_code=403)(scope, receive, send)
            return
        internal = dict(scope)
        internal[_SCOPE_KEY] = {"marker": _MARKER, "path": concrete_path,
                               "template": entry["path"], "email": email, "method": "GET"}
        await request.app(internal, receive, send)

    return dispatch


def is_assistant_read(request) -> bool:
    context = request.scope.get(_SCOPE_KEY)
    return bool(isinstance(context, dict) and context.get("marker") is _MARKER
                and context.get("method") == request.method == "GET"
                and context.get("path") == request.scope.get("path")
                and context.get("template") in _PRODUCT_READS
                and _path_pattern(context["template"]).fullmatch(context["path"])
                and getattr(request.state, "credential_kind", "") == "firebase"
                and context.get("email") == getattr(request.state, "user_email", None))


def apply_assistant_read_access(request) -> bool:
    """Called after real authentication/role derivation, before route gating."""
    if not is_assistant_read(request):
        return False
    request.state.assistant_read_only = True
    if request.scope["path"] in _IDENTITY_READS:
        return True
    request.state.is_admin = True
    request.state.is_dev = True
    request.state.can_access_hooks = True
    request.state.can_access_news = True
    request.state.can_access_promos = True
    request.state.queue_role_preview_active = False
    request.state.operating_role = "dev"
    request.state.operating_roles = ["dev", "vc", "pd"]
    # Identity and all stored roles/preferences remain untouched.
    return True
