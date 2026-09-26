"""Private DEV link collection. No third-party scraping or paid calls."""
import json
from datetime import datetime, timezone
from uuid import uuid4
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from .db import connect, utc_now
from .vault_text import fetch_tweet_text

router = APIRouter(prefix="/api/dashboard/vault", tags=["vault"])


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vault_links (
        id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '', shared_at TEXT NOT NULL,
        slack_url TEXT NOT NULL DEFAULT '', priority DOUBLE PRECISION NOT NULL,
        discarded INTEGER NOT NULL DEFAULT 0
    )""")
    from .db import _ensure_column
    for column, default in [('tweet_text', ''), ('tweet_author', ''), ('tweet_image', ''), ('tweet_avatar', ''), ('tweet_media_type', ''), ('text_status', 'pending')]:
        _ensure_column(conn, "vault_links", column, f"{column} TEXT NOT NULL DEFAULT '{default}'")
    _ensure_column(conn, "vault_links", "done", "done INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "vault_links", "pool_request_id", "pool_request_id INTEGER")


def require_dev(request: Request):
    if not getattr(request.state, "is_dev", False) or getattr(request.state, "queue_role_preview_active", False):
        raise HTTPException(403, "Vault is available in DEV full access only.")


class LinkInput(BaseModel):
    url: str = Field(max_length=4096)
    title: str = Field(default="", max_length=300)
    source: str = Field(default="", max_length=100)
    shared_at: datetime | None = None
    slack_url: str = Field(default="", max_length=4096)

    @field_validator("url", "slack_url")
    @classmethod
    def valid_url(cls, value):
        value = value.strip()
        if not value:
            return value
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError()
        except ValueError:
            raise ValueError("Use a valid HTTP or HTTPS link without credentials.")
        return value


class LinkUpdate(BaseModel):
    priority: float | None = Field(default=None, allow_inf_nan=False)
    discarded: bool | None = None
    done: bool | None = None


def add_link(item: LinkInput):
    if not item.url:
        raise HTTPException(422, "A link is required.")
    with connect() as conn:
        existing = conn.execute("SELECT * FROM vault_links WHERE url = ?", (item.url,)).fetchone()
        if existing:
            return dict(existing)
        priority = conn.execute("SELECT COALESCE(MIN(priority), 1) - 1 AS value FROM vault_links WHERE discarded = 0").fetchone()["value"]
        row = dict(id=uuid4().hex, url=item.url, title=item.title.strip() or urlsplit(item.url).hostname,
                   source=item.source.strip(), shared_at=(item.shared_at or datetime.now(timezone.utc)).isoformat(),
                   slack_url=item.slack_url, priority=priority, discarded=0)
        conn.execute("""INSERT INTO vault_links (id, url, title, source, shared_at, slack_url, priority, discarded)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(url) DO NOTHING""", tuple(row.values()))
        return dict(conn.execute("SELECT * FROM vault_links WHERE url = ?", (item.url,)).fetchone())


@router.get("", dependencies=[Depends(require_dev)])
def list_links(response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    with connect() as conn:
        return {"items": [dict(row) for row in conn.execute("SELECT * FROM vault_links ORDER BY priority, shared_at DESC, id").fetchall()]}


@router.post("", dependencies=[Depends(require_dev)])
def create_link(item: LinkInput, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    return enrich_link(add_link(item)["id"])


@router.patch("/{link_id}", dependencies=[Depends(require_dev)])
def update_link(link_id: str, item: LinkUpdate, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    if item.done and item.discarded:
        raise HTTPException(422, "A link cannot be done and discarded at the same time.")
    with connect() as conn:
        row = conn.execute("SELECT * FROM vault_links WHERE id = ?", (link_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Link not found.")
        if item.priority is not None:
            conn.execute("UPDATE vault_links SET priority = ? WHERE id = ?", (item.priority, link_id))
        if item.discarded is not None:
            conn.execute("UPDATE vault_links SET discarded = ?, done = CASE WHEN ? = 1 THEN 0 ELSE done END WHERE id = ?", (int(item.discarded), int(item.discarded), link_id))
        if item.done is not None:
            conn.execute("UPDATE vault_links SET done = ?, discarded = CASE WHEN ? = 1 THEN 0 ELSE discarded END WHERE id = ?", (int(item.done), int(item.done), link_id))
        return dict(conn.execute("SELECT * FROM vault_links WHERE id = ?", (link_id,)).fetchone())


def enrich_link(link_id: str):
    with connect() as conn:
        row = conn.execute("SELECT * FROM vault_links WHERE id = ?", (link_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Link not found.")
    if row["text_status"] in {"ready", "not_applicable"}:
        return dict(row)
    preview = fetch_tweet_text(row["url"])
    with connect() as conn:
        # Preserve a successful result if another request finished first.
        conn.execute("""UPDATE vault_links SET tweet_text = ?, tweet_author = ?, tweet_image = ?, tweet_avatar = ?, tweet_media_type = ?, text_status = ?
            WHERE id = ? AND text_status NOT IN ('ready', 'not_applicable')""",
            (preview["tweet_text"], preview["tweet_author"], preview.get("tweet_image", ""), preview.get("tweet_avatar", ""), preview.get("tweet_media_type", ""), preview["text_status"], link_id))
        return dict(conn.execute("SELECT * FROM vault_links WHERE id = ?", (link_id,)).fetchone())


@router.post("/{link_id}/text", dependencies=[Depends(require_dev)])
def load_tweet_text(link_id: str, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    return enrich_link(link_id)


@router.post("/{link_id}/pool", dependencies=[Depends(require_dev)])
def send_to_pool(link_id: str, request: Request, response: Response):
    """Atomically create one ordinary Queue request from a Vault source."""
    from .main import _queue_v2_log, _queue_v2_publish, _queue_v2_priority
    response.headers["Cache-Control"] = "private, no-store"
    caller = getattr(request.state, "user_email", "")
    if not caller:
        raise HTTPException(401, "Sign in required.")
    with connect() as conn:
        # A write locks this row on both supported databases until commit.
        # Creation and the link back to Vault must succeed or roll back together.
        conn.execute("UPDATE vault_links SET pool_request_id = pool_request_id WHERE id = ?", (link_id,))
        row = conn.execute("SELECT * FROM vault_links WHERE id = ?", (link_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Link not found.")
        if row["discarded"]:
            raise HTTPException(409, "Restore this link before sending it to the Pool.")
        if row["pool_request_id"]:
            return dict(row)
        shortcode = f"vault-{link_id}"
        existing = conn.execute("SELECT id FROM queue_requests WHERE post_account = '' AND post_shortcode = ?", (shortcode,)).fetchone()
        if existing:
            request_id = existing["id"]
        else:
            now = utc_now()
            title = (row["tweet_text"].splitlines()[0] if row["tweet_text"] else row["title"])[:160]
            post_type = "Reel" if row["tweet_media_type"] in {"video", "animated_gif"} else "Image"
            # Only the chosen source goes to the shared Pool, never DM provenance.
            cursor = conn.execute("""INSERT INTO queue_requests (
                post_account, post_shortcode, post_title, is_custom, post_permalink,
                post_caption, post_type, cover_url, production_points, priority,
                deadline_at, tags, brief, notes, reference_links, coordinator_email,
                created_at, updated_at
            ) VALUES ('', ?, ?, 1, ?, ?, ?, ?, 3, ?, '', '[]', ?, '', ?, ?, ?, ?)""",
                (shortcode, title, row["url"], row["tweet_text"], post_type, row["tweet_image"],
                 _queue_v2_priority("normal"), row["tweet_text"], json.dumps([row["url"]]), caller, now, now))
            request_id = int(cursor.lastrowid)
            _queue_v2_log(conn, request_id, caller, "created", {"title": title, "postType": post_type, "productionPoints": 3, "sourceUrl": row["url"]})
            _queue_v2_publish(conn, "created", caller, [request_id])
        conn.execute("UPDATE vault_links SET pool_request_id = ? WHERE id = ?", (request_id, link_id))
        return dict(conn.execute("SELECT * FROM vault_links WHERE id = ?", (link_id,)).fetchone())
