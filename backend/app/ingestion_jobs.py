"""Leased, resumable ingestion. A slot is complete only after DB writes succeed."""
from contextvars import ContextVar
from datetime import UTC, datetime
import json
import logging
import re
import threading
import time
import uuid

from . import db

_current = ContextVar('ingestion_job', default=None)
LEASE_SECONDS = 180
RETRY_SECONDS = 120


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS ingestion_jobs (
        job_key TEXT PRIMARY KEY, slot TEXT NOT NULL, status TEXT NOT NULL,
        owner TEXT NOT NULL, lease_until REAL NOT NULL, state TEXT NOT NULL,
        error TEXT, updated_at TEXT NOT NULL)''')


class Journal:
    def __init__(self, key, owner, state):
        self.key, self.owner, self.state = key, owner, state
        self.index = 0
        self.lost = threading.Event()

    def save(self):
        if self.lost.is_set():
            raise RuntimeError('Ingestion lease lost')
        with db.connect() as conn:
            changed = conn.execute(
                'UPDATE ingestion_jobs SET state = ?, updated_at = ? WHERE job_key = ? AND owner = ?',
                (json.dumps(self.state), db.utc_now(), self.key, self.owner)).rowcount
        if changed != 1:
            raise RuntimeError('Ingestion lease lost')

    def frozen(self, key, value):
        if key not in self.state:
            self.state[key] = value
            self.save()
        return self.state[key]

    def next_run(self, actor, payload):
        key = f'run:{self.index}'
        self.index += 1
        return self.frozen(key, {'actor': actor, 'payload': payload})


def current():
    return _current.get()


def now():
    journal = current()
    value = datetime.now(UTC)
    return datetime.fromisoformat(journal.frozen('now', value.isoformat())) if journal else value


def run(key, slot, callback):
    owner = uuid.uuid4().hex
    epoch = time.time()
    with db.connect() as conn:
        initialize(conn)
        claimed = conn.execute('''INSERT INTO ingestion_jobs
            (job_key, slot, status, owner, lease_until, state, updated_at)
            VALUES (?, ?, 'running', ?, ?, '{}', ?)
            ON CONFLICT(job_key) DO UPDATE SET
                slot = CASE WHEN ingestion_jobs.status = 'done' THEN excluded.slot ELSE ingestion_jobs.slot END,
                state = ingestion_jobs.state,
                status = 'running', owner = excluded.owner, lease_until = excluded.lease_until,
                updated_at = excluded.updated_at
            WHERE ingestion_jobs.lease_until <= ?
              AND (ingestion_jobs.status != 'done' OR ingestion_jobs.slot < excluded.slot)''',
            (key, slot, owner, epoch + LEASE_SECONDS, db.utc_now(), epoch)).rowcount
        if claimed != 1:
            return False
        row = conn.execute('SELECT state FROM ingestion_jobs WHERE job_key = ?', (key,)).fetchone()
    journal = Journal(key, owner, json.loads(row['state']))
    stop = threading.Event()

    def renew():
        while not stop.wait(30):
            try:
                with db.connect() as conn:
                    changed = conn.execute('UPDATE ingestion_jobs SET lease_until = ? WHERE job_key = ? AND owner = ?',
                        (time.time() + LEASE_SECONDS, key, owner)).rowcount
                if changed != 1:
                    journal.lost.set()
                    return
            except Exception:
                journal.lost.set()
                logging.exception('Ingestion lease renewal failed')
                return

    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    token = _current.set(journal)
    try:
        callback()
        journal.save()
        with db.connect() as conn:
            conn.execute("UPDATE ingestion_jobs SET status = 'done', lease_until = 0, error = NULL, state = ?, updated_at = ? WHERE job_key = ? AND owner = ?",
                         (json.dumps({"last_success_at": db.utc_now()}), db.utc_now(), key, owner))
        return True
    except Exception as exc:
        with db.connect() as conn:
            conn.execute("UPDATE ingestion_jobs SET status = 'retry', lease_until = ?, error = ?, updated_at = ? WHERE job_key = ? AND owner = ?",
                         (time.time() + RETRY_SECONDS, re.sub(r"token=[^&\s]+", "token=REDACTED", str(exc))[:2000], db.utc_now(), key, owner))
        logging.exception('Ingestion %s pending retry; saved runs retained', key)
        return False
    finally:
        _current.reset(token)
        stop.set()
        thread.join(timeout=2)
