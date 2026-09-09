"""Persistence and batch processing for the hidden Promos workspace."""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from .db import connect, utc_now
from . import db, ingestion_jobs
from .promos_detector import DETECTOR_VERSION, detect_promo


def _initialize_topic_stacks(conn: Any) -> None:
    """Keep Promos usable in older/local databases before stack migration."""
    from .topic_stacks import initialize

    initialize(conn)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _hash_post(post: dict[str, Any]) -> str:
    raw = _json({key: post.get(key) for key in ("caption", "first_comment", "hashtags", "mentions", "paid_partnership", "permalink")})
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_item(row: dict[str, Any]) -> dict[str, Any]:
    analysis = json.loads(row.get("analysis_json") or "{}")
    override = json.loads(row["review_override_json"]) if row.get("review_override_json") else {}
    stack_id = row.get("stack_id") or analysis.get("stack_id")
    stack_size = row.get("stack_size") or analysis.get("stack_size") or 1
    result = {**analysis, "account": row["account"], "shortcode": row["shortcode"], "classification": row["classification"], "client": row.get("client"), "product": row.get("product"), "review_status": row.get("review_status") or "new", "published_at": row.get("published_at"), "first_detected_at": row.get("first_detected_at"), "last_analyzed_at": row.get("last_analyzed_at"), "stack_id": stack_id, "stack_size": int(stack_size), "account_group": row.get("account_group"), "account_group_label": row.get("account_group_label")}
    if override:
        result["overrides"] = override
        result.update({key: value for key, value in override.items() if key in {"client", "product", "classification"} and value is not None})
    return result


def analyze_post(post: dict[str, Any]) -> dict[str, Any]:
    account, shortcode = str(post.get("account") or ""), str(post.get("shortcode") or "")
    if not account or not shortcode:
        raise ValueError("Promo posts require account and shortcode")
    now = utc_now()
    digest = _hash_post(post)
    analysis = detect_promo(post)
    with connect() as conn:
        _initialize_topic_stacks(conn)
        stack = _promo_stack_context(conn, account, shortcode)
        analysis["stack_id"] = stack["stack_id"]
        analysis["stack_size"] = stack["stack_size"]
        analysis["stack_support_count"] = stack["support_count"]
        # A stack is corroborating evidence only after the detector has found
        # a real brand relationship. It can move a relationship-only signal
        # from needs_review to likely when another post in the same stack is
        # already a confirmed promotion; a generic topic match alone never
        # creates a Promo opportunity.
        if analysis["classification"] == "needs_review" and stack["support_count"]:
            analysis["classification"] = "likely"
            analysis["is_promo"] = True
            analysis.setdefault("evidence", []).append({
                "family": "stack",
                "rule": "promo cluster support",
                "source": "topic_stack",
                "text": f"Another post in this {stack['stack_size']}-post stack is a confirmed promotion.",
            })
            analysis["signals"] = sorted(set(analysis.get("signals") or []) | {"promo cluster support"})
        existing = conn.execute("SELECT input_hash FROM promo_scans WHERE account = ? AND shortcode = ?", (account, shortcode)).fetchone()
        first = now
        conn.execute("""INSERT INTO promo_scans(account, shortcode, input_hash, detector_version, status, attempts, updated_at)
                       VALUES (?, ?, ?, ?, 'done', 1, ?)
                       ON CONFLICT(account, shortcode) DO UPDATE SET input_hash = excluded.input_hash, detector_version = excluded.detector_version, status = 'done', attempts = promo_scans.attempts + 1, error = NULL, updated_at = excluded.updated_at""", (account, shortcode, digest, DETECTOR_VERSION, now))
        if analysis["classification"] == "not_promo":
            conn.execute("DELETE FROM promo_opportunities WHERE account = ? AND shortcode = ?", (account, shortcode))
            return {**analysis, "account": account, "shortcode": shortcode, "published_at": post.get("published_at"), "first_detected_at": None, "last_analyzed_at": now, "review_status": "not_promo"}
        previous_row = conn.execute("SELECT first_detected_at, review_status, review_override_json FROM promo_opportunities WHERE account = ? AND shortcode = ?", (account, shortcode)).fetchone()
        previous = dict(previous_row) if previous_row else None
        first = previous["first_detected_at"] if previous and previous.get("first_detected_at") else first
        review_status = previous["review_status"] if previous else "new"
        override = previous["review_override_json"] if previous else None
        conn.execute("""INSERT INTO promo_opportunities(account, shortcode, classification, client, product, analysis_json, review_status, review_override_json, published_at, first_detected_at, last_analyzed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(account, shortcode) DO UPDATE SET classification = excluded.classification, client = excluded.client, product = excluded.product, analysis_json = excluded.analysis_json, published_at = excluded.published_at, last_analyzed_at = excluded.last_analyzed_at""", (account, shortcode, analysis["classification"], analysis.get("client"), analysis.get("product"), _json(analysis), review_status, override, post.get("published_at"), first, now))
    return {**analysis, "account": account, "shortcode": shortcode, "published_at": post.get("published_at"), "first_detected_at": first, "last_analyzed_at": now, "review_status": review_status}


def _promo_stack_context(conn: Any, account: str, shortcode: str) -> dict[str, Any]:
    """Return persisted stack metadata and confirmed promo corroboration."""
    post_key = f"{account}:{shortcode}"
    row = conn.execute("SELECT stack_id FROM topic_stack_members WHERE post_key = ?", (post_key,)).fetchone()
    if not row:
        return {"stack_id": None, "stack_size": 1, "support_count": 0}
    stack_id = row["stack_id"]
    members = conn.execute("SELECT post_key FROM topic_stack_members WHERE stack_id = ?", (stack_id,)).fetchall()
    member_keys = [item["post_key"] for item in members]
    if not member_keys:
        return {"stack_id": stack_id, "stack_size": 1, "support_count": 0}
    marks = ",".join("?" for _ in member_keys)
    peer_rows = conn.execute(
        f"SELECT classification, client FROM promo_opportunities WHERE account || ':' || shortcode IN ({marks}) AND NOT (account = ? AND shortcode = ?)",
        (*member_keys, account, shortcode),
    ).fetchall()
    support_count = sum(1 for peer in peer_rows if peer["classification"] in {"disclosed", "likely"})
    return {"stack_id": stack_id, "stack_size": len(member_keys), "support_count": support_count}


def _opportunity_select(extra: str = "") -> str:
    extra_select = f", {extra}" if extra else ""
    return f"""SELECT o.*, p.id AS post_id, p.cover_image_path, p.cover_source_url, p.permalink{extra_select},
                     sm.stack_id, COALESCE(sc.stack_size, 1) AS stack_size
              FROM promo_opportunities o
              LEFT JOIN dashboard_posts p ON p.account = o.account AND p.shortcode = o.shortcode
              LEFT JOIN topic_stack_members sm ON sm.post_key = o.account || ':' || o.shortcode
              LEFT JOIN (SELECT stack_id, COUNT(*) AS stack_size FROM topic_stack_members GROUP BY stack_id) sc ON sc.stack_id = sm.stack_id"""


def _account_metadata(conn: Any, account: str) -> dict[str, Any]:
    try:
        row = conn.execute("SELECT group_name AS account_group, label AS account_group_label FROM accounts WHERE handle = ?", (account,)).fetchone()
    except Exception:
        # Lightweight promo unit tests and older local databases may not have
        # the shared account catalog yet.
        return {}
    return dict(row) if row else {}


def _post_rows(conn: Any, account: str | None = None, from_date: str | None = None, to_date: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    clauses, params = ["a.group_name = 'competitors'", "p.account = a.handle"], []
    if account:
        clauses.append("p.account = ?"); params.append(account)
    if from_date:
        clauses.append("p.published_at >= ?"); params.append(from_date)
    if to_date:
        clauses.append("p.published_at <= ?"); params.append(to_date)
    rows = conn.execute(f"""SELECT p.account, p.shortcode, p.caption, p.first_comment, p.hashtags, p.mentions, p.paid_partnership, p.permalink, p.published_at, p.cover_image_path, p.cover_source_url
                            FROM dashboard_posts p JOIN accounts a ON a.handle = p.account
                            WHERE {' AND '.join(clauses)} ORDER BY p.published_at DESC LIMIT ?""", (*params, max(1, min(limit, 2000)))).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in ("hashtags", "mentions"):
            if item.get(key): item[key] = [value.strip() for value in str(item[key]).split(",") if value.strip()]
        result.append(item)
    return result


def process_posts(*, account: str | None = None, from_date: str | None = None, to_date: str | None = None, limit: int = 500, job_id: str | None = None) -> dict[str, int]:
    with connect() as conn:
        posts = _post_rows(conn, account, from_date, to_date, limit)
    processed = 0
    for post in posts:
        analyze_post(post)
        processed += 1
        if job_id:
            with connect() as conn:
                conn.execute("UPDATE promo_jobs SET processed = ?, heartbeat_at = ?, updated_at = ? WHERE job_id = ?", (processed, utc_now(), utc_now(), job_id))
    if job_id:
        with connect() as conn:
            conn.execute("UPDATE promo_jobs SET status = 'done', total = ?, processed = ?, finished_at = ?, updated_at = ? WHERE job_id = ?", (len(posts), processed, utc_now(), utc_now(), job_id))
    return {"processed": processed, "total": len(posts)}


_WAKE = threading.Event()
_WORKER_LOCK = threading.Lock()
_WORKER_STARTED = False


def _ensure_jobs(conn: Any) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS promo_jobs (
        job_id TEXT PRIMARY KEY, status TEXT NOT NULL, requested_from TEXT,
        requested_to TEXT, processed INTEGER NOT NULL DEFAULT 0,
        total INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, account TEXT, row_limit INTEGER NOT NULL DEFAULT 500,
        heartbeat_at TEXT, finished_at TEXT)""")
    for name, definition in (("account", "account TEXT"), ("row_limit", "row_limit INTEGER NOT NULL DEFAULT 500"), ("heartbeat_at", "heartbeat_at TEXT"), ("finished_at", "finished_at TEXT")):
        db._ensure_column(conn, "promo_jobs", name, definition)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_promo_jobs_status ON promo_jobs(status, created_at)")


def create_backfill(*, from_date: str | None = None, to_date: str | None = None, account: str | None = None, limit: int = 500) -> str:
    now = utc_now()
    with connect() as conn:
        _ensure_jobs(conn)
        existing = conn.execute("""SELECT job_id FROM promo_jobs WHERE status IN ('queued', 'running')
            AND COALESCE(requested_from, '') = COALESCE(?, '') AND COALESCE(requested_to, '') = COALESCE(?, '')
            AND COALESCE(account, '') = COALESCE(?, '') LIMIT 1""", (from_date, to_date, account)).fetchone()
        if existing:
            job_id = str(existing["job_id"])
        else:
            job_id = uuid.uuid4().hex
            conn.execute("""INSERT INTO promo_jobs(job_id, status, requested_from, requested_to, account, row_limit, created_at, updated_at)
                VALUES (?, 'queued', ?, ?, ?, ?, ?, ?)""", (job_id, from_date, to_date, account, max(1, min(limit, 2000)), now, now))
    _WAKE.set()
    return job_id


def _claim_next() -> dict[str, Any] | None:
    with connect() as conn:
        _ensure_jobs(conn)
        # An API/worker replacement can only leave a job running until its
        # persisted ingestion lease expires; return it to the queue afterwards.
        stale_before = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat(timespec="seconds")
        conn.execute("UPDATE promo_jobs SET status = 'queued', updated_at = ? WHERE status = 'running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)", (utc_now(), stale_before))
        row = conn.execute("SELECT * FROM promo_jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return None
        changed = conn.execute("UPDATE promo_jobs SET status = 'running', heartbeat_at = ?, updated_at = ? WHERE job_id = ? AND status = 'queued'", (utc_now(), utc_now(), row["job_id"])).rowcount
        return dict(row) if changed == 1 else None


def _run_job(task: dict[str, Any]) -> None:
    job_id = task["job_id"]
    try:
        def work() -> None:
            process_posts(account=task.get("account"), from_date=task.get("requested_from"), to_date=task.get("requested_to"), limit=int(task.get("row_limit") or 500), job_id=job_id)
        complete = ingestion_jobs.run(f"promo-backfill:{job_id}", "01", work)
        if not complete:
            raise RuntimeError("Promo processing lease failed; retained for retry")
    except Exception as exc:
        with connect() as conn:
            _ensure_jobs(conn)
            conn.execute("UPDATE promo_jobs SET status = 'failed', error = ?, finished_at = ?, updated_at = ? WHERE job_id = ?", (str(exc)[:500], utc_now(), utc_now(), job_id))


def _worker_loop() -> None:
    while True:
        try:
            task = _claim_next()
            if task:
                _run_job(task)
                continue
        except Exception:
            pass
        _WAKE.wait(2)
        _WAKE.clear()


def start_worker() -> None:
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True
        threading.Thread(target=_worker_loop, daemon=True, name="promo-worker").start()


def list_opportunities(*, client: str | None = None, account: str | None = None, classification: str | None = None, review: str | None = None, limit: int = 40, cursor: str | None = None) -> dict[str, Any]:
    clauses, params = ["1=1"], []
    if client: clauses.append("LOWER(COALESCE(o.client, '')) LIKE ?"); params.append(f"%{client.casefold()}%")
    if account: clauses.append("o.account = ?"); params.append(account)
    if classification: clauses.append("o.classification = ?"); params.append(classification)
    if review: clauses.append("o.review_status = ?"); params.append(review)
    if cursor:
        stamp, cur_account, cur_shortcode = (cursor.split("|", 2) + ["", ""])[:3]
        clauses.append("(o.first_detected_at, o.account, o.shortcode) < (?, ?, ?)"); params.extend([stamp, cur_account, cur_shortcode])
    with connect() as conn:
        _initialize_topic_stacks(conn)
        rows = conn.execute(f"{_opportunity_select()} WHERE {' AND '.join(clauses)} ORDER BY o.first_detected_at DESC, o.account, o.shortcode LIMIT ?", (*params, max(1, min(limit, 100)))).fetchall()
        account_metadata = {row["account"]: _account_metadata(conn, row["account"]) for row in rows}
    items = []
    for row in rows:
        row = dict(row)
        row.update(account_metadata.get(row["account"], {}))
        item = _row_item(row); item["cover_image_path"] = row.get("cover_image_path"); item["cover_source_url"] = row.get("cover_source_url"); item["permalink"] = row.get("permalink"); item["cover_url"] = f"/api/dashboard/covers/{row['account']}/{row['post_id']}" if row.get("post_id") is not None else row.get("cover_source_url")
        items.append(item)
    next_cursor = None
    if len(items) == min(limit, 100):
        last = items[-1]; next_cursor = "|".join(str(last.get(key) or "") for key in ("first_detected_at", "account", "shortcode"))
    return {"items": items, "next_cursor": next_cursor}


def get_opportunity(account: str, shortcode: str) -> dict[str, Any] | None:
    with connect() as conn:
        _initialize_topic_stacks(conn)
        row = conn.execute(f"{_opportunity_select('p.caption, p.raw_json')} WHERE o.account = ? AND o.shortcode = ?", (account, shortcode)).fetchone()
        account_metadata = _account_metadata(conn, account)
    if not row: return None
    row = dict(row)
    row.update(account_metadata)
    caption = row.get("caption") or ""
    if not caption and row.get("raw_json"):
        try:
            payload = json.loads(row["raw_json"])
            caption = str(payload.get("caption") or "") if isinstance(payload, dict) else ""
        except (TypeError, ValueError):
            caption = ""
    item = _row_item(row); item.update({"cover_image_path": row.get("cover_image_path"), "cover_source_url": row.get("cover_source_url"), "permalink": row.get("permalink"), "cover_url": f"/api/dashboard/covers/{row['account']}/{row['post_id']}" if row.get("post_id") is not None else row.get("cover_source_url"), "caption": caption})
    return item


def update_opportunity(account: str, shortcode: str, payload: dict[str, Any], reviewer: str) -> dict[str, Any] | None:
    allowed = {key: payload[key] for key in ("client", "product", "classification") if key in payload and isinstance(payload[key], str)}
    review = payload.get("review_status") if payload.get("review_status") in {"new", "reviewed", "dismissed"} else None
    with connect() as conn:
        row = conn.execute("SELECT * FROM promo_opportunities WHERE account = ? AND shortcode = ?", (account, shortcode)).fetchone()
        if not row: return None
        current = json.loads(dict(row).get("review_override_json") or "{}")
        current.update(allowed)
        conn.execute("UPDATE promo_opportunities SET review_status = COALESCE(?, review_status), review_override_json = ?, client = COALESCE(?, client), product = COALESCE(?, product), classification = COALESCE(?, classification), last_analyzed_at = ? WHERE account = ? AND shortcode = ?", (review, _json({**current, "reviewer": reviewer, "reviewed_at": utc_now()}), allowed.get("client"), allowed.get("product"), allowed.get("classification"), utc_now(), account, shortcode))
    return get_opportunity(account, shortcode)


def get_job(job_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        _ensure_jobs(conn)
        row = conn.execute("SELECT * FROM promo_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None
