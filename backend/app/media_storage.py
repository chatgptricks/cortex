"""Durable media storage with a deliberately reversible Render fallback.

Database rows historically contain absolute paths under ``/var/data/uploads``.
This module accepts those paths as well as ``r2://uploads/<filename>`` refs, so
the application can move one object at a time without a schema migration or a
flag day. R2 is the durable source of truth; the Render-disk mirror is an
explicit, temporary migration switch.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import (
    R2_ACCESS_KEY_ID,
    R2_BUCKET,
    R2_ENDPOINT_URL,
    R2_LOCAL_MIRROR_ENABLED,
    R2_MEDIA_ENABLED,
    R2_SECRET_ACCESS_KEY,
    UPLOAD_DIR,
)

logger = logging.getLogger("uvicorn.error")
_REF_PREFIX = "r2://"
_UPLOAD_PREFIX = "uploads/"
_PRESIGN_SECONDS = 60 * 60 * 24


def r2_enabled() -> bool:
    return bool(R2_MEDIA_ENABLED and R2_ENDPOINT_URL and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET)


def is_r2_reference(value: str | Path | None) -> bool:
    return isinstance(value, str) and value.startswith(_REF_PREFIX + _UPLOAD_PREFIX)


def _object_key(reference: str) -> str:
    if not is_r2_reference(reference):
        raise ValueError("Not a supported R2 media reference.")
    key = reference.removeprefix(_REF_PREFIX)
    parts = Path(key).parts
    if len(parts) != 2 or parts[0] != "uploads" or parts[1] in {"", ".", ".."}:
        raise ValueError("Invalid R2 media reference.")
    return key


def _reference_for_filename(filename: str) -> str:
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename:
        raise ValueError("Media filename must be a single filename.")
    return f"{_REF_PREFIX}{_UPLOAD_PREFIX}{safe_name}"


@lru_cache(maxsize=1)
def _client() -> Any:
    if not r2_enabled():
        raise RuntimeError("R2 media storage is not enabled.")
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("boto3 is required for configured R2 media storage.") from exc
    return boto3.client("s3", endpoint_url=R2_ENDPOINT_URL, aws_access_key_id=R2_ACCESS_KEY_ID,
                        aws_secret_access_key=R2_SECRET_ACCESS_KEY, region_name="auto")


def _local_path_for_key(key: str) -> Path:
    return UPLOAD_DIR / Path(key).name


def _write_local_fallback(local_path: Path, payload: bytes) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = local_path.with_name(f".{local_path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_bytes(payload)
        os.replace(temp_path, local_path)
    finally:
        temp_path.unlink(missing_ok=True)


def store_uploaded_media(filename: str, payload: bytes, *, content_type: str | None = None) -> str:
    """Store durable media in R2, retaining a local fallback only when needed."""
    reference = _reference_for_filename(filename)
    local_path = _local_path_for_key(_object_key(reference))
    if not r2_enabled():
        _write_local_fallback(local_path, payload)
        return str(local_path)
    try:
        _client().put_object(Bucket=R2_BUCKET, Key=_object_key(reference), Body=payload,
                             ContentType=content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream",
                             CacheControl="public, max-age=31536000, immutable")
    except Exception:
        logger.exception("R2 upload failed for %s; retaining local media fallback", filename)
        _write_local_fallback(local_path, payload)
        return str(local_path)
    if R2_LOCAL_MIRROR_ENABLED:
        _write_local_fallback(local_path, payload)
    return reference


def redirect_url(reference: str | Path | None) -> str | None:
    """Create a direct, short-lived URL so Render never proxies an R2 object."""
    if not isinstance(reference, str) or not is_r2_reference(reference) or not r2_enabled():
        return None
    try:
        return _client().generate_presigned_url("get_object", Params={"Bucket": R2_BUCKET, "Key": _object_key(reference)},
                                                ExpiresIn=_PRESIGN_SECONDS)
    except Exception:
        logger.exception("Could not create R2 read URL for %s", reference)
        return None


def materialize_local_path(reference: str | Path | None) -> Path | None:
    """Resolve media to a local file for OCR and a safe fallback response.

    With the durable mirror disabled, R2 reads land in a process-local temp
    file rather than recreating a billed Render-disk cache. Public media
    responses redirect to R2 and do not call this function in normal use.
    """
    if reference is None:
        return None
    if not is_r2_reference(reference):
        candidate = Path(str(reference))
        return candidate if candidate.is_file() else None
    key = _object_key(str(reference))
    local_path = _local_path_for_key(key)
    if local_path.is_file():
        return local_path
    if not r2_enabled():
        return None
    if R2_LOCAL_MIRROR_ENABLED:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = local_path.with_name(f".{local_path.name}.{os.getpid()}.download")
    else:
        handle = tempfile.NamedTemporaryFile(prefix="cortex-r2-", suffix=Path(key).suffix, delete=False)
        temp_path = Path(handle.name)
        handle.close()
    try:
        _client().download_file(R2_BUCKET, key, str(temp_path))
        if R2_LOCAL_MIRROR_ENABLED:
            os.replace(temp_path, local_path)
            return local_path
        return temp_path
    except Exception:
        logger.exception("Could not materialize R2 media %s", reference)
        temp_path.unlink(missing_ok=True)
        return None


def cleanup_materialized_path(path: str | Path | None) -> None:
    """Remove a non-mirrored R2 processing file once its immediate job ends."""
    if path is None:
        return
    candidate = Path(path)
    if candidate.parent == Path(tempfile.gettempdir()) and candidate.name.startswith("cortex-r2-"):
        candidate.unlink(missing_ok=True)


def upload_legacy_local_media(path: str | Path) -> str | None:
    """Copy one old disk object to R2 without deleting its disk fallback."""
    local_path = Path(path)
    if not local_path.is_file() or not r2_enabled():
        return None
    reference = _reference_for_filename(local_path.name)
    try:
        _client().upload_file(str(local_path), R2_BUCKET, _object_key(reference), ExtraArgs={
            "ContentType": mimetypes.guess_type(local_path.name)[0] or "application/octet-stream",
            "CacheControl": "public, max-age=31536000, immutable",
        })
    except Exception:
        logger.exception("Could not backfill local media %s to R2", local_path)
        return None
    return reference
