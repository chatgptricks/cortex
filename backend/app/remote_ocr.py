from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from .config import REMOTE_OCR_TIMEOUT, REMOTE_OCR_TOKEN, REMOTE_OCR_URL


class RemoteOcrUnavailable(RuntimeError):
    pass


def remote_ocr_status() -> dict[str, Any]:
    return {
        "configured": bool(REMOTE_OCR_URL),
        "url": REMOTE_OCR_URL,
        "token_present": bool(REMOTE_OCR_TOKEN),
        "timeout_seconds": REMOTE_OCR_TIMEOUT,
    }


def extract_images_text_remote(image_paths: list[Path], crop_region: str = "lower_half") -> list[dict[str, Any]]:
    if not REMOTE_OCR_URL:
        raise RemoteOcrUnavailable("REMOTE_OCR_URL is not configured.")
    if not image_paths:
        return []

    try:
        import httpx
    except ImportError as exc:
        raise RemoteOcrUnavailable(
            "httpx is not installed. Run `pip install -r backend/requirements.txt` in the backend virtualenv."
        ) from exc

    headers = {}
    if REMOTE_OCR_TOKEN:
        headers["Authorization"] = f"Bearer {REMOTE_OCR_TOKEN}"

    handles = []
    try:
        files = []
        for path in image_paths:
            handle = path.open("rb")
            handles.append(handle)
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            files.append(("files", (path.name, handle, content_type)))

        response = httpx.post(
            REMOTE_OCR_URL,
            data={"crop_region": crop_region},
            files=files,
            headers=headers,
            follow_redirects=True,
            timeout=REMOTE_OCR_TIMEOUT,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _response_detail(exc.response)
        raise RemoteOcrUnavailable(f"Remote OCR worker returned HTTP {exc.response.status_code}: {detail}") from exc
    except httpx.HTTPError as exc:
        raise RemoteOcrUnavailable(f"Remote OCR worker request failed: {exc}") from exc
    finally:
        for handle in handles:
            handle.close()

    try:
        payload = response.json()
    except ValueError as exc:
        raise RemoteOcrUnavailable("Remote OCR worker returned a non-JSON response.") from exc

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise RemoteOcrUnavailable("Remote OCR worker response did not include a results list.")
    return results


def _response_detail(response: Any) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict):
        return str(body.get("detail") or body)
    return str(body)
