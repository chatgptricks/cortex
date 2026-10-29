"""Stack IDs identify the full persisted membership across Research and Queue."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json

import pytest

from app import db, main, topic_stacks


CAPTION = 'Scientists discovered remarkable ancient dinosaur fossils underneath isolated volcanic mountains'


class SQLiteSnapshots:
    """Run the snapshot reader's Postgres ANY parameters against local SQLite."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, params=()):
        if '= ANY(?)' in statement:
            parts = statement.split('= ANY(?)')
            statement = parts[0]
            flattened = []
            for values, tail in zip(params, parts[1:]):
                statement += f"IN ({','.join('?' for _ in values)})" + tail
                flattened.extend(values)
            params = flattened
        return self.connection.execute(statement, params)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_URL', '')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'stacks.sqlite3')
    monkeypatch.setattr(db, 'ensure_directories', lambda: None)
    db.init_db()

    @contextmanager
    def connect():
        with db.connect() as connection:
            yield SQLiteSnapshots(connection)

    monkeypatch.setattr(main, 'connect', connect)
    monkeypatch.setattr(main, '_dashboard_catalogue_context', lambda: (
        {'test': 'competitors'}, {'handle': 'chatgptricks'},
    ))
    monkeypatch.setattr(main, '_DASHBOARD_CATALOGUE_GENERATION', 0)
    monkeypatch.setattr(main, '_DASHBOARD_CATALOGUE_DECORATION', None)
    monkeypatch.setattr(main, '_DASHBOARD_CATALOGUE_DECORATION_REVISION', '')
    monkeypatch.setattr(main, '_DASHBOARD_POSTS_CACHE_CONTENT', b'old catalogue')
    monkeypatch.setattr(main, '_DASHBOARD_POSTS_CACHE_EXPIRES_AT', 10000)
    now = datetime.now(timezone.utc).isoformat()
    posts = [
        {'account': 'test', 'shortcode': code, 'caption': caption, 'postDate': now}
        for code, caption in [('a', CAPTION), ('b', CAPTION), ('c', 'Unrelated story')]
    ]
    with db.connect() as conn:
        conn.execute("INSERT INTO accounts (handle,label,group_name,is_active,is_canonical,created_at,updated_at) VALUES ('test','Test','competitors',1,0,'','')")
        for post in posts:
            conn.execute(
                "INSERT INTO dashboard_posts (account,shortcode,caption,published_at,created_at,updated_at) VALUES ('test',?,?,?,?,?)",
                (post['shortcode'], post['caption'], now, now, now),
            )
    topic_stacks.attach(posts)
    return posts, connect


@pytest.mark.parametrize('operation', ['merge', 'separate', 'find-similar', 'regroup'])
def test_stack_mutations_refresh_cached_paginated_membership(workspace, monkeypatch, operation):
    posts, _ = workspace
    if operation == 'regroup':
        topic_stacks.separate(['test:a'])
    before = main._dashboard_catalogue_manifest()
    original = main._dashboard_catalogue_decoration(before['revision'])['stack_by_post']

    if operation == 'merge':
        main.dashboard_stacks_merge(json.dumps(['test:a', 'test:c']))
    elif operation == 'separate':
        main.dashboard_stacks_separate(json.dumps(['test:a']))
    elif operation == 'find-similar':
        monkeypatch.setattr(topic_stacks, '_semantic_similarity_scores', lambda reference, candidates: {
            key: .99 for key in candidates
        })
        main.dashboard_stacks_find_similar('test:a')
    else:
        monkeypatch.setattr(main, '_dashboard_posts_payload', lambda: {'posts': posts})
        main.admin_stacks_regroup_recent()

    after = main._dashboard_catalogue_manifest()
    current = main._dashboard_catalogue_decoration(after['revision'])['stack_by_post']
    assert after['sources'] == before['sources']
    assert after['projectionGeneration'] == 1
    assert after['revision'] != before['revision']
    assert current != original
    topic_stacks.apply_memberships(posts)
    assert current == {f"test:{post['shortcode']}": post['stackId'] for post in posts}
    assert main._DASHBOARD_POSTS_CACHE_CONTENT is None


def test_queue_snapshot_counts_unrequested_stack_members(workspace):
    posts, connect = workspace
    with connect() as conn:
        snapshots = main._queue_v2_post_snapshot_cache(conn, [
            {'post_account': 'test', 'post_shortcode': 'a'},
        ])
    assert snapshots['test', 'a']['stackId'] == posts[0]['stackId']
    assert snapshots['test', 'a']['stackSize'] == 2
    assert set(snapshots) == {('test', 'a')}


def test_stack_lookup_returns_full_identity_and_retains_keys_without_snapshots(workspace):
    posts, _ = workspace
    with db.connect() as conn:
        conn.execute("DELETE FROM dashboard_posts WHERE shortcode = 'b'")
    result = main.dashboard_stack_members('@TEST', 'a')
    assert result['stackId'] == posts[0]['stackId']
    assert result['postKeys'] == ['test:a', 'test:b']
    assert result['stackSize'] == 2
    assert [post['shortcode'] for post in result['posts']] == ['a']
    assert result['posts'][0]['stackId'] == result['stackId']
    assert result['posts'][0]['stackSize'] == 2


def test_stack_lookup_does_not_truncate_large_stack(workspace):
    posts, _ = workspace
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO topic_stack_members VALUES (?, ?, '[]', 0)",
            [(f'test:unavailable{index:03}', posts[0]['stackId']) for index in range(125)],
        )
    result = main.dashboard_stack_members('test', 'b')
    assert result['stackId'] == posts[0]['stackId']
    assert result['stackSize'] == 127
    assert len(result['postKeys']) == 127
    assert result['postKeys'] == sorted(result['postKeys'])


def test_missing_post_returns_empty_stack_identity(workspace):
    result = main.dashboard_stack_members('test', 'missing')
    assert result == {'posts': [], 'stackId': None, 'postKeys': [], 'stackSize': 0}
