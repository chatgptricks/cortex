from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _path_from_env(name: str, default: Path) -> Path:
    value = os.getenv(name)
    if not value:
        return default
    return Path(value).expanduser().resolve()


def _env_int(name: str, default: int) -> int:
    """Read an optional integer without making a blank Render field fatal."""
    try:
        return int(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """Read an optional numeric timeout without making a blank field fatal."""
    try:
        return float(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default


# Local data is only the development/SQLite fallback. Production uses the
# shared Postgres database and stores every uploaded asset in R2.
DATA_DIR = _path_from_env("SENTIENT_DATA_DIR", PROJECT_ROOT / "data")
DB_PATH = DATA_DIR / "sentient.sqlite3"
# Set in production. Leaving it empty keeps the small SQLite fallback useful
# for local development without making the deployed service depend on disk.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
POSTGRES_MIGRATION_URL = os.getenv("POSTGRES_MIGRATION_URL", "").strip()

# Separate password gating Sentient Dash's various admin-write endpoints
# (backfill, refresh, account settings, etc.) inside their own handlers.
TRICKS_DASH_REFRESH_PASSWORD = os.getenv("TRICKS_DASH_REFRESH_PASSWORD", "").strip() or None

# Sentient Dash's own cover-image OCR worker (workers/modal_ocr_worker.py) --
# a standalone, GPU-free Modal app with its own secret. Always OCRs the full
# cover image; there is no crop-region setting here on purpose.
SENTIENT_OCR_URL = os.getenv("SENTIENT_OCR_URL")
SENTIENT_OCR_TOKEN = os.getenv("SENTIENT_OCR_TOKEN")
SENTIENT_OCR_TIMEOUT = _env_float("SENTIENT_OCR_TIMEOUT", 300)
# Keep multipart OCR requests small enough for the web instance. The OCR
# worker receives the same total queue over sequential requests, without a
# burst of hundreds of full-resolution images resident at once.
SENTIENT_OCR_MAX_FILES_PER_REQUEST = max(1, min(_env_int("SENTIENT_OCR_MAX_FILES_PER_REQUEST", 10), 20))

# R2 is the only runtime media store. If its credentials are missing, uploads
# fail clearly instead of silently creating another local copy.
R2_MEDIA_ENABLED = os.getenv("R2_MEDIA_ENABLED", "true").strip().lower() in {"1", "true", "yes"}
R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL", "").strip().rstrip("/")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET = os.getenv("R2_BUCKET", "").strip()

# A parallel deployment must not independently run scheduled refreshes,
# publishing jobs, or R2 maintenance against the shared production database.
# The active production instance keeps the default; the pre-cutover instance
# explicitly sets this to false until traffic moves to it.  Render may retain
# a Blueprint-managed value for SCHEDULER_ENABLED after a service clone; the
# explicit override is intentionally higher priority so the cutover can be
# completed without deleting that rollback setting mid-incident.
_scheduler_enabled_value = os.getenv("SCHEDULER_ENABLED", "true")
_scheduler_override = os.getenv("SCHEDULER_ENABLED_OVERRIDE", "").strip()
if _scheduler_override:
    _scheduler_enabled_value = _scheduler_override
SCHEDULER_ENABLED = _scheduler_enabled_value.strip().lower() in {"1", "true", "yes"}

# Origins the deployed frontends are served from. These are baked in rather
# than left to an env var because they're a property of where this app lives,
# not of a particular deployment -- and a missing env var silently breaks
# every API call from the dashboard with an opaque CORS error.
# SENTIENT_ALLOWED_ORIGINS adds preview/local origins at runtime when needed.
_DEFAULT_CORS_ORIGINS = [
    "https://sentientdash.app",
    "https://www.sentientdash.app",
    # Kept alongside the custom domain: GitHub Pages keeps serving this URL
    # (it 301s to the custom domain), and links to it exist in Slack history.
    "https://chatgptricks.github.io",
]

EXTRA_CORS_ORIGINS = _DEFAULT_CORS_ORIGINS + [
    origin.strip()
    for origin in os.getenv("SENTIENT_ALLOWED_ORIGINS", "").split(",")
    if origin.strip() and origin.strip() not in _DEFAULT_CORS_ORIGINS
]


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
