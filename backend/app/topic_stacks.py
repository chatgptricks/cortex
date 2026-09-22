"""Shared append-only topic membership. Existing posts are never reclassified."""
import json
import logging
import os
import re
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .db import connect

logger = logging.getLogger(__name__)


class JevUnavailable(RuntimeError):
    """Find Similar cannot make a result without its semantic judge."""

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

def automatic_match(shared, score, coverage, age_seconds):
    """Use time proximity as evidence that two captions describe one story."""
    hours = age_seconds / 3600
    if hours <= 8:
        return shared >= 3 and (score >= .16 or coverage >= .45)
    if hours <= 24:
        return shared >= 4 and (score >= .22 or coverage >= .55)
    if hours <= 48:
        return shared >= 4 and (score >= .30 or coverage >= .65)
    if hours <= 72:
        return shared >= 5 and (score >= .40 or coverage >= .72)
    if hours <= 14 * 24:
        return shared >= 6 and (score >= .58 or coverage >= .85 and shared >= 10)
    return shared >= 6 and score >= .90

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
                candidates = {group for word in sorted(ws, key=lambda w: (frequency[w], w))[:8] for group in index[word]} if len(ws) >= 3 else set()
                for group in sorted(candidates):
                    other, other_date = representatives[group]
                    shared = len(ws & other)
                    score = shared / len(ws | other) if ws | other else 0
                    coverage = shared / min(len(ws), len(other)) if ws and other else 0
                    if automatic_match(shared, score, coverage, abs(date - other_date)) and score > best:
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

def apply_memberships(posts):
    """Project persisted stack memberships onto an API payload.

    This is deliberately read-only.  Ingestion calls :func:`attach` for a
    newly saved post; dashboard reads must never classify, merge, or otherwise
    alter an existing user's grouping just because somebody reloaded Research.
    """
    if not posts:
        return
    keys = [key(post) for post in posts]
    with connect() as conn:
        initialize(conn)
        rows = conn.execute('SELECT post_key, stack_id FROM topic_stack_members').fetchall()
    membership = {row['post_key']: row['stack_id'] for row in rows}
    counts = Counter(membership.values())
    for post, post_key in zip(posts, keys):
        stack_id = membership.get(post_key)
        # A legacy row that predates persistent stacks remains visibly usable
        # as a one-card group. It is only classified when a real ingestion or
        # explicit user action touches it.
        post['stackId'] = stack_id or post_key
        post['stackSize'] = counts.get(stack_id, 1)

def regroup_recent(posts, hours=72):
    """Reclassify only posts published inside the requested recent window."""
    if not 1 <= hours <= 168:
        raise ValueError('Choose a window between 1 and 168 hours.')
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    recent = [post for post in posts if timestamp(post) >= cutoff]
    recent_keys = [key(post) for post in recent]
    if not recent_keys:
        return {'hours': hours, 'postsProcessed': 0, 'stacks': 0, 'groupedPosts': 0}
    with connect() as conn:
        lock(conn)
        conn.executemany('DELETE FROM topic_stack_members WHERE post_key = ?', [(item,) for item in recent_keys])
    attach(recent)
    stacks = Counter(post['stackId'] for post in recent)
    return {
        'hours': hours,
        'postsProcessed': len(recent),
        'stacks': len(stacks),
        'groupedPosts': sum(size for size in stacks.values() if size > 1),
    }

def merge(keys):
    keys = list(dict.fromkeys(keys))
    if not 2 <= len(keys) <= 500 or any(not isinstance(k, str) or len(k) > 300 for k in keys):
        raise ValueError('Select between 2 and 500 posts.')
    with connect() as conn:
        lock(conn)
        placeholders = ','.join('?' for _ in keys)
        rows = conn.execute(f'SELECT post_key, stack_id FROM topic_stack_members WHERE post_key IN ({placeholders})', tuple(keys)).fetchall()
        # Manual grouping must work for freshly imported posts before the
        # automatic classifier has materialized their membership rows.
        present = {row['post_key'] for row in rows}
        missing = [item for item in keys if item not in present]
        if missing:
            valid = set()
            for item in missing:
                account, _, shortcode = item.partition(':')
                try:
                    if conn.execute('SELECT 1 FROM dashboard_posts WHERE account = ? AND shortcode = ? LIMIT 1', (account, shortcode)).fetchone() or conn.execute('SELECT 1 FROM posts WHERE shortcode = ? LIMIT 1', (shortcode,)).fetchone(): valid.add(item)
                except Exception:
                    pass
            if len(valid) != len(missing):
                raise ValueError('Some posts are no longer available. Refresh and try again.')
            conn.executemany(
                'INSERT INTO topic_stack_members (post_key, stack_id, words, posted_at) VALUES (?, ?, ?, 0)',
                [(item, uuid.uuid4().hex, '[]') for item in missing],
            )
            rows = conn.execute(f'SELECT post_key, stack_id FROM topic_stack_members WHERE post_key IN ({placeholders})', tuple(keys)).fetchall()
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
    counts = Counter(
        row['stack_id']
        for row in conn.execute('SELECT stack_id FROM topic_stack_members').fetchall()
    )
    return {'members': [{'postKey': row['post_key'], 'stackId': row['stack_id'], 'stackSize': counts[row['stack_id']]} for row in rows]}


def _source_text(conn, post_key):
    """Load the caption used to semantically compare a stored post."""
    account, separator, shortcode = post_key.partition(':')
    if not separator or not account or not shortcode:
        return ''
    try:
        row = conn.execute(
            'SELECT caption, hook_text, transcript, alt_text, first_comment '
            'FROM dashboard_posts WHERE account = ? AND shortcode = ? LIMIT 1',
            (account, shortcode),
        ).fetchone()
    except Exception:  # pragma: no cover - old schemas may not have rich fields
        try:
            row = conn.execute(
                'SELECT caption FROM dashboard_posts '
                'WHERE account = ? AND shortcode = ? LIMIT 1',
                (account, shortcode),
            ).fetchone()
        except Exception:  # pragma: no cover - old schemas may not have dashboard_posts
            row = None
    if row:
        fields = ('caption', 'hook_text', 'transcript', 'alt_text', 'first_comment')
        available = set(row.keys()) if hasattr(row, 'keys') else set(fields)
        return ' '.join(str(row[field] or '').strip() for field in fields if field in available)[:6000]
    try:
        row = conn.execute(
            'SELECT caption, title, hook_text FROM posts '
            'WHERE shortcode = ? ORDER BY id DESC LIMIT 1',
            (shortcode,),
        ).fetchone()
    except Exception:  # pragma: no cover - old schemas may not have source tables
        row = None
    return ' '.join(str(row[field] or '') for field in ('caption', 'title', 'hook_text')).strip() if row else ''


def _semantic_similarity_scores(reference_text, candidates):
    """Return strict Jev scores; never silently fall back to lexical matching."""
    api_key = os.getenv('TYPESAFE_API_KEY', '').strip()
    if not api_key:
        raise JevUnavailable('Find Similar requires TYPESAFE_API_KEY to use Jev.')
    if not reference_text.strip():
        raise ValueError('The selected post does not have caption text for Jev to compare.')
    if not candidates:
        return {}
    try:
        import httpx

        state = {
            'reference_post': reference_text[:4000],
            'candidate_posts': {
                candidate_id: text[:2400]
                for candidate_id, text in candidates.items()
            },
        }
        questions = {}
        for index, candidate_id in enumerate(candidates, start=1):
            questions[f'candidate_{index}_topic'] = {
                'type': 'noul',
                'instructions': (
                    f'Is candidate `{candidate_id}` about the same specific topic or story as '
                    'the reference post? Compare entities, event, product, claim, and angle. '
                    'Do not accept a match only because of a shared brand, industry, format, '
                    'hashtag, or generic advice.'
                ),
                'criteria': {
                    'true': 'Both posts cover the same concrete story, event, release, person, product, or claim.',
                    'false': 'They are only loosely related, share generic words, or discuss different facts.',
                },
            }
            questions[f'candidate_{index}_stack'] = {
                'type': 'noul',
                'instructions': (
                    f'Would an editor place candidate `{candidate_id}` in the same Topic Stack '
                    'as the reference post? Require the same underlying event, claim, product '
                    'release, or person—not merely the same broad subject or account.'
                ),
                'criteria': {
                    'true': 'The two posts are interchangeable members of one narrowly defined topic stack.',
                    'false': 'They belong in separate stacks because the event, claim, angle, or subject is materially different.',
                },
            }
        response = httpx.post(
            'https://api.typesafe.ai/v1/systemone',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'model': 'jev-latest', 'state': state, 'questions': questions},
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        answers = payload.get('answers') or {}
        scores = {}
        for index, candidate_id in enumerate(candidates, start=1):
            topic = (answers.get(f'candidate_{index}_topic') or {}).get('noul')
            stack = (answers.get(f'candidate_{index}_stack') or {}).get('noul')
            if isinstance(topic, (int, float)) and isinstance(stack, (int, float)):
                # The weaker independent judgment controls acceptance. This keeps
                # a strong topical overlap from masking a different editorial stack.
                scores[candidate_id] = min(
                    max(0.0, min(1.0, float(topic))),
                    max(0.0, min(1.0, float(stack))),
                )
        if len(scores) != len(candidates):
            raise JevUnavailable('Jev returned an incomplete Find Similar response.')
        return scores
    except Exception as exc:  # Keep Find Similar usable during provider/key outages.
        if isinstance(exc, JevUnavailable):
            raise
        logger.warning('Jev Find Similar request failed: %s', exc)
        raise JevUnavailable('Jev is temporarily unavailable. Find Similar was not applied.') from exc


def _semantic_threshold():
    try:
        configured = float(os.getenv('TYPESAFE_FIND_SIMILAR_THRESHOLD', '0.72'))
    except ValueError:
        configured = 0.72
    return max(0.60, min(0.95, configured))


def _semantic_int(name, default, minimum, maximum):
    try:
        configured = int(os.getenv(name, str(default)))
    except ValueError:
        configured = default
    return max(minimum, min(maximum, configured))

def find_similar(post_key):
    """User-triggered Jev-only search; never runs on reload."""
    if not isinstance(post_key, str) or not post_key or len(post_key) > 300:
        raise ValueError('Choose a valid post.')
    with connect() as conn:
        initialize(conn)
        reference = conn.execute('SELECT stack_id, words FROM topic_stack_members WHERE post_key = ?', (post_key,)).fetchone()
        if not reference:
            # Research deliberately renders a singleton fallback for legacy or
            # freshly imported rows whose durable membership has not arrived
            # yet. Materialize that one row here so the visible card and this
            # action share the same source of truth.
            account, separator, shortcode = post_key.partition(':')
            source = None
            if separator and account and shortcode:
                try:
                    source = conn.execute(
                        'SELECT caption, published_at FROM dashboard_posts WHERE account = ? AND shortcode = ? LIMIT 1',
                        (account, shortcode),
                    ).fetchone()
                except Exception:  # pragma: no cover - old schema without source tables
                    pass
                if source is None:
                    try:
                        source = conn.execute(
                            'SELECT caption, published_at FROM posts WHERE shortcode = ? LIMIT 1',
                            (shortcode,),
                        ).fetchone()
                    except Exception:  # pragma: no cover - old schema without source tables
                        source = None
            if source is None:
                raise ValueError('This post is no longer available. Refresh and try again.')
            lock(conn)
            conn.execute(
                """INSERT INTO topic_stack_members(post_key, stack_id, words, posted_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(post_key) DO NOTHING""",
                (post_key, uuid.uuid4().hex, json.dumps(words({
                    'caption': source['caption'],
                    'postDate': source['published_at'],
                })), timestamp({'postDate': source['published_at']})),
            )
            reference = conn.execute('SELECT stack_id, words FROM topic_stack_members WHERE post_key = ?', (post_key,)).fetchone()
            if not reference:
                raise ValueError('This post is no longer available. Refresh and try again.')
        try:
            reference_words = json.loads(reference['words'])
        except (TypeError, ValueError):
            reference_words = []
        reference_text = _source_text(conn, post_key) or ' '.join(reference_words)
        matching_groups = set()
        candidate_limit = _semantic_int('TYPESAFE_FIND_SIMILAR_CANDIDATE_LIMIT', 100, 10, 500)
        batch_size = _semantic_int('TYPESAFE_FIND_SIMILAR_BATCH_SIZE', 25, 5, 50)
        # Recency only controls the bounded input volume. Jev makes the actual
        # similarity decision; no keyword/Jaccard filter is applied here.
        candidate_rows = conn.execute(
            'SELECT post_key, stack_id, words FROM topic_stack_members '
            'WHERE post_key != ? AND stack_id != ? '
            'ORDER BY posted_at DESC, post_key DESC LIMIT ?',
            (post_key, reference['stack_id'], candidate_limit),
        ).fetchall()
        candidate_texts = {
            row['post_key']: _source_text(conn, row['post_key']) or ' '.join(
                json.loads(row['words']) if isinstance(row['words'], str) else []
            )
            for row in candidate_rows
        }
        candidate_texts = {candidate_id: text for candidate_id, text in candidate_texts.items() if text}
        if not candidate_texts:
            result = memberships(conn, [post_key])
            result['matchedCount'] = 0
            result['similarityMode'] = 'jev_only'
            result['candidatesEvaluated'] = 0
            return result
        semantic_scores = {}
        candidate_items = list(candidate_texts.items())
        for start in range(0, len(candidate_items), batch_size):
            semantic_scores.update(_semantic_similarity_scores(
                reference_text,
                dict(candidate_items[start:start + batch_size]),
            ))
        semantic_threshold = _semantic_threshold()
        for row in candidate_rows:
            if row['stack_id'] == reference['stack_id']:
                continue
            if semantic_scores.get(row['post_key'], 0.0) >= semantic_threshold:
                matching_groups.add(row['stack_id'])
        if not matching_groups:
            result = memberships(conn, [post_key])
            result['matchedCount'] = 0
            result['similarityMode'] = 'jev_only'
            result['candidatesEvaluated'] = len(candidate_texts)
            return result
        matching_groups.add(reference['stack_id'])
        # The expensive candidate scan is read-only. Serialize only the
        # short final merge, so a Find Similar request no longer blocks a cold
        # Research catalogue rebuild for its entire duration.
        lock(conn)
        marks = ','.join('?' for _ in matching_groups)
        destination = sorted(matching_groups)[0]
        conn.execute(f'UPDATE topic_stack_members SET stack_id = ? WHERE stack_id IN ({marks})', (destination, *sorted(matching_groups)))
        members = [row['post_key'] for row in conn.execute('SELECT post_key FROM topic_stack_members WHERE stack_id = ? ORDER BY post_key', (destination,)).fetchall()]
        result = {
            'stackId': destination,
            'postKeys': members,
            'stackSize': len(members),
            'matchedCount': len(members) - 1,
            'similarityMode': 'jev_only',
            'candidatesEvaluated': len(candidate_texts),
        }
        return result

def stack_keys(post_key):
    """Return the durable members for one post without triggering classification."""
    with connect() as conn:
        initialize(conn)
        row = conn.execute('SELECT stack_id FROM topic_stack_members WHERE post_key = ?', (post_key,)).fetchone()
        if not row:
            return []
        return [item['post_key'] for item in conn.execute('SELECT post_key FROM topic_stack_members WHERE stack_id = ? ORDER BY post_key', (row['stack_id'],)).fetchall()]
