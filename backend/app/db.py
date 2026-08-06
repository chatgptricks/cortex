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

            -- Standalone dataset for @traselveloreal (Sentient Dash's second
            -- account). Deliberately separate from `posts`, which is the
            -- canonical Predict Post DB used for prediction/calibration --
            -- traselveloreal must never be mixed into that table.
            CREATE TABLE IF NOT EXISTS traselveloreal_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shortcode TEXT NOT NULL UNIQUE,
                published_at TEXT,
                likes INTEGER,
                comments INTEGER,
                caption TEXT,
                post_type_label TEXT,
                is_animated INTEGER NOT NULL DEFAULT 0,
                permalink TEXT,
                cover_source_url TEXT,
                cover_image_path TEXT,
                is_hot INTEGER NOT NULL DEFAULT 0,
                likes_at_1h INTEGER,
                hot_checked INTEGER NOT NULL DEFAULT 0,
                hot_marked_at TEXT,
                hot_rate_multiplier REAL,
                refreshed_30d INTEGER NOT NULL DEFAULT 0,
                refreshed_120d INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trasel_published
                ON traselveloreal_posts(published_at DESC);

            -- Self-serve account registry for Sentient Dash. Every account
            -- except the canonical `chatgptricks` (which lives in `posts`,
            -- shared with Predict's prediction model) writes into the
            -- generic `dashboard_posts` table below, keyed by `account`.
            -- This is what lets new accounts (Sentient or Competitors) be
            -- added without a new SQL table or a code deploy per account.
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                handle TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                group_name TEXT NOT NULL CHECK(group_name IN ('sentient', 'competitors')),
                hot_threshold INTEGER NOT NULL DEFAULT 600,
                is_canonical INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- Generic per-post storage shared by every non-canonical
            -- account. Deliberately never merged into `posts`.
            CREATE TABLE IF NOT EXISTS dashboard_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account TEXT NOT NULL,
                shortcode TEXT NOT NULL,
                published_at TEXT,
                likes INTEGER,
                comments INTEGER,
                caption TEXT,
                post_type_label TEXT,
                is_animated INTEGER NOT NULL DEFAULT 0,
                permalink TEXT,
                cover_source_url TEXT,
                cover_image_path TEXT,
                is_hot INTEGER NOT NULL DEFAULT 0,
                likes_at_1h INTEGER,
                hot_checked INTEGER NOT NULL DEFAULT 0,
                hot_marked_at TEXT,
                hot_rate_multiplier REAL,
                refreshed_30d INTEGER NOT NULL DEFAULT 0,
                refreshed_120d INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(account, shortcode)
            );
            CREATE INDEX IF NOT EXISTS idx_dashboard_posts_account_published
                ON dashboard_posts(account, published_at DESC);

            /* Scheduler bookkeeping. Previously the "last run" markers lived
               only in module-level memory, so every redeploy reset them and
               the jobs re-fired immediately -- including the full daily
               engagement cycle, which costs real Apify credits on every
               restart. Persisting them here makes restarts free. */
            CREATE TABLE IF NOT EXISTS scheduler_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- Sentient Dash's Google-sign-in allowlist + roles. Replaces the
            -- old ALLOWED_EMAILS/ADMIN_EMAILS env vars as the live source of
            -- truth once seeded (see seed_dashboard_users_from_env) -- an
            -- admin can add/remove people and promote/demote roles from the
            -- Users tab in Settings without touching Render at all.
            CREATE TABLE IF NOT EXISTS dashboard_users (
                email TEXT PRIMARY KEY,
                role TEXT NOT NULL DEFAULT 'viewer' CHECK(role IN ('admin', 'viewer')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        _ensure_column(conn, "traselveloreal_posts", "hot_rate_multiplier", "hot_rate_multiplier REAL")
        # Locally-cached profile picture path for each account -- Instagram's
        # own CDN URLs (via Apify) are signed and expire, so we download once
        # and serve our own copy instead of persisting the raw CDN URL.
        _ensure_column(conn, "accounts", "avatar_path", "avatar_path TEXT")
        # OCR text extracted from the cover image, mirroring posts.hook_text.
        # Powers the dashboard's "includes cover text" search for every
        # non-canonical account (previously only chatgptricks had this).
        _ensure_column(conn, "dashboard_posts", "hook_text", "hook_text TEXT")
        _ensure_column(conn, "dashboard_posts", "ocr_checked", "ocr_checked INTEGER NOT NULL DEFAULT 0")

        # Apify returns ~36 fields per post and we were persisting 8, throwing
        # away data we'd already paid for (reel views/plays, carousel slide
        # counts, hashtags, audio, collaborators...). raw_json keeps the entire
        # payload so no future feature ever requires a re-scrape, and the
        # columns below promote the high-value fields so they can be filtered
        # and sorted without parsing JSON on every query.
        _ensure_column(conn, "dashboard_posts", "raw_json", "raw_json TEXT")
        _ensure_column(conn, "dashboard_posts", "video_views", "video_views INTEGER")
        _ensure_column(conn, "dashboard_posts", "video_plays", "video_plays INTEGER")
        _ensure_column(conn, "dashboard_posts", "video_duration", "video_duration REAL")
        _ensure_column(conn, "dashboard_posts", "product_type", "product_type TEXT")
        _ensure_column(conn, "dashboard_posts", "hashtags", "hashtags TEXT")
        _ensure_column(conn, "dashboard_posts", "mentions", "mentions TEXT")
        _ensure_column(conn, "dashboard_posts", "slide_count", "slide_count INTEGER")
        _ensure_column(conn, "dashboard_posts", "music_song", "music_song TEXT")
        _ensure_column(conn, "dashboard_posts", "music_artist", "music_artist TEXT")
        _ensure_column(conn, "dashboard_posts", "music_audio_id", "music_audio_id TEXT")
        _ensure_column(conn, "dashboard_posts", "uses_original_audio", "uses_original_audio INTEGER")
        # audio_id was added to extract_apify_fields after ~5.5k posts already had
        # raw_json captured -- backfill it from the payload we already paid for
        # instead of waiting for those rows to be re-scraped. Cheap once caught up:
        # the WHERE clause only ever matches rows that still need it.
        conn.execute(
            """
            UPDATE dashboard_posts
            SET music_audio_id = json_extract(raw_json, '$.musicInfo.audio_id')
            WHERE raw_json IS NOT NULL
              AND music_audio_id IS NULL
              AND json_extract(raw_json, '$.musicInfo.audio_id') IS NOT NULL
            """
        )
        _ensure_column(conn, "dashboard_posts", "paid_partnership", "paid_partnership INTEGER")
        _ensure_column(conn, "dashboard_posts", "alt_text", "alt_text TEXT")
        _ensure_column(conn, "dashboard_posts", "first_comment", "first_comment TEXT")
        _ensure_column(conn, "dashboard_posts", "ig_media_id", "ig_media_id TEXT")
        _ensure_column(conn, "dashboard_posts", "owner_full_name", "owner_full_name TEXT")
        _ensure_column(conn, "dashboard_posts", "coauthors", "coauthors TEXT")
        _ensure_column(conn, "dashboard_posts", "tagged_users", "tagged_users TEXT")
        _ensure_column(conn, "dashboard_posts", "dimensions", "dimensions TEXT")
        _ensure_column(conn, "dashboard_posts", "enriched_at", "enriched_at TEXT")
        # Sorting/filtering by reel performance is the main reason these exist.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dashboard_posts_video_views "
            "ON dashboard_posts(account, video_views DESC)"
        )

        # Seed the two existing accounts on first run (idempotent -- INSERT
        # OR IGNORE keyed by the UNIQUE handle column).
        _seed_now = utc_now()
        conn.execute(
            """
            INSERT OR IGNORE INTO accounts
                (handle, label, group_name, hot_threshold, is_canonical, is_active, created_at, updated_at)
            VALUES ('chatgptricks', 'chatgptricks', 'sentient', 600, 1, 1, ?, ?)
            """,
            (_seed_now, _seed_now),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO accounts
                (handle, label, group_name, hot_threshold, is_canonical, is_active, created_at, updated_at)
            VALUES ('traselveloreal', 'traselveloreal', 'sentient', 1500, 0, 1, ?, ?)
            """,
            (_seed_now, _seed_now),
        )

        # One-time (idempotent, safe to re-run every startup) migration of
        # the old dedicated traselveloreal_posts table into the generic
        # dashboard_posts table. UNIQUE(account, shortcode) means re-running
        # this only ever inserts genuinely new rows.
        conn.execute(
            """
            INSERT OR IGNORE INTO dashboard_posts (
                account, shortcode, published_at, likes, comments, caption,
                post_type_label, is_animated, permalink, cover_source_url,
                cover_image_path, is_hot, likes_at_1h, hot_checked,
                hot_marked_at, hot_rate_multiplier, refreshed_30d,
                refreshed_120d, created_at, updated_at
            )
            SELECT
                'traselveloreal', shortcode, published_at, likes, comments, caption,
                post_type_label, is_animated, permalink, cover_source_url,
                cover_image_path, is_hot, likes_at_1h, hot_checked,
                hot_marked_at, hot_rate_multiplier, refreshed_30d,
                refreshed_120d, created_at, updated_at
            FROM traselveloreal_posts
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
        # Engagement-refresh / HOT-detection tracking (see apify_sync.py).
        _ensure_column(conn, "posts", "is_hot", "is_hot INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "posts", "likes_at_1h", "likes_at_1h INTEGER")
        _ensure_column(conn, "posts", "hot_checked", "hot_checked INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "posts", "hot_marked_at", "hot_marked_at TEXT")
        _ensure_column(conn, "posts", "refreshed_30d", "refreshed_30d INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "posts", "refreshed_120d", "refreshed_120d INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "posts", "hot_rate_multiplier", "hot_rate_multiplier REAL")
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


# --- Sentient Dash users (Google sign-in allowlist + roles) ----------------

def list_dashboard_users() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT email, role, created_at, updated_at FROM dashboard_users ORDER BY role DESC, email ASC"
        ).fetchall()
        return [dict(row) for row in rows]


def get_dashboard_user_role(email: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT role FROM dashboard_users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
        return row["role"] if row else None


def count_dashboard_admins() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM dashboard_users WHERE role = 'admin'").fetchone()
        return int(row["c"]) if row else 0


def upsert_dashboard_user(email: str, role: str) -> None:
    if role not in ("admin", "viewer"):
        raise ValueError(f"Invalid role: {role!r}")
    email = email.strip().lower()
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO dashboard_users (email, role, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET role = excluded.role, updated_at = excluded.updated_at
            """,
            (email, role, now, now),
        )


def remove_dashboard_user(email: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM dashboard_users WHERE email = ?", (email.strip().lower(),))


def seed_dashboard_users_from_env(allowed_emails: set[str], admin_emails: set[str]) -> None:
    """One-time migration path: the allowlist used to live entirely in the
    ALLOWED_EMAILS/ADMIN_EMAILS env vars. On first startup after this table
    existed, copy anyone from those env vars who isn't already in the table
    yet, so nobody's access silently disappears the moment this shipped.
    Emails already in the table (added/edited via the Users tab) are left
    alone -- this only fills in gaps, never overwrites.
    """
    if not allowed_emails:
        return
    now = utc_now()
    with connect() as conn:
        existing = {row["email"] for row in conn.execute("SELECT email FROM dashboard_users").fetchall()}
        for email in allowed_emails:
            if email in existing:
                continue
            role = "admin" if email in admin_emails else "viewer"
            conn.execute(
                "INSERT INTO dashboard_users (email, role, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (email, role, now, now),
            )


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
