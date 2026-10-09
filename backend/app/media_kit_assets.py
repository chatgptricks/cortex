"""Optional, bounded thumbnails from the account's existing R2 assets."""
from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from PIL import Image, ImageOps

from . import media_storage

_MAX_BYTES = 4 * 1024 * 1024
_MAX_PIXELS = 20_000_000


def _thumbnail(client: Any, reference: str) -> bytes | None:
    body = None
    try:
        key = media_storage._object_key(reference)
        result = client.get_object(Bucket=media_storage.R2_BUCKET, Key=key, Range=f"bytes=0-{_MAX_BYTES - 1}")
        body = result["Body"]
        payload = body.read(_MAX_BYTES)
        with Image.open(io.BytesIO(payload)) as source:
            if source.width * source.height > _MAX_PIXELS:
                return None
            image = ImageOps.fit(ImageOps.exif_transpose(source).convert("RGB"), (320, 320), method=Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=85, optimize=True)
            return output.getvalue()
    except Exception:
        # A missing or invalid cover must not prevent a metrics download.
        return None
    finally:
        if body is not None:
            body.close()


def prepare_media_kit_assets(report: dict[str, Any]) -> None:
    """Attach small in-memory images; never fetch arbitrary remote URLs."""
    if not media_storage.r2_enabled():
        return
    account = report.get("account") or {}
    groups = report.get("public_best_posts") if "public_best_posts" in report else report.get("best_posts", {})
    posts = [post for key in ("all_time", "last_30_days") for post in (groups.get(key) or [])[:6]]
    targets = [(account, "avatar_bytes", account.get("avatar_path"))]
    targets += [(post, "thumbnail_bytes", post.get("cover_path")) for post in posts]
    references = list(dict.fromkeys(reference for _, _, reference in targets if media_storage.is_r2_reference(reference)))
    if not references:
        return
    import boto3
    from botocore.config import Config

    # Keep image enrichment bounded even if object storage is unavailable.
    # No paid profile/post scrapes or persistent media files are involved.
    client = boto3.client(
        "s3", endpoint_url=media_storage.R2_ENDPOINT_URL,
        aws_access_key_id=media_storage.R2_ACCESS_KEY_ID,
        aws_secret_access_key=media_storage.R2_SECRET_ACCESS_KEY,
        region_name="auto", config=Config(connect_timeout=2, read_timeout=4, retries={"max_attempts": 0}),
    )
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            images = dict(zip(references, pool.map(lambda reference: _thumbnail(client, reference), references)))
        for target, field, reference in targets:
            if reference in images and images[reference] is not None:
                target[field] = images[reference]
    finally:
        client.close()
