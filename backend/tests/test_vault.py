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
        conn.executescript("""
            CREATE TABLE queue_requests (
                id INTEGER PRIMARY KEY, post_account TEXT, post_shortcode TEXT,
                post_title TEXT, is_custom INTEGER, post_permalink TEXT,
                post_caption TEXT, post_type TEXT, cover_url TEXT,
                production_points INTEGER, priority TEXT, deadline_at TEXT,
                tags TEXT, brief TEXT, notes TEXT, reference_links TEXT,
                coordinator_email TEXT, created_at TEXT, updated_at TEXT,
                status TEXT DEFAULT 'pool', UNIQUE(post_account, post_shortcode)
            );
            CREATE TABLE queue_request_events (request_id INTEGER, actor_email TEXT, event_type TEXT, details TEXT, created_at TEXT);
            CREATE TABLE queue_live_state (id INTEGER PRIMARY KEY, revision INTEGER, event_type TEXT, actor_email TEXT, request_ids TEXT, updated_at TEXT);
            INSERT INTO queue_live_state VALUES (1, 0, '', '', '[]', '');
        """)
    app = FastAPI()
    @app.middleware('http')
    async def identity(request, next):
        request.state.user_email = 'dev@example.com'
        request.state.is_dev = request.headers.get('x-test-role') == 'dev'
        request.state.queue_role_preview_active = request.headers.get('x-test-preview') == '1'
        return await next(request)
    app.include_router(vault.router)
    return TestClient(app)

@pytest.mark.parametrize('role', ['', 'admin', 'ivan', 'pd'])
def test_non_dev_denied_all_routes(client, role):
    for method, path, body in [('get', '', None), ('post', '', {'url':'https://example.com'}), ('patch', '/secret', {'discarded':True}), ('post', '/secret/pool', None)]:
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


def test_done_is_persistent_reversible_and_distinct_from_discard(client):
    client.headers['x-test-role'] = 'dev'
    row = client.post('/api/dashboard/vault', json={'url':'https://example.com/done'}).json()
    path = '/api/dashboard/vault/'+row['id']
    assert client.patch(path, json={'done':True}).json()['done'] == 1
    assert client.get('/api/dashboard/vault').json()['items'][0]['done'] == 1
    assert client.patch(path, json={'done':False}).json()['done'] == 0
    assert client.patch(path, json={'discarded':True}).json()['discarded'] == 1
    done = client.patch(path, json={'done':True}).json()
    assert (done['done'], done['discarded']) == (1, 0)
    discarded = client.patch(path, json={'discarded':True}).json()
    assert (discarded['done'], discarded['discarded']) == (0, 1)
    assert client.patch(path, json={'done':True, 'discarded':True}).status_code == 422


def test_pool_transfer_is_idempotent_and_keeps_dm_private(client, monkeypatch):
    import json
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(vault, 'fetch_tweet_text', lambda url: {
        'tweet_text':'Public tweet content', 'tweet_author':'Author',
        'tweet_image':'https://pbs.twimg.com/media/test.jpg', 'tweet_media_type':'video', 'text_status':'ready'})
    client.headers['x-test-role'] = 'dev'
    row = client.post('/api/dashboard/vault', json={
        'url':'https://x.com/author/status/123', 'source':'Private sender',
        'slack_url':'https://private.slack.com/archives/secret'}).json()
    path = '/api/dashboard/vault/'+row['id']+'/pool'
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(path), range(2)))
    assert all(r.status_code == 200 for r in responses)
    ids = [r.json()['pool_request_id'] for r in responses]
    assert ids[0] == ids[1]
    assert not responses[0].json()['done']
    with vault.connect() as conn:
        rows = [dict(r) for r in conn.execute('SELECT * FROM queue_requests').fetchall()]
        assert len(rows) == 1
        request = rows[0]
        assert request['status'] == 'pool'
        assert request['post_caption'] == 'Public tweet content'
        assert request['post_type'] == 'Reel'
        assert request['cover_url'].endswith('/test.jpg')
        assert json.loads(request['reference_links']) == [row['url']]
        assert 'Private sender' not in json.dumps(request)
        assert 'private.slack' not in json.dumps(request)
        assert conn.execute('SELECT COUNT(*) FROM queue_request_events').fetchone()[0] == 1
        assert conn.execute('SELECT revision FROM queue_live_state').fetchone()[0] == 1
    assert client.get('/api/dashboard/vault').json()['items'][0]['pool_request_id'] == ids[0]
    client.patch('/api/dashboard/vault/'+row['id'], json={'discarded':True})
    assert client.post(path).status_code == 409


def test_failed_pool_publish_rolls_back_request_and_marker(client, monkeypatch):
    from app import main
    client.headers['x-test-role'] = 'dev'
    row = client.post('/api/dashboard/vault', json={'url':'https://example.com/rollback'}).json()
    def fail(*args): raise RuntimeError('publish failed')
    monkeypatch.setattr(main, '_queue_v2_publish', fail)
    with pytest.raises(RuntimeError):
        client.post('/api/dashboard/vault/'+row['id']+'/pool')
    with vault.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM queue_requests').fetchone()[0] == 0
        assert not conn.execute('SELECT pool_request_id FROM vault_links').fetchone()[0]
