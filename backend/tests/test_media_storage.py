from contextlib import contextmanager

from app import media_backfill, media_storage


def test_local_write_remains_the_default_when_r2_is_off(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(media_storage, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(media_storage, "r2_enabled", lambda: False)
    reference = media_storage.store_uploaded_media("dash-sentient-post.jpg", b"image-bytes", content_type="image/jpeg")
    assert reference == str(upload_dir / "dash-sentient-post.jpg")
    assert (upload_dir / "dash-sentient-post.jpg").read_bytes() == b"image-bytes"


def test_r2_reference_resolves_to_its_local_safety_copy(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    local = upload_dir / "avatar-sentient.jpg"
    local.write_bytes(b"avatar")
    monkeypatch.setattr(media_storage, "UPLOAD_DIR", upload_dir)
    assert media_storage.is_r2_reference("r2://uploads/avatar-sentient.jpg")
    assert media_storage.materialize_local_path("r2://uploads/avatar-sentient.jpg") == local


def test_r2_write_returns_a_durable_reference_while_retaining_local_copy(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def put_object(self, **kwargs):
            self.calls.append(kwargs)

    upload_dir = tmp_path / "uploads"
    client = FakeClient()
    monkeypatch.setattr(media_storage, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(media_storage, "R2_BUCKET", "sentient-media")
    monkeypatch.setattr(media_storage, "r2_enabled", lambda: True)
    monkeypatch.setattr(media_storage, "_client", lambda: client)
    reference = media_storage.store_uploaded_media("cover-post.webp", b"image", content_type="image/webp")
    assert reference == "r2://uploads/cover-post.webp"
    assert (upload_dir / "cover-post.webp").read_bytes() == b"image"
    assert client.calls[0]["Key"] == "uploads/cover-post.webp"


def test_media_filename_cannot_escape_uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(media_storage, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(media_storage, "r2_enabled", lambda: False)
    try:
        media_storage.store_uploaded_media("../escape.jpg", b"nope")
    except ValueError as exc:
        assert "single filename" in str(exc)
    else:
        raise AssertionError("Expected an invalid filename to be rejected")


def test_backfill_binds_the_r2_prefix_for_postgres_compatibility(monkeypatch):
    class FakeCursor:
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
    monkeypatch.setattr(media_backfill, "upload_legacy_local_media", lambda path: "r2://uploads/cover.jpg")

    assert media_backfill.backfill(1, dry_run=False) == {"scanned": 1, "uploaded": 1, "skipped": 0, "failed": 0}
    select_statement, select_params = connection.calls[0]
    assert "NOT LIKE ?" in select_statement
    assert select_params == ("r2://uploads/%", 1)
