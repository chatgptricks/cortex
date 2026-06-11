from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .config import DB_PATH, ensure_directories


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_directories()
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    ensure_directories()
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                section TEXT NOT NULL CHECK(section IN ('single', 'historical', 'ab')),
                title TEXT NOT NULL,
                caption TEXT,
                published_at TEXT,
                likes INTEGER,
                person_label TEXT,
                company_label TEXT,
                post_type_label TEXT,
                source_ref TEXT,
                source_row_number INTEGER,
                shortcode TEXT,
                image_path TEXT NOT NULL,
                original_filename TEXT,
                video_path TEXT,
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK(status IN ('queued', 'running', 'completed', 'failed')),
                error TEXT,
                progress_percent INTEGER NOT NULL DEFAULT 0,
                progress_message TEXT,
                analysis_path TEXT,
                analysis_summary TEXT,
                llm_report TEXT,
                tags TEXT,
                hook_text TEXT,
                is_animated INTEGER NOT NULL DEFAULT 0,
                comments INTEGER,
                brain_global_mean_abs REAL,
                brain_global_peak_abs REAL,
                virality_potential REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ab_tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running'
                    CHECK(status IN ('running', 'completed', 'failed')),
                winner_post_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(winner_post_id) REFERENCES posts(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS ab_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ab_test_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(ab_test_id) REFERENCES ab_tests(id) ON DELETE CASCADE,
                FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS metadata_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK(kind IN ('person', 'company', 'post_type')),
                label TEXT NOT NULL,
                slug TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(kind, slug)
            );

            CREATE INDEX IF NOT EXISTS idx_posts_section_created
                ON posts(section, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_candidates_test
                ON ab_candidates(ab_test_id);
            CREATE INDEX IF NOT EXISTS idx_metadata_options_kind
                ON metadata_options(kind, label);
            """
        )
        _ensure_column(conn, "posts", "progress_percent", "progress_percent INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "posts", "progress_message", "progress_message TEXT")
        _ensure_column(conn, "posts", "llm_report", "llm_report TEXT")
        _ensure_column(conn, "posts", "person_label", "person_label TEXT")
        _ensure_column(conn, "posts", "company_label", "company_label TEXT")
        _ensure_column(conn, "posts", "post_type_label", "post_type_label TEXT")
        _ensure_column(conn, "posts", "source_ref", "source_ref TEXT")
        _ensure_column(conn, "posts", "source_row_number", "source_row_number INTEGER")
        _ensure_column(conn, "posts", "shortcode", "shortcode TEXT")
        _ensure_column(conn, "posts", "tags", "tags TEXT")
        _ensure_column(conn, "posts", "hook_text", "hook_text TEXT")
        _ensure_column(conn, "posts", "is_animated", "is_animated INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "posts", "comments", "comments INTEGER")
        _ensure_column(conn, "posts", "brain_global_mean_abs", "brain_global_mean_abs REAL")
        _ensure_column(conn, "posts", "brain_global_peak_abs", "brain_global_peak_abs REAL")
        _ensure_column(conn, "posts", "virality_potential", "virality_potential REAL")
        conn.execute(
            """
            UPDATE posts
            SET brain_global_mean_abs = json_extract(analysis_summary, '$.metrics.global_mean_abs'),
                brain_global_peak_abs = json_extract(analysis_summary, '$.metrics.global_peak_abs'),
                virality_potential = json_extract(analysis_summary, '$.virality_potential')
            WHERE analysis_summary IS NOT NULL
              AND (
                brain_global_mean_abs IS NULL
                OR brain_global_peak_abs IS NULL
                OR virality_potential IS NULL
              )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_source_ref
                ON posts(source_ref)
                WHERE source_ref IS NOT NULL
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_shortcode ON posts(shortcode)")
        conn.execute(
            """
            UPDATE posts
            SET progress_percent = 100,
                progress_message = COALESCE(progress_message, 'Complete')
            WHERE status = 'completed' AND progress_percent < 100
            """
        )
        conn.execute("UPDATE posts SET progress_message = 'Complete' WHERE progress_message = 'Completado'")
        conn.execute("UPDATE posts SET progress_message = 'Queued' WHERE progress_message = 'En cola'")
        # One-time cleanup: drop the heavy brain-surface arrays from historical
        # rows. The full payload remains in the analysis JSON file referenced by
        # analysis_path; the API never returns surface data for historical posts.
        before_strip = conn.total_changes
        conn.execute(
            """
            UPDATE posts
            SET analysis_summary = json_remove(analysis_summary, '$.surface')
            WHERE section = 'historical'
              AND analysis_path IS NOT NULL
              AND json_type(analysis_summary, '$.surface') IS NOT NULL
            """
        )
        stripped = conn.total_changes - before_strip
    if stripped:
        _vacuum(stripped)


def _vacuum(stripped_rows: int) -> None:
    import logging

    logging.getLogger("uvicorn.error").info(
        "Stripped surface arrays from %d historical posts; running VACUUM (one-time, may take a minute)...",
        stripped_rows,
    )
    conn = sqlite3.connect(DB_PATH, timeout=300.0)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    logging.getLogger("uvicorn.error").info("VACUUM complete.")


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def decode_summary(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    return json.loads(value)


def row_to_post(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["analysis_summary"] = decode_summary(data.get("analysis_summary"))
    data["llm_report"] = decode_summary(data.get("llm_report"))
    data["tags"] = decode_summary(data.get("tags")) if data.get("tags") else []

    for field in ["person_label", "company_label"]:
        val = data.get(field)
        if val:
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    data[field] = ", ".join(str(i) for i in parsed)
            except Exception:
                pass

    return data


def make_relative(path: str | Path) -> str:
    return str(Path(path))
