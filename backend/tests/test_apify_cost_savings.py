"""Cost regressions: same observations, fewer paid starts/results."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import sqlite3
import time

import httpx
import pytest

from app import apify_sync as sync, db, ingestion_jobs as jobs, post_media as media
from app import post_refresh_queue as queue


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / 'costs.sqlite'
    @contextmanager
    def connect():
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    monkeypatch.setattr(db, 'connect', connect)
    monkeypatch.setattr(jobs, 'RETRY_SECONDS', 0)
    monkeypatch.setattr(media, '_CACHE', {})
    with connect() as conn:
        conn.execute('''CREATE TABLE dashboard_posts (
            id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT, published_at TEXT,
            refreshed_8h INTEGER, raw_json TEXT, enriched_at TEXT)''')
        conn.execute('CREATE TABLE posts (shortcode TEXT, published_at TEXT, refreshed_8h INTEGER)')
    return connect


def test_engagement_selection_preserves_boundaries_and_account_scope(database, monkeypatch):
    now = datetime(2026, 9, 25, 12, tzinfo=UTC)
    cases = [('young', 1, 0, True), ('eight', 8, 1, True),
             ('pending', 9, 0, True), ('eleven', 11, 0, True),
             ('finalized', 9, 1, False), ('old', 11.001, 0, False)]
    with database() as conn:
        for account, hours, done, expected in cases:
            conn.execute('INSERT INTO dashboard_posts(account, shortcode, published_at, refreshed_8h) VALUES (?, ?, ?, ?)',
                         (account, account, (now-timedelta(hours=hours)).isoformat(), done))
        conn.execute("INSERT INTO dashboard_posts(account, shortcode, published_at) VALUES ('invalid', 'post-placeholder', ?)", (now.isoformat(),))
        conn.execute("INSERT INTO posts VALUES ('canonical', ?, 0)", (now.isoformat(),))
    for account, _, _, expected in cases:
        assert sync._needs_eight_hour_refresh(account, {'table': 'dashboard_posts'}, now) is expected
    for account in ('invalid', 'empty'):
        assert not sync._needs_eight_hour_refresh(account, {'table': 'dashboard_posts'}, now)
    assert sync._needs_eight_hour_refresh('chatgptricks', {'table': 'posts'}, now)


def test_update_only_scan_omits_idle_profiles_but_keeps_processing_contract(database, monkeypatch):
    now = datetime.now(UTC)
    with database() as conn:
        conn.execute("INSERT INTO dashboard_posts(account, shortcode, published_at, refreshed_8h) VALUES ('busy', 'x', ?, 0)", (now.isoformat(),))
    monkeypatch.setattr(sync, 'get_account_config', lambda a: {'table': 'dashboard_posts', 'handle': a, 'scrape_mode': 'both'})
    fetched = []
    def collect(configs, limit, current, **kwargs):
        fetched.append(list(configs))
        assert kwargs == dict(include_posts=True, include_reels=False, lookback_hours=11)
        assert limit == 100
        return {a: [] for a in configs}
    monkeypatch.setattr(sync, '_collect_short_term_items', collect)
    monkeypatch.setattr(sync, '_process_short_term_items', lambda a, c, i, n, **kw: {'engagement': {'updated': 0}})
    monkeypatch.setattr(sync, '_reconcile_queue_hot', lambda: None)
    result = sync.run_day_engagement_cycle_batch(['busy', 'empty'])
    assert fetched == [['busy']]
    assert result['empty']['engagement']['checked'] == 0


def paid_transport(monkeypatch):
    calls = []
    monkeypatch.setenv('APIFY_TOKEN', 'test')
    def respond(request):
        calls.append(request.method)
        if request.method == 'POST':
            return httpx.Response(200, json={'data': {'id': 'paid', 'status': 'SUCCEEDED', 'defaultDatasetId': 'data'}})
        return httpx.Response(200, json=[{'shortCode': 'ABC', 'displayUrl': 'https://cdn.example/a.jpg'}])
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(respond)))
    return calls


def test_manual_call_recovers_paid_data_after_failure_and_allows_new_refresh(database, monkeypatch):
    calls = paid_transport(monkeypatch)
    attempts = []
    def work():
        result = sync._run_apify_actor_and_fetch({'directUrls': ['ABC']})
        attempts.append(result)
        if len(attempts) == 1:
            raise RuntimeError('DB failure after scrape')
        return {'likes': 42}
    with pytest.raises(jobs.IngestionPending):
        jobs.call('manual', work, slot='01')
    assert jobs.call('manual', work, slot='01') == {'likes': 42}
    assert jobs.call('manual', lambda: pytest.fail('redelivery'), slot='01') == {'likes': 42}
    assert calls.count('POST') == 1
    assert jobs.call('manual', work, slot='02') == {'likes': 42}
    assert calls.count('POST') == 2


def test_reload_queue_retry_reuses_paid_run(database, monkeypatch):
    calls = paid_transport(monkeypatch)
    attempts = []
    def refresh(account, code):
        sync._run_apify_actor_and_fetch({'directUrls': [code]})
        attempts.append(code)
        if len(attempts) == 1:
            raise RuntimeError('interrupted after payment')
        return {'likes': 42}
    monkeypatch.setattr(sync, 'refresh_single_post', refresh)
    task = queue.enqueue(account='test', shortcode='ABC')
    queue._run(task)
    assert queue.get(task['job_id'])['status'] == 'error'
    retry = queue.enqueue(account='test', shortcode='ABC')
    assert retry['requested_at'] == task['requested_at']
    queue._run(retry)
    assert queue.get(task['job_id'])['result'] == {'likes': 42}
    assert calls.count('POST') == 1


def test_media_cache_survives_restart_without_extending_ttl(database, monkeypatch):
    items = [{'url': 'https://cdn.example/a.jpg', 'kind': 'image', 'poster': None, 'index': 1, 'filename': '01.jpg'}]
    media._cache_put('ABC', items, 'apify')
    with database() as conn:
        conn.execute('UPDATE post_media_cache SET stored_at = ?', (time.time()-850,))
    media._CACHE.clear()
    monkeypatch.setattr(media, '_urls_available', lambda items: True)
    assert media._cache_get('ABC') == (items, 'apify')
    assert 840 < time.monotonic() - media._CACHE['ABC'][0] < 870
    media._CACHE.clear()
    monkeypatch.setattr(media, '_urls_available', lambda items: False)
    assert media._cache_get('ABC') is None
    with database() as conn:
        conn.execute('UPDATE post_media_cache SET stored_at = ?', (time.time()-901,))
    monkeypatch.setattr(media, '_urls_available', lambda items: pytest.fail('expired cache should not probe'))
    assert media._cache_get('ABC') is None


def test_saved_carousel_preserves_video_order_and_rejects_incomplete_payload(database, monkeypatch):
    raw = {'shortCode': 'ABC', 'type': 'Sidecar', 'childPosts': [
        {'displayUrl': 'https://cdn.example/1.jpg'},
        {'displayUrl': 'https://cdn.example/2.jpg', 'videoUrl': 'https://cdn.example/2.mp4'}]}
    with database() as conn:
        conn.execute('INSERT INTO dashboard_posts(shortcode, raw_json, enriched_at) VALUES (?, ?, ?)',
                     ('ABC', json.dumps(raw), datetime.now(UTC).isoformat()))
    monkeypatch.setattr(media, '_urls_available', lambda items: True)
    assert [item['kind'] for item in media._saved_media('ABC')] == ['image', 'video']
    raw['childPosts'].append({'type': 'Video'})
    with database() as conn:
        conn.execute('UPDATE dashboard_posts SET raw_json = ?', (json.dumps(raw),))
    assert media._saved_media('ABC') is None


def test_paid_media_cache_write_failure_reuses_dataset(database, monkeypatch):
    calls = paid_transport(monkeypatch)
    real_put = media._cache_put
    attempts = []
    def save(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('cache DB temporarily unavailable')
        return real_put(*args)
    monkeypatch.setattr(media, '_cache_put', save)
    with pytest.raises(media.PostMediaError):
        media._slides_from_apify('ABC')
    assert media._slides_from_apify('ABC')[0]['url'] == 'https://cdn.example/a.jpg'
    assert calls.count('POST') == 1


def test_media_validation_rejects_login_html_and_expired_urls(monkeypatch):
    original = httpx.Client
    for status, kind, expected in [(200, 'image/jpeg', True), (403, 'image/jpeg', False), (200, 'text/html', False)]:
        monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(
            lambda req: httpx.Response(status, headers={'content-type': kind}))))
        assert media._urls_available([{'url': 'https://cdn.example/a.jpg', 'kind': 'image'}]) is expected


def test_all_idle_engagement_accounts_never_call_apify(database, monkeypatch):
    monkeypatch.setattr(sync, 'get_account_config', lambda a: {'table': 'dashboard_posts', 'handle': a, 'scrape_mode': 'posts'})
    monkeypatch.setattr(sync, '_collect_short_term_items', lambda *a, **kw: pytest.fail('no paid lookup for idle accounts'))
    monkeypatch.setattr(sync, '_reconcile_queue_hot', lambda: None)
    result = sync.run_day_engagement_cycle_batch(['idle'])
    assert result['idle']['engagement']['updated'] == 0


def test_retried_engagement_reuses_frozen_account_selection(database, monkeypatch):
    now = datetime.now(UTC)
    with database() as conn:
        conn.execute("INSERT INTO dashboard_posts(account, shortcode, published_at, refreshed_8h) VALUES ('busy', 'x', ?, 0)", (now.isoformat(),))
    monkeypatch.setattr(sync, 'get_account_config', lambda a: {'table': 'dashboard_posts', 'handle': a, 'scrape_mode': 'posts'})
    monkeypatch.setattr(sync, '_reconcile_queue_hot', lambda: None)
    calls = []
    def collect(configs, *args, **kwargs):
        calls.append(list(configs))
        raise RuntimeError('transport failed after selection')
    monkeypatch.setattr(sync, '_collect_short_term_items', collect)
    work = lambda: sync.run_day_engagement_cycle_batch(['busy', 'idle'])
    assert not jobs.run('engagement', '01', work)
    with database() as conn:
        conn.execute('DELETE FROM dashboard_posts')
    assert not jobs.run('engagement', '01', work)
    assert calls == [['busy'], ['busy']]
