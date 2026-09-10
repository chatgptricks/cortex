from contextlib import contextmanager
import sqlite3

from app import db
from app import post_refresh_queue as queue


def test_enqueue_deduplicates_active_job_and_requeues_finished_job(tmp_path, monkeypatch):
    path = tmp_path / "refresh.sqlite3"

    @contextmanager
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(db, "connect", connect)
    first = queue.enqueue(account="@chatgptricks", shortcode="ABC")
    second = queue.enqueue(account="chatgptricks", shortcode="ABC")
    assert second["job_id"] == first["job_id"]
    assert second["status"] == "queued"

    with connect() as connection:
        connection.execute(
            "UPDATE post_refresh_jobs SET status = 'done', result_json = ? WHERE job_id = ?",
            ('{"likes": 42}', first["job_id"]),
        )
    third = queue.enqueue(account="chatgptricks", shortcode="ABC")
    assert third["job_id"] == first["job_id"]
    assert third["status"] == "queued"
    assert third["result"] == {}


def test_worker_persists_successful_result(tmp_path, monkeypatch):
    path = tmp_path / "refresh.sqlite3"

    @contextmanager
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr("app.apify_sync.refresh_single_post", lambda account, shortcode: {
        "account": account, "shortcode": shortcode, "likes": 42,
    })
    task = queue.enqueue(account="chatgptricks", shortcode="ABC")
    queue._run(task)
    finished = queue.get(task["job_id"])
    assert finished["status"] == "done"
    assert finished["result"]["likes"] == 42
