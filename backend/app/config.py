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


# PREDICT_DATA_DIR / predict.sqlite3 are legacy names from when this backend
# was Predict's -- Predict is archived, but this is still the live Sentient
# Dash database file (dashboard_posts, accounts, dashboard_users,
# account_snapshots, account_lists all live here). Renaming it would be a
# real migration with downtime risk for zero functional benefit, so it's left
# as-is on purpose.
DATA_DIR = _path_from_env("PREDICT_DATA_DIR", PROJECT_ROOT / "data")
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "predict.sqlite3"
# Set only after the one-time SQLite import completes. Keeping this optional
# makes the cutover reversible: removing DATABASE_URL returns the service to
# its still-intact SQLite disk while the Postgres copy is investigated.
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

# R2 is opt-in: a deployment before the bucket credentials are added behaves
# exactly like the current Render-disk deployment.
R2_MEDIA_ENABLED = os.getenv("R2_MEDIA_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL", "").strip().rstrip("/")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET = os.getenv("R2_BUCKET", "").strip()
# During the R2 cutover this was kept on so every successful upload also left
# a Render-disk copy. Once all legacy media is backfilled, disabling it makes
# R2 the only durable copy while retaining a local fallback if an upload fails.
R2_LOCAL_MIRROR_ENABLED = os.getenv("R2_LOCAL_MIRROR_ENABLED", "true").strip().lower() in {"1", "true", "yes"}
R2_BACKFILL_ENABLED = os.getenv("R2_BACKFILL_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
R2_BACKFILL_BATCH_SIZE = max(1, min(_env_int("R2_BACKFILL_BATCH_SIZE", 25), 150))
R2_BACKFILL_INTERVAL_SECONDS = max(30, _env_int("R2_BACKFILL_INTERVAL_SECONDS", 60))

# Origins the deployed frontends are served from. These are baked in rather
# than left to an env var because they're a property of where this app lives,
# not of a particular deployment -- and a missing env var silently breaks
# every API call from the dashboard with an opaque CORS error.
# PREDICT_ALLOWED_ORIGINS still adds more at runtime (previews, local hosts) --
# kept under its old env var name so nothing needs to change on Render.
_DEFAULT_CORS_ORIGINS = [
    "https://sentientdash.app",
    "https://www.sentientdash.app",
    # Kept alongside the custom domain: GitHub Pages keeps serving this URL
    # (it 301s to the custom domain), and links to it exist in Slack history.
    "https://chatgptricks.github.io",
]

EXTRA_CORS_ORIGINS = _DEFAULT_CORS_ORIGINS + [
    origin.strip()
    for origin in os.getenv("PREDICT_ALLOWED_ORIGINS", "").split(",")
    if origin.strip() and origin.strip() not in _DEFAULT_CORS_ORIGINS
]


def ensure_directories() -> None:
    for directory in [DATA_DIR, UPLOAD_DIR]:
        directory.mkdir(parents=True, exist_ok=True)
