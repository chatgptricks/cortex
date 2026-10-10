"""Bounded, read-only collaboration projection for the Research catalogue."""
from __future__ import annotations

from typing import Any

from .account_media_kit import _METRICS, _row_quality
from .public_collaboration import public_collaboration, stored_collaboration


_BATCH_SIZE = 400


def _columns(conn: Any, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _compact_raw(conn: Any) -> str:
    """Read only coauthors, owner and identity, never carousel/media payloads."""
    if getattr(conn, "is_postgres", False):
        payload = "collaboration_payload"
        return f"""(jsonb_build_object(
            'ownerUsername', {payload}->'ownerUsername',
            'owner', jsonb_build_object('username', {payload}#>'{{owner,username}}'),
            'shortCode', {payload}->'shortCode', 'shortcode', {payload}->'shortcode'
        ) || CASE WHEN jsonb_typeof({payload}) = 'object' AND jsonb_exists({payload}, 'coauthorProducers')
            THEN jsonb_build_object('coauthorProducers', {payload}->'coauthorProducers')
            ELSE '{{}}'::jsonb END)::text"""
    owner = """'ownerUsername', json_extract(raw_json, '$.ownerUsername'),
        'owner', json_object('username', json_extract(raw_json, '$.owner.username')),
        'shortCode', json_extract(raw_json, '$.shortCode'),
        'shortcode', json_extract(raw_json, '$.shortcode')"""
    return f"""CASE WHEN json_valid(raw_json) THEN
        CASE WHEN json_type(raw_json, '$.coauthorProducers') IS NOT NULL
            THEN json_object({owner}, 'coauthorProducers', json_extract(raw_json, '$.coauthorProducers'))
            ELSE json_object({owner}) END
        ELSE NULL END"""


def _metadata_rows(conn: Any, table: str, columns: set[str], where: str,
                   params: list[Any]) -> list[dict[str, Any]]:
    selected = [name for name in ("id", "account", "shortcode", "coauthors", "published_at", "enriched_at", "updated_at", "observed_at")
                if name in columns]
    selected.extend(sorted(columns & set(_METRICS)))
    raw = _compact_raw(conn) if "raw_json" in columns else "NULL"
    guarded = bool(getattr(conn, "is_postgres", False) and "raw_json" in columns)
    # The lateral subquery's OFFSET prevents flattening, so Postgres parses
    # each large stored payload once rather than once per extracted field.
    payload_join = """ CROSS JOIN LATERAL (
        SELECT NULLIF(TRIM(raw_json), '')::jsonb AS collaboration_payload OFFSET 0
    ) collaboration_metadata""" if guarded else ""
    query = f"SELECT {', '.join(selected)}, {raw} AS raw_json FROM {table}{payload_join} WHERE {where}"
    # Stored payloads are serialized JSON. A malformed legacy value must
    # remain unknown, not break the entire page or leave Postgres aborted.
    if guarded:
        conn.execute("SAVEPOINT research_collaboration_json")
    try:
        rows = conn.execute(query, params).fetchall()
    except Exception as error:
        if not guarded:
            raise
        conn.execute("ROLLBACK TO SAVEPOINT research_collaboration_json")
        if getattr(error, "sqlstate", None) != "22P02":
            raise
        # Only this at-most-400-shortcode batch uses full payloads in the
        # exceptional malformed-JSON path; normal pages transfer tiny facts.
        rows = conn.execute(f"SELECT {', '.join(selected)}, raw_json FROM {table} WHERE {where}", params).fetchall()
    finally:
        if guarded:
            conn.execute("RELEASE SAVEPOINT research_collaboration_json")
    return [dict(row) | {"_table": table} for row in rows]


def collaboration_by_post(conn: Any, posts: list[dict[str, Any]], *, canonical_handle: str = "chatgptricks") -> dict[tuple[str, str], dict[str, Any]]:
    """Resolve selected account+shortcode pairs across stored sources only.

    Queries are bounded by the page's shortcodes. Canonical duplicates and a
    matching dashboard copy contribute their newest usable explicit facts;
    another account's row cannot supply an account-relative classification.
    """
    keys = {(str(post.get("account") or "").strip().removeprefix("@").lower(),
             str(post.get("shortcode") or "").strip()) for post in posts}
    keys = {(account, code) for account, code in keys if account and code}
    if not keys:
        return {}
    canonical = canonical_handle.strip().removeprefix("@").lower()
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {key: [] for key in keys}
    account_variants: dict[str, set[str]] = {}
    for post in posts:
        original = str(post.get("account") or "").strip().removeprefix("@")
        account_variants.setdefault(original.lower(), set()).update((original, original.lower()))
    observations: dict[str, dict[str, Any]] = {}
    columns = {table: _columns(conn, table) for table in ("posts", "dashboard_posts", "engagement_observations")}
    codes = sorted({code for _, code in keys})
    for start in range(0, len(codes), _BATCH_SIZE):
        batch = codes[start:start + _BATCH_SIZE]
        placeholders = ",".join("?" for _ in batch)
        canonical_codes = [code for code in batch if (canonical, code) in keys]
        if canonical_codes and "shortcode" in columns["posts"]:
            canonical_marks = ",".join("?" for _ in canonical_codes)
            for row in _metadata_rows(conn, "posts", columns["posts"], f"shortcode IN ({canonical_marks})", canonical_codes):
                grouped[(canonical, str(row["shortcode"]).strip())].append(row)
        batch_codes = set(batch)
        accounts = sorted({variant for account, code in keys if code in batch_codes
                           for variant in account_variants.get(account, {account})})
        if {"account", "shortcode"} <= columns["dashboard_posts"]:
            for account_start in range(0, len(accounts), _BATCH_SIZE):
                account_batch = accounts[account_start:account_start + _BATCH_SIZE]
                account_marks = ",".join("?" for _ in account_batch)
                for row in _metadata_rows(conn, "dashboard_posts", columns["dashboard_posts"],
                                          f"shortcode IN ({placeholders}) AND account IN ({account_marks})", [*batch, *account_batch]):
                    key = (str(row["account"]).strip().lower(), str(row["shortcode"]).strip())
                    if key in grouped:
                        grouped[key].append(row)
        if {"shortcode", "observed_at", "raw_json"} <= columns["engagement_observations"]:
            for row in _metadata_rows(conn, "engagement_observations", columns["engagement_observations"],
                                      f"shortcode IN ({placeholders})", batch):
                observations[str(row["shortcode"]).strip()] = row
    result = {}
    for key, records in grouped.items():
        account, code = key
        # Match public media-kit tie-breaking too: same timestamp duplicates
        # prefer the richer published/current record before source fallback.
        records.sort(key=_row_quality, reverse=True)
        # Include the requested identity even when only an observation has
        # explicit metadata, so foreign observation payloads are rejected.
        annotation = stored_collaboration(records or [{"shortcode": code}], account, observations.get(code))
        fact = public_collaboration({"_public_collaboration": annotation}, account)
        result[key] = {"isCollab": fact["is_collab"], "collaborators": fact["collaborators"]}
    return result


def attach_collaboration(conn: Any, posts: list[dict[str, Any]], *, canonical_handle: str = "chatgptricks") -> None:
    facts = collaboration_by_post(conn, posts, canonical_handle=canonical_handle)
    for post in posts:
        key = (str(post.get("account") or "").strip().removeprefix("@").lower(),
               str(post.get("shortcode") or "").strip())
        post.update(facts.get(key, {"isCollab": None, "collaborators": []}))


def collaboration_updates(conn: Any, codes: list[str]) -> list[dict[str, Any]]:
    """Account-scoped collaboration deltas for bounded engagement updates."""
    codes = sorted({str(code).strip() for code in codes if code})
    if not codes:
        return []
    keys = set()
    columns = {table: _columns(conn, table) for table in ("posts", "dashboard_posts")}
    canonical_handle = "chatgptricks"
    if {"handle", "is_canonical"} <= _columns(conn, "accounts"):
        canonical = conn.execute("SELECT handle FROM accounts WHERE is_canonical = 1 ORDER BY handle LIMIT 1").fetchone()
        if canonical:
            canonical_handle = str(canonical["handle"]).strip().lower()
    for start in range(0, len(codes), _BATCH_SIZE):
        batch = codes[start:start + _BATCH_SIZE]
        placeholders = ",".join("?" for _ in batch)
        if "shortcode" in columns["posts"]:
            rows = conn.execute(f"SELECT shortcode FROM posts WHERE shortcode IN ({placeholders})", batch).fetchall()
            keys.update((canonical_handle, str(row["shortcode"]).strip()) for row in rows)
        if {"account", "shortcode"} <= columns["dashboard_posts"]:
            rows = conn.execute(f"SELECT account, shortcode FROM dashboard_posts WHERE shortcode IN ({placeholders})", batch).fetchall()
            keys.update((str(row["account"]).strip().lower(), str(row["shortcode"]).strip()) for row in rows)
    posts = [{"account": account, "shortcode": code} for account, code in sorted(keys)]
    attach_collaboration(conn, posts, canonical_handle=canonical_handle)
    return posts
