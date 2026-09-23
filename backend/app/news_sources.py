"""Bounded public article extraction for the News sourcing workflow."""
from html.parser import HTMLParser
from urllib.parse import urlsplit
import httpx

# Explicit publisher allowlist prevents a supplied URL from reaching internal services.
PUBLISHERS = {
    'www.kdnuggets.com', 'kdnuggets.com', 'blogs.nvidia.com',
    'techcrunch.com', 'www.therobotreport.com', 'siliconangle.com',
    'manufacturingdigital.com', 'www.citriniresearch.com',
    'openai.com', 'www.anthropic.com', 'blog.google',
    'www.theverge.com', 'arstechnica.com', 'venturebeat.com',
    # Publishers observed in the connected RSS.app feeds.
    'bastillepost.com', 'www.bastillepost.com', 'news.sbs.co.kr',
    'winnipegfreepress.com', 'www.winnipegfreepress.com', 'jpost.com', 'www.jpost.com',
    'benzinga.com', 'www.benzinga.com', 'fortune.com', 'www.fortune.com',
    'axios.com', 'www.axios.com', 'prospect.org', 'www.prospect.org',
    'finance.yahoo.com', 'www.foxnews.com', 'foxnews.com', 'apnews.com', 'www.apnews.com',
    'macrumors.com', 'www.macrumors.com', 'gizmodo.com', 'www.gizmodo.com',
    'www.nvidia.com', 'news.google.com',
    'ft.com', 'www.ft.com', 'businessinsider.com', 'www.businessinsider.com',
    'qz.com', 'www.qz.com', 'bbc.com', 'www.bbc.com', 'cnbc.com', 'www.cnbc.com',
    'techspot.com', 'www.techspot.com', 'theglobeandmail.com', 'www.theglobeandmail.com',
    'techcrunch.com', 'www.techcrunch.com', 'theverge.com', 'arstechnica.com',
}

class ArticleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.paragraph = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'nav', 'footer', 'header'}:
            self.skip += 1
        if tag == 'p' and not self.skip:
            self.paragraph = []

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'nav', 'footer', 'header'}:
            self.skip = max(0, self.skip - 1)
        if tag == 'p' and self.paragraph is not None:
            text = ' '.join(''.join(self.paragraph).split())
            if len(text) >= 70:
                self.parts.append(text)
            self.paragraph = None

    def handle_data(self, data):
        if self.paragraph is not None and not self.skip:
            self.paragraph.append(data)


def article_evidence(url: str, excerpt: str) -> tuple[str, str]:
    """Prefer readable article paragraphs, falling back honestly to feed evidence."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in PUBLISHERS or parsed.port not in {None, 443} or parsed.username or parsed.password:
            return excerpt, 'feed_excerpt'
        with httpx.stream('GET', url, follow_redirects=False, timeout=8.0, headers={'User-Agent': 'SentientNews/1.0'}) as response:
            response.raise_for_status()
            if 'text/html' not in response.headers.get('content-type', ''):
                return excerpt, 'feed_excerpt'
            chunks = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 1_500_000:
                    break
                chunks.append(chunk)
            parser = ArticleText()
            parser.feed(b''.join(chunks).decode('utf-8', errors='replace'))
            text = '\n\n'.join(dict.fromkeys(parser.parts))[:14000]
            if len(text) > max(500, len(excerpt) + 200):
                return text, 'article'
    except (httpx.HTTPError, ValueError, UnicodeError):
        pass
    return excerpt, 'feed_excerpt'
