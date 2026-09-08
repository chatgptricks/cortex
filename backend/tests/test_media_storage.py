from contextlib import contextmanager

import pytest

from app import media_backfill, media_storage


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


def test_media_filename_cannot_escape_uploads(monkeypatch):
    monkeypatch.setattr(media_storage, "r2_enabled", lambda: False)
    try:
        media_storage.store_uploaded_media("../escape.jpg", b"nope")
    except ValueError as exc:
        assert "single filename" in str(exc)
    else:
        raise AssertionError("Expected an invalid filename to be rejected")


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
