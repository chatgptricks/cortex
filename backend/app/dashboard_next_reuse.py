"""Read-only destination history for /next; deterministic exclusions plus review context.

The caller authenticates and validates the owned destination first. This module
does not scrape, initialize topic stacks, mutate Queue, or infer publication
from an assignment. Semantic similarity is context for the caller's reviewer,
never automatic proof that two different stories are the same.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
import html
import json
import math
import re
import unicodedata
from urllib.parse import urlsplit

from . import account_media_kit as kit, db, external_research
from .topic_stacks import STOP

_HANDLE = re.compile(r"[a-z0-9_.]{1,30}\Z")
_CODE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def _handle(value):
    return str(value or "").strip().removeprefix("@").casefold()


def _normal(value):
    value = unicodedata.normalize("NFKC", html.unescape(str(value or ""))).casefold()
    return " ".join(_WORDS.findall(value))


def _tokens(value):
    return {word for word in _normal(value).split() if len(word) >= 3 and word not in STOP}


def _exact(value):
    normalized = _normal(value)
    return normalized if len(normalized) >= 48 and len(normalized.split()) >= 6 else None


def _code_from_link(value):
    try:
        url = urlsplit(str(value or "").strip())
    except ValueError:
        return None
    if url.scheme not in {"https", "http"} or url.hostname not in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
        return None
    parts = url.path.strip("/").split("/")
    return parts[1] if len(parts) == 2 and parts[0] in {"p", "reel", "reels", "tv"} and _CODE.fullmatch(parts[1]) else None


def _list(value):
    parsed = json.loads(value) if isinstance(value, str) and value.strip() else value or []
    if not isinstance(parsed, list):
        raise ValueError("Stored Queue publication metadata is incomplete.")
    return parsed


def _published_links(row, target):
    links = []
    saved = _list(row.get("final_permalinks"))
    for item in saved:
        if not isinstance(item, dict):
            raise ValueError("Stored Queue publication metadata is incomplete.")
        account = _handle(item.get("account"))
        if not _HANDLE.fullmatch(account):
            raise ValueError("Stored Queue publication metadata is incomplete.")
        if account != target:
            continue
        code = _code_from_link(item.get("url"))
        if not code:
            raise ValueError("Stored Queue publication metadata is incomplete.")
        links.append((account, code))
    if saved:
        return links
    # A legacy singular URL proves publication only on its first destination,
    # matching Queue's own projection; a multi-account assignment is not proof.
    if str(row.get("final_permalink") or "").strip():
        destinations = [_handle(value) for value in _list(row.get("recommended_accounts"))]
        account = destinations[0] if destinations else _handle(row.get("post_account"))
        if not _HANDLE.fullmatch(account):
            raise ValueError("Stored Queue publication metadata is incomplete.")
        if account != target:
            return []
        code = _code_from_link(row.get("final_permalink"))
        if not code:
            raise ValueError("Stored Queue publication metadata is incomplete.")
        return [(account, code)]
    return []


def _snippet(value, query_tokens, limit):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    # Locate the best matching window across the whole text, including tails
    # beyond the old caption/OCR preview limits. Keep surrounding prose.
    matches = [match for match in _WORDS.finditer(text)
               if _normal(match.group()) in query_tokens]
    start = 0
    if matches:
        best = (-1, -1)
        end = 0
        for index, match in enumerate(matches):
            while end < len(matches) and matches[end].end() - match.start() <= limit - 80:
                end += 1
            window = matches[index:end]
            score = (len({_normal(item.group()) for item in window}), len(window))
            if score > best:
                best = score
                start = max(0, match.start() - 60)
    start = min(start, max(0, len(text) - limit))
    return ("…" if start else "") + text[start:start + limit] + ("…" if start + limit < len(text) else "")


def _compact(post, query_tokens=frozenset()):
    return {"key": post["key"], "account": post["account"], "shortcode": post["shortcode"],
            "caption": _snippet(post.get("caption"), query_tokens, 1000),
            "ocr_text": _snippet(post.get("ocr_text"), query_tokens, 700),
            "published_at": post.get("published_at")}


class ReuseHistory:
    def __init__(self, target, posts, memberships, source_keys, source_codes):
        self.target = target
        self._posts = sorted(posts, key=lambda post: (
            kit._date(post.get("published_at")) or datetime.min.replace(tzinfo=UTC), post["key"]), reverse=True)
        self.post_count = len(self._posts)
        self.examples = [_compact(post) for post in self._posts[:3]]
        self._memberships = memberships
        self._codes = {post["shortcode"] for post in posts} | source_codes
        self._keys = {post["key"] for post in posts} | source_keys
        self._stacks = {memberships[key] for key in self._keys if key in memberships}
        self._exact_text = {value for post in posts for field in ("caption", "ocr_text") if (value := _exact(post.get(field)))}
        self._index = defaultdict(list)
        self._doc_tokens = []
        for index, post in enumerate(self._posts):
            tokens = _tokens(post.get("caption")) | _tokens(post.get("ocr_text"))
            self._doc_tokens.append(tokens)
            for token in tokens:
                self._index[token].append(index)
        self._idf = {token: math.log1p((self.post_count + 1) / (len(indices) + 1))
                     for token, indices in self._index.items()}

    def filter_candidates(self, candidates):
        """Preserve candidate order/fields, excluding proven destination reuse."""
        accepted = []
        for candidate in candidates:
            account, code = _handle(candidate.get("account")), str(candidate.get("shortcode") or "")
            if not _HANDLE.fullmatch(account) or not _CODE.fullmatch(code):
                raise ValueError("Candidate identity is incomplete.")
            key = account + ":" + code
            if (code in self._codes or key in self._keys
                    or self._memberships.get(key) in self._stacks
                    or any(_exact(candidate.get(field)) in self._exact_text for field in ("caption", "ocr_text"))):
                continue
            literal_tokens = _tokens(candidate.get("caption")) | _tokens(candidate.get("ocr_text"))
            # The caller may supply bilingual factual search terms. They can
            # retrieve review evidence, but cannot prove duplication or turn
            # a textless source into a sufficiently understood candidate.
            tokens = literal_tokens | _tokens(candidate.get("reuse_search_text"))
            scores = Counter()
            for token in sorted(tokens):
                for index in self._index.get(token, ()):
                    scores[index] += self._idf[token]
            # Mild length normalization avoids generic long roundups eclipsing
            # a focused old story. Sorting remains deterministic on equal scores.
            relevant = sorted(scores, key=lambda index: (
                -(scores[index] / math.sqrt(max(1, len(self._doc_tokens[index])))), index))[:8]
            selected = list(dict.fromkeys([*relevant, *range(min(3, self.post_count))]))
            accepted.append({**candidate, "prior_posts": [_compact(self._posts[index], tokens) for index in selected],
                             "reuse_uncertain": len(literal_tokens) < 3})
        return accepted


def load_history(target: str) -> ReuseHistory:
    """Load the complete stored destination history once; fail closed on errors."""
    target = _handle(target)
    if not _HANDLE.fullmatch(target):
        raise ValueError("Invalid /next destination.")
    stage = "history_source"
    try:
        with db.connect() as conn:
            # Posts, stack identities, and Queue deliveries must describe one
            # database snapshot even when ingestion finishes during the read.
            if getattr(conn, "is_postgres", False):
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                conn.execute("SET LOCAL statement_timeout = '25s'")
            elif not getattr(conn, "in_transaction", False):
                conn.execute("BEGIN")
            cte, params, registry = external_research._sources(conn, [target])
            if target not in registry or registry[target].get("group_name") != "sentient":
                raise ValueError("The /next destination must be a stored Sentient account.")
            rows = conn.execute(cte + "SELECT account, shortcode, caption, hook_text, published_at FROM catalogue", params).fetchall()
            now = datetime.now(UTC)
            posts = {}
            stage = "history_dates"
            for row in rows:
                raw_published = row["published_at"]
                published = kit._date(raw_published)
                if published and published > now:
                    continue
                # A persisted shortcode with a missing/invalid date can be an
                # old published post. Retain its identity/text conservatively;
                # an incomplete timestamp must never prove unused history.
                code = str(row["shortcode"] or "")
                if not _CODE.fullmatch(code):
                    continue
                posts[code] = {"account": target, "shortcode": code, "key": target + ":" + code,
                               "caption": str(row["caption"] or ""), "ocr_text": str(row["hook_text"] or ""),
                               "published_at": raw_published if published else None}
            # Read persisted memberships directly: the topic_stacks helpers
            # can initialize or classify, which is forbidden for /next reads.
            stage = "history_stacks"
            memberships = {str(row["post_key"]): str(row["stack_id"]) for row in conn.execute(
                "SELECT post_key, stack_id FROM topic_stack_members").fetchall() if row["stack_id"]}
            stage = "history_queue"
            queue_rows = conn.execute(
                "SELECT post_account, post_shortcode, post_permalink, post_caption, final_permalink, "
                "final_permalinks, recommended_accounts, closed_at, completed_at, updated_at "
                "FROM queue_requests WHERE status = 'closed'").fetchall()
            source_keys, source_codes = set(), set()
            for raw in queue_rows:
                row = dict(raw)
                published_codes = [code for account, code in _published_links(row, target)]
                if not published_codes:
                    continue
                source_account = _handle(row.get("post_account"))
                codes = {str(row.get("post_shortcode") or ""), _code_from_link(row.get("post_permalink"))}
                for code in codes:
                    if code and _CODE.fullmatch(code):
                        source_codes.add(code)
                        if _HANDLE.fullmatch(source_account):
                            source_keys.add(source_account + ":" + code)
                for code in published_codes:
                    if code not in posts:
                        posts[code] = {"account": target, "shortcode": code, "key": target + ":" + code,
                                       "caption": str(row.get("post_caption") or ""), "ocr_text": "",
                                       "published_at": row.get("closed_at") or row.get("completed_at") or row.get("updated_at")}
        stage = "history_index"
        return ReuseHistory(target, list(posts.values()), memberships, source_keys, source_codes)
    except Exception as exc:
        error = ValueError("Unable to verify complete stored destination publication history.")
        error.code = stage
        raise error from exc
