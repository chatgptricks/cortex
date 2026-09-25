"""Private DEV link collection. No third-party scraping or paid calls."""
from datetime import datetime, timezone
from uuid import uuid4
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from .db import connect

router = APIRouter(prefix="/api/dashboard/vault", tags=["vault"])


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vault_links (
        id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '', shared_at TEXT NOT NULL,
        slack_url TEXT NOT NULL DEFAULT '', priority DOUBLE PRECISION NOT NULL,
        discarded INTEGER NOT NULL DEFAULT 0
    )""")


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
    return add_link(item)


@router.patch("/{link_id}", dependencies=[Depends(require_dev)])
def update_link(link_id: str, item: LinkUpdate, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    with connect() as conn:
        row = conn.execute("SELECT * FROM vault_links WHERE id = ?", (link_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Link not found.")
        if item.priority is not None:
            conn.execute("UPDATE vault_links SET priority = ? WHERE id = ?", (item.priority, link_id))
        if item.discarded is not None:
            conn.execute("UPDATE vault_links SET discarded = ? WHERE id = ?", (int(item.discarded), link_id))
        return dict(conn.execute("SELECT * FROM vault_links WHERE id = ?", (link_id,)).fetchone())
