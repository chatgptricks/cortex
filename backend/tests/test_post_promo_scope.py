from contextlib import contextmanager
import sqlite3
import pytest
from fastapi import HTTPException
from app import main


@pytest.mark.parametrize('group', ['competitors', None])
@pytest.mark.parametrize('value', [True, False])
def test_non_ours_rejected_before_write(monkeypatch, group, value):
    monkeypatch.setattr(main, '_resolve_post_table', lambda _: 'dashboard_posts')
    monkeypatch.setattr(main, 'list_accounts', lambda **_: [{'handle': 'other', 'group': group}])
    monkeypatch.setattr(main, 'connect', lambda: pytest.fail('Must not write competitor flags'))
    with pytest.raises(HTTPException) as error:
        main.dashboard_post_flags('other', 'post', value, None)
    assert error.value.status_code == 403


def test_ours_promo_and_competitor_hide(monkeypatch):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript("CREATE TABLE dashboard_posts(account TEXT, shortcode TEXT, is_promo INTEGER, hidden INTEGER, updated_at TEXT); INSERT INTO dashboard_posts VALUES ('ours', 'post', 0, 0, ''); INSERT INTO dashboard_posts VALUES ('other', 'post', 0, 0, '');")
    @contextmanager
    def connect():
        yield conn
    monkeypatch.setattr(main, 'connect', connect)
    monkeypatch.setattr(main, '_resolve_post_table', lambda _: 'dashboard_posts')
    monkeypatch.setattr(main, '_invalidate_dashboard_posts_cache', lambda: None)
    monkeypatch.setattr(main, 'list_accounts', lambda **_: [{'handle': 'ours', 'group': 'sentient'}])
    assert main.dashboard_post_flags('ours', 'post', True, None)['is_promo']
    assert main.dashboard_post_flags('other', 'post', None, True)['hidden']
    conn.close()
