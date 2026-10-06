import sqlite3
from contextlib import contextmanager
import time
import pytest
from app import db, apify_sync, sentient_ocr, media_storage, scheduler, ingestion_jobs


@pytest.fixture
def queue(tmp_path, monkeypatch):
    @contextmanager
    def connect():
        conn = sqlite3.connect(tmp_path / 'queue.sqlite', timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    monkeypatch.setattr(db, 'connect', connect)
    with connect() as conn:
        conn.execute('CREATE TABLE dashboard_posts (id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT, cover_image_path TEXT, cover_source_url TEXT, hook_text TEXT, ocr_checked INTEGER DEFAULT 0, published_at TEXT, updated_at TEXT)')
        db._ensure_ocr_queue_schema(conn)
        db._ensure_ocr_queue_schema(conn)
        for i in range(1, 9):
            conn.execute('INSERT INTO dashboard_posts (id, account, shortcode, cover_image_path, published_at) VALUES (?, ?, ?, ?, ?)',
                         (i, 'test', str(i), str(tmp_path / f'{i}.jpg'), f'2026-10-{i:02d}'))
    monkeypatch.setattr(media_storage, 'materialize_local_path', lambda p: __import__('pathlib').Path(p))
    monkeypatch.setattr(media_storage, 'cleanup_materialized_path', lambda p: None)
    monkeypatch.setattr(sentient_ocr, 'extract_images_text_sentient', lambda paths: [{'filename': p.name, 'text': 'hook'} for p in paths])
    return connect


def test_old_new_and_expired_claims(queue):
    with queue() as conn:
        conn.execute("UPDATE dashboard_posts SET ocr_checked=2, ocr_owner='dead', ocr_lease_until=0 WHERE id=1")
        conn.execute("UPDATE dashboard_posts SET ocr_checked=2, ocr_owner='live', ocr_lease_until=? WHERE id=8", (time.time()+600,))
    result = apify_sync.run_ocr_sweep(4)
    assert result['with_text'] == 4
    with queue() as conn:
        assert [r['id'] for r in conn.execute('SELECT id FROM dashboard_posts WHERE ocr_checked=1 ORDER BY id')] == [1, 2, 6, 7]
        assert conn.execute('SELECT ocr_owner FROM dashboard_posts WHERE id=8').fetchone()[0] == 'live'


def test_bad_response_retries_then_stops(queue, monkeypatch):
    monkeypatch.setattr(sentient_ocr, 'extract_images_text_sentient', lambda paths: [])
    for attempt in range(5):
        result = apify_sync.run_ocr_sweep(8)
        assert result['retried'] == 8
        with queue() as conn:
            rows = conn.execute('SELECT * FROM dashboard_posts').fetchall()
            assert all(r['ocr_checked'] == (3 if attempt == 4 else 0) for r in rows)
            assert all(r['ocr_attempts'] == attempt + 1 and r['ocr_owner'] is None for r in rows)
        assert apify_sync.run_ocr_sweep(8)['retried'] == 0
        with queue() as conn:
            conn.execute('UPDATE dashboard_posts SET ocr_retry_at=0')


def test_blank_success_is_not_reprocessed(queue, monkeypatch):
    monkeypatch.setattr(sentient_ocr, 'extract_images_text_sentient', lambda paths: [{'filename': p.name, 'text': None} for p in paths])
    assert apify_sync.run_ocr_sweep(8)['sent'] == 8
    assert apify_sync.run_ocr_sweep(8)['sent'] == 0


def test_download_failure_is_retryable(queue, monkeypatch):
    monkeypatch.setattr(media_storage, 'materialize_local_path', lambda p: None)
    monkeypatch.setattr(apify_sync, 'ensure_cover', lambda *a: None)
    assert apify_sync.run_ocr_sweep(8)['retried'] == 8
    with queue() as conn:
        assert all(r['ocr_checked'] == 0 and r['ocr_error'] == 'cover_unavailable'
                   for r in conn.execute('SELECT * FROM dashboard_posts'))


def test_ocr_runs_when_post_collection_does_not(monkeypatch):
    callbacks = {}
    monkeypatch.setattr(scheduler, '_launch', lambda name, callback: callbacks.setdefault(name, callback))
    monkeypatch.setattr(ingestion_jobs, 'run', lambda key, slot, callback, **kw: callback() if key == 'scheduled-ocr' else False)
    ran = []
    monkeypatch.setattr(scheduler, '_run_ocr_job', lambda: ran.append(True))
    scheduler._tick()
    callbacks['short']()
    assert ran == []
    callbacks['ocr']()
    assert ran == [True]


def test_concurrent_sweeps_do_not_duplicate_covers(queue, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    seen = []
    lock = threading.Lock()
    def extract(paths):
        with lock:
            seen.extend(p.name for p in paths)
        time.sleep(.03)
        return [{'filename': p.name, 'text': 'hook'} for p in paths]
    monkeypatch.setattr(sentient_ocr, 'extract_images_text_sentient', extract)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: apify_sync.run_ocr_sweep(8), range(2)))
    assert len(seen) == len(set(seen)) == 8


def test_status_includes_automatic_queue(queue, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "connect", queue)
    temp_ocr_status = main.temp_ocr_status
    with queue() as conn:
        conn.execute('UPDATE dashboard_posts SET ocr_checked=3 WHERE id=1')
        conn.execute('UPDATE dashboard_posts SET ocr_retry_at=? WHERE id=2', (time.time()+600,))
    status = temp_ocr_status()
    assert status['failed'] == 1
    assert status['retry_waiting'] == 1
    assert status['automatic'] is True
    assert status['oldest_pending_at'] == '2026-10-02'
