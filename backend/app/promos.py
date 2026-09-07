"""Persistence and batch processing for the hidden Promos workspace."""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from .db import connect, utc_now
from .promos_detector import DETECTOR_VERSION, detect_promo


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _hash_post(post: dict[str, Any]) -> str:
    raw = _json({key: post.get(key) for key in ("caption", "first_comment", "hashtags", "mentions", "paid_partnership", "permalink")})
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_item(row: dict[str, Any]) -> dict[str, Any]:
    analysis = json.loads(row.get("analysis_json") or "{}")
    override = json.loads(row["review_override_json"]) if row.get("review_override_json") else {}
    result = {**analysis, "account": row["account"], "shortcode": row["shortcode"], "classification": row["classification"], "client": row.get("client"), "product": row.get("product"), "review_status": row.get("review_status") or "new", "published_at": row.get("published_at"), "first_detected_at": row.get("first_detected_at"), "last_analyzed_at": row.get("last_analyzed_at")}
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
        existing = conn.execute("SELECT input_hash FROM promo_scans WHERE account = ? AND shortcode = ?", (account, shortcode)).fetchone()
        first = now
        conn.execute("""INSERT INTO promo_scans(account, shortcode, input_hash, detector_version, status, attempts, updated_at)
                       VALUES (?, ?, ?, ?, 'done', 1, ?)
                       ON CONFLICT(account, shortcode) DO UPDATE SET input_hash = excluded.input_hash, detector_version = excluded.detector_version, status = 'done', attempts = promo_scans.attempts + 1, error = NULL, updated_at = excluded.updated_at""", (account, shortcode, digest, DETECTOR_VERSION, now))
        previous_row = conn.execute("SELECT first_detected_at, review_status, review_override_json FROM promo_opportunities WHERE account = ? AND shortcode = ?", (account, shortcode)).fetchone()
        previous = dict(previous_row) if previous_row else None
        first = previous["first_detected_at"] if previous and previous.get("first_detected_at") else first
        review_status = previous["review_status"] if previous else "new"
        override = previous["review_override_json"] if previous else None
        conn.execute("""INSERT INTO promo_opportunities(account, shortcode, classification, client, product, analysis_json, review_status, review_override_json, published_at, first_detected_at, last_analyzed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(account, shortcode) DO UPDATE SET classification = excluded.classification, client = excluded.client, product = excluded.product, analysis_json = excluded.analysis_json, published_at = excluded.published_at, last_analyzed_at = excluded.last_analyzed_at""", (account, shortcode, analysis["classification"], analysis.get("client"), analysis.get("product"), _json(analysis), review_status, override, post.get("published_at"), first, now))
    return {**analysis, "account": account, "shortcode": shortcode, "published_at": post.get("published_at"), "first_detected_at": first, "last_analyzed_at": now, "review_status": review_status}


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
                conn.execute("UPDATE promo_jobs SET processed = ?, updated_at = ? WHERE job_id = ?", (processed, utc_now(), job_id))
    if job_id:
        with connect() as conn:
            conn.execute("UPDATE promo_jobs SET status = 'done', total = ?, processed = ?, updated_at = ? WHERE job_id = ?", (len(posts), processed, utc_now(), job_id))
    return {"processed": processed, "total": len(posts)}


def create_backfill(*, from_date: str | None = None, to_date: str | None = None, account: str | None = None, limit: int = 500) -> str:
    job_id = uuid.uuid4().hex
    now = utc_now()
    with connect() as conn:
        conn.execute("INSERT INTO promo_jobs(job_id, status, requested_from, requested_to, created_at, updated_at) VALUES (?, 'queued', ?, ?, ?, ?)", (job_id, from_date, to_date, now, now))
    def run() -> None:
        with connect() as conn:
            conn.execute("UPDATE promo_jobs SET status = 'running', updated_at = ? WHERE job_id = ?", (utc_now(), job_id))
        try:
            process_posts(account=account, from_date=from_date, to_date=to_date, limit=limit, job_id=job_id)
        except Exception as exc:
            with connect() as conn:
                conn.execute("UPDATE promo_jobs SET status = 'failed', error = ?, updated_at = ? WHERE job_id = ?", (str(exc)[:500], utc_now(), job_id))
    threading.Thread(target=run, name=f"promos-{job_id[:8]}", daemon=True).start()
    return job_id


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
        rows = conn.execute(f"SELECT o.*, p.id AS post_id, p.cover_image_path, p.cover_source_url, p.permalink FROM promo_opportunities o LEFT JOIN dashboard_posts p ON p.account = o.account AND p.shortcode = o.shortcode WHERE {' AND '.join(clauses)} ORDER BY o.first_detected_at DESC, o.account, o.shortcode LIMIT ?", (*params, max(1, min(limit, 100)))).fetchall()
    items = []
    for row in rows:
        row = dict(row)
        item = _row_item(row); item["cover_image_path"] = row.get("cover_image_path"); item["cover_source_url"] = row.get("cover_source_url"); item["permalink"] = row.get("permalink"); item["cover_url"] = f"/api/dashboard/covers/{row['account']}/{row['post_id']}" if row.get("post_id") is not None else row.get("cover_source_url")
        items.append(item)
    next_cursor = None
    if len(items) == min(limit, 100):
        last = items[-1]; next_cursor = "|".join(str(last.get(key) or "") for key in ("first_detected_at", "account", "shortcode"))
    return {"items": items, "next_cursor": next_cursor}


def get_opportunity(account: str, shortcode: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT o.*, p.id AS post_id, p.cover_image_path, p.cover_source_url, p.permalink, p.caption, p.raw_json FROM promo_opportunities o LEFT JOIN dashboard_posts p ON p.account = o.account AND p.shortcode = o.shortcode WHERE o.account = ? AND o.shortcode = ?", (account, shortcode)).fetchone()
    if not row: return None
    row = dict(row)
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
        row = conn.execute("SELECT * FROM promo_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None
