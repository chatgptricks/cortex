"""Resource-bound, expiring links for authenticated staff avatars.

Image elements cannot send the browser's Firebase bearer. The API issues a
capability for one avatar after authorizing the surrounding JSON request.
The signing key stays server-side and is derived from the existing Firebase
credential; it is never a browser or repository configuration value.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode


_AVATAR_PATH = re.compile(r"/api/dashboard/user-avatar/U[A-Z0-9]{8,20}")
_LIFETIME_SECONDS = 60 * 60


@lru_cache(maxsize=1)
def _signing_key() -> bytes | None:
    for credential_path in (
        Path("/etc/secrets/firebase-adminsdk.json"),
        Path(__file__).resolve().parent.parent / "firebase-adminsdk.json",
    ):
        try:
            credential = json.loads(credential_path.read_text())
            private_key = credential.get("private_key")
            if isinstance(private_key, str) and "BEGIN PRIVATE KEY" in private_key:
                return hashlib.sha256(("sentient-staff-avatar-v1\0" + private_key).encode()).digest()
        except (OSError, ValueError, TypeError):
            continue
    return None


def _signature(key: bytes, path: str, expires: int) -> str:
    return hmac.new(key, f"GET\n{path}\n{expires}".encode(), hashlib.sha256).hexdigest()


def staff_avatar_url(slack_user_id: str) -> str:
    path = f"/api/dashboard/user-avatar/{str(slack_user_id or '').strip().upper()}"
    key = _signing_key()
    if key is None or _AVATAR_PATH.fullmatch(path) is None:
        return ""
    expires = int(time.time()) + _LIFETIME_SECONDS
    return f"{path}?{urlencode({'expires': expires, 'signature': _signature(key, path, expires)})}"


def valid_private_media_request(request) -> bool:
    path = request.url.path
    if request.method not in {"GET", "HEAD"} or _AVATAR_PATH.fullmatch(path) is None:
        return False
    # Reject ambiguous query strings instead of choosing among credentials.
    if len(request.query_params.getlist("expires")) != 1 or len(request.query_params.getlist("signature")) != 1:
        return False
    signature = request.query_params.get("signature", "")
    if re.fullmatch(r"[0-9a-f]{64}", signature) is None:
        return False
    try:
        expires = int(request.query_params["expires"])
    except (ValueError, TypeError):
        return False
    now = int(time.time())
    if expires <= now or expires > now + _LIFETIME_SECONDS:
        return False
    key = _signing_key()
    return key is not None and hmac.compare_digest(signature, _signature(key, path, expires))
