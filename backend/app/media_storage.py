"""R2-only media storage for Sentient Dash.

Runtime requests write durable ``r2://uploads/<filename>`` references and
public responses redirect directly to signed R2 URLs. The only local file
created here is a short-lived temp file for OCR processing.
"""

from __future__ import annotations

import logging
import mimetypes
import tempfile
import time
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any

from .config import (
    R2_ACCESS_KEY_ID,
    R2_BUCKET,
    R2_ENDPOINT_URL,
    R2_MEDIA_ENABLED,
    R2_SECRET_ACCESS_KEY,
)

logger = logging.getLogger("uvicorn.error")
_REF_PREFIX = "r2://"
_UPLOAD_PREFIX = "uploads/"
_PRESIGN_SECONDS = 60 * 60 * 24
_PUBLIC_CACHE_CONTROL = "public, max-age=31536000, immutable"
_PRIVATE_CACHE_CONTROL = "private, no-store"
_PRIVATE_OBJECT_PREFIXES = ("uploads/queue-", "uploads/alert-")
_PRIVATE_METADATA_CACHE_SECONDS = 60
_PRIVATE_METADATA_CACHE_LIMIT = 1024
_private_metadata_cache: OrderedDict[tuple[str, str], float] = OrderedDict()
_private_metadata_lock = RLock()


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


def store_uploaded_media(filename: str, payload: bytes, *, content_type: str | None = None, private: bool = False) -> str:
    """Store media in R2 and return its durable object reference.

    There is intentionally no disk fallback. A successful API response must
    always point at the same durable store that serves the media in production.
    """
    reference = _reference_for_filename(filename)
    if not r2_enabled():
        raise RuntimeError("R2 media storage is not configured; local media fallback is disabled.")
    key = _object_key(reference)
    try:
        _client().put_object(Bucket=R2_BUCKET, Key=key, Body=payload,
                             ContentType=content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream",
                             CacheControl=_PRIVATE_CACHE_CONTROL if private else _PUBLIC_CACHE_CONTROL)
        with _private_metadata_lock:
            _private_metadata_cache.pop((R2_BUCKET, key), None)
    except Exception as exc:
        logger.exception("R2 upload failed for %s", filename)
        raise RuntimeError(f"R2 upload failed for {filename}.") from exc
    return reference


def _has_private_cache_control(metadata: dict[str, Any]) -> bool:
    directives = {part.strip().lower() for part in str(metadata.get("CacheControl", "")).split(",")}
    return {"private", "no-store"}.issubset(directives) and "public" not in directives


def _ensure_private_cache_control(client: Any, key: str) -> None:
    """Repair only private upload metadata before issuing a bearer URL.

    Queue/alert filenames are immutable random keys. A bounded, short cache
    avoids repeating metadata reads; uploads through this module invalidate it.
    """
    if not key.startswith(_PRIVATE_OBJECT_PREFIXES):
        raise ValueError("Private downloads require a Queue or alert object.")
    cache_key = (R2_BUCKET, key)
    with _private_metadata_lock:
        if _private_metadata_cache.get(cache_key, 0) > time.monotonic():
            _private_metadata_cache.move_to_end(cache_key)
            return
        _private_metadata_cache.pop(cache_key, None)
        metadata = client.head_object(Bucket=R2_BUCKET, Key=key)
        if not _has_private_cache_control(metadata):
            etag = metadata.get("ETag")
            if not etag:
                raise RuntimeError("Cannot safely repair private media metadata without an ETag.")
            copy_args = {
                "Bucket": R2_BUCKET,
                "Key": key,
                "CopySource": {"Bucket": R2_BUCKET, "Key": key},
                "CopySourceIfMatch": etag,
                "MetadataDirective": "REPLACE",
                "CacheControl": _PRIVATE_CACHE_CONTROL,
            }
            for field in ("ContentType", "ContentDisposition", "ContentEncoding", "ContentLanguage", "Expires", "Metadata", "StorageClass"):
                if metadata.get(field) is not None:
                    copy_args[field] = metadata[field]
            client.copy_object(**copy_args)
            # Do not trust a successful copy response to establish the header
            # seen by the final download, or cache an unsuccessful repair.
            metadata = client.head_object(Bucket=R2_BUCKET, Key=key)
            if not _has_private_cache_control(metadata):
                raise RuntimeError("Private media cache metadata could not be verified.")
        _private_metadata_cache[cache_key] = time.monotonic() + _PRIVATE_METADATA_CACHE_SECONDS
        while len(_private_metadata_cache) > _PRIVATE_METADATA_CACHE_LIMIT:
            _private_metadata_cache.popitem(last=False)


def redirect_url(reference: str | Path | None, *, private: bool = False, lifetime_seconds: int = 300) -> str | None:
    """Create a direct, short-lived URL so Render never proxies an R2 object."""
    if not isinstance(reference, str) or not is_r2_reference(reference) or not r2_enabled():
        return None
    try:
        params = {"Bucket": R2_BUCKET, "Key": _object_key(reference)}
        client = _client()
        if private:
            _ensure_private_cache_control(client, params["Key"])
            params["ResponseCacheControl"] = _PRIVATE_CACHE_CONTROL
        return client.generate_presigned_url("get_object", Params=params,
                                                ExpiresIn=max(1, min(lifetime_seconds, _PRESIGN_SECONDS)) if private else _PRESIGN_SECONDS)
    except Exception:
        logger.exception("Could not create R2 read URL for %s", reference)
        return None


def materialize_local_path(reference: str | Path | None) -> Path | None:
    """Download an R2 object to a short-lived processing temp file.

    This is used by OCR only. It never recreates a persistent local media
    cache, and legacy absolute disk paths are intentionally not served.
    """
    if reference is None:
        return None
    if not is_r2_reference(reference):
        return None
    key = _object_key(str(reference))
    if not r2_enabled():
        return None
    handle = tempfile.NamedTemporaryFile(prefix="sentient-r2-", suffix=Path(key).suffix, delete=False)
    temp_path = Path(handle.name)
    handle.close()
    try:
        _client().download_file(R2_BUCKET, key, str(temp_path))
        return temp_path
    except Exception:
        logger.exception("Could not materialize R2 media %s", reference)
        temp_path.unlink(missing_ok=True)
        return None


def cleanup_materialized_path(path: str | Path | None) -> None:
    """Remove a short-lived R2 processing file once its immediate job ends."""
    if path is None:
        return
    candidate = Path(path)
    if candidate.parent == Path(tempfile.gettempdir()) and candidate.name.startswith("sentient-r2-"):
        candidate.unlink(missing_ok=True)


def upload_local_media_for_migration(path: str | Path) -> str | None:
    """Upload one legacy disk object during the one-time R2 migration.

    This helper is deliberately isolated to ``media_backfill.py``. Runtime
    requests never read or write the old disk path.
    """
    local_path = Path(path)
    if not local_path.is_file() or not r2_enabled():
        return None
    reference = _reference_for_filename(local_path.name)
    try:
        _client().upload_file(str(local_path), R2_BUCKET, _object_key(reference), ExtraArgs={
            "ContentType": mimetypes.guess_type(local_path.name)[0] or "application/octet-stream",
            "CacheControl": _PUBLIC_CACHE_CONTROL,
        })
    except Exception:
        logger.exception("Could not backfill local media %s to R2", local_path)
        return None
    return reference
