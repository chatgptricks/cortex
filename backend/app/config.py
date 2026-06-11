from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _path_from_env(name: str, default: Path) -> Path:
    value = os.getenv(name)
    if not value:
        return default
    return Path(value).expanduser().resolve()


DATA_DIR = _path_from_env("PREDICT_DATA_DIR", PROJECT_ROOT / "data")
UPLOAD_DIR = DATA_DIR / "uploads"
VIDEO_DIR = DATA_DIR / "videos"
ANALYSIS_DIR = DATA_DIR / "analyses"
DB_PATH = DATA_DIR / "predict.sqlite3"

TRIBEV2_MODEL_ID = os.getenv("TRIBEV2_MODEL_ID", "facebook/tribev2")
TRIBEV2_DEVICE = os.getenv("TRIBEV2_DEVICE", "auto")
TRIBEV2_CACHE_DIR = _path_from_env("TRIBEV2_CACHE_DIR", DATA_DIR / "tribev2-cache")

LLM_REPORT_MODEL_ID = os.getenv("LLM_REPORT_MODEL_ID", "meta-llama/Meta-Llama-3.1-8B-Instruct")
LLM_REPORT_PROVIDER = os.getenv("LLM_REPORT_PROVIDER", "featherless-ai")
LLM_REPORT_MAX_TOKENS = int(os.getenv("LLM_REPORT_MAX_TOKENS", "700"))
LLM_REPORT_TEMPERATURE = float(os.getenv("LLM_REPORT_TEMPERATURE", "0.15"))
LLM_REPORT_TIMEOUT = float(os.getenv("LLM_REPORT_TIMEOUT", "90"))

REMOTE_TRIBE_URL = os.getenv("REMOTE_TRIBE_URL")
REMOTE_TRIBE_TOKEN = os.getenv("REMOTE_TRIBE_TOKEN")
REMOTE_TRIBE_TIMEOUT = float(os.getenv("REMOTE_TRIBE_TIMEOUT", "1200"))

REMOTE_OCR_URL = os.getenv("REMOTE_OCR_URL")
REMOTE_OCR_TOKEN = os.getenv("REMOTE_OCR_TOKEN") or REMOTE_TRIBE_TOKEN
REMOTE_OCR_TIMEOUT = float(os.getenv("REMOTE_OCR_TIMEOUT", "1800"))
OCR_BATCH_MIN_READY = int(os.getenv("OCR_BATCH_MIN_READY", "100"))
OCR_BATCH_SIZE = int(os.getenv("OCR_BATCH_SIZE", "100"))
OCR_CROP_REGION = os.getenv("OCR_CROP_REGION", "lower_half")

MIN_CALIBRATION_SAMPLES = int(os.getenv("MIN_CALIBRATION_SAMPLES", "3"))

# When set, all /api and /media requests must carry this key
# (X-API-Key header or ?token= query param). Leave unset for local dev.
PREDICT_API_KEY = os.getenv("PREDICT_API_KEY", "").strip() or None

# Comma-separated extra CORS origins for deployed frontends
# (e.g. "https://user.github.io").
EXTRA_CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("PREDICT_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_VIDEO_SECONDS = 2
DEFAULT_VIDEO_FPS = 1


def ensure_directories() -> None:
    for directory in [DATA_DIR, UPLOAD_DIR, VIDEO_DIR, ANALYSIS_DIR, TRIBEV2_CACHE_DIR]:
        directory.mkdir(parents=True, exist_ok=True)
