"""Shared append-only topic membership. Existing posts are never reclassified."""
import json
import re
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import datetime

from .db import connect

STOP = set('the a an and or of to in on for with from by at is are was were be been this that these those it its as but you your we our they their has have had just new now more most how what when who why can could will would says said than into about after before all not only one out over up so do does did using use used follow swipe comment link bio ai de la el los las un una unos unas y o en con por para del al es son fue ser como que se su sus este esta esto lo le te tu tus ha han mas muy ya pero si no sobre entre hoy nuevo nueva aqui'.split())

def initialize(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS topic_stack_lock (id INTEGER PRIMARY KEY)')
    conn.execute('INSERT INTO topic_stack_lock (id) VALUES (1) ON CONFLICT (id) DO NOTHING')
    conn.execute('CREATE TABLE IF NOT EXISTS topic_stack_members (post_key TEXT PRIMARY KEY, stack_id TEXT NOT NULL, words TEXT NOT NULL, posted_at REAL NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS topic_stack_members_group ON topic_stack_members(stack_id)')

def lock(conn):
    initialize(conn)
    # Serialize ingestion and manual merges across API processes as well.
    conn.execute('UPDATE topic_stack_lock SET id = 1 WHERE id = 1')

def key(post):
    return f"{post.get('account', '')}:{post.get('shortcode') or post.get('rank', '')}"

def words(post):
    text = unicodedata.normalize('NFD', str(post.get('caption') or ''))
    text = ''.join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r'https?://\S+|[@#][\w.]+', ' ', text)
    return list(dict.fromkeys(w for w in re.split(r'\W+', text) if len(w) > 2 and w not in STOP))[:65]

def timestamp(post):
    try:
        return datetime.fromisoformat(post.get('postDate', '').replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return 0

def attach(posts):
    """Check unseen keys only; likes, reloads and filters cannot alter membership."""
    with connect() as conn:
        lock(conn)
        rows = [dict(row) for row in conn.execute('SELECT post_key, stack_id FROM topic_stack_members').fetchall()]
        membership = {row['post_key']: row['stack_id'] for row in rows}
        unseen = list({key(post): post for post in posts if key(post) not in membership}.values())
        if unseen:
            representatives = {}
            for row in conn.execute('SELECT post_key, stack_id, words, posted_at FROM topic_stack_members ORDER BY post_key').fetchall():
                representatives.setdefault(row['stack_id'], (set(json.loads(row['words'])), row['posted_at']))
            docs = [(post, words(post)) for post in sorted(unseen, key=key)]
            frequency = Counter(w for ws, _ in representatives.values() for w in ws)
            frequency.update(w for _, ws in docs for w in ws)
            index = defaultdict(list)
            def index_group(group, ws):
                for word in sorted(ws, key=lambda w: (frequency[w], w))[:8]:
                    if len(index[word]) < 400:
                        index[word].append(group)
            for group, (ws, _) in representatives.items():
                index_group(group, ws)
            additions = []
            for post, ws in docs:
                ws = set(ws); date = timestamp(post); winner = None; best = 0
                candidates = {group for word in sorted(ws, key=lambda w: (frequency[w], w))[:8] for group in index[word]} if len(ws) >= 6 else set()
                for group in sorted(candidates):
                    other, other_date = representatives[group]
                    shared = len(ws & other)
                    score = shared / len(ws | other) if ws | other else 0
                    coverage = shared / min(len(ws), len(other)) if ws and other else 0
                    if shared >= 6 and (score >= .58 or coverage >= .85 and shared >= 10) and (abs(date - other_date) <= 14 * 86400 or score >= .9) and score > best:
                        winner, best = group, score
                if winner is None:
                    winner = uuid.uuid4().hex
                    representatives[winner] = (ws, date)
                    index_group(winner, ws)
                post_key = key(post)
                additions.append((post_key, winner, json.dumps(sorted(ws)), date))
                membership[post_key] = winner
            conn.executemany('INSERT INTO topic_stack_members (post_key, stack_id, words, posted_at) VALUES (?, ?, ?, ?)', additions)
        counts = Counter(membership.values())
        for post in posts:
            group = membership[key(post)]
            post['stackId'] = group
            post['stackSize'] = counts[group]

def merge(keys):
    keys = list(dict.fromkeys(keys))
    if not 2 <= len(keys) <= 500 or any(not isinstance(k, str) or len(k) > 300 for k in keys):
        raise ValueError('Select between 2 and 500 posts.')
    with connect() as conn:
        lock(conn)
        placeholders = ','.join('?' for _ in keys)
        rows = conn.execute(f'SELECT post_key, stack_id FROM topic_stack_members WHERE post_key IN ({placeholders})', tuple(keys)).fetchall()
        if len(rows) != len(keys):
            raise ValueError('Some posts are no longer available. Refresh and try again.')
        groups = sorted(set(row['stack_id'] for row in rows))
        destination = groups[0]
        marks = ','.join('?' for _ in groups)
        conn.execute(f'UPDATE topic_stack_members SET stack_id = ? WHERE stack_id IN ({marks})', (destination, *groups))
        members = [row['post_key'] for row in conn.execute('SELECT post_key FROM topic_stack_members WHERE stack_id = ? ORDER BY post_key', (destination,)).fetchall()]
        return {'stackId': destination, 'postKeys': members, 'stackSize': len(members)}

def separate(keys):
    """Give selected posts their own durable stacks without reclassifying them."""
    keys = list(dict.fromkeys(keys))
    if not 1 <= len(keys) <= 500 or any(not isinstance(k, str) or len(k) > 300 for k in keys):
        raise ValueError('Select between 1 and 500 posts.')
    with connect() as conn:
        lock(conn)
        marks = ','.join('?' for _ in keys)
        rows = conn.execute(f'SELECT post_key, stack_id FROM topic_stack_members WHERE post_key IN ({marks})', tuple(keys)).fetchall()
        if len(rows) != len(keys):
            raise ValueError('Some posts are no longer available. Refresh and try again.')
        affected = set(keys)
        for row in rows:
            conn.execute('UPDATE topic_stack_members SET stack_id = ? WHERE post_key = ?', (uuid.uuid4().hex, row['post_key']))
        groups = {row['stack_id'] for row in rows}
        group_marks = ','.join('?' for _ in groups)
        affected.update(row['post_key'] for row in conn.execute(f'SELECT post_key FROM topic_stack_members WHERE stack_id IN ({group_marks})', tuple(groups)).fetchall())
        return memberships(conn, affected)

def memberships(conn, keys):
    keys = sorted(set(keys))
    if not keys:
        return {'members': []}
    marks = ','.join('?' for _ in keys)
    rows = conn.execute(f'SELECT post_key, stack_id FROM topic_stack_members WHERE post_key IN ({marks})', tuple(keys)).fetchall()
    counts = Counter(row['stack_id'] for row in conn.execute('SELECT stack_id FROM topic_stack_members').fetchall())
    return {'members': [{'postKey': row['post_key'], 'stackId': row['stack_id'], 'stackSize': counts[row['stack_id']]} for row in rows]}

def find_similar(post_key):
    """User-triggered search across the stored topic signatures; never runs on reload."""
    if not isinstance(post_key, str) or not post_key or len(post_key) > 300:
        raise ValueError('Choose a valid post.')
    with connect() as conn:
        lock(conn)
        reference = conn.execute('SELECT stack_id, words FROM topic_stack_members WHERE post_key = ?', (post_key,)).fetchone()
        if not reference:
            raise ValueError('This post is no longer available. Refresh and try again.')
        reference_words = set(json.loads(reference['words']))
        matching_groups = set()
        for row in conn.execute('SELECT post_key, stack_id, words FROM topic_stack_members WHERE post_key != ?', (post_key,)).fetchall():
            other = set(json.loads(row['words']))
            shared = len(reference_words & other)
            score = shared / len(reference_words | other) if reference_words | other else 0
            coverage = shared / min(len(reference_words), len(other)) if reference_words and other else 0
            if shared >= 5 and (score >= .38 or coverage >= .70):
                matching_groups.add(row['stack_id'])
        if not matching_groups:
            result = memberships(conn, [post_key])
            result['matchedCount'] = 0
            return result
        matching_groups.add(reference['stack_id'])
        marks = ','.join('?' for _ in matching_groups)
        destination = sorted(matching_groups)[0]
        conn.execute(f'UPDATE topic_stack_members SET stack_id = ? WHERE stack_id IN ({marks})', (destination, *sorted(matching_groups)))
        members = [row['post_key'] for row in conn.execute('SELECT post_key FROM topic_stack_members WHERE stack_id = ? ORDER BY post_key', (destination,)).fetchall()]
        result = {'stackId': destination, 'postKeys': members, 'stackSize': len(members), 'matchedCount': len(members) - 1}
        return result
