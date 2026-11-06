"""Assistant product access is an authenticated, route-bound read capability."""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.testclient import TestClient

from app import assistant_access as access, hooks_lab, main


def entry(path, method="get"):
    return {"path": path, "method": method, "write": method != "get"}


def caller(app, *, email="allowed@example.com", credential="firebase"):
    return SimpleNamespace(app=app, state=SimpleNamespace(user_email=email, credential_kind=credential))


def internal(app, path, *, route=None, token="allowed@example.com", method="GET", headers=None):
    dispatch = access.assistant_dispatch(caller(app), entry(route or path), path)
    async def read():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dispatch), base_url="https://cortex.internal") as client:
            return await client.request(method, path, headers={**({"Authorization": "Bearer " + token} if token else {}), **(headers or {})})
    return asyncio.run(read())


@pytest.fixture
def product(monkeypatch):
    monkeypatch.setattr(main, "FIREBASE_APP", object())
    monkeypatch.setattr(main, "log_usage_event", lambda *args: None)
    def verify(token):
        if token == "invalid": raise ValueError("token rejected")
        return {"email": token, "uid": "real-member"}
    monkeypatch.setattr(main.firebase_auth, "verify_id_token", verify)
    role = {"name": "pd"}
    def user(email):
        return {"is_admin": False, "operating_role": role["name"], "operating_roles": '["' + role["name"] + '"]', "time_zone": "America/Costa_Rica"} if email in {"allowed@example.com", "other@example.com"} else None
    monkeypatch.setattr(main, "get_dashboard_user_access", user)
    app = FastAPI()
    app.middleware("http")(main._require_firebase_user)
    reached = []
    @app.get("/api/admin/users")
    def team(request: Request):
        reached.append("team")
        return {"all_product_users": True, "caller": request.state.user_email, "dev": request.state.is_dev, "admin": request.state.is_admin}
    @app.post("/api/admin/users")
    def mutation():
        reached.append("mutation")
        return {"mutated": True}
    @app.get("/api/dashboard/news")
    def news(request: Request):
        if not request.state.can_access_news: raise HTTPException(403, "News restricted")
        return {"stories": ["Stored story"]}
    @app.get("/api/dashboard/me")
    def me(request: Request):
        return {"caller": request.state.user_email, "role": request.state.operating_role,
                "admin": request.state.is_admin, "dev": request.state.is_dev}
    return app, role, reached


@pytest.mark.parametrize("role", ["pd", "sales", "trainee"])
def test_all_signed_in_roles_get_bot_reads_without_browser_privilege(product, role):
    app, current, reached = product; current["name"] = role
    browser = TestClient(app)
    assert browser.get("/api/admin/users", headers={"Authorization": "Bearer allowed@example.com"}).status_code == 403
    result = internal(app, "/api/admin/users")
    assert result.status_code == 200
    assert result.json() == {"all_product_users": True, "caller": "allowed@example.com", "dev": True, "admin": True}
    assert internal(app, "/api/dashboard/news").status_code == 200
    # The internal grant does not mutate the caller's saved or next-request role.
    own = browser.get("/api/dashboard/me", headers={"Authorization": "Bearer allowed@example.com"}).json()
    assert own == {"caller": "allowed@example.com", "role": role, "admin": False, "dev": False}
    assert reached == ["team"]


def test_identity_reads_remain_the_true_caller_and_timezone_is_not_written(product, monkeypatch):
    app, _, _ = product
    monkeypatch.setattr(main, "set_dashboard_user_time_zone", lambda *args: pytest.fail("Assistant read must not write timezone"))
    result = internal(app, "/api/dashboard/me", headers={"x-queue-role-preview": "admin", "x-sentient-time-zone": "America/Bogota"})
    assert result.json() == {"caller": "allowed@example.com", "role": "pd", "admin": False, "dev": False}


@pytest.mark.parametrize("token,status", [(None, 401), ("invalid", 401), ("not-allowlisted@example.com", 403), ("other@example.com", 403)])
def test_every_internal_read_revalidates_the_original_firebase_identity(product, token, status):
    app, _, reached = product
    assert internal(app, "/api/admin/users", token=token).status_code == status
    assert reached == []


def test_missing_firebase_and_revoked_allowlist_never_gain_access(product, monkeypatch):
    app, _, reached = product
    monkeypatch.setattr(main, "FIREBASE_APP", None)
    assert internal(app, "/api/admin/users").status_code == 503
    monkeypatch.setattr(main, "FIREBASE_APP", object())
    monkeypatch.setattr(main, "get_dashboard_user_access", lambda email: None)
    assert internal(app, "/api/admin/users").status_code == 403
    assert reached == []


@pytest.mark.parametrize("credential", ["agent_code", "oauth", "website", ""])
def test_non_browser_callers_cannot_construct_the_capability(product, credential):
    app, _, _ = product
    with pytest.raises(HTTPException):
        access.assistant_dispatch(caller(app, credential=credential), entry("/api/admin/users"), "/api/admin/users")


def test_scope_marker_cannot_be_forged_by_headers_or_a_different_object(product):
    app, _, reached = product
    headers = {"Authorization": "Bearer allowed@example.com", "x-sentient-assistant-read": "true", "x-assistant-admin": "true"}
    assert TestClient(app).get("/api/admin/users", headers=headers).status_code == 403
    async def forged(scope, receive, send):
        scope["sentient_assistant_read"] = {"marker": object(), "path": scope["path"], "template": "/api/admin/users", "method": "GET", "email": "allowed@example.com"}
        await app(scope, receive, send)
    assert TestClient(forged).get("/api/admin/users", headers=headers).status_code == 403
    assert reached == []


def test_read_capability_rejects_changed_path_or_method(product):
    app, _, reached = product
    assert internal(app, "/api/admin/users", method="POST").status_code == 403
    dispatch = access.assistant_dispatch(caller(app), entry("/api/admin/users"), "/api/admin/users")
    assert TestClient(dispatch).get("/api/dashboard/news", headers={"Authorization": "Bearer allowed@example.com"}).status_code == 403
    assert reached == []


@pytest.mark.parametrize("path", ["/api/auth/token", "/api/dashboard/me/agent-connections", "/api/dashboard/me/api-keys", "/api/dashboard/me/oauth/grants", "/api/dashboard/posts/media", "/api/admin/accounts/preview", "/api/dashboard/jev/search", "/api/admin/apify/runs", "/api/admin/apify/run-log-summary/123", "/api/admin/sample-enriched", "/api/admin/accounts/{handle}/media-kit.pdf", "/api/unreviewed/read"])
def test_credential_file_paid_debug_and_new_routes_are_never_capabilities(product, path):
    app, _, _ = product
    assert not access.read_operation_allowed(entry(path))
    with pytest.raises(ValueError): access.assistant_dispatch(caller(app), entry(path), path)


def test_writes_never_appear_as_assistant_reads(product):
    app, _, _ = product
    for method in ("post", "patch", "put", "delete", "head"):
        assert not access.read_operation_allowed(entry("/api/admin/users", method))
        with pytest.raises(ValueError): access.assistant_dispatch(caller(app), entry("/api/admin/users", method), "/api/admin/users")


def test_hooks_companion_reads_do_not_sync_categorize_or_call_semantic_ai(monkeypatch):
    monkeypatch.setattr(hooks_lab, "is_assistant_read", lambda request: True)
    for name in ("sync_sources", "sync_remote_sources", "_safe_categorize", "_jev_rerank"):
        monkeypatch.setattr(hooks_lab, name, lambda *args, **kwargs: pytest.fail("Stored Hook read caused a side effect"))
    called = []
    monkeypatch.setattr(hooks_lab, "search_hooks", lambda q, mode, owner, limit, **kwargs: (called.append((mode, kwargs)) or [], None))
    monkeypatch.setattr(hooks_lab, "_status", lambda owner, **kwargs: {"pending": 2})
    tasks = BackgroundTasks()
    result = hooks_lab.list_hooks(caller(None), Response(), tasks, q="AI", mode="context", limit=10)
    assert result["mode"] == "words" and result["sync"] == {"stored_only": True}
    assert called == [("words", {"stored_only": True})] and tasks.tasks == []


def test_stored_hook_search_does_not_initialize_schema_or_call_ai(monkeypatch):
    class Conn:
        def execute(self, sql, args=()):
            assert sql.lstrip().startswith("SELECT")
            return SimpleNamespace(fetchall=lambda: [])
    @contextmanager
    def connect(): yield Conn()
    monkeypatch.setattr(hooks_lab, "connect", connect)
    monkeypatch.setattr(hooks_lab, "ensure_schema", lambda *args: pytest.fail("Read initialized schema"))
    monkeypatch.setattr(hooks_lab, "_jev_rerank", lambda *args: pytest.fail("Read used paid AI"))
    assert hooks_lab.search_hooks("AI", "hybrid", "allowed@example.com", stored_only=True) == ([], None)
    assert hooks_lab.search_hooks("", "context", "allowed@example.com", stored_only=True) == ([], None)


def test_queue_companion_read_skips_retention_purge(monkeypatch):
    class StopAfterGuard(Exception): pass
    @contextmanager
    def connect(): yield object()
    monkeypatch.setattr(main, "connect", connect)
    monkeypatch.setattr(main, "_queue_v2_access", lambda request: ("allowed@example.com", True, ["vc", "pd"]))
    monkeypatch.setattr(access, "is_assistant_read", lambda request: True)
    monkeypatch.setattr(main, "_queue_v2_purge_expired", lambda *args: pytest.fail("Read deleted Queue records"))
    monkeypatch.setattr(main, "_queue_v2_publish", lambda *args: pytest.fail("Read published Queue event"))
    def stop(*args, **kwargs): raise StopAfterGuard()
    monkeypatch.setattr(main, "_queue_v2_hot_source_rows", stop)
    with pytest.raises(StopAfterGuard): main.dashboard_queue_v2(caller(None), date="2026-11-05")


def test_stack_companion_read_never_classifies_or_bootstraps(monkeypatch):
    from app import topic_stacks
    class Conn:
        def execute(self, sql, args=()):
            assert sql.lstrip().startswith("SELECT")
            return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])
    @contextmanager
    def connect(): yield Conn()
    monkeypatch.setattr(main, "connect", connect)
    monkeypatch.setattr(access, "is_assistant_read", lambda request: True)
    monkeypatch.setattr(topic_stacks, "stack_keys", lambda *args: pytest.fail("Stored read bootstrapped membership"))
    monkeypatch.setattr(topic_stacks, "attach", lambda *args: pytest.fail("Stored read classified a post"))
    monkeypatch.setattr(main, "_queue_v2_post_snapshot_cache", lambda *args: {})
    result = main.dashboard_stack_members("test", "missing", caller(None))
    assert result == {"posts": [], "stackId": None, "postKeys": [], "stackSize": 0}


def test_assistant_promos_benchmark_computation_does_not_persist(monkeypatch):
    from app import promos_engagement as engagement
    @contextmanager
    def connect(): yield object()
    monkeypatch.setattr(engagement.db, "connect", connect)
    monkeypatch.setattr(engagement, "_columns", lambda *args: set())
    monkeypatch.setattr(engagement, "_engagement_roster", lambda: [{"handle": "test", "label": "Test"}])
    def fail(*args, **kwargs): pytest.fail("Assistant benchmark read wrote cache")
    monkeypatch.setattr(engagement.benchmark_cache, "ensure_schema", fail)
    monkeypatch.setattr(engagement.benchmark_cache, "save", fail)
    monkeypatch.setattr(engagement, "calculate_promos_engagement", lambda **kwargs: {"accounts": [{"handle": "test", **{field: None for field in engagement.benchmark_cache.FIELDS}}]})
    result = engagement.build_promos_engagement(persist_cache=False)
    assert result["accounts"][0]["handle"] == "test"
    assert result["cache"]["recalculated_count"] == 1


def test_assistant_can_read_all_stored_hook_drafts_without_initializing(monkeypatch):
    captured = []
    class Conn:
        def execute(self, sql, args=()):
            captured.append((sql, args))
            return SimpleNamespace(fetchall=lambda: [{"id": "draft", "owner_email": "other@example.com", "source_hook_ids": "[]", "generation_context": "{}"}])
    @contextmanager
    def connect(): yield Conn()
    monkeypatch.setattr(hooks_lab, "connect", connect)
    monkeypatch.setattr(hooks_lab, "is_assistant_read", lambda request: True)
    monkeypatch.setattr(hooks_lab, "ensure_schema", lambda *args: pytest.fail("Assistant draft read initialized schema"))
    result = hooks_lab.list_drafts(caller(None), Response())
    assert result["drafts"][0]["owner_email"] == "other@example.com"
    assert result["complete"] is True
    assert "WHERE owner_email" not in captured[0][0] and captured[0][1] == ()


def test_actual_promos_route_disables_cache_writes_only_for_assistant(product, monkeypatch):
    app, _, _ = product
    captured = []
    def build(**params):
        captured.append(params)
        return {"accounts": []}
    monkeypatch.setattr(main, "build_promos_engagement", build)
    app.add_api_route("/api/admin/promos/engagement", main.admin_promos_engagement, methods=["GET"])
    assert internal(app, "/api/admin/promos/engagement").status_code == 200
    assert captured == [{"days": 0, "format": "all", "accounts": None, "persist_cache": False}]
