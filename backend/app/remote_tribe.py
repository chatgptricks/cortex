from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import REMOTE_TRIBE_TIMEOUT, REMOTE_TRIBE_TOKEN, REMOTE_TRIBE_URL


class RemoteTribeUnavailable(RuntimeError):
    pass


def remote_tribe_status() -> dict[str, Any]:
    return {
        "configured": bool(REMOTE_TRIBE_URL),
        "url": REMOTE_TRIBE_URL,
        "token_present": bool(REMOTE_TRIBE_TOKEN),
        "timeout_seconds": REMOTE_TRIBE_TIMEOUT,
    }


def analyze_video_remote(video_path: Path, duration_seconds: int | float | None = None) -> dict[str, Any]:
    if not REMOTE_TRIBE_URL:
        raise RemoteTribeUnavailable("REMOTE_TRIBE_URL is not configured.")

    try:
        import httpx
    except ImportError as exc:
        raise RemoteTribeUnavailable(
            "httpx is not installed. Run `pip install -r backend/requirements.txt` in the backend virtualenv."
        ) from exc

    headers = {}
    if REMOTE_TRIBE_TOKEN:
        headers["Authorization"] = f"Bearer {REMOTE_TRIBE_TOKEN}"

    try:
        with video_path.open("rb") as handle:
            response = httpx.post(
                REMOTE_TRIBE_URL,
                data={"duration_seconds": str(duration_seconds or "")},
                files={"file": (video_path.name, handle, "video/mp4")},
                headers=headers,
                follow_redirects=True,
                timeout=REMOTE_TRIBE_TIMEOUT,
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _response_detail(exc.response)
        raise RemoteTribeUnavailable(f"Remote TRIBE worker returned HTTP {exc.response.status_code}: {detail}") from exc
    except httpx.HTTPError as exc:
        raise RemoteTribeUnavailable(f"Remote TRIBE worker request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise RemoteTribeUnavailable("Remote TRIBE worker returned a non-JSON response.") from exc

    summary = payload.get("summary") if isinstance(payload, dict) else None
    if not isinstance(summary, dict):
        raise RemoteTribeUnavailable("Remote TRIBE worker response did not include a summary object.")
    summary["remote_worker"] = {
        "url": REMOTE_TRIBE_URL,
        "provider": (payload.get("worker") or {}).get("provider") if isinstance(payload, dict) else None,
        "gpu": (payload.get("worker") or {}).get("gpu") if isinstance(payload, dict) else None,
    }
    return summary


def _response_detail(response: Any) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict):
        return str(body.get("detail") or body)
    return str(body)
