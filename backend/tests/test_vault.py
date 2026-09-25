import sqlite3
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import vault

@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / 'vault.sqlite'
    @contextmanager
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    monkeypatch.setattr(vault, 'connect', connect)
    with connect() as conn:
        vault.ensure_schema(conn)
        vault.ensure_schema(conn)
    app = FastAPI()
    @app.middleware('http')
    async def identity(request, next):
        request.state.is_dev = request.headers.get('x-test-role') == 'dev'
        request.state.queue_role_preview_active = request.headers.get('x-test-preview') == '1'
        return await next(request)
    app.include_router(vault.router)
    return TestClient(app)

@pytest.mark.parametrize('role', ['', 'admin', 'ivan', 'pd'])
def test_non_dev_denied_all_routes(client, role):
    for method, path, body in [('get', '', None), ('post', '', {'url':'https://example.com'}), ('patch', '/secret', {'discarded':True})]:
        response = client.request(method, '/api/dashboard/vault'+path, headers={'x-test-role':role}, json=body)
        assert response.status_code == 403

def test_dev_preview_denied(client):
    assert client.get('/api/dashboard/vault', headers={'x-test-role':'dev', 'x-test-preview':'1'}).status_code == 403

def test_persistent_add_duplicate_priority_discard_restore(client):
    client.headers['x-test-role'] = 'dev'
    one = client.post('/api/dashboard/vault', json={'url':'https://example.com/one', 'title':'One'}).json()
    two = client.post('/api/dashboard/vault', json={'url':'https://example.com/two', 'title':'Two'}).json()
    assert client.get('/api/dashboard/vault').json()['items'][0]['id'] == two['id']
    assert client.post('/api/dashboard/vault', json={'url':one['url']}).json()['id'] == one['id']
    assert client.patch('/api/dashboard/vault/'+one['id'], json={'priority':-100}).status_code == 200
    assert client.get('/api/dashboard/vault').json()['items'][0]['id'] == one['id']
    for discarded in [True, False]:
        assert client.patch('/api/dashboard/vault/'+one['id'], json={'discarded':discarded}).json()['discarded'] == int(discarded)
        rows = client.get('/api/dashboard/vault')
        assert rows.headers['cache-control'] == 'private, no-store'
        assert len(rows.json()['items']) == 2
        assert rows.json()['items'][0]['discarded'] == int(discarded)
    assert client.patch('/api/dashboard/vault/missing', json={'discarded':True}).status_code == 404

@pytest.mark.parametrize('url', ['javascript:alert(1)', 'data:text/html,x', 'https://user:pass@example.com', '', 'not a url'])
def test_invalid_links_rejected(client, url):
    assert client.post('/api/dashboard/vault', headers={'x-test-role':'dev'}, json={'url':url}).status_code == 422


def test_tweet_text_is_persistent_and_not_refetched(client, monkeypatch):
    calls = []
    def preview(url):
        calls.append(url)
        return {'tweet_text':'A readable tweet', 'tweet_author':'Creator', 'text_status':'ready'}
    monkeypatch.setattr(vault, 'fetch_tweet_text', preview)
    client.headers['x-test-role'] = 'dev'
    row = client.post('/api/dashboard/vault', json={'url':'https://x.com/creator/status/123'}).json()
    assert row['tweet_text'] == 'A readable tweet'
    assert client.get('/api/dashboard/vault').json()['items'][0]['tweet_text'] == 'A readable tweet'
    assert client.post('/api/dashboard/vault/'+row['id']+'/text').json()['tweet_text'] == 'A readable tweet'
    assert calls == [row['url']]
    client.headers['x-test-role'] = 'admin'
    assert client.post('/api/dashboard/vault/'+row['id']+'/text').status_code == 403
