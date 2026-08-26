"""Standalone OCR worker for Sentient Dash cover images.

Deliberately independent of workers/modal_tribe_worker.py (Predict's video
worker, shared with the "Post DB" OCR feature at /api/post-db/ocr/*). That
worker piggybacked Sentient Dash's cover-image OCR onto the exact same
GPU-and-32GB-RAM function spec built for the tribev2 video model -- so every
OCR call paid for an L40S GPU it never touched (PaddleOCR there installs the
CPU build of paddlepaddle, not paddlepaddle-gpu) plus a multi-GB CUDA/torch
image just to cold-start.

This file has its own Modal App, its own image, its own secret, and its own
endpoint label -- nothing here is shared with Predict. Measured on 30 real
Sentient Dash covers: RapidOCR runs the full image in ~0.2s/image on plain
CPU with zero crashes, versus PaddleOCR which segfaulted outright in a
constrained container (a portability problem with its CPU wheel, independent
of the GPU-waste issue). Hence RapidOCR here, not Paddle.

Always OCRs the FULL image, never a crop. A fixed "lower half" (or any other)
crop region silently misses posts whose text sits outside it -- different
accounts template their cover text at the top, center, or bottom -- so
cropping trades a small speed gain for a real accuracy loss. At ~0.2s/image
the speed gain isn't worth it anyway.

Deploy: `modal deploy workers/modal_ocr_worker.py` (requires the Modal CLI
logged into the workspace, and a Modal secret named "sentient-ocr-secret"
with a SENTIENT_OCR_TOKEN key -- see scripts/deploy_sentient_ocr_worker.sh).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import modal
from fastapi import File, Header, HTTPException, UploadFile

CPU_CORES = float(os.getenv("SENTIENT_OCR_CPU", "2"))
MEMORY_MB = int(os.getenv("SENTIENT_OCR_MEMORY_MB", "2048"))
TIMEOUT_SECONDS = int(os.getenv("SENTIENT_OCR_TIMEOUT", "300"))
SCALEDOWN_WINDOW_SECONDS = int(os.getenv("SENTIENT_OCR_SCALEDOWN_WINDOW", "60"))
MAX_CONTAINERS = int(os.getenv("SENTIENT_OCR_MAX_CONTAINERS", "3"))
OCR_MIN_CONFIDENCE = float(os.getenv("SENTIENT_OCR_MIN_CONFIDENCE", "0.35"))

# Small volume just to cache RapidOCR's own tiny ONNX models (~10-15MB)
# across cold starts -- the image itself has no ML weights baked in.
cache_volume = modal.Volume.from_name("sentient-ocr-cache", create_if_missing=True)

image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "fastapi>=0.115,<1.0",
    "python-multipart>=0.0.9,<1.0",
    "pillow>=10,<13",
    "rapidocr-onnxruntime>=1.3,<2.0",
)

app = modal.App("sentient-cover-ocr", image=image)

_ENGINE = None


def _get_engine():
    global _ENGINE
    if _ENGINE is None:
        os.environ.setdefault("RAPIDOCR_MODEL_DIR", "/cache/rapidocr")
        from rapidocr_onnxruntime import RapidOCR

        _ENGINE = RapidOCR()
    return _ENGINE


def _extract_text(image_path: Path) -> str | None:
    engine = _get_engine()
    result, _elapsed = engine(str(image_path))
    if not result:
        return None
    lines: list[str] = []
    for item in result:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        text = str(item[1]).strip()
        confidence = 1.0
        if len(item) >= 3:
            try:
                confidence = float(item[2])
            except (TypeError, ValueError):
                confidence = 1.0
        if text and confidence >= OCR_MIN_CONFIDENCE:
            lines.append(text)
    if not lines:
        return None
    cleaned = [" ".join(line.split()) for line in lines]
    cleaned = [line for line in cleaned if line and len(line) > 1]
    if not cleaned:
        return None
    return "\n".join(cleaned)[:2000]


@app.function(
    cpu=CPU_CORES,
    memory=MEMORY_MB,
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
    max_containers=MAX_CONTAINERS,
    volumes={"/cache": cache_volume},
    secrets=[modal.Secret.from_name("sentient-ocr-secret")],
)
@modal.fastapi_endpoint(method="POST", label="sentient-cover-ocr", docs=False)
async def ocr_batch(
    files: list[UploadFile] = File(...),
    authorization: str | None = Header(default=None),
) -> dict:
    expected_token = os.getenv("SENTIENT_OCR_TOKEN")
    if expected_token and authorization != f"Bearer {expected_token}":
        raise HTTPException(status_code=401, detail="Invalid Sentient OCR token.")
    if len(files) > 100:
        raise HTTPException(status_code=400, detail="OCR batch is limited to 100 files.")

    results = []
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        for index, file in enumerate(files):
            suffix = Path(file.filename or "cover.jpg").suffix or ".jpg"
            saved_path = temp_path / f"{index:04d}{suffix}"
            saved_path.write_bytes(await file.read())
            results.append({"filename": file.filename, "text": _extract_text(saved_path)})

    await cache_volume.commit.aio()
    return {
        "results": results,
        "worker": {"provider": "modal", "engine": "rapidocr", "gpu": None, "app": "sentient-cover-ocr"},
    }
