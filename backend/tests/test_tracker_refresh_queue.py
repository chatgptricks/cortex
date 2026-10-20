import json
import sqlite3
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app import apify_sync, db, ingestion_jobs, tracker_refresh_queue as queue


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / 'tracker.sqlite'

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
    with connect() as conn:
        queue.initialize(conn)
    return connect


def enqueue(handle='openai'):
    return queue.enqueue(kind='account' if handle else 'all', handle=handle, requested_by='user@example.com')


def test_concurrent_clicks_share_active_job_but_completed_reload_is_fresh(database):
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = list(pool.map(lambda _: enqueue(), range(6)))
    assert len({job['job_id'] for job in jobs}) == 1
    first = jobs[0]
    with database() as conn:
        conn.execute("UPDATE tracker_refresh_jobs SET status='done' WHERE job_id=?", (first['job_id'],))
    second = enqueue()
    assert second['job_id'] != first['job_id']
    with database() as conn:
        conn.execute("UPDATE tracker_refresh_jobs SET status='error' WHERE job_id=?", (second['job_id'],))
    assert enqueue()['job_id'] != second['job_id']


def test_new_account_is_not_silently_absorbed_by_preexisting_all_roster(database):
    all_job = enqueue(None)
    assert enqueue(None)['job_id'] == all_job['job_id']
    assert enqueue('new_account')['job_id'] != all_job['job_id']


def test_claim_is_atomic_across_workers(database):
    enqueue()
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(lambda _: queue._claim(), range(4)))
    assert len([task for task in claimed if task]) == 1


def test_running_batch_and_account_have_independent_serial_lanes(database):
    batch = enqueue(None)
    first, second = enqueue('openai'), enqueue('chatgptricks')
    assert queue._claim('all')['job_id'] == batch['job_id']
    assert queue._claim('account')['job_id'] == first['job_id']
    assert queue._claim('account') is None
    assert queue._claim('all') is None
    # Even an unscoped claimant in another process must respect each lane.
    assert queue._claim() is None
    with database() as conn:
        conn.execute("UPDATE tracker_refresh_jobs SET status='done' WHERE job_id=?", (first['job_id'],))
    assert queue._claim('account')['job_id'] == second['job_id']
    assert queue.get(batch['job_id'])['status'] == 'running'


def test_concurrent_workers_cannot_run_two_jobs_in_one_lane(database):
    enqueue('openai')
    enqueue('chatgptricks')
    enqueue('anthropic')
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(lambda _: queue._claim('account'), range(4)))
    assert len([task for task in claimed if task]) == 1


def test_completed_ingestion_result_survives_queue_redelivery(database, monkeypatch):
    queued = enqueue()
    task = queue._claim()
    expected = {'handle': 'openai', 'followers_count': 123456, 'captured_at': '2026-10-17T12:00:00+00:00'}
    ingestion_jobs.call(f"tracker-refresh:{task['job_key']}", lambda: expected, slot=task['requested_at'])
    monkeypatch.setattr(apify_sync, 'snapshot_one_account', lambda *a, **k: pytest.fail('must reuse paid result'))
    queue._run(task)
    done = queue.get(queued['job_id'])
    assert done['status'] == 'done'
    assert done['result']['followers_count'] == 123456
    assert done['progress'] == {'total': 1, 'completed': 1, 'succeeded': 1, 'failed': 0}


def test_transient_failure_requeues_and_resumes_same_paid_journal(database, monkeypatch):
    job = enqueue()
    calls = []
    def snapshot(*a, **k):
        journal = ingestion_jobs.current()
        calls.append(journal.frozen('paid_run_id', 'already-paid'))
        if len(calls) == 1:
            raise RuntimeError('temporary network failure token=secret')
        return {'handle': 'openai', 'followers_count': 123456}
    monkeypatch.setattr(apify_sync, 'snapshot_one_account', snapshot)
    queue._run(queue._claim())
    retry = queue.get(job['job_id'])
    assert retry['status'] == 'queued'
    assert 'secret' not in retry['error']
    assert queue._claim() is None
    with database() as conn:
        conn.execute('UPDATE ingestion_jobs SET lease_until=0')
        conn.execute('UPDATE tracker_refresh_jobs SET available_at=NULL')
    queue._run(queue._claim())
    assert queue.get(job['job_id'])['status'] == 'done'
    assert calls == ['already-paid', 'already-paid']


def test_partial_batch_reports_saved_and_failed_profiles(database, monkeypatch):
    job = enqueue(None)
    def batch(*, force, on_progress):
        assert force
        on_progress({'total': 2, 'completed': 1, 'succeeded': 1, 'failed': 0})
        assert queue.get(job['job_id'])['progress']['completed'] == 1
        return {'snapshotted': ['openai'], 'failed': {'missing': 'No exact reading'}}
    monkeypatch.setattr(apify_sync, 'snapshot_all_accounts', batch)
    queue._run(queue._claim())
    done = queue.get(job['job_id'])
    assert done['status'] == 'done'
    assert done['result']['failed'] == {'missing': 'No exact reading'}
    assert done['progress']['completed'] == 2 and done['progress']['failed'] == 1


def test_invalid_profile_is_terminal_after_bounded_data_retries(database, monkeypatch):
    job = enqueue()
    def invalid(*a, **k):
        raise apify_sync.ProfileSnapshotError('Instagram did not return an exact follower count')
    monkeypatch.setattr(apify_sync, 'snapshot_one_account', invalid)
    queue._run(queue._claim())
    assert queue.get(job['job_id'])['status'] == 'error'


def test_stale_worker_is_recovered(database):
    job = enqueue()
    queue._claim()
    old = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    with database() as conn:
        conn.execute('UPDATE tracker_refresh_jobs SET heartbeat_at=?', (old,))
    queue._recover_stale()
    assert queue.get(job['job_id'])['status'] == 'queued'
    assert queue._claim()['job_id'] == job['job_id']


def test_legacy_day_slot_is_preserved_for_recovery_and_result_redelivery(database, monkeypatch):
    job = enqueue()
    task = queue._claim()
    key = f"tracker-refresh:{task['job_key']}"
    with database() as conn:
        ingestion_jobs.initialize(conn)
        conn.execute(
            "INSERT INTO ingestion_jobs VALUES (?, '2026-10-17', 'retry', 'old-worker', 0, ?, NULL, 'old')",
            (key, json.dumps({'paid_run': 'retained'})),
        )
    calls = []
    def snapshot(*a, **kw):
        calls.append(ingestion_jobs.current().state['paid_run'])
        return {'handle': 'openai', 'followers_count': 98765}
    monkeypatch.setattr(apify_sync, 'snapshot_one_account', snapshot)
    queue._run(task)
    with database() as conn:
        assert conn.execute('SELECT slot FROM ingestion_jobs WHERE job_key=?', (key,)).fetchone()['slot'] == '2026-10-17'
        conn.execute("UPDATE tracker_refresh_jobs SET status='running' WHERE job_id=?", (job['job_id'],))
    queue._run(task)  # crash/redelivery after ingestion committed its result
    assert calls == ['retained']
    assert queue.get(job['job_id'])['result']['followers_count'] == 98765
    assert queue.get(job['job_id'])['status'] == 'done'
