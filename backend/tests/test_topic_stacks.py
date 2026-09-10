from datetime import datetime, timedelta, timezone

import pytest
from app import db, topic_stacks

@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_URL', '')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'stacks.sqlite')

def post(key, caption, likes=1, date='2026-09-05T00:00:00Z'):
    return dict(account='test', shortcode=key, caption=caption, postDate=date, likes=likes)

CAPTION = 'Scientists discovered remarkable ancient dinosaur fossils underneath isolated volcanic mountains during research expedition'

def test_persistent_membership_and_only_new_posts_classified(monkeypatch):
    posts = [post('a', CAPTION), post('b', CAPTION), post('c', 'Different story')]
    topic_stacks.attach(posts)
    original = [p['stackId'] for p in posts]
    assert original[0] == original[1] != original[2]
    assert posts[0]['stackSize'] == 2
    posts.reverse()
    posts[0]['caption'] = CAPTION
    monkeypatch.setattr(topic_stacks, 'words', lambda _: pytest.fail('Existing posts must not be classified again'))
    topic_stacks.attach(posts)
    assert [p['stackId'] for p in reversed(posts)] == original

def test_apply_memberships_projects_saved_groups_without_reclassification(monkeypatch):
    posts = [post('a', CAPTION), post('b', CAPTION), post('legacy', 'unclassified')]
    topic_stacks.attach(posts[:2])
    topic_stacks.merge(['test:a', 'test:b'])
    monkeypatch.setattr(topic_stacks, 'words', lambda _: pytest.fail('Read projection must not classify'))
    fresh = [dict(item) for item in posts]
    topic_stacks.apply_memberships(fresh)
    assert fresh[0]['stackId'] == fresh[1]['stackId']
    assert fresh[0]['stackSize'] == 2
    assert fresh[2]['stackId'] == 'test:legacy'
    assert fresh[2]['stackSize'] == 1

def test_new_post_joins_existing_stack_without_moving_old_members():
    posts = [post('a', CAPTION), post('b', CAPTION)]
    topic_stacks.attach(posts)
    group = posts[0]['stackId']
    posts.append(post('new', CAPTION))
    topic_stacks.attach(posts)
    assert {p['stackId'] for p in posts} == {group}
    assert all(p['stackSize'] == 3 for p in posts)

def test_manual_merge_is_durable_and_includes_existing_members():
    posts = [post('a', CAPTION), post('b', CAPTION), post('c', 'Different story')]
    topic_stacks.attach(posts)
    result = topic_stacks.merge(['test:a', 'test:c'])
    assert result['postKeys'] == ['test:a', 'test:b', 'test:c']
    topic_stacks.attach(posts)
    assert {p['stackId'] for p in posts} == {result['stackId']}
    assert topic_stacks.merge(['test:c', 'test:a']) == result
    with pytest.raises(ValueError): topic_stacks.merge(['test:a', 'missing:x'])
    topic_stacks.attach(posts)
    assert all(p['stackSize'] == 3 for p in posts)

def test_separate_keeps_posts_out_of_their_old_stack_after_reload():
    posts = [post('a', CAPTION), post('b', CAPTION)]
    topic_stacks.attach(posts)
    result = topic_stacks.separate(['test:a'])
    states = {row['postKey']: row for row in result['members']}
    assert states['test:a']['stackSize'] == 1
    assert states['test:b']['stackSize'] == 1
    topic_stacks.attach(posts)
    assert posts[0]['stackId'] != posts[1]['stackId']

def test_find_similar_merges_matching_existing_posts_only_when_requested():
    posts = [post('a', CAPTION), post('b', CAPTION), post('c', 'A completely unrelated cooking recipe with tomatoes and basil')]
    topic_stacks.attach(posts)
    topic_stacks.separate(['test:a', 'test:b'])
    result = topic_stacks.find_similar('test:a')
    assert result['matchedCount'] == 1
    assert set(result['postKeys']) == {'test:a', 'test:b'}

def test_find_similar_bootstraps_a_visible_post_without_membership():
    with db.connect() as connection:
        connection.execute(
            'CREATE TABLE dashboard_posts (account TEXT, shortcode TEXT, caption TEXT, published_at TEXT)'
        )
        connection.execute(
            'INSERT INTO dashboard_posts VALUES (?, ?, ?, ?)',
            ('test', 'legacy', CAPTION, '2026-09-05T00:00:00Z'),
        )
    result = topic_stacks.find_similar('test:legacy')
    assert result['matchedCount'] == 0
    assert result['members'][0]['postKey'] == 'test:legacy'

@pytest.mark.parametrize(('hours', 'shared', 'score', 'coverage'), [
    (8, 3, .16, .20),
    (24, 4, .22, .20),
    (48, 4, .30, .20),
    (72, 5, .40, .20),
])
def test_recent_posts_use_progressively_more_permissive_thresholds(hours, shared, score, coverage):
    assert topic_stacks.automatic_match(shared, score, coverage, hours * 3600)

def test_time_proximity_alone_does_not_group_unrelated_posts():
    assert not topic_stacks.automatic_match(2, .15, .44, 60)

def test_posts_with_three_topic_words_group_inside_eight_hours_only():
    first = post('a', 'quantum robot mars research discovery', date='2026-09-05T00:00:00Z')
    close = post('b', 'quantum robot mars video launch', date='2026-09-05T08:00:00Z')
    later = post('c', 'quantum robot mars future update', date='2026-09-05T17:00:00Z')
    topic_stacks.attach([first, close, later])
    assert first['stackId'] == close['stackId']
    assert later['stackId'] != first['stackId']

def test_regroup_recent_rebuilds_only_last_72_hours():
    now = datetime.now(timezone.utc)
    recent_date = (now - timedelta(hours=2)).isoformat()
    old_date = (now - timedelta(hours=80)).isoformat()
    recent_a = post('recent-a', CAPTION, date=recent_date)
    recent_b = post('recent-b', CAPTION, date=recent_date)
    old = post('old', CAPTION, date=old_date)
    topic_stacks.attach([recent_a, recent_b, old])
    old_stack = old['stackId']
    result = topic_stacks.regroup_recent([recent_a, recent_b, old])
    assert result == {'hours': 72, 'postsProcessed': 2, 'stacks': 1, 'groupedPosts': 2}
    fresh = [dict(recent_a), dict(recent_b), dict(old)]
    topic_stacks.apply_memberships(fresh)
    assert fresh[0]['stackId'] == fresh[1]['stackId']
    assert fresh[2]['stackId'] == old_stack
