from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from app import media_backfill, media_storage


@pytest.fixture(autouse=True)
def clear_private_metadata_cache():
    media_storage._private_metadata_cache.clear()
    yield
    media_storage._private_metadata_cache.clear()


class FakePrivateClient:
    def __init__(self, *, cache_control="public, max-age=31536000, immutable"):
        self.metadata = {
            "ETag": '"unchanged-content"',
            "CacheControl": cache_control,
            "ContentType": "image/jpeg",
            "ContentDisposition": 'attachment; filename="example.jpg"',
            "ContentEncoding": "identity",
            "ContentLanguage": "en",
            "Expires": datetime(2030, 1, 1, tzinfo=timezone.utc),
            "Metadata": {"source": "example"},
            "StorageClass": "STANDARD",
        }
        self.head_calls = []
        self.copy_calls = []
        self.presign_calls = []
        self.put_calls = []

    def head_object(self, **kwargs):
        self.head_calls.append(kwargs)
        return dict(self.metadata)

    def copy_object(self, **kwargs):
        self.copy_calls.append(kwargs)
        assert kwargs["CopySourceIfMatch"] == self.metadata["ETag"]
        self.metadata["CacheControl"] = kwargs["CacheControl"]

    def generate_presigned_url(self, operation, **kwargs):
        self.presign_calls.append((operation, kwargs))
        return "https://example.com/signed-test-image"

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.metadata["CacheControl"] = kwargs["CacheControl"]


@pytest.fixture
def private_client(monkeypatch):
    client = FakePrivateClient()
    monkeypatch.setattr(media_storage, "R2_BUCKET", "test-media")
    monkeypatch.setattr(media_storage, "r2_enabled", lambda: True)
    monkeypatch.setattr(media_storage, "_client", lambda: client)
    return client


def test_upload_requires_r2_when_local_media_is_disabled(monkeypatch):
    monkeypatch.setattr(media_storage, "r2_enabled", lambda: False)
    with pytest.raises(RuntimeError, match="local media fallback is disabled"):
        media_storage.store_uploaded_media("dash-sentient-post.jpg", b"image-bytes", content_type="image/jpeg")


def test_r2_reference_materializes_to_a_short_lived_temp_file(tmp_path, monkeypatch):
    class FakeClient:
        def download_file(self, bucket, key, filename):
            assert bucket == "sentient-media"
            assert key == "uploads/avatar-sentient.jpg"
            tmp_path.joinpath(filename).write_bytes(b"avatar")

    monkeypatch.setattr(media_storage, "R2_BUCKET", "sentient-media")
    monkeypatch.setattr(media_storage, "r2_enabled", lambda: True)
    monkeypatch.setattr(media_storage, "_client", lambda: FakeClient())
    assert media_storage.is_r2_reference("r2://uploads/avatar-sentient.jpg")
    path = media_storage.materialize_local_path("r2://uploads/avatar-sentient.jpg")
    assert path is not None
    assert path.read_bytes() == b"avatar"
    media_storage.cleanup_materialized_path(path)
    assert not path.exists()


def test_r2_write_returns_a_durable_reference_without_a_local_mirror(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def put_object(self, **kwargs):
            self.calls.append(kwargs)

    client = FakeClient()
    monkeypatch.setattr(media_storage, "R2_BUCKET", "sentient-media")
    monkeypatch.setattr(media_storage, "r2_enabled", lambda: True)
    monkeypatch.setattr(media_storage, "_client", lambda: client)
    reference = media_storage.store_uploaded_media("cover-post.webp", b"image", content_type="image/webp")
    assert reference == "r2://uploads/cover-post.webp"
    assert client.calls[0]["Key"] == "uploads/cover-post.webp"
    assert client.calls[0]["CacheControl"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize("filename", ["queue-9-example.jpg", "alert-example.jpg"])
def test_private_uploads_store_no_cache_metadata(private_client, filename):
    reference = media_storage.store_uploaded_media(filename, b"private-image", private=True)
    assert reference == f"r2://uploads/{filename}"
    assert private_client.put_calls[-1]["CacheControl"] == "private, no-store"
    assert private_client.put_calls[-1]["Body"] == b"private-image"


def test_media_filename_cannot_escape_uploads(monkeypatch):
    monkeypatch.setattr(media_storage, "r2_enabled", lambda: False)
    try:
        media_storage.store_uploaded_media("../escape.jpg", b"nope")
    except ValueError as exc:
        assert "single filename" in str(exc)
    else:
        raise AssertionError("Expected an invalid filename to be rejected")


def test_private_downloads_repair_stored_headers_and_expire_quickly(private_client):
    assert media_storage.redirect_url("r2://uploads/queue-9-example.jpg", private=True)
    assert private_client.metadata["CacheControl"] == "private, no-store"
    assert private_client.presign_calls[-1][1]["ExpiresIn"] == 300
    assert private_client.presign_calls[-1][1]["Params"]["ResponseCacheControl"] == "private, no-store"
    assert media_storage.redirect_url("r2://uploads/alert-example.jpg", private=True, lifetime_seconds=86400)
    assert private_client.presign_calls[-1][1]["ExpiresIn"] == 86400
    old_head_count = len(private_client.head_calls)
    media_storage.redirect_url("r2://uploads/public-cover.jpg")
    assert "ResponseCacheControl" not in private_client.presign_calls[-1][1]["Params"]
    assert private_client.presign_calls[-1][1]["ExpiresIn"] == 86400
    assert len(private_client.head_calls) == old_head_count


def test_private_repair_preserves_metadata_and_uses_conditional_copy(private_client):
    original = dict(private_client.metadata)
    assert media_storage.redirect_url("r2://uploads/queue-9-example.jpg", private=True)
    copy = private_client.copy_calls[0]
    assert copy["Bucket"] == "test-media"
    assert copy["Key"] == "uploads/queue-9-example.jpg"
    assert copy["CopySource"] == {"Bucket": "test-media", "Key": "uploads/queue-9-example.jpg"}
    assert copy["CopySourceIfMatch"] == original["ETag"]
    assert copy["MetadataDirective"] == "REPLACE"
    assert copy["CacheControl"] == "private, no-store"
    for field in ("ContentType", "ContentDisposition", "ContentEncoding", "ContentLanguage", "Expires", "Metadata", "StorageClass"):
        assert copy[field] == original[field]
    assert len(private_client.head_calls) == 2


def test_private_repair_is_idempotent_for_concurrent_downloads(private_client):
    reference = "r2://uploads/queue-9-example.jpg"
    with ThreadPoolExecutor(max_workers=4) as executor:
        urls = list(executor.map(lambda _: media_storage.redirect_url(reference, private=True), range(8)))
    assert all(urls)
    assert len(private_client.copy_calls) == 1
    assert len(private_client.head_calls) == 2
    assert len(private_client.presign_calls) == 8


def test_private_cache_expires_and_is_invalidated_after_upload(private_client, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(media_storage.time, "monotonic", lambda: now[0])
    reference = "r2://uploads/queue-9-example.jpg"
    assert media_storage.redirect_url(reference, private=True)
    assert media_storage.redirect_url(reference, private=True)
    assert len(private_client.head_calls) == 2
    now[0] += media_storage._PRIVATE_METADATA_CACHE_SECONDS + 1
    assert media_storage.redirect_url(reference, private=True)
    assert len(private_client.head_calls) == 3
    media_storage.store_uploaded_media("queue-9-example.jpg", b"new-private-image", private=True)
    assert media_storage.redirect_url(reference, private=True)
    assert len(private_client.head_calls) == 4
    assert len(private_client.copy_calls) == 1


def test_private_cache_is_bounded_and_separate_for_each_key(private_client, monkeypatch):
    monkeypatch.setattr(media_storage, "_PRIVATE_METADATA_CACHE_LIMIT", 2)
    private_client.metadata["CacheControl"] = "private, no-store"
    for number in range(3):
        assert media_storage.redirect_url(f"r2://uploads/queue-{number}-example.jpg", private=True)
    assert len(media_storage._private_metadata_cache) == 2
    assert len(private_client.head_calls) == 3
    assert not private_client.copy_calls
    assert media_storage.redirect_url("r2://uploads/queue-0-example.jpg", private=True)
    assert len(private_client.head_calls) == 4


@pytest.mark.parametrize("filename", ["cover-example.jpg", "avatar-example.jpg", "attachment.jpg"])
def test_private_download_cannot_reclassify_public_objects(private_client, filename):
    assert media_storage.redirect_url(f"r2://uploads/{filename}", private=True) is None
    assert not private_client.head_calls
    assert not private_client.copy_calls
    assert not private_client.presign_calls


@pytest.mark.parametrize("failure", ["head", "missing-etag", "copy", "race", "unverified-copy"])
def test_private_download_fails_closed_and_retries_failed_repair(private_client, monkeypatch, failure):
    original_head = private_client.head_object
    original_copy = private_client.copy_object
    if failure == "missing-etag":
        private_client.metadata.pop("ETag")
    elif failure == "head":
        def failed_head(**kwargs):
            raise RuntimeError("metadata unavailable")
        monkeypatch.setattr(private_client, "head_object", failed_head)
    elif failure in {"copy", "race"}:
        def failed_copy(**kwargs):
            raise RuntimeError("precondition failed" if failure == "race" else "copy unavailable")
        monkeypatch.setattr(private_client, "copy_object", failed_copy)
    else:
        monkeypatch.setattr(private_client, "copy_object", lambda **kwargs: None)
    reference = "r2://uploads/queue-9-example.jpg"
    assert media_storage.redirect_url(reference, private=True) is None
    assert not private_client.presign_calls
    assert not media_storage._private_metadata_cache
    monkeypatch.setattr(private_client, "head_object", original_head)
    monkeypatch.setattr(private_client, "copy_object", original_copy)
    private_client.metadata["ETag"] = '"unchanged-content"'
    assert media_storage.redirect_url(reference, private=True)
    assert private_client.presign_calls


def test_backfill_binds_the_r2_prefix_for_postgres_compatibility(monkeypatch):
    class FakeCursor:
        rowcount = 1

        def __init__(self, rows=None):
            self.rows = rows or []

        def fetchall(self):
            return self.rows

    class FakeConnection:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params=()):
            self.calls.append((statement, params))
            if statement.startswith("SELECT"):
                return FakeCursor([{"id": 9, "media_ref": "/var/data/uploads/cover.jpg"}])
            return FakeCursor()

    connection = FakeConnection()

    @contextmanager
    def fake_connect():
        yield connection

    monkeypatch.setattr(media_backfill, "_SOURCES", (("dashboard_posts", "cover_image_path"),))
    monkeypatch.setattr(media_backfill, "connect", fake_connect)
    monkeypatch.setattr(media_backfill, "init_db", lambda: None)
    monkeypatch.setattr(media_backfill, "r2_enabled", lambda: True)
    monkeypatch.setattr(media_backfill, "upload_local_media_for_migration", lambda path: "r2://uploads/cover.jpg")

    assert media_backfill.backfill(1, dry_run=False) == {"scanned": 1, "uploaded": 1, "skipped": 0, "failed": 0}
    select_statement, select_params = connection.calls[0]
    assert "NOT LIKE ?" in select_statement
    assert select_params == ("r2://uploads/%", 1)
