"""Read public tweet text from X's official oEmbed response, without scripts."""
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx


def tweet_url(value):
    try:
        url = urlsplit(value)
        if url.scheme not in {'https', 'http'} or url.hostname not in {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com', 'mobile.twitter.com'}:
            return ''
        if url.username or url.password or url.port not in {None, 80, 443}:
            return ''
        match = re.fullmatch(r'/(?:([A-Za-z0-9_]{1,30})/status|i/web/status)/(\d+)/?', url.path)
        if not match:
            return ''
        return f'https://twitter.com/{match[1] or "i"}/status/{match[2]}'
    except ValueError:
        return ''


class TweetText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inside = False
        self.done = False
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.skip += 1
        if tag == 'p' and not self.done and not self.skip:
            self.inside = True
        if tag == 'br' and self.inside and not self.skip:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag == 'p' and self.inside:
            self.inside = False
            self.done = True
        if tag in {'script', 'style'}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if self.inside and not self.skip:
            self.parts.append(data)

    @property
    def text(self):
        return ''.join(self.parts).strip()[:30000]


def _read_json(url, params):
    with httpx.stream('GET', url, params=params, timeout=10.0, follow_redirects=False) as response:
        response.raise_for_status()
        chunks = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > 200000:
                raise ValueError('Oversized embed response')
            chunks.append(chunk)
    return json.loads(b''.join(chunks))


def _image_url(value):
    try:
        url = urlsplit(str(value or ''))
        if url.scheme == 'https' and url.hostname == 'pbs.twimg.com' and not url.username and not url.password and url.port in {None, 443}:
            return str(value)
    except ValueError:
        pass
    return ''


def fetch_tweet_text(value):
    canonical = tweet_url(value)
    empty = {'tweet_text': '', 'tweet_author': '', 'tweet_image': '', 'tweet_avatar': '', 'tweet_media_type': '', 'text_status': 'not_applicable'}
    if not canonical:
        return empty
    # Fixed public X embed destinations. User-supplied hosts are never fetched.
    try:
        data = _read_json('https://cdn.syndication.twimg.com/tweet-result',
                          {'id': canonical.rsplit('/', 1)[-1], 'lang': 'en', 'token': '0'})
        text = str(data.get('text') or '').strip()
        if not text:
            raise ValueError('Tweet text unavailable')
        user = data.get('user') or {}
        media = data.get('mediaDetails') or []
        preview = next((item for item in media if _image_url(item.get('media_url_https'))), {})
        # Remove only the trailing attachment URL rendered separately as media.
        for attachment in (data.get('entities') or {}).get('media') or []:
            url = attachment.get('url') or ''
            if url and text.endswith(url):
                text = text[:-len(url)].rstrip()
        return {**empty, 'tweet_text': text[:30000], 'tweet_author': str(user.get('name') or '')[:300],
                'tweet_image': _image_url(preview.get('media_url_https')),
                'tweet_avatar': _image_url(user.get('profile_image_url_https')),
                'tweet_media_type': preview.get('type') if preview.get('type') in {'photo', 'video', 'animated_gif'} else '',
                'text_status': 'ready'}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        pass
    try:
        data = _read_json('https://publish.x.com/oembed', {'url': canonical, 'omit_script': 'true'})
        parser = TweetText()
        parser.feed(str(data.get('html') or ''))
        if not parser.text:
            raise ValueError('Tweet text unavailable')
        return {**empty, 'tweet_text': parser.text, 'tweet_author': str(data.get('author_name') or '')[:300], 'text_status': 'ready'}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return {**empty, 'text_status': 'unavailable'}
