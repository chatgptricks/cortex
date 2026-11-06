from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import sqlite3
import threading

import pytest

from app import insights_music_cache as cache


ACCOUNTS = [
    {"handle": "alpha", "group": "sentient", "is_canonical": True, "label": "Alpha", "hot_threshold": 600},
    {"handle": "beta", "group": "competitors", "is_canonical": False, "label": "Beta", "hot_threshold": 900},
]
NOW = datetime(2026, 10, 26, 18, tzinfo=UTC)


def report(marker="original", *, at=NOW, accounts=ACCOUNTS):
    posts = [{
        "a": account["handle"], "aa": [account["handle"]], "g": account["group"],
        "sc": f"{marker}-{index}", "d": "2026-10-01T12:00:00+00:00",
        "l": 100, "c": 5, "t": "Video", "pt": "clips", "v": 1000,
        "pl": 1200, "dur": 12.5, "sl": None, "hot": 0, "hm": None,
        "l24": 50, "l48": 100, "u": f"https://www.instagram.com/p/{marker}-{index}/",
        "mu": at.isoformat(), "ms": "Song", "ma": "Artist", "ai": "123456", "oa": False,
    } for index, account in enumerate(accounts)]
    return {
        "posts": posts,
        "accounts": [{"handle": account["handle"], "group": account["group"],
                      "threshold": account["hot_threshold"], "label": account["label"]} for account in accounts],
        "coverage": {"total_posts": len(posts), "metadata_posts": len(posts), "unknown_audio_posts": 0,
                     "audio_id_posts": len(posts), "excluded_posts": 0,
                     "source": "stored_metadata", "generated_at": at.isoformat()},
    }


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(cache, "_ENTRIES", OrderedDict())
    monkeypatch.setattr(cache, "_INFLIGHT", {})
    monkeypatch.setattr(cache, "_RETRY_AFTER", {})
    clock = [NOW]
    monkeypatch.setattr(cache, "_now", lambda: clock[0])
    return clock


@pytest.fixture
def stored(tmp_path):
    filename = tmp_path / "music-cache.sqlite3"

    @contextmanager
    def connect():
        connection = sqlite3.connect(filename, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    with connect() as connection:
        cache.ensure_schema(connection)
    return connect


def seed(connect, payload, *, completed_at=NOW, accounts=ACCOUNTS):
    with connect() as connection:
        assert cache.save(connection, cache.cache_key(accounts), accounts, payload, completed_at)


def test_key_is_order_independent_and_exactly_scopes_roster():
    key = cache.cache_key(ACCOUNTS)
    assert cache.cache_key(list(reversed(ACCOUNTS))) == key
    assert cache.cache_key([{**account, "handle": account["handle"].upper()} for account in ACCOUNTS]) == key
    assert cache.cache_key(ACCOUNTS[:1]) != key
    for field, value in [("label", "Renamed"), ("group", "changed"), ("hot_threshold", 999), ("is_canonical", False)]:
        changed = [{**ACCOUNTS[0], field: value}, ACCOUNTS[1]]
        assert cache.cache_key(changed) != key


def test_process_restart_restores_complete_report_without_running_loader(stored):
    source = report(at=NOW - timedelta(minutes=1))
    seed(stored, source)
    cache._ENTRIES.clear()

    def unexpected_loader():
        pytest.fail("A durable complete report must survive a process restart")

    result = cache.cached_report(ACCOUNTS, unexpected_loader, database_identity="db", connection_factory=stored)
    assert result["posts"] == source["posts"]
    assert result["coverage"]["generated_at"] == source["coverage"]["generated_at"]
    assert result["cache"]["stale"] is False


def test_stale_report_returns_while_one_background_refresh_is_blocked(stored):
    old = report(at=NOW - timedelta(minutes=10))
    seed(stored, old, completed_at=NOW - timedelta(minutes=6))
    started, release = threading.Event(), threading.Event()
    calls = []

    def loader():
        calls.append(1)
        started.set()
        assert release.wait(5)
        return report("refreshed")

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            readers = [pool.submit(cache.cached_report, ACCOUNTS, loader,
                                   database_identity="db", connection_factory=stored) for _ in range(4)]
            results = [future.result(timeout=2) for future in readers]
        assert started.wait(2)
        assert len(calls) == 1
        assert all(value["posts"] == old["posts"] for value in results)
        assert all(value["cache"]["stale"] for value in results)
        assert all(value["coverage"]["generated_at"] == old["coverage"]["generated_at"] for value in results)
        futures = list(cache._INFLIGHT.values())
        assert len(futures) == 1
    finally:
        release.set()
    for future in futures:
        future.result(timeout=5)
    with stored() as connection:
        restored = cache.load(connection, cache.cache_key(ACCOUNTS), ACCOUNTS, NOW)
    assert restored.payload["posts"][0]["sc"] == "refreshed-0"


@pytest.mark.parametrize("damage", ["partial", "private-account", "invalid-json", "malformed-roster", "future", "ancient"])
def test_invalid_or_out_of_scope_persisted_report_is_not_servable(stored, damage):
    payload = report()
    seed(stored, payload)
    if damage == "partial":
        payload["posts"].pop()
    elif damage == "private-account":
        payload["posts"][0]["aa"].append("private")
    elif damage == "malformed-roster":
        payload["accounts"] = [None]
    elif damage in {"future", "ancient"}:
        generated = NOW + timedelta(days=1) if damage == "future" else NOW - timedelta(days=2)
        payload["coverage"]["generated_at"] = generated.isoformat()
    raw = "{broken" if damage == "invalid-json" else json.dumps(payload)
    with stored() as connection:
        connection.execute("UPDATE insights_music_cache SET payload_json = ?, generated_at = ? WHERE cache_key = ?",
                           (raw, payload["coverage"]["generated_at"], cache.cache_key(ACCOUNTS)))
        assert cache.load(connection, cache.cache_key(ACCOUNTS), ACCOUNTS, NOW) is None


def test_old_generation_or_lost_owner_cannot_replace_newer_report(stored):
    seed(stored, report("new", at=NOW))
    key = cache.cache_key(ACCOUNTS)
    with stored() as connection:
        assert not cache.save(connection, key, ACCOUNTS, report("old", at=NOW - timedelta(minutes=1)), NOW)
        connection.execute("UPDATE insights_music_cache SET refresh_owner = ?, lease_until = ? WHERE cache_key = ?",
                           ("active-owner", (NOW + timedelta(seconds=300)).isoformat(), key))
        assert not cache.save(connection, key, ACCOUNTS, report("lost", at=NOW),
                              NOW, owner="lost-owner")
        assert cache.load(connection, key, ACCOUNTS, NOW).payload["posts"][0]["sc"] == "new-0"


def test_missing_lease_row_cannot_be_recreated_by_a_lost_owner(stored):
    key = cache.cache_key(ACCOUNTS)
    with stored() as connection:
        assert not cache.save(connection, key, ACCOUNTS, report(), NOW, owner="expired-owner")
        assert cache.load(connection, key, ACCOUNTS, NOW) is None


def test_durable_lease_has_one_owner_and_expired_owner_cannot_publish(stored):
    key = cache.cache_key(ACCOUNTS)
    seed(stored, report())
    later = NOW + timedelta(seconds=cache.LEASE_SECONDS + 1)
    with stored() as connection:
        assert cache.claim_refresh(connection, key, "first", NOW)
        assert not cache.claim_refresh(connection, key, "second", NOW)
        assert cache.load(connection, key, ACCOUNTS, NOW).payload["posts"][0]["sc"] == "original-0"
        assert cache.claim_refresh(connection, key, "second", later)
        assert not cache.save(connection, key, ACCOUNTS, report("old-owner", at=later), later, owner="first")
        assert cache.save(connection, key, ACCOUNTS, report("active-owner", at=later), later, owner="second")
        assert cache.load(connection, key, ACCOUNTS, later).payload["posts"][0]["sc"] == "active-owner-0"


def test_durable_retry_backoff_survives_process_local_cache_clear(stored):
    key = cache.cache_key(ACCOUNTS)
    seed(stored, report())
    until = NOW + timedelta(seconds=cache.RETRY_SECONDS)
    with stored() as connection:
        connection.execute("UPDATE insights_music_cache SET retry_after = ? WHERE cache_key = ?", (until.isoformat(), key))
        cache._RETRY_AFTER.clear()
        assert not cache.claim_refresh(connection, key, "early", until - timedelta(seconds=1))
        assert cache.claim_refresh(connection, key, "ready", until)


def test_failed_background_refresh_retains_report_and_respects_retry_backoff(stored, isolated):
    source = report(at=NOW - timedelta(minutes=10))
    seed(stored, source, completed_at=NOW - timedelta(minutes=6))
    started, release, recovered = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def loader():
        calls.append(1)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
            raise RuntimeError("temporary read failure")
        recovered.set()
        return report("recovered", at=isolated[0])

    try:
        result = cache.cached_report(ACCOUNTS, loader, database_identity="db", connection_factory=stored)
        assert result["posts"] == source["posts"]
        assert started.wait(2)
        future = next(iter(cache._INFLIGHT.values()))
    finally:
        release.set()
    with pytest.raises(RuntimeError, match="temporary read failure"):
        future.result(timeout=5)
    for _ in range(3):
        assert cache.cached_report(ACCOUNTS, loader, database_identity="db", connection_factory=stored)["posts"] == source["posts"]
    assert len(calls) == 1
    with stored() as connection:
        assert cache.load(connection, cache.cache_key(ACCOUNTS), ACCOUNTS, NOW).payload["posts"] == source["posts"]
        retry = connection.execute("SELECT retry_after FROM insights_music_cache WHERE cache_key = ?", (cache.cache_key(ACCOUNTS),)).fetchone()[0]
    assert datetime.fromisoformat(retry) > NOW
    isolated[0] += timedelta(seconds=cache.RETRY_SECONDS + 1)
    cache.cached_report(ACCOUNTS, loader, database_identity="db", connection_factory=stored)
    assert recovered.wait(2)
    for future in list(cache._INFLIGHT.values()):
        future.result(timeout=5)
    with stored() as connection:
        assert cache.load(connection, cache.cache_key(ACCOUNTS), ACCOUNTS, isolated[0]).payload["posts"][0]["sc"] == "recovered-0"


def test_current_roster_never_accepts_previously_authorized_account_report(stored):
    seed(stored, report())
    narrowed = ACCOUNTS[:1]
    with stored() as connection:
        assert cache.load(connection, cache.cache_key(narrowed), narrowed, NOW) is None
        assert cache.load(connection, cache.cache_key(ACCOUNTS), narrowed, NOW) is None


def test_completed_report_retention_is_bounded(stored):
    for index in range(5):
        accounts = [{**account, "label": f"Version {index} {account['handle']}"} for account in ACCOUNTS]
        seed(stored, report(str(index), accounts=accounts), accounts=accounts,
             completed_at=NOW + timedelta(seconds=index))
    with stored() as connection:
        count = connection.execute("SELECT COUNT(*) FROM insights_music_cache WHERE payload_json IS NOT NULL").fetchone()[0]
    assert count <= 2, "Roster edits must not retain an unbounded sequence of 25 MB reports"
