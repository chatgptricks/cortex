from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import sqlite3

import httpx
import pytest

from app import apify_sync as sync, db, engagement_refresh as refresh, ingestion_jobs as jobs


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / 'metrics.sqlite'
    @contextmanager
    def connect():
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    monkeypatch.setattr(db, 'connect', connect)
    monkeypatch.setattr(jobs, 'RETRY_SECONDS', 0)
    monkeypatch.setattr(sync, '_reconcile_queue_hot', lambda: None)
    monkeypatch.setattr(sync, 'get_account_config', lambda a: {'table': 'dashboard_posts', 'handle': a, 'scrape_mode': 'posts', 'hot_threshold': 600})
    with connect() as conn:
        conn.execute('''CREATE TABLE dashboard_posts (
            id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT, published_at TEXT,
            likes INTEGER DEFAULT 10, comments INTEGER DEFAULT 1, updated_at TEXT,
            is_hot INTEGER DEFAULT 0, hot_checked INTEGER DEFAULT 1, refreshed_8h INTEGER DEFAULT 0,
            likes_at_8h INTEGER, comments_at_8h INTEGER, is_deleted INTEGER DEFAULT 0,
            permalink TEXT, cover_image_path TEXT, cover_source_url TEXT)''')
        conn.execute('CREATE TABLE queue_requests (post_account TEXT, post_shortcode TEXT, status TEXT, is_custom INTEGER DEFAULT 0)')
        refresh.initialize(conn)
    return connect


def add(database, now, code, *, account='a', age=1, hot=0, done=0, refreshed=None):
    with database() as conn:
        conn.execute('INSERT INTO dashboard_posts(account, shortcode, published_at, is_hot, refreshed_8h) VALUES (?, ?, ?, ?, ?)',
                     (account, code, (now-timedelta(hours=age)).isoformat(), hot, done))
        if refreshed is not None:
            refresh.observe(conn, code, {'shortCode': code, 'likesCount': 10}, (now-timedelta(hours=refreshed)).isoformat())


def test_age_cadence_queue_priority_and_discovery_reuse(database):
    now = datetime.now(UTC)
    add(database, now, 'young-fresh', refreshed=.9)
    add(database, now, 'young-due', refreshed=1)
    add(database, now, 'gap-fresh', age=16, done=1, refreshed=2.9)
    add(database, now, 'gap-due', age=16, done=1, refreshed=3)
    add(database, now, 'ordinary-old', age=50)
    add(database, now, 'hot-old', age=50, hot=1, done=1, refreshed=3)
    add(database, now, 'queued-old', age=50, done=1, refreshed=3)
    add(database, now, 'expired-hot', age=169, hot=1)
    add(database, now, 'deleted', age=1)
    with database() as conn:
        conn.execute("UPDATE dashboard_posts SET is_deleted = 1 WHERE shortcode = 'deleted'")
        conn.execute("INSERT INTO queue_requests VALUES ('a', 'queued-old', 'pool', 0)")
    picked, _ = refresh.candidates(['a'], now)
    assert {p['shortcode'] for p in picked} == {'young-due', 'gap-due', 'hot-old', 'queued-old'}
    assert picked[0]['shortcode'] in {'hot-old', 'queued-old'}


def test_budget_survives_restart_and_retry_and_paces_day(database, monkeypatch):
    now = datetime(2026, 10, 6, 12, tzinfo=UTC)
    monkeypatch.setenv('SENTIENT_ENGAGEMENT_DAILY_ITEMS', '9')
    selection = [{'shortcode': str(i)} for i in range(100)]
    first, charge = refresh.reserve(selection, now, 'first')
    assert len(first) == 7 and charge == 21
    assert refresh.reserve([], now, 'first') == (first, charge)
    assert len(refresh.reserve(selection, now, 'second')[0]) == 2
    assert refresh.reserve(selection, now, 'third') == ([], 0)
    assert len(refresh.reserve(selection, now+timedelta(days=1), 'tomorrow')[0]) == 7


def test_dollar_limit_is_reserved_before_paid_start(database, monkeypatch):
    now = datetime.now(UTC)
    monkeypatch.setenv('SENTIENT_ENGAGEMENT_DAILY_USD', '0.012')
    selection = [{'shortcode': str(i)} for i in range(100)]
    for i in range(24):
        refresh.reserve(selection, now, str(i))
    with database() as conn:
        budget = conn.execute('SELECT * FROM engagement_budget').fetchone()
    assert budget['reserved_milliusd'] == 12 and budget['reserved_items'] == 4


def test_idle_or_disabled_never_starts_paid_run(database, monkeypatch):
    monkeypatch.setattr(sync, '_run_apify_actor_and_fetch', lambda *a, **k: pytest.fail('unnecessary charge'))
    assert refresh.run_cycle(['a'])['requested'] == 0
    add(database, datetime.now(UTC), 'new')
    monkeypatch.setenv('SENTIENT_ENGAGEMENT_DAILY_USD', '0')
    assert refresh.run_cycle(['a'])['requested'] == 0


def test_one_url_updates_reposted_rows_and_preserves_snapshot(database, monkeypatch):
    now = datetime.now(UTC)
    monkeypatch.setattr(jobs, 'now', lambda: now)
    add(database, now, 'shared', age=16)
    add(database, now, 'shared', account='b', age=16, done=1)
    with database() as conn:
        conn.execute("UPDATE dashboard_posts SET likes_at_8h = 42 WHERE account = 'b'")
    calls = []
    def fetch(payload, **kwargs):
        calls.append((payload, kwargs))
        return [{'shortCode': 'shared', 'likesCount': 90, 'commentsCount': 3, 'videoViewCount': 500}]
    monkeypatch.setattr(sync, '_run_apify_actor_and_fetch', fetch)
    result = refresh.run_cycle(['a', 'b'])
    assert result['requested'] == 1 and result['updated'] == 2
    assert calls[0][0]['directUrls'] == ['https://www.instagram.com/p/shared/']
    assert calls[0][1]['run_limits']['maxTotalChargeUsd'] == .003
    with database() as conn:
        rows = conn.execute('SELECT * FROM dashboard_posts ORDER BY account').fetchall()
        observation = conn.execute('SELECT * FROM engagement_observations').fetchone()
    assert rows[0]['likes'] == rows[0]['likes_at_8h'] == 90
    assert rows[1]['likes'] == 90 and rows[1]['likes_at_8h'] == 42
    assert json.loads(observation['raw_json'])['videoViewCount'] == 500


def test_unknown_likes_never_finalize_or_old_posts_create_snapshot(database, monkeypatch):
    now = datetime.now(UTC)
    monkeypatch.setattr(jobs, 'now', lambda: now)
    add(database, now, 'hidden', age=10)
    add(database, now, 'late', age=50, hot=1)
    monkeypatch.setattr(sync, '_run_apify_actor_and_fetch', lambda *a, **kw: [
        {'shortCode': 'hidden', 'likesCount': -1}, {'shortCode': 'late', 'likesCount': 90}])
    refresh.run_cycle(['a'])
    with database() as conn:
        rows = conn.execute('SELECT * FROM dashboard_posts').fetchall()
    assert all(row['likes_at_8h'] is None and row['refreshed_8h'] == 0 for row in rows)


def test_manual_refresh_reuses_shared_recent_measurement(database, monkeypatch):
    now = datetime.now(UTC)
    add(database, now, 'shared')
    with database() as conn:
        refresh.observe(conn, 'shared', {'shortCode': 'shared', 'likesCount': 70, 'commentsCount': 5}, now.isoformat())
    monkeypatch.setattr(sync, '_fetch_apify_items', lambda *a, **kw: pytest.fail('fresh shared metric already paid for'))
    monkeypatch.setattr(sync, '_refresh_cover_from_item', lambda *a, **kw: False)
    result = sync.refresh_single_post('a', 'shared')
    assert result['cached'] and result['likes'] == 70
    assert result['likesUpdatedAt'] == now.isoformat()


def test_paid_run_limits_and_partial_dataset_recovery(database, monkeypatch):
    monkeypatch.setenv('APIFY_TOKEN', 'test')
    posts = []
    def respond(req):
        if req.method == 'POST':
            posts.append(req)
            return httpx.Response(201, json={'data': {'id': 'paid', 'status': 'ABORTED', 'defaultDatasetId': 'partial'}})
        return httpx.Response(200, json=[{'shortCode': 'p', 'likesCount': 70}])
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(respond)))
    attempts = []
    def work():
        result = sync._run_apify_actor_and_fetch({'directUrls': ['p']}, run_limits={'maxTotalChargeUsd': .003}, accept_partial=True)
        attempts.append(result)
        if len(attempts) == 1:
            raise RuntimeError('database interrupted after payment')
        return result
    assert not jobs.run('paid', '01', work)
    assert jobs.run('paid', '01', work)
    assert len(posts) == 1
    assert posts[0].url.params['maxTotalChargeUsd'] == '0.003'
    assert attempts[0] == attempts[1]


def test_hidden_final_snapshot_waits_normal_interval(database):
    now = datetime.now(UTC)
    add(database, now, 'hidden-after-eight', age=10, refreshed=1)
    add(database, now, 'crossing-eight', age=8.1, refreshed=.5)
    picked, _ = refresh.candidates(['a'], now)
    assert [p['shortcode'] for p in picked] == ['crossing-eight']
