from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .config import DATABASE_URL, DB_PATH, ensure_directories
from .postgres import Connection as PostgresConnection


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[Any]:
    if DATABASE_URL:
        conn = PostgresConnection(DATABASE_URL)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
        return
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
    # Postgres was initialized by a one-time SQLite import. New additive
    # columns/tables still need to be applied after that cut-over: otherwise a
    # normal feature deploy can reference a field that the imported schema
    # never had and every authenticated request fails before reaching its
    # route. Keep the large SQLite bootstrap out of Postgres, but always run
    # the small idempotent runtime extension set.
    if DATABASE_URL:
        with connect() as conn:
            _ensure_runtime_schema_extensions(conn)
        return
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
                -- Which public Instagram surface this account follows. New
                -- accounts choose this in Settings; existing accounts keep
                -- the historical posts-only behaviour through the default.
                scrape_mode TEXT NOT NULL DEFAULT 'posts',
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
            /* User-defined account lists, surfaced as extra tabs next to
               All/Sentient/Competitors/HOT. Owned by the Google account that
               created them and private by default; `is_shared` exists so a
               "share with the team" toggle can be added later without a
               migration. handles is a JSON array rather than a join table --
               a list is always read and written whole, and the roster is ~30
               accounts, so a second table would buy nothing. */
            CREATE TABLE IF NOT EXISTS account_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_email TEXT NOT NULL,
                name TEXT NOT NULL,
                handles TEXT NOT NULL,
                is_shared INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(owner_email, name)
            );
            CREATE INDEX IF NOT EXISTS idx_account_lists_owner
                ON account_lists(owner_email);

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
                display_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'viewer' CHECK(role IN ('admin', 'viewer')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- A queue row belongs to one person, not just one Instagram post:
            -- the same post can be meaningful work for several teammates, and
            -- each person must be able to progress it independently.  The
            -- post is identified by account + shortcode because canonical
            -- posts and every dynamically-added dashboard account live in
            -- different source tables.
            --
            -- There deliberately are no foreign keys to dashboard_users or
            -- the post tables. Removing a person's dashboard access or
            -- hiding/removing a post must preserve the assignment as history.
            CREATE TABLE IF NOT EXISTS post_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_account TEXT NOT NULL,
                post_shortcode TEXT NOT NULL,
                assignee_email TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queue'
                    CHECK(status IN ('queue', 'in_progress', 'posted')),
                note TEXT NOT NULL DEFAULT '',
                priority TEXT
                    CHECK(priority IS NULL OR priority IN ('low', 'medium', 'high', 'urgent')),
                due_date TEXT,
                tags TEXT NOT NULL DEFAULT '[]',
                position INTEGER NOT NULL DEFAULT 0,
                created_by_email TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(post_account, post_shortcode, assignee_email)
            );
            CREATE INDEX IF NOT EXISTS idx_post_assignments_assignee_status
                ON post_assignments(assignee_email, status, position, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_post_assignments_post
                ON post_assignments(post_account, post_shortcode);

            -- A compact immutable audit trail for Queue work. Event rows are
            -- intentionally kept outside the assignment row so the task view
            -- can explain who changed what without overloading the current
            -- task state with historical fields.
            CREATE TABLE IF NOT EXISTS post_assignment_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assignment_id INTEGER NOT NULL,
                actor_email TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_post_assignment_events_assignment
                ON post_assignment_events(assignment_id, created_at DESC);

            -- Queue V2 is a production scheduler, deliberately separate from
            -- the earlier thumbnail board above.  A source post normally has
            -- one active production request, while explicit Queue duplicates
            -- use a private synthetic shortcode so several designers can be
            -- assigned independent copies of the same source.
            CREATE TABLE IF NOT EXISTS queue_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_account TEXT NOT NULL,
                post_shortcode TEXT NOT NULL,
                post_title TEXT NOT NULL DEFAULT '',
                is_custom INTEGER NOT NULL DEFAULT 0,
                post_permalink TEXT NOT NULL DEFAULT '',
                post_caption TEXT NOT NULL DEFAULT '',
                post_type TEXT NOT NULL DEFAULT '',
                cover_url TEXT NOT NULL DEFAULT '',
                production_points INTEGER NOT NULL CHECK(production_points > 0),
                minutes_per_pp INTEGER NOT NULL DEFAULT 10 CHECK(minutes_per_pp > 0),
                priority TEXT NOT NULL DEFAULT 'medium'
                    CHECK(priority IN ('low', 'medium', 'high', 'urgent')),
                -- Retained as an internal compatibility column for databases
                -- created before Queue switched from deadlines to priority.
                -- Active Queue code never reads or exposes it.
                deadline_at TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                brief TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                reference_links TEXT NOT NULL DEFAULT '[]',
                attachments TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pool'
                    CHECK(status IN ('pool','scheduled','in_progress','completed','closed','cancelled')),
                designer_email TEXT,
                coordinator_email TEXT NOT NULL,
                recommended_accounts TEXT NOT NULL DEFAULT '[]',
                -- Slack DM metadata for compact follow-up updates. The
                -- initial assignment stores its DM channel/message timestamp
                -- so later changes can be threaded without repeating media.
                slack_channel_id TEXT,
                slack_message_ts TEXT,
                scheduled_date TEXT,
                scheduled_start_minutes INTEGER,
                actual_started_at TEXT,
                completed_at TEXT,
                closed_at TEXT,
                final_permalink TEXT,
                cancellation_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(post_account, post_shortcode)
            );
            CREATE INDEX IF NOT EXISTS idx_queue_requests_day
                ON queue_requests(scheduled_date, designer_email, scheduled_start_minutes);
            CREATE TABLE IF NOT EXISTS queue_request_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                actor_email TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(request_id) REFERENCES queue_requests(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_queue_request_events_request
                ON queue_request_events(request_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS queue_designer_accounts (
                designer_email TEXT NOT NULL,
                account_handle TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(designer_email, account_handle)
            );
            -- A user's initial account selection is deliberately separate
            -- from the selected accounts themselves: an empty selection can
            -- be an intentional answer and should not reopen onboarding on
            -- every Queue visit.
            CREATE TABLE IF NOT EXISTS queue_user_account_onboarding (
                user_email TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            -- Scheduler placements are collaborative before Submit.  Keeping
            -- the provisional position on the server lets the assigned PD
            -- see a VC's drag immediately while the committed request remains
            -- unchanged (and therefore sends no notification yet).
            CREATE TABLE IF NOT EXISTS queue_schedule_drafts (
                request_id INTEGER PRIMARY KEY,
                coordinator_email TEXT NOT NULL,
                designer_email TEXT NOT NULL,
                scheduled_date TEXT NOT NULL,
                scheduled_start_minutes INTEGER NOT NULL,
                recommended_accounts TEXT NOT NULL DEFAULT '[]',
                production_points INTEGER,
                minutes_per_pp INTEGER,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(request_id) REFERENCES queue_requests(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_queue_schedule_drafts_designer
                ON queue_schedule_drafts(designer_email, scheduled_date, scheduled_start_minutes);
            -- Queue tickets cover personal scheduler holds plus designer
            -- requests for PP changes and cancellations. Pending time blocks
            -- are visible immediately, but only approved tickets are firm.
            CREATE TABLE IF NOT EXISTS queue_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_type TEXT NOT NULL
                    CHECK(ticket_type IN ('time_block','pp_revision','cancellation')),
                requester_email TEXT NOT NULL,
                request_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','approved','rejected')),
                block_category TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                scheduled_date TEXT,
                scheduled_start_minutes INTEGER,
                duration_minutes INTEGER,
                requested_production_points INTEGER,
                requested_accounts TEXT NOT NULL DEFAULT '[]',
                reason TEXT NOT NULL DEFAULT '',
                reviewer_email TEXT,
                review_note TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(request_id) REFERENCES queue_requests(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_queue_tickets_status
                ON queue_tickets(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_queue_tickets_user_day
                ON queue_tickets(requester_email, scheduled_date, scheduled_start_minutes);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_tickets_pending_request
                ON queue_tickets(ticket_type, request_id)
                WHERE status = 'pending' AND request_id IS NOT NULL;
            -- A durable monotonic revision is the rendezvous point for the
            -- authenticated live stream.  It works across reconnects and
            -- process restarts instead of relying on in-memory pub/sub.
            CREATE TABLE IF NOT EXISTS queue_live_state (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                revision INTEGER NOT NULL DEFAULT 0,
                event_type TEXT NOT NULL DEFAULT '',
                actor_email TEXT NOT NULL DEFAULT '',
                request_ids TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            );

            -- One row per authenticated request, logged from the Firebase
            -- middleware. Feeds the Users tab's usage heatmap (who's active,
            -- when, and in which part of the app) -- there was previously no
            -- record at all of who actually opens the dashboard vs. just
            -- having access to it.
            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                path TEXT NOT NULL,
                method TEXT NOT NULL,
                ts TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_log_email ON usage_log(email);
            CREATE INDEX IF NOT EXISTS idx_usage_log_ts ON usage_log(ts);

            -- One row per account per day (see the scheduler's daily
            -- account-snapshot job), capturing Instagram's profile-level
            -- stats -- follower count above all, the one number a
            -- Social-Blade-style Tracker page is built around. This can
            -- only ever grow forward from whenever it started being
            -- written; there is no way to backfill Instagram's past
            -- follower counts for a newly tracked account.
            CREATE TABLE IF NOT EXISTS account_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                handle TEXT NOT NULL,
                followers_count INTEGER,
                posts_count INTEGER,
                full_name TEXT,
                verified INTEGER NOT NULL DEFAULT 0,
                private INTEGER NOT NULL DEFAULT 0,
                captured_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_account_snapshots_handle ON account_snapshots(handle);
            CREATE INDEX IF NOT EXISTS idx_account_snapshots_captured_at ON account_snapshots(captured_at);
            """
        )
        _ensure_column(conn, "traselveloreal_posts", "hot_rate_multiplier", "hot_rate_multiplier REAL")
        # Optional destination account for a Queue task. It is deliberately
        # independent from post_account: a post found on one account can be
        # recommended for another active Sentient account.
        _ensure_column(conn, "post_assignments", "recommended_account", "recommended_account TEXT")
        _ensure_column(conn, "dashboard_users", "operating_role", "operating_role TEXT NOT NULL DEFAULT 'sales'")
        _ensure_column(conn, "dashboard_users", "operating_roles", "operating_roles TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "dashboard_users", "is_admin", "is_admin INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "dashboard_users", "slack_user_id", "slack_user_id TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "dashboard_users", "display_name", "display_name TEXT NOT NULL DEFAULT ''")
        # A Queue schedule is rendered against the assignee's local clock.
        # Keep the canonical Olson name on the user instead of inferring it
        # from a browser or an email address; Colombia and Costa Rica share
        # Spanish but differ by one hour year-round.
        _ensure_column(conn, "dashboard_users", "time_zone", "time_zone TEXT NOT NULL DEFAULT ''")
        # Advanced PDs can curate their own work without gaining VC/Admin
        # coordination powers. This is intentionally an explicit capability,
        # not another operating role.
        _ensure_column(conn, "dashboard_users", "can_self_assign", "can_self_assign INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS queue_scheduler_preferences (
                   viewer_email TEXT PRIMARY KEY,
                   hidden_users TEXT NOT NULL DEFAULT '[]',
                   row_order TEXT NOT NULL DEFAULT '[]',
                   updated_at TEXT NOT NULL
               )"""
        )
        _ensure_column(conn, "queue_requests", "priority", "priority TEXT NOT NULL DEFAULT 'medium'")
        _ensure_column(conn, "queue_requests", "post_title", "post_title TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "queue_requests", "is_custom", "is_custom INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "queue_requests", "slack_channel_id", "slack_channel_id TEXT")
        _ensure_column(conn, "queue_requests", "slack_message_ts", "slack_message_ts TEXT")
        _ensure_column(conn, "queue_schedule_drafts", "production_points", "production_points INTEGER")
        _ensure_column(conn, "queue_requests", "minutes_per_pp", "minutes_per_pp INTEGER NOT NULL DEFAULT 10")
        _ensure_column(conn, "queue_schedule_drafts", "minutes_per_pp", "minutes_per_pp INTEGER")
        _ensure_column(conn, "queue_tickets", "requested_accounts", "requested_accounts TEXT NOT NULL DEFAULT '[]'")
        conn.execute("DROP INDEX IF EXISTS idx_queue_requests_status")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_requests_status_priority ON queue_requests(status, priority)")
        conn.execute(
            """INSERT OR IGNORE INTO queue_live_state
               (id, revision, event_type, actor_email, request_ids, updated_at)
               VALUES (1, 0, '', '', '[]', ?)""",
            (utc_now(),),
        )
        # Following count -- added after account_snapshots shipped, so older
        # snapshots have NULL here; the Tracker's historical-stats table just
        # shows "--" for those rows instead of a delta.
        _ensure_column(conn, "account_snapshots", "following_count", "following_count INTEGER")
        # Locally-cached profile picture path for each account -- Instagram's
        # own CDN URLs (via Apify) are signed and expire, so we download once
        # and serve our own copy instead of persisting the raw CDN URL.
        _ensure_column(conn, "accounts", "avatar_path", "avatar_path TEXT")
        _ensure_column(conn, "accounts", "scrape_mode", "scrape_mode TEXT NOT NULL DEFAULT 'posts'")
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
        # Curation flags set from the dashboard card menu. Both tables get
        # them: `posts` holds the canonical account and `dashboard_posts`
        # everything else, and the card menu has to work on either.
        #   is_promo -- paid placement. Also inferred from the #aitoolsentient
        #     hashtag on the frontend; this column is the manual override for
        #     placements that didn't use the tag.
        #   hidden   -- excluded from the dashboard grid but NOT deleted, so
        #     it still counts in totals and can be brought back.
        for _post_table in ("posts", "dashboard_posts"):
            _ensure_column(conn, _post_table, "is_promo", "is_promo INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, _post_table, "hidden", "hidden INTEGER NOT NULL DEFAULT 0")
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
    # sqlite3 cursors are directly iterable; our Postgres compatibility
    # cursor deliberately exposes fetchall() instead. Use the shared surface
    # so startup migrations behave identically on both engines.
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _ensure_runtime_schema_extensions(conn: Any) -> None:
    """Apply additive schema introduced after the managed-Postgres import.

    The Postgres compatibility connection translates PRAGMA table_info into
    information_schema, so the same idempotent checks work for SQLite and
    Postgres. Keep post-cutover additions here as well as in SQLite's full
    bootstrap above so a newly deployed backend cannot outrun its database.
    """
    _ensure_column(conn, "dashboard_users", "time_zone", "time_zone TEXT NOT NULL DEFAULT ''")
    _ensure_column(
        conn,
        "dashboard_users",
        "can_self_assign",
        "can_self_assign INTEGER NOT NULL DEFAULT 0",
    )
    # Accounts already existed when the managed Postgres database was first
    # imported, so this additive field must run here as well as in SQLite's
    # full bootstrap. Otherwise production would accept the UI form but fail
    # on its first account read after deployment.
    _ensure_column(conn, "accounts", "scrape_mode", "scrape_mode TEXT NOT NULL DEFAULT 'posts'")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS queue_scheduler_preferences (
               viewer_email TEXT PRIMARY KEY,
               hidden_users TEXT NOT NULL DEFAULT '[]',
               row_order TEXT NOT NULL DEFAULT '[]',
               updated_at TEXT NOT NULL
           )"""
    )


# --- Sentient Dash users (Google sign-in allowlist + roles) ----------------

# Self-assignment is an internal Queue exception, not a role that Settings
# admins should grant accidentally. There are currently no approved
# exceptions: Gabo is a normal VC, so his capabilities come from his
# persisted operating role rather than this bypass.
INTERNAL_SELF_ASSIGN_EMAILS = frozenset()


def _has_internal_self_assign(email: str) -> bool:
    return email.strip().lower() in INTERNAL_SELF_ASSIGN_EMAILS

def _default_dashboard_display_name(email: str) -> str:
    local = email.strip().split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ")
    words = ["".join(char for char in word if not char.isdigit()) for word in local.split()]
    return " ".join(word.capitalize() for word in words if word) or "User"


def list_dashboard_users() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT email, display_name, role, operating_role, operating_roles, is_admin, slack_user_id,
                      time_zone, can_self_assign, created_at, updated_at
               FROM dashboard_users
               ORDER BY is_admin DESC, operating_role ASC, email ASC"""
        ).fetchall()
        users = [dict(row) for row in rows]
        for user in users:
            user["can_self_assign"] = int(_has_internal_self_assign(str(user.get("email") or "")))
        return users


def get_dashboard_user_role(email: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT role FROM dashboard_users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
        return row["role"] if row else None


def get_dashboard_user_access(email: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT email, operating_role, operating_roles, is_admin, role, time_zone, can_self_assign FROM dashboard_users WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
        if not row:
            return None
        value = dict(row)
        # Existing databases only have the legacy role until init_db's
        # migration has run; honour it during that tiny transition window.
        value["is_admin"] = bool(value["is_admin"] or value["role"] == "admin")
        value["operating_role"] = value["operating_role"] or "sales"
        value["can_self_assign"] = _has_internal_self_assign(email)
        return value


def set_dashboard_user_time_zone(email: str, time_zone: str) -> None:
    """Persist the recognized Queue clock without touching roles or access."""
    if time_zone not in {"America/Costa_Rica", "America/Bogota"}:
        return
    with connect() as conn:
        conn.execute(
            "UPDATE dashboard_users SET time_zone = ?, updated_at = ? WHERE email = ?",
            (time_zone, utc_now(), email.strip().lower()),
        )


def count_dashboard_admins() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM dashboard_users WHERE is_admin = 1 OR role = 'admin'").fetchone()
        return int(row["c"]) if row else 0


def upsert_dashboard_user(
    email: str, role: str = "viewer", operating_role: str | None = None,
    is_admin: bool | None = None, slack_user_id: str | None = None,
    display_name: str | None = None, time_zone: str | None = None,
    can_self_assign: bool | None = None,
) -> None:
    if role not in ("admin", "viewer"):
        raise ValueError(f"Invalid legacy role: {role!r}")
    if operating_role is not None and operating_role not in ("vc", "pd", "sales", "trainee"):
        raise ValueError(f"Invalid operating role: {operating_role!r}")
    email = email.strip().lower()
    operating_role = operating_role or "sales"
    admin_value = bool(role == "admin") if is_admin is None else bool(is_admin)
    if time_zone is not None and time_zone not in {"", "America/Costa_Rica", "America/Bogota"}:
        raise ValueError(f"Invalid time zone: {time_zone!r}")
    legacy_role = "admin" if admin_value else "viewer"
    now = utc_now()
    with connect() as conn:
        # Unit-test and legacy database fixtures can bypass init_db(). Keep
        # this write path forward-compatible when those small databases do.
        try:
            conn.execute("ALTER TABLE dashboard_users ADD COLUMN time_zone TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE dashboard_users ADD COLUMN can_self_assign INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        existing = conn.execute(
            "SELECT operating_role, operating_roles, time_zone, can_self_assign FROM dashboard_users WHERE email = ?",
            (email,),
        ).fetchone()
        existing_roles: list[str] = []
        if existing:
            try:
                parsed_roles = json.loads(existing["operating_roles"] or "[]")
                if isinstance(parsed_roles, list):
                    existing_roles = [
                        str(value).strip().lower() for value in parsed_roles
                        if str(value).strip().lower() in {"vc", "pd", "sales", "trainee", "dev"}
                    ]
            except (TypeError, json.JSONDecodeError):
                existing_roles = []

        # Post Designer is the baseline capability for every allowlisted user.
        # Editing an unrelated field (display name, Slack ID, or Admin flag)
        # must not collapse special multi-role accounts such as Ivan or
        # Esteban back to a single role.
        if email == "ivan@sentientagency.io":
            operating_roles = ["vc", "pd", "sales", "trainee"]
        elif email == "esteban@sentientagency.io":
            operating_roles = list(dict.fromkeys([operating_role, "pd", "vc", "dev"]))
        elif existing and existing["operating_role"] == operating_role:
            operating_roles = list(dict.fromkeys([*existing_roles, operating_role, "pd"]))
        else:
            operating_roles = list(dict.fromkeys([operating_role, "pd"]))
        conn.execute(
            """
            INSERT INTO dashboard_users (email, display_name, role, operating_role, operating_roles, is_admin, slack_user_id, time_zone, can_self_assign, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                display_name = CASE WHEN ? IS NULL THEN dashboard_users.display_name ELSE excluded.display_name END,
                role = excluded.role, operating_role = excluded.operating_role,
                operating_roles = excluded.operating_roles,
                is_admin = excluded.is_admin,
                slack_user_id = CASE WHEN ? IS NULL THEN dashboard_users.slack_user_id ELSE excluded.slack_user_id END,
                time_zone = CASE WHEN ? IS NULL THEN dashboard_users.time_zone ELSE excluded.time_zone END,
                can_self_assign = CASE WHEN ? IS NULL THEN dashboard_users.can_self_assign ELSE excluded.can_self_assign END,
                updated_at = excluded.updated_at
            """,
            (
                email, (display_name or "").strip(), legacy_role, operating_role,
                json.dumps(operating_roles), int(admin_value), (slack_user_id or "").strip(), (time_zone or "").strip(), int(bool(can_self_assign)), now, now,
                display_name, slack_user_id, time_zone, can_self_assign,
            ),
        )


def remove_dashboard_user(email: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM dashboard_users WHERE email = ?", (email.strip().lower(),))


_DASHBOARD_EMAIL_ALIASES = {
    "sebastianruizurquillo@gmail.com": "sebastianruizurquijo@gmail.com",
}


def _migrate_dashboard_user_email_aliases(conn: sqlite3.Connection) -> None:
    """Rename reviewed legacy email typos without losing Queue history.

    This runs before the environment allowlist seed, so an old spelling in
    Render cannot recreate a duplicate user after the canonical row exists.
    """
    reference_columns = (
        ("account_lists", "owner_email"),
        ("post_assignments", "assignee_email"),
        ("post_assignments", "created_by_email"),
        ("post_assignment_events", "actor_email"),
        ("queue_requests", "designer_email"),
        ("queue_requests", "coordinator_email"),
        ("queue_request_events", "actor_email"),
        ("queue_designer_accounts", "designer_email"),
        ("queue_schedule_drafts", "coordinator_email"),
        ("queue_schedule_drafts", "designer_email"),
        ("queue_tickets", "requester_email"),
        ("queue_tickets", "reviewer_email"),
        ("queue_live_state", "actor_email"),
        ("usage_log", "email"),
    )
    for legacy_email, canonical_email in _DASHBOARD_EMAIL_ALIASES.items():
        legacy = conn.execute("SELECT email FROM dashboard_users WHERE email = ?", (legacy_email,)).fetchone()
        canonical = conn.execute("SELECT email FROM dashboard_users WHERE email = ?", (canonical_email,)).fetchone()
        if not legacy or canonical:
            continue
        for table, column in reference_columns:
            conn.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (canonical_email, legacy_email))
        conn.execute("UPDATE dashboard_users SET email = ?, updated_at = ? WHERE email = ?", (canonical_email, utc_now(), legacy_email))


def seed_dashboard_users_from_env(allowed_emails: set[str], admin_emails: set[str]) -> None:
    """One-time migration path: the allowlist used to live entirely in the
    ALLOWED_EMAILS/ADMIN_EMAILS env vars. On first startup after this table
    existed, copy anyone from those env vars who isn't already in the table
    yet, so nobody's access silently disappears the moment this shipped.
    Emails already in the table (added/edited via the Users tab) are left
    alone -- this only fills in gaps, never overwrites.
    """
    allowed_emails = {_DASHBOARD_EMAIL_ALIASES.get(email, email) for email in allowed_emails}
    admin_emails = {_DASHBOARD_EMAIL_ALIASES.get(email, email) for email in admin_emails}
    now = utc_now()
    with connect() as conn:
        _migrate_dashboard_user_email_aliases(conn)
        if not allowed_emails:
            return
        existing = {row["email"] for row in conn.execute("SELECT email FROM dashboard_users").fetchall()}
        for email in allowed_emails:
            if email in existing:
                continue
            role = "admin" if email in admin_emails else "viewer"
            conn.execute(
                """INSERT INTO dashboard_users (email, display_name, role, operating_role, is_admin, created_at, updated_at)
                   VALUES (?, ?, ?, 'sales', ?, ?, ?)""",
                (email, _default_dashboard_display_name(email), role, int(role == "admin"), now, now),
            )


def seed_queue_role_roster() -> None:
    """Apply the agreed initial Queue operating roles once, after env users
    have been seeded.  Admin is independent of VC/PD/Sales."""
    roster = {
        "esteban@sentientagency.io": ("pd", True),
        "louis@sentientagency.io": ("vc", True),
        "ivan@sentientagency.io": ("vc", True),
        "sergio@sentientagency.io": ("vc", True),
        "victor@sentientagency.io": ("sales", False),
        "egor@sentientagency.io": ("sales", False),
        "santiagoflhi@gmail.com": ("pd", False),
        "dsflorezl@gmail.com": ("pd", False),
        "sara1107giraldo@gmail.com": ("pd", False),
        "sebastianruizurquijo@gmail.com": ("pd", False),
        "tevi@sentientagency.io": ("vc", False),
        "gabo@sentientagency.io": ("pd", False),
    }
    display_names = {
        "esteban@sentientagency.io": "Esteban",
        "louis@sentientagency.io": "Louis",
        "ivan@sentientagency.io": "Ivan",
        "sergio@sentientagency.io": "Sergio",
        "victor@sentientagency.io": "Victor",
        "egor@sentientagency.io": "Egor",
        "santiagoflhi@gmail.com": "Santiago",
        "dsflorezl@gmail.com": "Florez",
        "sara1107giraldo@gmail.com": "Sara",
        "sebastianruizurquijo@gmail.com": "Sebastian",
        "sebastianruizurquillo@gmail.com": "Sebastian",
        "tevi@sentientagency.io": "Tevi",
        "gabo@sentientagency.io": "Gabo",
        "trainee@sentientagency.io": "Trainee",
    }
    slack_user_ids = {
        "esteban@sentientagency.io": "U08UYJMPJ76",
        "louis@sentientagency.io": "U06DZPVNTBR",
        "ivan@sentientagency.io": "U0516SU09J9",
        "sergio@sentientagency.io": "U087U6470M6",
        "victor@sentientagency.io": "U0BAJA1AC6P",
        "egor@sentientagency.io": "U081LU7PVK3",
        "santiagoflhi@gmail.com": "U0AGH0MJ3EH",
        "dsflorezl@gmail.com": "U0BH9R6EE4Q",
        "sara1107giraldo@gmail.com": "U0BGHD1HD0R",
        "sebastianruizurquijo@gmail.com": "U0BG04Q4Z8F",
        "sebastianruizurquillo@gmail.com": "U0BG04Q4Z8F",
        "tevi@sentientagency.io": "U05QU9WCR1N",
        "gabo@sentientagency.io": "U0BLJHSUNJG",
    }
    now = utc_now()
    with connect() as conn:
        for email, display_name in display_names.items():
            slack_user_id = slack_user_ids.get(email, "")
            conn.execute(
                """UPDATE dashboard_users
                   SET display_name = CASE WHEN TRIM(display_name) = '' THEN ? ELSE display_name END,
                       slack_user_id = CASE WHEN TRIM(slack_user_id) = '' THEN ? ELSE slack_user_id END
                   WHERE email = ?""",
                (display_name, slack_user_id, email),
            )
        marker = conn.execute("SELECT value FROM scheduler_state WHERE key = 'queue_roles_v4_seeded'").fetchone()
        if not marker:
            for email, (operating_role, is_admin) in roster.items():
                exists = conn.execute("SELECT email FROM dashboard_users WHERE email = ?", (email,)).fetchone()
                if not exists:
                    # Do not silently grant dashboard access to emails not in the
                    # Firebase allowlist. Settings can add them later if needed.
                    continue
                operating_roles = (
                    ["pd", "vc", "dev"] if email == "esteban@sentientagency.io"
                    else ["vc", "pd"] if email == "ivan@sentientagency.io"
                    else [operating_role]
                )
                conn.execute(
                    """UPDATE dashboard_users SET role = ?, operating_role = ?, operating_roles = ?, is_admin = ?, updated_at = ?
                       WHERE email = ?""",
                    ("admin" if is_admin else "viewer", operating_role, json.dumps(operating_roles), int(is_admin), now, email),
                )
            # Initial agreed account mapping. The Settings API owns all later
            # additions, so this is intentionally a one-time seed too.
            for handle in ("chatgptricks", "costarica"):
                conn.execute(
                    "INSERT OR IGNORE INTO queue_designer_accounts (designer_email, account_handle, created_at) VALUES (?, ?, ?)",
                    ("esteban@sentientagency.io", handle, now),
                )
            conn.execute(
                "INSERT INTO scheduler_state (key, value, updated_at) VALUES ('queue_roles_v4_seeded', '1', ?)",
                (now,),
            )

        # Role additions after the initial seed need their own idempotent
        # marker because most production databases already have v4 applied.
        ivan_marker = conn.execute("SELECT value FROM scheduler_state WHERE key = 'queue_roles_v5_ivan_pd'").fetchone()
        if not ivan_marker:
            ivan = conn.execute(
                "SELECT operating_role, operating_roles FROM dashboard_users WHERE email = ?",
                ("ivan@sentientagency.io",),
            ).fetchone()
            if ivan:
                roles = json.loads(ivan["operating_roles"] or "[]")
                if not roles:
                    roles = [ivan["operating_role"]]
                roles = list(dict.fromkeys([*roles, "vc", "pd"]))
                conn.execute(
                    "UPDATE dashboard_users SET operating_roles = ?, updated_at = ? WHERE email = ?",
                    (json.dumps(roles), now, "ivan@sentientagency.io"),
                )
            conn.execute(
                "INSERT INTO scheduler_state (key, value, updated_at) VALUES ('queue_roles_v5_ivan_pd', '1', ?)",
                (now,),
            )

        # Ivan is the non-Dev role-preview account: he needs every operating
        # perspective available in Queue (VC, PD, Sales, and Trainee), while
        # Admin remains granted through is_admin and Dev stays Esteban-only.
        # This is a separate migration because v5 has already run in
        # production and must not prevent the expanded capability set.
        ivan_all_roles_marker = conn.execute(
            "SELECT value FROM scheduler_state WHERE key = 'queue_roles_v7_ivan_all_non_dev'"
        ).fetchone()
        if not ivan_all_roles_marker:
            conn.execute(
                """UPDATE dashboard_users
                   SET role = 'admin', operating_role = 'vc', operating_roles = ?, is_admin = 1, updated_at = ?
                   WHERE email = ?""",
                (json.dumps(["vc", "pd", "sales", "trainee"]), now, "ivan@sentientagency.io"),
            )
            conn.execute(
                "INSERT INTO scheduler_state (key, value, updated_at) VALUES ('queue_roles_v7_ivan_all_non_dev', '1', ?)",
                (now,),
            )

        # Normalize the reviewed production roster after Settings edits from
        # older releases could silently collapse a user's capabilities. PD is
        # implicit for everyone; the explicit role is their additional
        # operating perspective. This migration is one-time so later, valid
        # changes made in Settings remain authoritative.
        reviewed_roles_marker = conn.execute(
            "SELECT value FROM scheduler_state WHERE key = 'queue_roles_v8_reviewed_roster'"
        ).fetchone()
        if not reviewed_roles_marker:
            reviewed_roles = {
                "esteban@sentientagency.io": ("vc", ["vc", "pd", "dev"], True),
                "louis@sentientagency.io": ("vc", ["vc", "pd"], True),
                "ivan@sentientagency.io": ("vc", ["vc", "pd", "sales", "trainee"], True),
                "sergio@sentientagency.io": ("vc", ["vc", "pd"], True),
                "victor@sentientagency.io": ("sales", ["sales", "pd"], False),
                "egor@sentientagency.io": ("sales", ["sales", "pd"], False),
                "santiagoflhi@gmail.com": ("pd", ["pd"], False),
                "dsflorezl@gmail.com": ("pd", ["pd"], False),
                "sara1107giraldo@gmail.com": ("pd", ["pd"], False),
                "sebastianruizurquijo@gmail.com": ("pd", ["pd"], False),
                "tevi@sentientagency.io": ("vc", ["vc", "pd"], False),
                "gabo@sentientagency.io": ("pd", ["pd"], False),
                "trainee@sentientagency.io": ("trainee", ["trainee", "pd"], False),
            }
            for email, (operating_role, operating_roles, is_admin) in reviewed_roles.items():
                conn.execute(
                    """UPDATE dashboard_users
                       SET role = ?, operating_role = ?, operating_roles = ?, is_admin = ?, updated_at = ?
                       WHERE email = ?""",
                    (
                        "admin" if is_admin else "viewer", operating_role, json.dumps(operating_roles),
                        int(is_admin), now, email,
                    ),
                )
            conn.execute(
                "INSERT INTO scheduler_state (key, value, updated_at) VALUES ('queue_roles_v8_reviewed_roster', '1', ?)",
                (now,),
            )

        # Correct the two roster labels that were crossed in the production
        # Users table. Keep their emails and Slack IDs untouched; this is a
        # one-time data repair so later edits made in Settings remain the
        # source of truth.
        display_name_fix_marker = conn.execute(
            "SELECT value FROM scheduler_state WHERE key = 'queue_roles_v9_fix_santiago_florez_names'"
        ).fetchone()
        if not display_name_fix_marker:
            for email, display_name in (
                ("santiagoflhi@gmail.com", "Santiago"),
                ("dsflorezl@gmail.com", "Florez"),
            ):
                conn.execute(
                    "UPDATE dashboard_users SET display_name = ?, updated_at = ? WHERE email = ?",
                    (display_name, now, email),
                )
            conn.execute(
                "INSERT INTO scheduler_state (key, value, updated_at) VALUES ('queue_roles_v9_fix_santiago_florez_names', '1', ?)",
                (now,),
            )

        # Gabo's former self-assignment bypass never granted coordinator
        # permissions reliably. Replace it with the durable, ordinary VC
        # operating role once; later Settings edits still remain authoritative.
        gabo_vc_marker = conn.execute(
            "SELECT value FROM scheduler_state WHERE key = 'queue_roles_v10_gabo_vc'"
        ).fetchone()
        if not gabo_vc_marker:
            conn.execute(
                """UPDATE dashboard_users
                   SET role = 'viewer', operating_role = 'vc', operating_roles = ?,
                       is_admin = 0, can_self_assign = 0, updated_at = ?
                   WHERE email = 'gabo@sentientagency.io'""",
                (json.dumps(["vc", "pd"]), now),
            )
            conn.execute(
                "INSERT INTO scheduler_state (key, value, updated_at) VALUES ('queue_roles_v10_gabo_vc', '1', ?)",
                (now,),
            )

        # A real Trainee role uses longer production-point units. This seeded
        # placeholder keeps the scheduler and assignment flow testable before
        # the first trainee receives a company account. Notifications are
        # routed separately so the row can keep its own neutral identity.
        trainee_marker = conn.execute(
            "SELECT value FROM scheduler_state WHERE key = 'queue_roles_v6_trainee_test'"
        ).fetchone()
        if not trainee_marker:
            conn.execute(
                """INSERT INTO dashboard_users
                   (email, role, operating_role, operating_roles, is_admin, slack_user_id, created_at, updated_at)
                   VALUES (?, 'viewer', 'trainee', ?, 0, '', ?, ?)
                   ON CONFLICT(email) DO UPDATE SET
                     role = 'viewer', operating_role = 'trainee', operating_roles = excluded.operating_roles,
                     is_admin = 0, updated_at = excluded.updated_at""",
                ("trainee@sentientagency.io", json.dumps(["trainee", "pd"]), now, now),
            )
            conn.execute(
                "INSERT INTO scheduler_state (key, value, updated_at) VALUES ('queue_roles_v6_trainee_test', '1', ?)",
                (now,),
            )


def log_usage_event(email: str, path: str, method: str) -> None:
    """Called from the Firebase middleware on every authenticated request.
    Best-effort by design (the caller swallows any exception) -- a missed
    usage row is a cosmetic gap in a heatmap, not worth risking the request
    it's piggybacking on.
    """
    with connect() as conn:
        conn.execute(
            "INSERT INTO usage_log (email, path, method, ts) VALUES (?, ?, ?, ?)",
            (email.strip().lower(), path, method, utc_now()),
        )


def _usage_section(path: str) -> str:
    if path.startswith("/api/admin/"):
        return "admin"
    if path.startswith("/api/insights/"):
        return "insights"
    if path.startswith("/api/dashboard/"):
        return "dashboard"
    return "other"


def get_usage_summary(days: int = 30) -> dict[str, Any]:
    """Aggregated usage analytics for the admin Users tab' heatmap.

    Every registered user gets a row (even one who has never signed in --
    that's exactly the kind of thing this is meant to surface), zero-filled
    for the requested window so the frontend can render a fixed-width grid
    without caring which days actually had events. Per user: a day-by-day
    count (the heatmap's main grid), an hour-of-day and day-of-week
    breakdown (when they're actually online), and a dashboard/insights/admin
    split (what they use). A team-wide day x hour grid is included
    separately so "when is anyone online" doesn't require summing 12 rows
    in the browser.
    """
    now = datetime.now(UTC)
    # Window is `days` calendar days ending TODAY, inclusive -- days=30 means
    # today and the 29 before it. (An off-by-one here silently dropped today
    # from the heatmap: since_dt = now - timedelta(days=days) puts the last
    # bucket at "yesterday", so a person active moments ago showed up in
    # total_all_time/last_seen but not in last_7d/last_30d or today's cell.)
    since_date = now.date() - timedelta(days=days - 1)
    since_dt = datetime(since_date.year, since_date.month, since_date.day, tzinfo=UTC)
    since = since_dt.isoformat(timespec="seconds")

    roster = list_dashboard_users()  # [{email, role, created_at, updated_at}]

    with connect() as conn:
        window_rows = conn.execute(
            "SELECT email, path, ts FROM usage_log WHERE ts >= ? ORDER BY ts ASC",
            (since,),
        ).fetchall()
        totals = conn.execute(
            "SELECT email, COUNT(*) AS total, MIN(ts) AS first_seen, MAX(ts) AS last_seen "
            "FROM usage_log GROUP BY email"
        ).fetchall()
    totals_by_email = {row["email"]: row for row in totals}

    day_keys = [(since_date + timedelta(days=d)).isoformat() for d in range(days)]
    DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    per_user: dict[str, dict[str, Any]] = {
        row["email"]: {
            "daily": dict.fromkeys(day_keys, 0),
            "hourly": [0] * 24,
            "dow": [0] * 7,
            "sections": {"dashboard": 0, "insights": 0, "admin": 0, "other": 0},
        }
        for row in roster
    }
    global_dow_hour = [[0] * 24 for _ in range(7)]

    for row in window_rows:
        email = row["email"]
        # A usage row can outlive the person's access (removed from the
        # roster after being active) -- still count it in the global grid,
        # just don't manufacture a per-user row for someone no longer listed.
        ts = row["ts"]
        day = ts[:10]
        try:
            when = datetime.fromisoformat(ts)
        except ValueError:
            continue
        hour = when.hour
        dow = when.weekday()  # Monday=0 .. Sunday=6
        global_dow_hour[dow][hour] += 1

        u = per_user.get(email)
        if u is None:
            continue
        if day in u["daily"]:
            u["daily"][day] += 1
        u["hourly"][hour] += 1
        u["dow"][dow] += 1
        section = _usage_section(row["path"])
        u["sections"][section] = u["sections"].get(section, 0) + 1

    users_out = []
    for row in roster:
        email = row["email"]
        u = per_user[email]
        daily_list = [{"date": k, "count": u["daily"][k]} for k in day_keys]
        t = totals_by_email.get(email)
        users_out.append(
            {
                "email": email,
                "role": row["role"],
                "total_all_time": int(t["total"]) if t else 0,
                "first_seen": t["first_seen"] if t else None,
                "last_seen": t["last_seen"] if t else None,
                "active_days": sum(1 for d in daily_list if d["count"] > 0),
                "last_7d": sum(d["count"] for d in daily_list[-7:]),
                "last_30d": sum(d["count"] for d in daily_list),
                "daily": daily_list,
                "hourly": u["hourly"],
                "dow": u["dow"],
                "sections": u["sections"],
            }
        )
    # Most recently active first; never-active users (last_seen None) sink
    # to the bottom, which is exactly who this feature exists to surface.
    users_out.sort(key=lambda u: u["last_seen"] or "", reverse=True)

    return {
        "days": days,
        "day_keys": day_keys,
        "dow_labels": DOW,
        "users": users_out,
        "global_dow_hour": global_dow_hour,
        "total_events_in_range": len(window_rows),
        "active_users_7d": sum(1 for u in users_out if u["last_7d"] > 0),
        "active_users_30d": sum(1 for u in users_out if u["last_30d"] > 0),
        "total_users": len(users_out),
    }


# --- Account snapshots (Tracker page) --------------------------------------

def insert_account_snapshot(
    handle: str,
    followers_count: int | None,
    posts_count: int | None,
    full_name: str | None,
    verified: bool,
    private: bool,
    following_count: int | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO account_snapshots
                (handle, followers_count, posts_count, full_name, verified, private, following_count, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                handle.strip().lower(),
                followers_count,
                posts_count,
                full_name,
                int(bool(verified)),
                int(bool(private)),
                following_count,
                utc_now(),
            ),
        )


def list_account_snapshots(handle: str) -> list[dict[str, Any]]:
    """Full history for one account, oldest first -- the Tracker detail
    page's follower-growth line is drawn directly from this."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT handle, followers_count, posts_count, full_name, verified, private, following_count, captured_at "
            "FROM account_snapshots WHERE handle = ? ORDER BY captured_at ASC",
            (handle.strip().lower(),),
        ).fetchall()
        return [dict(row) for row in rows]


def all_account_snapshots() -> dict[str, list[dict[str, Any]]]:
    """Every snapshot for every account, oldest first, grouped by handle.
    Used to build the Tracker leaderboard's 1d/7d/30d deltas -- one query
    instead of one round trip per tracked account."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT handle, followers_count, posts_count, full_name, verified, private, following_count, captured_at "
            "FROM account_snapshots ORDER BY handle ASC, captured_at ASC"
        ).fetchall()
    by_handle: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_handle.setdefault(row["handle"], []).append(dict(row))
    return by_handle


# --- User-defined account lists (custom dashboard tabs) --------------------

def _account_list_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    try:
        handles = json.loads(data.get("handles") or "[]")
    except (TypeError, ValueError):
        handles = []
    return {
        "id": data["id"],
        "name": data["name"],
        "handles": [h for h in handles if isinstance(h, str)],
        "owner_email": data["owner_email"],
        "is_shared": bool(data.get("is_shared")),
        "updated_at": data.get("updated_at"),
    }


def list_account_lists(email: str) -> list[dict[str, Any]]:
    """Lists this user can see: their own, plus anything explicitly shared.

    Ordered by name so the extra tabs don't reshuffle between page loads.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM account_lists
            WHERE owner_email = ? OR is_shared = 1
            ORDER BY name COLLATE NOCASE
            """,
            (email.strip().lower(),),
        ).fetchall()
    return [_account_list_row(row) for row in rows]


def upsert_account_list(email: str, name: str, handles: list[str], list_id: int | None = None) -> dict[str, Any]:
    """Creates a list, or renames/re-scopes one the caller owns.

    Scoped by owner_email on update so a crafted id can't edit someone
    else's list -- ownership is enforced here rather than trusted from the
    client.
    """
    owner = email.strip().lower()
    # Lowercase to match how handles are stored, so a list saved with mixed
    # case still matches posts when the frontend filters by handle.
    payload = json.dumps([h.strip().lstrip("@").lower() for h in handles if h and h.strip()])
    now = utc_now()
    with connect() as conn:
        if list_id is not None:
            cursor = conn.execute(
                """
                UPDATE account_lists SET name = ?, handles = ?, updated_at = ?
                WHERE id = ? AND owner_email = ?
                """,
                (name.strip(), payload, now, list_id, owner),
            )
            if not cursor.rowcount:
                raise ValueError("List not found.")
            row = conn.execute("SELECT * FROM account_lists WHERE id = ?", (list_id,)).fetchone()
        else:
            cursor = conn.execute(
                """
                INSERT INTO account_lists (owner_email, name, handles, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(owner_email, name)
                DO UPDATE SET handles = excluded.handles, updated_at = excluded.updated_at
                """,
                (owner, name.strip(), payload, now, now),
            )
            row = conn.execute(
                "SELECT * FROM account_lists WHERE owner_email = ? AND name = ?", (owner, name.strip())
            ).fetchone()
    return _account_list_row(row)


def delete_account_list(email: str, list_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            "DELETE FROM account_lists WHERE id = ? AND owner_email = ?", (list_id, email.strip().lower())
        )
    return bool(cursor.rowcount)
