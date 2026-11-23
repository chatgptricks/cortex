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
MAX_PROVIDER_RETRY_SECONDS = 30 * 60
PROVIDER_RETRY_REASONS = {
    'apify_auth', 'apify_quota', 'apify_forbidden', 'apify_unavailable', 'apify_pending',
}
_AUTH_ERROR_TYPES = {'invalid-token', 'invalid-credentials', 'missing-api-token',
                     'token-not-provided', 'user-or-token-not-found'}
_QUOTA_ERROR_TYPES = {
    'not-enough-usage-to-run-paid-actor', 'monthly-usage-limit-too-low',
    'apify-plan-required-to-use-paid-actor', 'user-problem-with-card',
    'failed-to-charge-user', 'missing-billing-info', 'no-payment-method-available',
    'user-has-no-subscription',
}


def _exception_chain(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _apify_http_error(exc):
    import httpx

    for cause in _exception_chain(exc):
        if isinstance(cause, httpx.HTTPError):
            try:
                if cause.request.url.host == 'api.apify.com':
                    return cause
            except RuntimeError:
                pass
    return None


def safe_error_text(value, *, secrets=(), limit=500):
    """Keep diagnostic text without credentials, URLs or Slack markup."""
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        value = value.replace(secret, '[redacted]')
    value = re.sub(r'apify_api_[A-Za-z0-9_-]+', '[redacted]', value)
    value = re.sub(r'(?i)\bbearer\s+\S+', 'Bearer [redacted]', value)
    value = re.sub(r'''(?ix)\b(token|api[_-]?key|authorization|cookie|set-cookie|sessionid|csrftoken)\b
                      \s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)''', r'\1=[redacted]', value)
    value = re.sub(r'''https?://[^\s<>"']+''', '[redacted-url]', value, flags=re.I)
    value = re.sub(r'[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f]', ' ', value)
    value = value.translate(str.maketrans({'<': '(', '>': ')', '`': "'", '&': ' and '}))
    return ' '.join(value.split())[:limit]


def apify_error_details(exc):
    """Read only bounded scalar fields from Apify's documented error object."""
    import httpx

    failure = _apify_http_error(exc)
    if not isinstance(failure, httpx.HTTPStatusError):
        return {}
    try:
        # Do not decode or retain arbitrary provider bodies (e.g. gateway HTML).
        if len(failure.response.content) > 64 * 1024:
            return {}
        body = failure.response.json()
    except (ValueError, httpx.ResponseNotRead):
        return {}
    error = body.get('error') if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return {}
    result = {}
    error_type = error.get('type')
    if isinstance(error_type, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,119}', error_type):
        result['type'] = error_type
    message = error.get('message')
    if isinstance(message, str):
        authorization = failure.request.headers.get('authorization', '')
        secrets = [failure.request.url.params.get('token', '')]
        if authorization.lower().startswith('bearer '):
            secrets.append(authorization[7:].strip())
        result['message'] = safe_error_text(message, secrets=secrets)
    return result


def classify_apify_failure(exc):
    """Separate provider outages/unfinished paid work from poison input.

    ApifySyncError wraps HTTPX failures, while reconciliation can raise them
    directly. Inspect the chain and request host instead of matching error
    text (a database or unrelated HTTP failure still uses the attempt cap).
    """
    import httpx

    for cause in _exception_chain(exc):
        reason = getattr(cause, 'ingestion_retry_reason', None)
        if reason in PROVIDER_RETRY_REASONS:
            return reason
    failure = _apify_http_error(exc)
    if isinstance(failure, httpx.HTTPStatusError):
        status = failure.response.status_code
        details = apify_error_details(failure)
        error_type = details.get('type')
        if status == 401 or (status == 403 and error_type in _AUTH_ERROR_TYPES):
            return 'apify_auth'
        if status == 402 or (status == 403 and error_type in _QUOTA_ERROR_TYPES):
            return 'apify_quota'
        # These generic types also cover memory/concurrency restrictions. Only
        # explicit account-spending wording can identify them as a billing limit.
        if status == 403 and error_type in {None, 'limit-reached', 'platform-feature-disabled'}:
            message = details.get('message', '').lower()
            if re.search(r'(?:monthly|account) (?:usage|spending).*(?:limit|exceed|reach|exhaust)|'
                         r'(?:monthly|spending) limit|'
                         r'insufficient (?:funds|credits?|balance)|not enough (?:usage|credits?|funds)|'
                         r'(?:credits?|balance) (?:is |are )?exhausted', message):
                return 'apify_quota'
        if status == 403:
            return 'apify_forbidden'
        if status in {408, 429} or 500 <= status < 600:
            return 'apify_unavailable'
    elif isinstance(failure, httpx.TransportError):
        return 'apify_unavailable'
    return None


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


def run(key, slot, callback, *, retain_result=False, max_attempts=None):
    """Run `callback` for `slot` under a durable lease and journal.

    With `max_attempts`, a slot that keeps failing is abandoned instead of
    retried forever: it is marked done WITHOUT advancing `last_success_at`,
    so the next slot starts and its window covers the abandoned one. A
    deterministic failure can then delay a scheduled pass, never stop it.
    Provider outages and uncertain/unfinished paid runs retain their journal
    and retry with backoff without consuming that deterministic-failure cap.
    """
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
        result = callback()
        journal.save()
        # The watermark is the moment the collected data describes (the frozen
        # `now` of this slot), not when processing finished. A slot resumed
        # hours after a stall must not claim the gap as covered, or the next
        # discovery window starts after it and those posts are never fetched.
        completed_state = {"last_success_at": journal.state.get("now") or db.utc_now()}
        if retain_result:
            completed_state["result"] = result
        with db.connect() as conn:
            conn.execute("UPDATE ingestion_jobs SET status = 'done', lease_until = 0, error = NULL, state = ?, updated_at = ? WHERE job_key = ? AND owner = ?",
                         (json.dumps(completed_state), db.utc_now(), key, owner))
        return True
    except Exception as exc:
        error = safe_error_text(str(exc), limit=2000)
        provider_error = apify_error_details(exc)
        if provider_error:
            failure = _apify_http_error(exc)
            error = f'Apify HTTP {failure.response.status_code}'
            if provider_error.get('type'):
                error += f" ({provider_error['type']})"
            if provider_error.get('message'):
                error += f": {provider_error['message']}"
        journal.state.pop('provider_error', None)
        if provider_error:
            journal.state['provider_error'] = provider_error
        retry_reason = classify_apify_failure(exc)
        if retry_reason:
            failures = int(journal.state.get('transient_attempts') or 0) + 1
            journal.state['transient_attempts'] = failures
            journal.state['retry_reason'] = retry_reason
            journal.state.setdefault('retry_started_at', db.utc_now())
            delay = RETRY_SECONDS
            if retry_reason != 'apify_pending':
                delay = min(MAX_PROVIDER_RETRY_SECONDS, RETRY_SECONDS * 2 ** min(failures - 1, 10))
            with db.connect() as conn:
                conn.execute("UPDATE ingestion_jobs SET status = 'retry', lease_until = ?, error = ?, state = ?, updated_at = ? WHERE job_key = ? AND owner = ?",
                             (time.time() + delay, error, json.dumps(journal.state), db.utc_now(), key, owner))
            logging.warning('Ingestion %s waiting for %s; retry in %ss, journal retained: %s',
                            key, retry_reason, delay, error[:300])
            return False
        for field in ('transient_attempts', 'retry_reason', 'retry_started_at'):
            journal.state.pop(field, None)
        attempts = int(journal.state.get("attempts") or 0) + 1
        if max_attempts and attempts >= max_attempts:
            abandoned_state = {
                "last_success_at": journal.state.get("last_success_at"),
                "abandoned": {"attempts": attempts, "error": error[:500], "at": db.utc_now(),
                              "window_end": journal.state.get("now")},
            }
            with db.connect() as conn:
                conn.execute("UPDATE ingestion_jobs SET status = 'done', lease_until = 0, error = ?, state = ?, updated_at = ? WHERE job_key = ? AND owner = ?",
                             (error, json.dumps(abandoned_state), db.utc_now(), key, owner))
            logging.error('Ingestion %s abandoned after %s attempts; next slot covers its window: %s', key, attempts, error[:300])
            return False
        journal.state["attempts"] = attempts
        with db.connect() as conn:
            conn.execute("UPDATE ingestion_jobs SET status = 'retry', lease_until = ?, error = ?, state = ?, updated_at = ? WHERE job_key = ? AND owner = ?",
                         (time.time() + RETRY_SECONDS, error, json.dumps(journal.state), db.utc_now(), key, owner))
        logging.exception('Ingestion %s pending retry (attempt %s); saved runs retained', key, attempts)
        return False
    finally:
        _current.reset(token)
        stop.set()
        thread.join(timeout=2)


class IngestionPending(RuntimeError):
    """Paid work is retained and can be resumed without starting another run."""


def call(key, callback, *, slot=None):
    """Return a durable result for a manual operation.

    Explicit slots make queue redelivery idempotent. A new HTTP request gets a
    new slot, but an unfinished prior request always resumes its original run.
    Completed results are retained to cover a crash before queue/UI delivery.
    """
    requested_slot = slot or datetime.now(UTC).isoformat(timespec="microseconds")
    output = []

    def work():
        result = callback()
        output.append(result)
        return result

    if run(key, requested_slot, work, retain_result=True):
        return output[0]
    with db.connect() as conn:
        row = conn.execute("SELECT slot, status, state, error FROM ingestion_jobs WHERE job_key = ?", (key,)).fetchone()
    if row and row["status"] == "done" and row["slot"] == requested_slot:
        state = json.loads(row["state"])
        if "result" in state:
            return state["result"]
    raise IngestionPending(
        (row["error"] if row else None)
        or "Refresh is already running; retry shortly. The paid run is retained."
    )
