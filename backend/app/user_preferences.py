"""Per-user interface preferences, stored on the server.

Language, theme, accent and the other personal settings used to live only in
one browser's localStorage, so they were lost on another device. The server is
now the source of truth; pages keep at most a first-paint copy.
"""
import json
import re
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from .db import connect, utc_now

router = APIRouter(prefix="/api/dashboard/me/preferences", tags=["preferences"])

_HEX = re.compile(r"^#[0-9a-f]{6}$")
_ACCENT_PRESETS = {"green", "lime", "blue", "coral"}
_HANDLE = re.compile(r"^[a-z0-9._]{1,60}$")


def ensure_schema(conn: Any) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS user_preferences (
        email TEXT PRIMARY KEY,
        preferences TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
    )""")


def _accent(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in _ACCENT_PRESETS or _HEX.match(text):
        return text
    raise ValueError


def _custom_accent(value: Any) -> str:
    text = str(value or "").strip().lower()
    if _HEX.match(text):
        return text
    raise ValueError


def _choice(*allowed: str):
    def validate(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in allowed:
            return text
        raise ValueError
    return validate


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError


def _scope(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) <= 200:
        return text
    raise ValueError


def _handles(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 500:
        raise ValueError
    handles = []
    for item in value:
        handle = str(item or "").strip().lstrip("@").lower()
        if not _HANDLE.match(handle):
            raise ValueError
        if handle not in handles:
            handles.append(handle)
    return handles


VALIDATORS = {
    "language": _choice("en", "es"),
    "theme": _choice("dark", "light"),
    "accent": _accent,
    "accentCustom": _custom_accent,
    "effects": _choice("immersive", "subtle", "off"),
    "queueGuideCompleted": _flag,
    "queueDesignerScope": _scope,
    "trackerFavorites": _handles,
}


def _owner(request: Request) -> str:
    email = str(getattr(request.state, "user_email", "") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return email


def _read(conn: Any, email: str) -> dict[str, Any]:
    row = conn.execute("SELECT preferences FROM user_preferences WHERE email = ?", (email,)).fetchone()
    try:
        stored = json.loads(row["preferences"]) if row else {}
    except (TypeError, ValueError):
        stored = {}
    return {key: value for key, value in stored.items() if key in VALIDATORS} if isinstance(stored, dict) else {}


@router.get("")
def get_preferences(request: Request) -> dict[str, Any]:
    email = _owner(request)
    with connect() as conn:
        ensure_schema(conn)
        return {"preferences": _read(conn, email)}


@router.post("")
def update_preferences(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Merge the given keys; null removes a key. Unknown keys are rejected."""
    email = _owner(request)
    changes = payload.get("preferences") if isinstance(payload, dict) else None
    if not isinstance(changes, dict) or not changes:
        raise HTTPException(status_code=400, detail="Send a non-empty preferences object.")
    unknown = sorted(set(changes) - set(VALIDATORS))
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown preference: {', '.join(unknown)}.")
    with connect() as conn:
        ensure_schema(conn)
        preferences = _read(conn, email)
        for key, value in changes.items():
            if value is None:
                preferences.pop(key, None)
                continue
            try:
                preferences[key] = VALIDATORS[key](value)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid value for {key}.") from None
        conn.execute(
            """INSERT INTO user_preferences (email, preferences, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET preferences = excluded.preferences, updated_at = excluded.updated_at""",
            (email, json.dumps(preferences, ensure_ascii=False), utc_now()),
        )
    return {"preferences": preferences}
