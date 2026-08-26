"""HTTP client for Sentient Dash's own OCR worker (workers/modal_ocr_worker.py).

Deliberately separate from remote_ocr.py, which still serves Predict's
"Post DB" OCR feature through the shared tribev2 GPU worker. This module
talks to a different Modal app, with its own URL/token, and always requests
OCR on the full cover image -- there is no crop-region concept here.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from .config import SENTIENT_OCR_TIMEOUT, SENTIENT_OCR_TOKEN, SENTIENT_OCR_URL


class SentientOcrUnavailable(RuntimeError):
    pass


def sentient_ocr_status() -> dict[str, Any]:
    return {
        "configured": bool(SENTIENT_OCR_URL),
        "url": SENTIENT_OCR_URL,
        "token_present": bool(SENTIENT_OCR_TOKEN),
        "timeout_seconds": SENTIENT_OCR_TIMEOUT,
    }


def extract_images_text_sentient(image_paths: list[Path]) -> list[dict[str, Any]]:
    if not SENTIENT_OCR_URL:
        raise SentientOcrUnavailable("SENTIENT_OCR_URL is not configured.")
    if not image_paths:
        return []

    try:
        import httpx
    except ImportError as exc:
        raise SentientOcrUnavailable(
            "httpx is not installed. Run `pip install -r backend/requirements.txt` in the backend virtualenv."
        ) from exc

    headers = {}
    if SENTIENT_OCR_TOKEN:
        headers["Authorization"] = f"Bearer {SENTIENT_OCR_TOKEN}"

    handles = []
    try:
        files = []
        for path in image_paths:
            handle = path.open("rb")
            handles.append(handle)
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            files.append(("files", (path.name, handle, content_type)))

        response = httpx.post(
            SENTIENT_OCR_URL,
            files=files,
            headers=headers,
            follow_redirects=True,
            timeout=SENTIENT_OCR_TIMEOUT,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _response_detail(exc.response)
        raise SentientOcrUnavailable(f"Sentient OCR worker returned HTTP {exc.response.status_code}: {detail}") from exc
    except httpx.HTTPError as exc:
        raise SentientOcrUnavailable(f"Sentient OCR worker request failed: {exc}") from exc
    finally:
        for handle in handles:
            handle.close()

    try:
        payload = response.json()
    except ValueError as exc:
        raise SentientOcrUnavailable("Sentient OCR worker returned a non-JSON response.") from exc

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise SentientOcrUnavailable("Sentient OCR worker response did not include a results list.")
    return results


def _response_detail(response: Any) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict):
        return str(body.get("detail") or body)
    return str(body)
