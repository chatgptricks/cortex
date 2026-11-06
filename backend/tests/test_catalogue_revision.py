"""Research cache versions survive deployment and are shared across workers."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from textwrap import dedent

import pytest
from starlette.requests import Request

from app import main
from app.catalogue_revision import advance_generation, read_generation


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    path = tmp_path / "catalogue.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE posts (id INTEGER PRIMARY KEY);
            CREATE TABLE dashboard_posts (id INTEGER PRIMARY KEY);
            CREATE TABLE queue_requests (id INTEGER PRIMARY KEY);
            CREATE TABLE scheduler_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO posts VALUES (4);
            INSERT INTO dashboard_posts VALUES (12);
        """)

    @contextmanager
    def connect():
        with sqlite3.connect(path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            yield conn

    monkeypatch.setattr(main, "connect", connect)
    monkeypatch.setattr(main, "_DASHBOARD_POSTS_CACHE_CONTENT", b"old catalogue")
    monkeypatch.setattr(main, "_DASHBOARD_POSTS_CACHE_EXPIRES_AT", 10000)
    return path, connect


def request(etag=""):
    return Request({"type": "http", "headers": [(b"if-none-match", etag.encode())]})


def worker(path, *, invalidate=False, etag=""):
    # Import in a completely separate interpreter: a process-global version
    # counter would return to zero here after the first mutation.
    script = dedent("""
        import json, sqlite3, sys
        from contextlib import contextmanager
        from starlette.requests import Request
        from app import main
        @contextmanager
        def connect():
            with sqlite3.connect(sys.argv[1]) as conn:
                conn.row_factory = sqlite3.Row
                yield conn
        main.connect = connect
        if sys.argv[2] == 'invalidate':
            main._invalidate_dashboard_posts_cache()
        response = main.dashboard_posts_manifest(Request({
            'type': 'http', 'headers': [(b'if-none-match', sys.argv[3].encode())],
        }))
        print(json.dumps({'status': response.status_code, 'etag': response.headers['etag'],
                          'manifest': json.loads(response.body) if response.body else None}))
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), "invalidate" if invalidate else "read", etag],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def test_restart_preserves_etag_and_other_workers_see_mutations(catalogue):
    path, _ = catalogue
    initial = main.dashboard_posts_manifest(request())
    assert json.loads(initial.body)["projectionGeneration"] == 0

    main._invalidate_dashboard_posts_cache()
    updated = main.dashboard_posts_manifest(request(initial.headers["etag"]))
    assert updated.status_code == 200
    assert json.loads(updated.body)["projectionGeneration"] == 1
    assert main._DASHBOARD_POSTS_CACHE_CONTENT is None

    restarted = worker(path, etag=updated.headers["etag"])
    assert restarted["status"] == 304
    assert restarted["etag"] == updated.headers["etag"]

    other_worker = worker(path, invalidate=True, etag=updated.headers["etag"])
    assert other_worker["status"] == 200
    assert other_worker["manifest"]["projectionGeneration"] == 2
    # The original worker must not retain generation 1 in process memory.
    observed = main.dashboard_posts_manifest(request(updated.headers["etag"]))
    assert observed.status_code == 200
    assert observed.headers["etag"] == other_worker["etag"]
    assert worker(path, etag=other_worker["etag"])["status"] == 304


def test_manifest_reads_do_not_create_or_update_generation(catalogue, monkeypatch):
    _, connect = catalogue
    writes = []

    @contextmanager
    def read_only_connect():
        with connect() as conn:
            conn.execute("PRAGMA query_only = ON")
            yield conn
            writes.append(conn.total_changes)

    monkeypatch.setattr(main, "connect", read_only_connect)
    initial = main.dashboard_posts_manifest(request())
    assert main.dashboard_posts_manifest(request(initial.headers["etag"])).status_code == 304
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduler_state").fetchone()[0] == 0
        advance_generation(conn, "2026-01-01")
    current = main.dashboard_posts_manifest(request())
    with connect() as conn:
        # Audit metadata and unrelated scheduler activity do not expire data.
        conn.execute("UPDATE scheduler_state SET updated_at = '2026-12-01'")
        conn.execute("INSERT INTO scheduler_state VALUES ('last_daily_date', '2026-12-01', '2026-12-01')")
    assert main.dashboard_posts_manifest(request(current.headers["etag"])).status_code == 304
    assert writes == [0, 0, 0, 0]


def test_concurrent_worker_invalidations_are_atomic(catalogue):
    path, connect = catalogue
    script = dedent("""
        import sqlite3, sys
        from app.catalogue_revision import advance_generation
        for _ in range(15):
            with sqlite3.connect(sys.argv[1], timeout=30) as conn:
                advance_generation(conn, '2026-11-06')
    """)
    workers = [subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for _ in range(4)]
    for process in workers:
        _, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
    with connect() as conn:
        assert read_generation(conn) == 60


def test_failed_transaction_does_not_publish_a_new_generation(catalogue):
    _, connect = catalogue
    with pytest.raises(RuntimeError):
        with connect() as conn:
            advance_generation(conn, "2026-11-06")
            raise RuntimeError("mutation rolled back")
    with connect() as conn:
        assert read_generation(conn) == 0


def test_generation_remains_exact_for_browser_numbers(catalogue):
    _, connect = catalogue
    with connect() as conn:
        conn.execute("INSERT INTO scheduler_state VALUES ('dashboard_catalogue_generation', '9007199254740990', '')")
        advance_generation(conn, "2026-11-06")
    with connect() as conn:
        assert read_generation(conn) == 9_007_199_254_740_991
        with pytest.raises(ValueError, match="cannot be advanced safely"):
            advance_generation(conn, "2026-11-06")
        assert read_generation(conn) == 9_007_199_254_740_991


def test_revision_and_source_bound_checks_still_reject_stale_pages(catalogue):
    _, connect = catalogue
    initial = main._dashboard_catalogue_manifest()
    main._invalidate_dashboard_posts_cache()
    with pytest.raises(main.HTTPException) as stale:
        main.dashboard_posts_page("canonical", 0, 10, initial["revision"])
    assert stale.value.status_code == 409
    with connect() as conn:
        conn.execute("DELETE FROM posts")
    with pytest.raises(main.HTTPException) as rolled_back:
        main.dashboard_posts_page("canonical", 0, 10, initial["revision"], after_id=0, until_id=4)
    assert rolled_back.value.status_code == 409
