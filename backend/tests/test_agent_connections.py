import sqlite3
from contextlib import contextmanager

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from app import agent_connections as agent


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / 'agent.sqlite'

    @contextmanager
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(agent, 'connect', connect)
    app = FastAPI()

    @app.middleware('http')
    async def identity(request, call_next):
        request.state.user_email = request.headers.get('x-test-user', '')
        request.state.agent_connection_id = request.headers.get('x-test-agent')
        return await call_next(request)

    app.include_router(agent.router)
    return TestClient(app)


def create(client, owner='ana@example.com', **payload):
    response = client.post(agent.URL, headers={'x-test-user': owner}, json={'name': 'Dots', **payload})
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_key_is_only_returned_once_and_never_stored_plaintext(client):
    result = create(client)
    key = result['key']
    assert len(key) == 53
    listed = client.get(agent.URL, headers={'x-test-user': 'ana@example.com'}).json()
    assert key not in str(listed)
    assert 'key_hash' not in str(listed)
    with agent.connect() as conn:
        row = dict(conn.execute('SELECT * FROM agent_connections').fetchone())
    assert key not in row.values()
    assert row['key_hash'] != key
    identity = agent.authenticate(key, '/api/dashboard/me', 'GET')
    assert identity['email'] == 'ana@example.com'
    assert identity['agent_access_mode'] == 'full'


def test_ownership_and_revocation(client):
    result = create(client)
    identity = result['connection']['id']
    assert client.get(agent.URL, headers={'x-test-user': 'bob@example.com'}).json()['connections'] == []
    assert client.delete(f'{agent.URL}/{identity}', headers={'x-test-user': 'bob@example.com'}).status_code == 404
    assert client.delete(f'{agent.URL}/{identity}', headers={'x-test-user': 'ana@example.com'}).status_code == 200
    with pytest.raises(HTTPException) as error:
        agent.authenticate(result['key'], '/api/dashboard/me', 'GET')
    assert error.value.status_code == 401


def test_expired_or_malformed_keys_fail(client):
    result = create(client)
    with agent.connect() as conn:
        conn.execute('UPDATE agent_connections SET expires_at = ?', ('2000-01-01T00:00:00+00:00',))
    for key in [result['key'], 'sad_agent_short', 'wrong']:
        with pytest.raises(HTTPException) as error:
            agent.authenticate(key, '/api/dashboard/me', 'GET')
        assert error.value.status_code == 401


def test_read_access_and_credential_management_are_enforced_server_side(client):
    read = create(client, access_mode='read')['key']
    assert agent.authenticate(read, '/api/dashboard/me', 'GET')['email'] == 'ana@example.com'
    with pytest.raises(HTTPException) as error:
        agent.authenticate(read, '/api/dashboard/queue/v2/create', 'POST')
    assert error.value.status_code == 403
    full = create(client)['key']
    for path in [agent.URL, agent.URL + '/123', '/api/auth/custom-token', '/api/slack/interactions']:
        with pytest.raises(HTTPException) as error:
            agent.authenticate(full, path, 'POST')
        assert error.value.status_code == 403
    for method in ['GET', 'POST', 'DELETE']:
        url = agent.URL if method != 'DELETE' else agent.URL + '/123'
        assert client.request(method, url, headers={'x-test-user':'ana@example.com','x-test-agent':'123'}, json={'name':'Bad'} if method=='POST' else None).status_code == 403


def test_middleware_loads_current_owner_permissions_and_rejects_removed_users(client, monkeypatch):
    from app import main
    result = create(client)
    monkeypatch.setattr(main, 'FIREBASE_APP', object())
    access = {'is_admin':False,'operating_role':'pd','operating_roles':'["pd"]'}
    monkeypatch.setattr(main, 'get_dashboard_user_access', lambda email: access)
    monkeypatch.setattr(main, 'log_usage_event', lambda *args: None)
    app = FastAPI()
    app.middleware('http')(main._require_firebase_user)

    @app.get('/api/dashboard/me')
    def me(request: Request):
        return main.dashboard_me(request)

    @app.get('/api/admin/users')
    def admin():
        return {'allowed':True}

    test_client = TestClient(app)
    headers = {'Authorization': 'Bearer ' + result['key']}
    me = test_client.get('/api/dashboard/me', headers=headers)
    assert me.status_code == 200
    assert me.json()['email'] == 'ana@example.com'
    assert test_client.get('/api/admin/users', headers=headers).status_code == 403
    access['is_admin'] = True
    assert test_client.get('/api/admin/users', headers=headers).status_code == 200
    monkeypatch.setattr(main, 'get_dashboard_user_access', lambda email: None)
    assert test_client.get('/api/dashboard/me', headers=headers).status_code == 403


def test_hosted_mcp_authentication_discovery_and_actions(client, monkeypatch):
    from typing import Annotated
    from fastapi import Form
    from app import main
    from app.product_mcp import install
    full = create(client)['key']
    read = create(client, access_mode='read')['key']
    monkeypatch.setattr(main, 'FIREBASE_APP', object())
    monkeypatch.setattr(main, 'get_dashboard_user_access', lambda email: {'is_admin':False,'operating_role':'pd','operating_roles':'["pd"]'})
    monkeypatch.setattr(main, 'log_usage_event', lambda *args: None)
    app = FastAPI()
    app.middleware('http')(main._require_firebase_user)

    @app.get('/api/dashboard/me')
    def me(request: Request):
        return main.dashboard_me(request)

    @app.post('/api/dashboard/test-action')
    def action(accounts: Annotated[str, Form()]):
        return {'accounts':accounts}

    install(app)

    def rpc(host, key, method, params=None):
        return host.post('/mcp', headers={'Authorization':'Bearer '+key,'Accept':'application/json, text/event-stream'}, json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})

    with TestClient(app) as host:
        assert host.post('/mcp').status_code == 401
        assert rpc(host, 'sad_agent_'+'x'*43, 'tools/list').status_code == 401
        init = rpc(host, full, 'initialize', {'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'test-agent','version':'1.0'}})
        assert init.status_code == 200, init.text
        assert init.json()['result']['serverInfo']['name'] == 'sentient-dash'
        names = [t['name'] for t in rpc(host, full, 'tools/list').json()['result']['tools']]
        assert 'post_dashboard_test_action' in names
        assert 'post_dashboard_test_action' not in [t['name'] for t in rpc(host, read, 'tools/list').json()['result']['tools']]
        me = rpc(host, full, 'tools/call', {'name':'product_me','arguments':{}}).json()['result']
        import json
        assert json.loads(me['content'][0]['text'])['data']['email'] == 'ana@example.com'
        args = {'name':'post_dashboard_test_action','arguments':{'body':{'accounts':'[]'},'confirm':True}}
        result = rpc(host, full, 'tools/call', args).json()['result']
        assert not result.get('isError'), result
        assert json.loads(result['content'][0]['text'])['data'] == {'accounts':'[]'}
        denied = rpc(host, read, 'tools/call', args).json()
        assert denied.get('error') or denied['result'].get('isError')
        resource = rpc(host, full, 'resources/read', {'uri':'sentient://guide'}).json()
        assert 'contents' in resource['result']
        assert host.post('/mcp',headers={'Authorization':'Bearer '+full,'Origin':'https://evil.example'}).status_code == 403
        with agent.connect() as conn:
            conn.execute('UPDATE agent_connections SET revoked_at = ?', (agent.utc_now(),))
        assert rpc(host, full, 'tools/list').status_code == 401
