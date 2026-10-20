import sqlite3
from contextlib import contextmanager

import pytest

from app import apify_sync, db, ingestion_jobs, scheduler


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / 'profiles.sqlite'
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
    monkeypatch.setattr(db, 'utc_now', lambda: '2026-10-17T12:00:00+00:00')
    monkeypatch.setattr(apify_sync.time, 'sleep', lambda _: None)
    with connect() as conn:
        conn.execute('''CREATE TABLE account_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, handle TEXT NOT NULL,
            followers_count INTEGER, posts_count INTEGER, full_name TEXT,
            verified INTEGER, private INTEGER, following_count INTEGER,
            captured_at TEXT NOT NULL, snapshot_date TEXT,
            UNIQUE(handle, snapshot_date))''')
    return connect


def test_all_accounts_share_one_profile_run_and_retry_only_unusable_rows(database, monkeypatch):
    monkeypatch.setattr(apify_sync, 'list_accounts', lambda **kw: [{'handle': h} for h in ['b', 'a', 'c']])
    calls = []
    def actor(payload, **kw):
        calls.append((kw['actor_id'], payload))
        assert kw['accept_partial'] is True
        assert kw['run_limits']['timeout'] == 180
        assert kw['max_wait_seconds'] == 210.0
        if len(calls) == 1:
            assert kw['run_limits']['memory'] == 4096
            return [
                {'username': 'a', 'followersCount': 1234567},
                {'username': 'a', 'followersCount': None},
                {'username': 'b', 'followersCount': '75'},
                {'username': 'c', 'followersCount': '1.2M'},
                {'username': 'foreign', 'followersCount': 999},
            ]
        assert 'memory' not in kw['run_limits']
        return [{'username': 'c', 'followersCount': 1200123}]
    monkeypatch.setattr(apify_sync, '_run_apify_actor_and_fetch', actor)
    updates = []
    result = apify_sync.snapshot_all_accounts(force=True, on_progress=updates.append)
    assert result == {'snapshotted': ['a', 'b', 'c'], 'failed': {}}
    assert calls == [
        (apify_sync.APIFY_PROFILE_ACTOR_ID, {'usernames': ['a', 'b', 'c']}),
        (apify_sync.APIFY_ACTOR_ID, {'directUrls': ['https://www.instagram.com/c/'], 'resultsType': 'details'}),
    ]
    assert db.get_account_snapshot_for_day('a')['followers_count'] == 1234567
    assert db.get_account_snapshot_for_day('c')['followers_count'] == 1200123
    assert any(update['completed'] == 2 for update in updates)


def test_unresolved_profile_returns_to_dedicated_actor_on_last_attempt(database, monkeypatch):
    calls = []
    def actor(payload, **kw):
        calls.append(kw['actor_id'])
        if len(calls) < 3:
            return [{'inputUrl': 'https://www.instagram.com/openai/', 'error': 'no_items',
                     'errorDescription': 'Empty or private data'}]
        assert payload == {'usernames': ['openai']}
        return [{'username': 'openai', 'followersCount': 123456}]
    monkeypatch.setattr(apify_sync, '_run_apify_actor_and_fetch', actor)
    assert apify_sync.snapshot_one_account('openai', force=True)['followers_count'] == 123456
    assert calls == [apify_sync.APIFY_PROFILE_ACTOR_ID, apify_sync.APIFY_ACTOR_ID, apify_sync.APIFY_PROFILE_ACTOR_ID]


def test_error_input_url_preserves_provider_reason_without_trusting_identity(database, monkeypatch):
    db.insert_account_snapshot('openai', 123456, 1, 'OpenAI', False, False)
    monkeypatch.setattr(apify_sync, '_run_apify_actor_and_fetch', lambda *a, **kw: [
        {'inputUrl': 'https://www.instagram.com/openai/?token=secret', 'followersCount': 888888,
         'error': 'no_items', 'errorDescription': 'Empty or private data token=secret'},
        {'inputUrl': 'https://www.instagram.com/openai/', 'username': 'wrong', 'followersCount': 999999},
    ])
    with pytest.raises(apify_sync.ProfileSnapshotError, match='Empty or private data token=REDACTED') as caught:
        apify_sync.snapshot_one_account('openai', force=True)
    assert 'secret' not in str(caught.value)
    assert db.get_account_snapshot_for_day('openai')['followers_count'] == 123456


@pytest.mark.parametrize('count', [None, -1, True, 100.0, '1.2M', '1,200', ''])
def test_invalid_counts_and_wrong_identity_never_overwrite_last_good_reading(database, monkeypatch, count):
    db.insert_account_snapshot('openai', 123456, 1, 'OpenAI', False, False)
    monkeypatch.setattr(apify_sync, '_run_apify_actor_and_fetch', lambda *a, **kw: [
        {'username': 'openai', 'followersCount': count},
        {'username': 'wrong', 'followersCount': 999999},
    ])
    with pytest.raises(apify_sync.ProfileSnapshotError):
        apify_sync.snapshot_one_account('openai', force=True)
    assert db.get_account_snapshot_for_day('openai')['followers_count'] == 123456


def test_recovery_reuses_frozen_paid_batch_and_provider_observation_day(database, monkeypatch):
    starts = []
    fail = [True]
    def actor(payload, **kw):
        journal = ingestion_jobs.current()
        saved = journal.next_run(kw['actor_id'], payload)
        if 'run' not in saved:
            saved['run'] = {'id': 'paid-run'}
            starts.append(payload)
            journal.save()
        if fail[0]:
            fail[0] = False
            raise RuntimeError('network interrupted')
        saved['finished_at'] = '2026-10-18T06:00:00.123456Z'
        journal.save()
        return [{'username': 'a', 'followersCount': 999}]
    monkeypatch.setattr(apify_sync, '_run_apify_actor_and_fetch', actor)
    monkeypatch.setattr(apify_sync, 'list_accounts', lambda **kw: [{'handle': 'a'}])
    with pytest.raises(ingestion_jobs.IngestionPending):
        ingestion_jobs.call('manual-test', lambda: apify_sync.snapshot_all_accounts(force=True), slot='request')
    with database() as conn:
        conn.execute('UPDATE ingestion_jobs SET lease_until=0')
    monkeypatch.setattr(db, 'utc_now', lambda: '2026-10-19T15:00:00+00:00')
    # New registry rows cannot shift the original paid batch on replay.
    monkeypatch.setattr(apify_sync, 'list_accounts', lambda **kw: [{'handle': 'a'}, {'handle': 'new'}])
    result = ingestion_jobs.call('manual-test', lambda: apify_sync.snapshot_all_accounts(force=True), slot='request')
    assert result['snapshotted'] == ['a']
    assert len(starts) == 1
    row = db.get_account_snapshot_for_day('a', '2026-10-18')
    assert row['captured_at'] == '2026-10-18T06:00:00.123456+00:00'
    assert db.get_account_snapshot_for_day('a', '2026-10-19') is None


def test_older_recovered_reading_cannot_replace_newer_same_day_even_within_second(database):
    db.insert_account_snapshot('a', 200, 1, 'A', False, False, captured_at='2026-10-17T12:00:00.9Z')
    db.insert_account_snapshot('a', 100, 1, 'A', False, False, captured_at='2026-10-17T12:00:00.1Z')
    row = db.get_account_snapshot_for_day('a')
    assert row['followers_count'] == 200
    assert row['captured_at'] == '2026-10-17T12:00:00.900000+00:00'


def test_new_profile_actor_resumes_already_paid_generic_batch_without_starting_another(database, monkeypatch):
    import json
    import httpx
    old_payload = {'directUrls': ['https://www.instagram.com/a/'], 'resultsType': 'details'}
    state = {
        'tracker_snapshot_legacy_steps': [], 'tracker_snapshot_handles': ['a'],
        'tracker_snapshot_pending': ['a'], 'tracker_snapshot_batch:0': ['a'],
        'run:0': {'actor': apify_sync.APIFY_ACTOR_ID, 'payload': old_payload,
                  'run': {'id': 'paid-generic', 'status': 'RUNNING', 'defaultDatasetId': 'paid-data'}},
    }
    with database() as conn:
        ingestion_jobs.initialize(conn)
        conn.execute("INSERT INTO ingestion_jobs VALUES ('generic-recovery', 'request', 'retry', 'old', 0, ?, NULL, 'old')", (json.dumps(state),))
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.path == '/v2/actor-runs/paid-generic':
            return httpx.Response(200, json={'data': {
                'id': 'paid-generic', 'status': 'SUCCEEDED', 'defaultDatasetId': 'paid-data',
                'finishedAt': '2026-10-17T12:02:00Z',
            }})
        assert request.url.path == '/v2/datasets/paid-data/items'
        return httpx.Response(200, json=[{'username': 'a', 'followersCount': 101}])
    original_client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original_client(transport=httpx.MockTransport(respond)))
    monkeypatch.setenv('APIFY_TOKEN', 'fake-token')
    monkeypatch.setattr(apify_sync, 'list_accounts', lambda **kw: [{'handle': 'a'}])
    result = ingestion_jobs.call('generic-recovery', lambda: apify_sync.snapshot_all_accounts(force=True), slot='request')
    assert result == {'snapshotted': ['a'], 'failed': {}}
    assert len(requests) == 2 and all(request.method == 'GET' for request in requests)
    assert db.get_account_snapshot_for_day('a')['followers_count'] == 101


def test_scheduler_snapshot_uses_a_durable_job(database, monkeypatch):
    from app import slack_alerts
    monkeypatch.setattr(slack_alerts, 'slack_configured', lambda: False)
    seen = []
    def snapshot():
        seen.append(ingestion_jobs.current().key)
        return {'snapshotted': ['a'], 'failed': {}}
    monkeypatch.setattr(apify_sync, 'snapshot_all_accounts', snapshot)
    assert scheduler._run_account_snapshot_job()['snapshotted'] == ['a']
    assert seen == ['scheduled-tracker-snapshot']


def test_recovered_previous_day_cannot_complete_todays_scheduler_marker(database, monkeypatch):
    from datetime import UTC, datetime
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 18, 15, tzinfo=UTC).astimezone(tz)
    monkeypatch.setattr(scheduler, 'datetime', FixedDateTime)
    monkeypatch.setattr(scheduler, '_active_account_handles', lambda: ['a'])
    monkeypatch.setattr(scheduler, '_launch', lambda name, work: work() if name == 'daily' else None)
    monkeypatch.setattr(ingestion_jobs, 'run', lambda *a, **kw: True)
    monkeypatch.setattr(scheduler, '_state_get', lambda key: '')
    monkeypatch.setattr(scheduler, '_run_account_snapshot_job', lambda: {'snapshotted': ['a'], 'failed': {}})
    completed = []
    monkeypatch.setattr(scheduler, '_claim_bucket', lambda key, day: completed.append(day))
    db.insert_account_snapshot('a', 100, 1, 'A', False, False, captured_at='2026-10-17T15:00:00Z')
    scheduler._tick()
    assert completed == []
    db.insert_account_snapshot('a', 200, 1, 'A', False, False, captured_at='2026-10-18T15:00:00Z')
    scheduler._tick()
    assert completed == ['2026-10-18']


def test_legacy_single_profile_journal_recovers_paid_rows_before_new_batch(database, monkeypatch):
    import json
    old_payload = lambda handle: {'directUrls': [f'https://www.instagram.com/{handle}/'], 'resultsType': 'details'}
    state = {
        'run:0': {'actor': apify_sync.APIFY_ACTOR_ID, 'payload': old_payload('a')},
        'run:1': {'actor': apify_sync.APIFY_ACTOR_ID, 'payload': old_payload('a'),
                  'run': {'id': 'paid-a', 'startedAt': '2026-10-17T12:01:00Z'},
                  'items': [{'username': 'a', 'followersCount': 101}]},
        'run:2': {'actor': apify_sync.APIFY_ACTOR_ID, 'payload': old_payload('b'),
                  'run': {'id': 'paid-b', 'startedAt': '2026-10-17T12:01:10Z'}},
    }
    with database() as conn:
        ingestion_jobs.initialize(conn)
        conn.execute("INSERT INTO ingestion_jobs VALUES ('legacy', '2026-10-17', 'retry', 'old', 0, ?, NULL, 'old')", (json.dumps(state),))
    monkeypatch.setattr(apify_sync, 'list_accounts', lambda **kw: [{'handle': h} for h in ['a', 'b', 'c', 'd']])
    starts = []
    fail = [True]
    def actor(payload, **kw):
        journal = ingestion_jobs.current()
        saved = journal.next_run(kw['actor_id'], payload)
        if saved.get('items') is not None:
            return saved['items']
        if saved.get('run', {}).get('id') == 'paid-b':
            saved['items'] = [{'username': 'b', 'followersCount': 202}]
        else:
            # Legacy run positions must never consume the new roster batch.
            assert journal.index == 4
            assert kw['actor_id'] == apify_sync.APIFY_PROFILE_ACTOR_ID
            assert payload == {'usernames': ['c', 'd']}
            if 'run' not in saved:
                starts.append(payload)
                saved['run'] = {'id': 'paid-new-batch'}
                journal.save()
            if fail[0]:
                fail[0] = False
                raise RuntimeError('one transient polling failure')
            saved['items'] = [{'username': 'c', 'followersCount': 303}, {'username': 'd', 'followersCount': 404}]
        saved['finished_at'] = '2026-10-17T12:02:00Z'
        journal.save()
        return saved['items']
    monkeypatch.setattr(apify_sync, '_run_apify_actor_and_fetch', actor)
    with pytest.raises(ingestion_jobs.IngestionPending):
        ingestion_jobs.call('legacy', lambda: apify_sync.snapshot_all_accounts(force=True), slot='2026-10-17')
    with database() as conn:
        conn.execute('UPDATE ingestion_jobs SET lease_until=0')
    result = ingestion_jobs.call('legacy', lambda: apify_sync.snapshot_all_accounts(force=True), slot='2026-10-17')
    assert result == {'snapshotted': ['a', 'b', 'c', 'd'], 'failed': {}}
    assert len(starts) == 1
    assert [db.get_account_snapshot_for_day(handle)['followers_count'] for handle in ['a', 'b', 'c', 'd']] == [101, 202, 303, 404]
