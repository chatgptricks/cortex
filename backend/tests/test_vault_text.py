import json
from contextlib import contextmanager

import pytest
from app import vault_text


def test_parse_tweet_preserves_text_and_line_breaks_without_html():
    parser = vault_text.TweetText()
    parser.feed('<blockquote><p>Hello &amp; world<br><br>Next <a href="https://example.com">link</a><script>bad()</script></p>Author <p>Other</p></blockquote>')
    assert parser.text == 'Hello & world\n\nNext link'


@pytest.mark.parametrize('url', ['https://x.com/search?q=ai', 'http://127.0.0.1/status/123', 'https://x.com.evil.test/u/status/123', 'https://user:pass@x.com/u/status/123', 'https://x.com:999/u/status/123', 'https://x.com/u/status/not-an-id'])
def test_only_real_status_paths_are_eligible(url):
    assert vault_text.tweet_url(url) == ''


def test_normalize_status_url():
    assert vault_text.tweet_url('https://x.com/person/status/123?s=20') == 'https://twitter.com/person/status/123'
    assert vault_text.tweet_url('https://twitter.com/i/web/status/123') == 'https://twitter.com/i/status/123'


def test_fixed_destination_and_parsed_response(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def iter_bytes(self):
            yield json.dumps({'text':'Readable tweet\nSecond line', 'user':{'name':'Creator'}, 'mediaDetails':[{'media_url_https':'https://pbs.twimg.com/media/sample.jpg','type':'photo'}]}).encode()
    @contextmanager
    def stream(method, url, **kwargs):
        assert url == 'https://cdn.syndication.twimg.com/tweet-result'
        assert kwargs['params']['id'] == '123'
        assert kwargs['follow_redirects'] is False
        yield Response()
    monkeypatch.setattr(vault_text.httpx, 'stream', stream)
    assert vault_text.fetch_tweet_text('https://x.com/person/status/123') == {'tweet_text':'Readable tweet\nSecond line','tweet_author':'Creator','text_status':'ready','tweet_image':'https://pbs.twimg.com/media/sample.jpg','tweet_avatar':'','tweet_media_type':'photo'}


def test_failure_is_honest(monkeypatch):
    def fail(*args, **kwargs): raise vault_text.httpx.ReadTimeout('timeout')
    monkeypatch.setattr(vault_text.httpx, 'stream', fail)
    assert vault_text.fetch_tweet_text('https://x.com/person/status/123')['text_status'] == 'unavailable'
    assert vault_text.fetch_tweet_text('https://example.com')['text_status'] == 'not_applicable'


def test_fallback_to_oembed_and_reject_untrusted_images(monkeypatch):
    def read(url, params):
        if 'syndication' in url:
            return {}
        return {'html':'<p>Fallback &amp; text</p>', 'author_name':'Creator'}
    monkeypatch.setattr(vault_text, '_read_json', read)
    preview = vault_text.fetch_tweet_text('https://x.com/person/status/123')
    assert preview['tweet_text'] == 'Fallback & text'
    assert preview['text_status'] == 'ready'
    assert vault_text._image_url('https://pbs.twimg.com.evil.test/image') == ''
    assert vault_text._image_url('javascript:alert(1)') == ''
