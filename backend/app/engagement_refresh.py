"""Budgeted refresh of overdue post metrics; all paid starts use durable journals."""
from datetime import UTC, datetime, timedelta, timezone
import json
import logging
import math
import os
import uuid

from . import db

logger = logging.getLogger('uvicorn.error')
_CST = timezone(timedelta(hours=-6))


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS engagement_observations (
        shortcode TEXT PRIMARY KEY, observed_at TEXT NOT NULL, raw_json TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS engagement_budget (
        day TEXT PRIMARY KEY, reserved_items INTEGER NOT NULL DEFAULT 0,
        reserved_milliusd INTEGER NOT NULL DEFAULT 0)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS engagement_reservations (
        reservation_key TEXT PRIMARY KEY, day TEXT NOT NULL, selection TEXT NOT NULL,
        max_charge_milliusd INTEGER NOT NULL)''')


def observe(conn, shortcode, item, observed_at):
    # Keep the entire paid response, including views/audio/carousel metadata.
    initialize(conn)
    conn.execute('''INSERT INTO engagement_observations(shortcode, observed_at, raw_json)
        VALUES (?, ?, ?) ON CONFLICT(shortcode) DO UPDATE SET
        observed_at = excluded.observed_at, raw_json = excluded.raw_json
        WHERE engagement_observations.observed_at <= excluded.observed_at''',
        (shortcode, observed_at, json.dumps(item)))


def cached(shortcode, max_age_minutes=15):
    from .apify_sync import _post_age_hours
    with db.connect() as conn:
        initialize(conn)
        row = conn.execute('SELECT observed_at, raw_json FROM engagement_observations WHERE shortcode = ?',
                           (shortcode,)).fetchone()
    if row:
        age = _post_age_hours(row['observed_at'], datetime.now(UTC))
        if age is not None and 0 <= age < max_age_minutes / 60:
            item = json.loads(row['raw_json'])
            if item.get('shortCode') == shortcode:
                return item, row['observed_at']
    return None


def attach_freshness(posts):
    codes = list({p.get('shortcode') for p in posts if p.get('shortcode')})
    stamps = {}
    with db.connect() as conn:
        initialize(conn)
        for start in range(0, len(codes), 200):
            batch = codes[start:start + 200]
            placeholders = ','.join('?' for _ in batch)
            rows = conn.execute(f'SELECT shortcode, observed_at FROM engagement_observations WHERE shortcode IN ({placeholders})', batch).fetchall()
            stamps.update({r['shortcode']: r['observed_at'] for r in rows})
    for post in posts:
        post['likesUpdatedAt'] = stamps.get(post.get('shortcode'))


def candidates(accounts, now):
    from .apify_sync import get_account_config, _account_scope, _post_age_hours
    selected = {}
    configs = {}
    cutoff = (now - timedelta(days=7)).isoformat()
    with db.connect() as conn:
        initialize(conn)
        stamps = {r['shortcode']: r['observed_at'] for r in conn.execute(
            'SELECT shortcode, observed_at FROM engagement_observations WHERE observed_at >= ?', (cutoff,)).fetchall()}
        queued = {(r['post_account'], r['post_shortcode']) for r in conn.execute(
            "SELECT post_account, post_shortcode FROM queue_requests WHERE status NOT IN ('closed', 'cancelled') AND is_custom = 0"
        ).fetchall()}
        for account in accounts:
            cfg = get_account_config(account)
            configs[account] = cfg
            table = cfg['table']
            scope, params = _account_scope(table, account)
            rows = conn.execute(f'''SELECT id, shortcode, published_at, is_hot, refreshed_8h
                FROM {table} WHERE published_at >= ? AND is_deleted = 0{scope}''', [cutoff, *params]).fetchall()
            for row in rows:
                code = row['shortcode']
                if not code or code.startswith('post-'):
                    continue
                age = _post_age_hours(row['published_at'], now)
                priority = bool(row['is_hot']) or (account, code) in queued
                if age is None or age < 0 or age > 168 or (age > 24 and not priority):
                    continue
                stamp = stamps.get(code)
                # Unknown freshness after rollout gets an actual measurement.
                overdue = _post_age_hours(stamp, now) if stamp else None
                interval = 1 if age < 8 else 3
                finalize = 8 <= age <= 24 and not bool(row['refreshed_8h'])
                # If a post was already read after 8h with hidden likes, wait
                # for its normal interval instead of retrying it every hour.
                finalize_due = finalize and (overdue is None or age - overdue < 8)
                if overdue is not None and overdue < interval and not finalize_due:
                    continue
                # A very overdue ordinary post eventually outranks a priority post.
                score = (overdue if overdue is not None else interval) / interval + (2 if priority else 0) + (2 if finalize_due else 0)
                target = selected.setdefault(code, {'shortcode': code, 'targets': [], 'score': score})
                target['score'] = max(score, target['score'])
                target['targets'].append(account)
    return sorted(selected.values(), key=lambda c: (-c['score'], c['shortcode'])), configs


def reserve(selection, now, key):
    """Reserve the full possible charge before POST; crashes/retries never refund it."""
    day = now.astimezone(_CST).strftime('%Y-%m-%d')
    item_limit = max(0, int(os.getenv('SENTIENT_ENGAGEMENT_DAILY_ITEMS', '250')))
    money_limit = max(0, round(float(os.getenv('SENTIENT_ENGAGEMENT_DAILY_USD', '0.50')) * 1000))
    batch_limit = max(0, min(100, int(os.getenv('SENTIENT_ENGAGEMENT_BATCH_ITEMS', '25'))))
    with db.connect() as conn:
        initialize(conn)
        existing = conn.execute('SELECT selection, max_charge_milliusd FROM engagement_reservations WHERE reservation_key = ?', (key,)).fetchone()
        if existing:
            return json.loads(existing['selection']), existing['max_charge_milliusd']
        conn.execute('INSERT INTO engagement_budget(day) VALUES (?) ON CONFLICT(day) DO NOTHING', (day,))
        budget = conn.execute('SELECT reserved_items, reserved_milliusd FROM engagement_budget WHERE day = ?', (day,)).fetchone()
        # Pace the daily allowance so morning traffic cannot consume the whole day.
        hourly_limit = math.ceil(money_limit / 24 / 3) if money_limit else 0
        count = min(len(selection), batch_limit, hourly_limit, max(0, item_limit - budget['reserved_items']), max(0, (money_limit - budget['reserved_milliusd']) // 3))
        picked = selection[:count]
        charge = count * 3  # Up to $0.003 per URL; actual actor charge may be lower.
        changed = conn.execute('''UPDATE engagement_budget SET reserved_items = reserved_items + ?,
            reserved_milliusd = reserved_milliusd + ? WHERE day = ?
            AND reserved_items + ? <= ? AND reserved_milliusd + ? <= ?''',
            (count, charge, day, count, item_limit, charge, money_limit)).rowcount
        if changed != 1:
            return [], 0
        conn.execute('INSERT INTO engagement_reservations VALUES (?, ?, ?, ?)',
                     (key, day, json.dumps(picked), charge))
    return picked, charge


def run_cycle(accounts):
    from . import apify_sync as sync, ingestion_jobs as jobs
    now = jobs.now()
    journal = jobs.current()
    if journal and 'engagement_selection' in journal.state:
        selection, configs = journal.state['engagement_selection'], journal.state['engagement_configs']
    else:
        selection, configs = candidates(accounts, now)
        if journal:
            journal.frozen('engagement_configs', configs)
            selection = journal.frozen('engagement_selection', selection)
    key = f'{journal.key}:{now.isoformat()}' if journal else uuid.uuid4().hex
    picked, charge = reserve(selection, now, key)
    summary = {'eligible': len(selection), 'requested': len(picked), 'updated': 0,
               'reserved_usd': charge / 1000, 'deferred': len(selection) - len(picked), 'commit': os.getenv('RENDER_GIT_COMMIT')}
    if not picked:
        logger.info('Budgeted engagement: %s', summary)
        return summary
    items = sync._run_apify_actor_and_fetch(
        {'directUrls': [f"https://www.instagram.com/p/{p['shortcode']}/" for p in picked], 'resultsType': 'details'},
        max_wait_seconds=900, run_limits={'maxTotalChargeUsd': charge / 1000}, accept_partial=True)
    by_code = {it.get('shortCode'): it for it in items if it.get('shortCode')}
    failures = {}
    for account, cfg in configs.items():
        account_items = [by_code[p['shortcode']] for p in picked if account in p['targets'] and p['shortcode'] in by_code]
        if not account_items:
            continue
        try:
            result = sync._process_short_term_items(account, cfg, account_items, now,
                lookback_hours=168, insert_new=False, finalize_after_hours=8,
                # First-eight-hours snapshot must never use a days-old reading.
                refresh_window_hours=None, hot_check_window_hours=8)
            summary['updated'] += result['engagement']['updated']
        except Exception as exc:
            failures[account] = str(exc)
    if failures:
        # Retry the saved dataset rather than buy it again.
        raise sync.ApifySyncError(f'Budgeted engagement persistence failed: {failures}')
    sync._reconcile_queue_hot()
    logger.info('Budgeted engagement: %s', summary)
    return summary


def status():
    from . import ingestion_jobs as jobs
    day = datetime.now(_CST).strftime('%Y-%m-%d')
    with db.connect() as conn:
        initialize(conn)
        jobs.initialize(conn)
        budget = conn.execute('SELECT reserved_items, reserved_milliusd FROM engagement_budget WHERE day = ?', (day,)).fetchone()
        job = conn.execute("SELECT status, state, error, updated_at FROM ingestion_jobs WHERE job_key = 'scheduled-engagement-adaptive'").fetchone()
        observation = conn.execute('SELECT COUNT(*) AS total, MAX(observed_at) AS latest FROM engagement_observations').fetchone()
    return {'day': day, 'daily_usd_limit': float(os.getenv('SENTIENT_ENGAGEMENT_DAILY_USD', '0.50')),
        'daily_item_limit': int(os.getenv('SENTIENT_ENGAGEMENT_DAILY_ITEMS', '250')),
        'reserved_usd': budget['reserved_milliusd'] / 1000 if budget else 0,
        'reserved_items': budget['reserved_items'] if budget else 0,
        'observations': dict(observation),
        'last_job': {**dict(job), 'state': json.loads(job['state'])} if job else None}
