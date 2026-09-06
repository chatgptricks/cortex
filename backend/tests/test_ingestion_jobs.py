import json
import sqlite3
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest
from app import db, ingestion_jobs as jobs, apify_sync, scheduler


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / 'jobs.sqlite'
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
    return connect


def test_retry_reuses_paid_run_and_dataset_after_database_failure(database, monkeypatch):
    import httpx
    monkeypatch.setenv('APIFY_TOKEN', 'test')
    requests = []
    def handler(req):
        requests.append(req.method)
        data = {'data': {'id': 'paid-run', 'status': 'SUCCEEDED', 'defaultDatasetId': 'dataset'}} if req.method == 'POST' else [{'shortCode': 'new'}]
        return httpx.Response(200, json=data)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    calls = []
    def work():
        items = apify_sync._run_apify_actor_and_fetch({'directUrls': ['account']})
        calls.append(items)
        if len(calls) == 1:
            raise RuntimeError('Database unavailable after paid fetch')
    assert not jobs.run('short', '01', work)
    assert jobs.run('short', '02', work)
    assert requests == ['POST', 'GET']
    assert calls[0] == calls[1]
    with database() as conn:
        row = conn.execute('SELECT * FROM ingestion_jobs').fetchone()
        assert row['slot'] == '01'
        assert row['status'] == 'done'
    assert jobs.run('short', '02', lambda: None)
    assert not jobs.run('short', '02', lambda: pytest.fail('duplicate slot'))


def test_crashed_lease_resumes_existing_state(database):
    with database() as conn:
        jobs.initialize(conn)
        conn.execute("INSERT INTO ingestion_jobs VALUES ('short','01','running','dead',0,?,NULL,'old')", (json.dumps({'paid': 'run-id'}),))
    seen = []
    assert jobs.run('short', '02', lambda: seen.append(jobs.current().state['paid']))
    assert seen == ['run-id']


def test_only_one_worker_can_claim_job(database):
    entered = threading.Event()
    release = threading.Event()
    def work():
        entered.set()
        release.wait(3)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(jobs.run, 'short', '01', work)
        assert entered.wait(2)
        assert not jobs.run('short', '01', lambda: pytest.fail('duplicate worker'))
        release.set()
        assert first.result()


def test_partial_batch_failure_is_not_success(monkeypatch):
    monkeypatch.setattr(scheduler, '_active_account_handles', lambda: ['a'])
    monkeypatch.setattr(apify_sync, 'run_short_term_cycle_batch', lambda *a, **k: {'a': {'error': 'insert failed'}})
    with pytest.raises(apify_sync.ApifySyncError):
        scheduler._run_short_term_jobs()


def test_restart_after_poll_failure_resumes_run(database, monkeypatch):
    import httpx
    monkeypatch.setenv('APIFY_TOKEN', 'test')
    seen = []
    failed = False
    def handler(req):
        nonlocal failed
        seen.append(req.method)
        if req.method == 'POST':
            return httpx.Response(200, json={'data': {'id': 'paid', 'status': 'RUNNING'}})
        if 'actor-runs' in str(req.url):
            if not failed:
                failed = True
                raise httpx.ReadTimeout('lost poll', request=req)
            return httpx.Response(200, json={'data': {'id': 'paid', 'status': 'SUCCEEDED', 'defaultDatasetId': 'd'}})
        return httpx.Response(200, json=[])
    real = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real(transport=httpx.MockTransport(handler)))
    work = lambda: apify_sync._run_apify_actor_and_fetch({}, poll_interval=0)
    assert not jobs.run('short', '01', work)
    assert jobs.run('short', '01', work)
    assert seen.count('POST') == 1


def test_ambiguous_start_finds_paid_run_by_input(database, monkeypatch):
    import httpx
    monkeypatch.setenv('APIFY_TOKEN', 'test')
    seen = []
    def handler(req):
        seen.append(req.method)
        if req.method == 'POST':
            raise httpx.ReadTimeout('lost start response', request=req)
        if '/acts/' in str(req.url):
            return httpx.Response(200, json={'data': {'items': [{'id': 'paid', 'status': 'SUCCEEDED', 'startedAt': '2099-01-01', 'defaultKeyValueStoreId': 'k', 'defaultDatasetId': 'd'}]}})
        if '/records/INPUT' in str(req.url):
            return httpx.Response(200, json={'directUrls': ['a']})
        return httpx.Response(200, json=[])
    real = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real(transport=httpx.MockTransport(handler)))
    work = lambda: apify_sync._run_apify_actor_and_fetch({'directUrls': ['a']})
    assert not jobs.run('short', '01', work)
    assert jobs.run('short', '01', work)
    assert seen.count('POST') == 1


def test_long_daily_work_does_not_block_short_collection(monkeypatch):
    release = threading.Event()
    daily = threading.Event()
    short = threading.Event()
    monkeypatch.setattr(scheduler, '_jobs', {})
    def slow():
        daily.set()
        release.wait(2)
    scheduler._launch('daily', slow)
    assert daily.wait(1)
    scheduler._launch('short', short.set)
    assert short.wait(1)
    release.set()
