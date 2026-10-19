"""Shared editorial decisions survive feed ingestion and other News writes."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import jev_features, main, news_sources, news_store


STORY_ID = 'https://example.com/story'
STORY = {
    'id': STORY_ID, 'title': 'A specific AI story', 'description': 'Feed evidence',
    'link': STORY_ID, 'source': 'Example', 'sourceType': 'news',
    'feedLabel': 'Technology', 'feedGroup': 'All', 'socialSignal': '', 'published': '',
}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    path = tmp_path / 'news.sqlite'

    @contextmanager
    def connect():
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(news_store, 'connect', connect)
    with connect() as conn:
        # Upgrade an existing workspace, including its saved brief and review.
        conn.execute('''CREATE TABLE news_stories (
            id TEXT PRIMARY KEY, item_json TEXT NOT NULL, review_json TEXT,
            saved INTEGER NOT NULL DEFAULT 0, brief TEXT NOT NULL DEFAULT '',
            reviewed_at TEXT, error TEXT, updated_at TEXT NOT NULL)''')
        conn.execute('''INSERT INTO news_stories
            (id,item_json,review_json,saved,brief,updated_at) VALUES (?,?,?,?,?,?)''',
            (STORY_ID, json.dumps(STORY), json.dumps({'reviewVersion': news_store.VERSION,
                                                     'label': 'potential'}),
             1, 'Keep this brief', '2026-01-01'))
        news_store.ensure(conn)
        news_store.ensure(conn)

    app = FastAPI()

    @app.middleware('http')
    async def identity(request, call_next):
        request.state.can_access_news = request.headers.get('x-news', '1') == '1'
        request.state.user_email = request.headers.get('x-user', 'editor@example.com')
        return await call_next(request)

    app.add_api_route('/api/dashboard/news', main.dashboard_news, methods=['GET'])
    app.add_api_route('/api/dashboard/news/status', main.dashboard_news_status, methods=['POST'])
    return TestClient(app), connect


def test_editorial_decision_is_shared_persistent_and_reversible(workspace):
    client, _ = workspace
    for status in ('done', 'rejected'):
        response = client.post('/api/dashboard/news/status', json={'id': STORY_ID, 'status': status})
        assert response.status_code == 200
        assert response.json() == {'ok': True, 'id': STORY_ID, 'status': status}
        payload = client.get('/api/dashboard/news', headers={'x-user': 'other@example.com'}).json()
        stored = payload['statuses'][STORY_ID]
        assert stored['status'] == status
        assert stored['item'] == STORY
        assert stored['updatedBy'] == 'editor@example.com'
        assert stored['updatedAt']
        assert payload['saved'][STORY_ID]['brief'] == 'Keep this brief'
        assert payload['reviews'][STORY_ID]['label'] == 'potential'

    assert client.post('/api/dashboard/news/status', json={'id': STORY_ID, 'status': 'pending'}).status_code == 200
    payload = client.get('/api/dashboard/news').json()
    assert payload['statuses'][STORY_ID]['status'] == 'pending'
    assert payload['statuses'][STORY_ID]['item'] == STORY
    assert payload['saved'][STORY_ID]['brief'] == 'Keep this brief'
    assert payload['reviews'][STORY_ID]['label'] == 'potential'


def test_processed_story_retained_outside_current_feed_window(workspace):
    client, connect = workspace
    client.post('/api/dashboard/news/status', json={'id': STORY_ID, 'status': 'done'})
    with connect() as conn:
        conn.executemany('INSERT INTO news_stories (id,item_json,updated_at) VALUES (?,?,?)',
                         ((f'https://example.com/new-{i}', json.dumps({'id': f'new-{i}'}), '2026-10-19')
                          for i in range(1500)))
    payload = client.get('/api/dashboard/news').json()
    assert len(payload['items']) == 1500
    assert all(item['id'] != STORY_ID for item in payload['items'])
    assert payload['statuses'][STORY_ID]['item'] == STORY
    assert payload['saved'][STORY_ID]['brief'] == 'Keep this brief'
    assert payload['reviews'][STORY_ID]['label'] == 'potential'
    assert client.post('/api/dashboard/news/status', json={'id': STORY_ID, 'status': 'pending'}).status_code == 200
    reopened = client.get('/api/dashboard/news').json()
    assert reopened['statuses'][STORY_ID]['status'] == 'pending'
    assert reopened['statuses'][STORY_ID]['item'] == STORY
    assert reopened['saved'][STORY_ID]['brief'] == 'Keep this brief'
    assert reopened['reviews'][STORY_ID]['label'] == 'potential'
    assert set(reopened['statuses']) == {STORY_ID}


def test_feed_refresh_and_legacy_import_preserve_decision(workspace, monkeypatch):
    client, _ = workspace
    client.post('/api/dashboard/news/status', json={'id': STORY_ID, 'status': 'rejected'})
    monkeypatch.setattr(news_store, 'FEEDS', [('feed', 'Technology', 'All', 'news')])

    class FeedClient:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url):
            return httpx.Response(200, json={'items': [
                {'url': STORY_ID + '?utm_source=refresh', 'title': 'Refreshed title'},
                {'url': STORY_ID + '?utm_source=duplicate', 'title': 'Same story elsewhere'},
            ]}, request=httpx.Request('GET', url))

    monkeypatch.setattr(news_store.httpx, 'Client', FeedClient)
    assert news_store.collect()[0]['ok']
    news_store.import_legacy({STORY_ID: {'item': STORY, 'brief': 'Older brief'}}, {})
    payload = news_store.list_stories()
    assert len(payload['items']) == 1
    assert payload['items'][0]['title'] == 'Refreshed title'
    assert payload['statuses'][STORY_ID]['status'] == 'rejected'
    assert payload['statuses'][STORY_ID]['item']['title'] == 'Refreshed title'
    assert payload['saved'][STORY_ID]['brief'] == 'Keep this brief'


def test_save_review_and_status_writes_do_not_overwrite_each_other(workspace, monkeypatch):
    _, _ = workspace
    monkeypatch.setattr(main, '_news_existing_posts', lambda text: [])
    monkeypatch.setattr(news_sources, 'article_evidence', lambda link, excerpt: (excerpt, 'feed_excerpt'))
    monkeypatch.setattr(jev_features, 'golden_nugget_review', lambda *args, **kwargs: {'label': 'golden_nugget'})
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(news_store.save, STORY_ID, True, 'New brief'),
                   pool.submit(news_store.review_one, STORY_ID),
                   pool.submit(news_store.set_status, STORY_ID, 'done', 'editor@example.com')]
        for future in futures:
            assert future.result()
    payload = news_store.list_stories()
    assert payload['statuses'][STORY_ID]['status'] == 'done'
    assert payload['saved'][STORY_ID]['brief'] == 'New brief'
    assert payload['reviews'][STORY_ID]['label'] == 'golden_nugget'


@pytest.mark.parametrize('payload', [None, [], {}, {'id': STORY_ID}, {'id': STORY_ID, 'status': 'saved'},
                                    {'id': STORY_ID, 'status': {}}, {'id': '', 'status': 'done'},
                                    {'id': 12, 'status': 'done'}])
def test_invalid_status_payload_is_rejected_without_mutation(workspace, payload):
    client, _ = workspace
    assert client.post('/api/dashboard/news/status', json=payload).status_code == 400
    assert client.get('/api/dashboard/news').json()['statuses'] == {}


def test_malformed_json_unknown_story_and_access_checks(workspace):
    client, _ = workspace
    assert client.post('/api/dashboard/news/status', content='{').status_code == 400
    assert client.post('/api/dashboard/news/status', json={'id': 'missing', 'status': 'done'}).status_code == 404
    assert client.post('/api/dashboard/news/status', headers={'x-news': '0'},
                       json={'id': STORY_ID, 'status': 'done'}).status_code == 403
    assert client.get('/api/dashboard/news', headers={'x-news': '0'}).status_code == 403
    assert client.get('/api/dashboard/news').json()['statuses'] == {}
