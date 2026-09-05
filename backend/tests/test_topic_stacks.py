import pytest
from app import db, topic_stacks

@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_URL', '')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'stacks.sqlite')

def post(key, caption, likes=1):
    return dict(account='test', shortcode=key, caption=caption, postDate='2026-09-05T00:00:00Z', likes=likes)

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
