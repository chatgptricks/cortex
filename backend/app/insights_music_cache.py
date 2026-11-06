"""Durable complete Music reports with bounded stale reads and leased refreshes."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import math
import threading
import time
import uuid
from typing import Any, Callable

SCHEMA_VERSION = 1
FRESH_SECONDS = 300
MAX_AGE_SECONDS = 86_400
LEASE_SECONDS = 300
RENEW_SECONDS = 30
RETRY_SECONDS = 60
_LOCK = threading.Lock()
_ENTRIES: OrderedDict[tuple[str, str], "Snapshot"] = OrderedDict()
_INFLIGHT: dict[tuple[str, str], Future["Snapshot"]] = {}
_RETRY_AFTER: dict[tuple[str, str], datetime] = {}
logger = logging.getLogger("uvicorn.error")
_POST_KEYS = set("a aa g sc d l c t pt v pl dur sl hot hm l24 l48 u mu ms ma ai oa".split())
_COVERAGE_KEYS = set("total_posts metadata_posts unknown_audio_posts audio_id_posts excluded_posts source generated_at".split())


@dataclass(frozen=True)
class Snapshot:
    payload: dict[str, Any]
    generated_at: datetime
    completed_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def _date(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _roster(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(({
        "handle": str(account["handle"]).strip().lower(), "group": account.get("group"),
        "label": account.get("label"), "threshold": account.get("hot_threshold"),
        "is_canonical": bool(account.get("is_canonical")),
    } for account in accounts), key=lambda account: account["handle"])


def cache_key(accounts: list[dict[str, Any]]) -> str:
    encoded = json.dumps([SCHEMA_VERSION, _roster(accounts)], sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def ensure_schema(conn: Any) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS insights_music_cache (
        cache_key TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
        generated_at TEXT, completed_at TEXT, payload_json TEXT,
        refresh_owner TEXT NOT NULL DEFAULT '', lease_until TEXT, retry_after TEXT
    )""")


def _validated(payload: Any, accounts: list[dict[str, Any]], completed_at: datetime) -> Snapshot:
    if not isinstance(payload, dict) or set(payload) != {"posts", "accounts", "coverage"}:
        raise ValueError("Incomplete Music report")
    posts, roster, coverage = payload["posts"], payload["accounts"], payload["coverage"]
    expected = [{key: value for key, value in account.items() if key != "is_canonical"} for account in _roster(accounts)]
    if not isinstance(roster, list) or sorted(roster, key=lambda account: account.get("handle", "")) != expected:
        raise ValueError("Music report roster mismatch")
    if not isinstance(posts, list) or not isinstance(coverage, dict) or set(coverage) != _COVERAGE_KEYS:
        raise ValueError("Incomplete Music report")
    generated_at = _date(coverage.get("generated_at"))
    if generated_at is None or generated_at > completed_at or coverage.get("source") != "stored_metadata":
        raise ValueError("Invalid Music report timestamp")
    handles = {account["handle"] for account in expected}
    codes: set[str] = set()
    known = audio_ids = 0
    for post in posts:
        if not isinstance(post, dict) or set(post) != _POST_KEYS:
            raise ValueError("Incomplete Music post")
        code = post["sc"]
        if not isinstance(code, str) or not code or code in codes or post["a"] not in handles:
            raise ValueError("Invalid Music post identity")
        codes.add(code)
        if not isinstance(post["aa"], list) or post["a"] not in post["aa"] or not set(post["aa"]).issubset(handles):
            raise ValueError("Invalid Music post account scope")
        if post["oa"] is not None and type(post["oa"]) is not bool:
            raise ValueError("Invalid Music audio flag")
        if post["ai"] is not None and not isinstance(post["ai"], str):
            raise ValueError("Invalid Music audio identity")
        if any(post[name] is not None and not isinstance(post[name], str) for name in ("d", "t", "pt", "u", "mu", "ms", "ma")):
            raise ValueError("Invalid Music text field")
        for name in ("l", "c", "v", "pl", "dur", "sl", "hot", "hm", "l24", "l48"):
            value = post[name]
            if value is not None and (type(value) not in {int, float} or not math.isfinite(value)):
                raise ValueError("Invalid Music metric")
        known += bool(post["ms"] or post["ma"] or post["ai"] or post["oa"] is True)
        audio_ids += bool(post["ai"])
    counts = {"total_posts": len(posts), "metadata_posts": known,
              "unknown_audio_posts": len(posts) - known, "audio_id_posts": audio_ids}
    if any(type(coverage.get(key)) is not int or coverage[key] != value for key, value in counts.items()):
        raise ValueError("Incomplete Music report coverage")
    if type(coverage.get("excluded_posts")) is not int or coverage["excluded_posts"] < 0:
        raise ValueError("Invalid Music exclusion count")
    return Snapshot(payload, generated_at, completed_at)


def load(conn: Any, key: str, accounts: list[dict[str, Any]], current: datetime) -> Snapshot | None:
    if key != cache_key(accounts):
        return None
    row = conn.execute("SELECT schema_version, generated_at, completed_at, payload_json FROM insights_music_cache WHERE cache_key = ?", (key,)).fetchone()
    if not row or row["schema_version"] != SCHEMA_VERSION or not row["payload_json"]:
        return None
    generated, completed = _date(row["generated_at"]), _date(row["completed_at"])
    if generated is None or completed is None or completed > current or not 0 <= (current - generated).total_seconds() <= MAX_AGE_SECONDS:
        return None
    try:
        snapshot = _validated(json.loads(row["payload_json"]), accounts, completed)
        return snapshot if snapshot.generated_at == generated else None
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def save(conn: Any, key: str, accounts: list[dict[str, Any]], payload: dict[str, Any],
         completed_at: datetime, owner: str | None = None) -> bool:
    if key != cache_key(accounts):
        raise ValueError("Music cache key does not match current roster")
    completed_at = completed_at.astimezone(UTC)
    snapshot = _validated(payload, accounts, completed_at)
    serialized = json.dumps(payload, allow_nan=False, separators=(",", ":"), ensure_ascii=False)
    if owner is not None:
        # An expired owner must not recreate a row removed by retention or
        # replace the report after another process has acquired its lease.
        result = conn.execute("""UPDATE insights_music_cache SET
            schema_version = ?, generated_at = ?, completed_at = ?, payload_json = ?,
            refresh_owner = '', lease_until = NULL, retry_after = NULL
            WHERE cache_key = ? AND refresh_owner = ?
              AND (generated_at IS NULL OR generated_at <= ?)""",
            (SCHEMA_VERSION, snapshot.generated_at.isoformat(), completed_at.isoformat(), serialized,
             key, owner, snapshot.generated_at.isoformat()))
    else:
        result = conn.execute("""INSERT INTO insights_music_cache
            (cache_key, schema_version, generated_at, completed_at, payload_json, refresh_owner)
            VALUES (?, ?, ?, ?, ?, '')
            ON CONFLICT(cache_key) DO UPDATE SET
                schema_version = excluded.schema_version, generated_at = excluded.generated_at,
                completed_at = excluded.completed_at, payload_json = excluded.payload_json,
                refresh_owner = '', lease_until = NULL, retry_after = NULL
            WHERE insights_music_cache.generated_at IS NULL OR insights_music_cache.generated_at <= excluded.generated_at""",
            (key, SCHEMA_VERSION, snapshot.generated_at.isoformat(), completed_at.isoformat(), serialized))
    published = result.rowcount > 0
    if published:
        # Retain at most two completed roster versions, plus any active refresh.
        conn.execute("""DELETE FROM insights_music_cache
            WHERE cache_key NOT IN (SELECT cache_key FROM insights_music_cache
                WHERE payload_json IS NOT NULL ORDER BY completed_at DESC LIMIT 2)
              AND (lease_until IS NULL OR lease_until <= ?)""", (completed_at.isoformat(),))
    return published


def claim_refresh(conn: Any, key: str, owner: str, current: datetime) -> bool:
    conn.execute("INSERT OR IGNORE INTO insights_music_cache (cache_key, schema_version) VALUES (?, ?)", (key, SCHEMA_VERSION))
    result = conn.execute("""UPDATE insights_music_cache SET refresh_owner = ?, lease_until = ?, retry_after = NULL
        WHERE cache_key = ? AND (lease_until IS NULL OR lease_until <= ?)
          AND (retry_after IS NULL OR retry_after <= ?)""",
        (owner, (current + timedelta(seconds=LEASE_SECONDS)).isoformat(), key, current.isoformat(), current.isoformat()))
    return result.rowcount > 0


def _remember(key: tuple[str, str], snapshot: Snapshot) -> Snapshot:
    with _LOCK:
        previous = _ENTRIES.get(key)
        if previous is not None and previous.generated_at > snapshot.generated_at:
            return previous
        _ENTRIES[key] = snapshot
        _ENTRIES.move_to_end(key)
        while len(_ENTRIES) > 2:
            _ENTRIES.popitem(last=False)
    return snapshot


def _build(key: tuple[str, str], accounts: list[dict[str, Any]], loader: Callable[[], dict[str, Any]],
           connection_factory: Callable[[], Any]) -> Snapshot:
    owner = uuid.uuid4().hex
    while True:
        try:
            with connection_factory() as conn:
                claimed = claim_refresh(conn, key[1], owner, _now())
                if not claimed:
                    saved = load(conn, key[1], accounts, _now())
            if claimed:
                break
            if saved is not None:
                return _remember(key, saved)
            time.sleep(1)
        except Exception as error:
            # A cache outage must not prevent a complete stored-data report.
            logger.warning("Music cache lease unavailable error_type=%s", type(error).__name__)
            owner = None
            break
    stop = threading.Event()
    def renew() -> None:
        while not stop.wait(RENEW_SECONDS):
            try:
                with connection_factory() as conn:
                    result = conn.execute("UPDATE insights_music_cache SET lease_until = ? WHERE cache_key = ? AND refresh_owner = ?",
                        ((_now() + timedelta(seconds=LEASE_SECONDS)).isoformat(), key[1], owner))
                    if result.rowcount == 0:
                        return
            except Exception as error:
                logger.warning("Music cache lease renewal failed error_type=%s", type(error).__name__)
    if owner is not None:
        threading.Thread(target=renew, daemon=True, name="music-cache-lease").start()
    try:
        payload = loader()
        snapshot = _validated(payload, accounts, _now())
        try:
            with connection_factory() as conn:
                published = save(conn, key[1], accounts, payload, snapshot.completed_at, owner)
                newer = None if published else load(conn, key[1], accounts, _now())
            if newer is not None and newer.generated_at > snapshot.generated_at:
                snapshot = newer
        except Exception as error:
            logger.warning("Music cache persistence failed error_type=%s", type(error).__name__)
            if owner is not None:
                try:
                    with connection_factory() as conn:
                        conn.execute("UPDATE insights_music_cache SET refresh_owner = '', lease_until = NULL WHERE cache_key = ? AND refresh_owner = ?", (key[1], owner))
                except Exception:
                    pass
        return _remember(key, snapshot)
    except BaseException:
        retry = _now() + timedelta(seconds=RETRY_SECONDS)
        with _LOCK:
            _RETRY_AFTER[key] = retry
        if owner is not None:
            try:
                with connection_factory() as conn:
                    conn.execute("UPDATE insights_music_cache SET refresh_owner = '', lease_until = NULL, retry_after = ? WHERE cache_key = ? AND refresh_owner = ?",
                                 (retry.isoformat(), key[1], owner))
            except Exception as error:
                logger.warning("Music cache failure bookkeeping unavailable error_type=%s", type(error).__name__)
        raise
    finally:
        stop.set()


def _resolve(key: tuple[str, str], accounts: list[dict[str, Any]], loader: Callable[[], dict[str, Any]],
             connection_factory: Callable[[], Any], future: Future[Snapshot], *, restore: bool) -> None:
    try:
        snapshot = None
        if restore:
            try:
                with connection_factory() as conn:
                    snapshot = load(conn, key[1], accounts, _now())
            except Exception as error:
                logger.warning("Music cache restore unavailable error_type=%s", type(error).__name__)
        snapshot = _remember(key, snapshot) if snapshot is not None else _build(key, accounts, loader, connection_factory)
        future.set_result(snapshot)
    except BaseException as error:
        future.set_exception(error)
        logger.warning("Music cache refresh failed error_type=%s", type(error).__name__)
    finally:
        with _LOCK:
            if _INFLIGHT.get(key) is future:
                _INFLIGHT.pop(key, None)


def cached_report(accounts: list[dict[str, Any]], loader: Callable[[], dict[str, Any]], *,
                  database_identity: str, connection_factory: Callable[[], Any]) -> dict[str, Any]:
    """Call only after current authentication and current account resolution."""
    key = (hashlib.sha256(database_identity.encode()).hexdigest(), cache_key(accounts))
    current = _now()
    with _LOCK:
        snapshot = _ENTRIES.get(key)
        if snapshot is not None and not 0 <= (current - snapshot.generated_at).total_seconds() <= MAX_AGE_SECONDS:
            snapshot = None
        pending = _INFLIGHT.get(key)
        cold_owner = snapshot is None and pending is None
        if cold_owner:
            pending = Future()
            _INFLIGHT[key] = pending
    if snapshot is None:
        if cold_owner:
            _resolve(key, accounts, loader, connection_factory, pending, restore=True)
        snapshot = pending.result()
    current = _now()
    stale = (current - snapshot.completed_at).total_seconds() >= FRESH_SECONDS
    with _LOCK:
        pending = _INFLIGHT.get(key)
        if stale and pending is None and _RETRY_AFTER.get(key, datetime.min.replace(tzinfo=UTC)) <= current:
            pending = Future()
            _INFLIGHT[key] = pending
            threading.Thread(target=_resolve, args=(key, accounts, loader, connection_factory, pending),
                             kwargs={"restore": False}, daemon=True, name="music-cache-refresh").start()
        refreshing = pending is not None
    return {**snapshot.payload, "cache": {"stale": stale, "refreshing": refreshing,
             "age_seconds": max(0, int((current - snapshot.generated_at).total_seconds()))}}
