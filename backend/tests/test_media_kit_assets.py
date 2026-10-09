import io

from PIL import Image

from app import media_kit_assets as assets


def image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (400, 200), "teal").save(output, format="PNG")
    return output.getvalue()


def test_thumbnail_limits_request_resizes_and_closes_body():
    body = io.BytesIO(image_bytes())
    requests = []

    class Client:
        def get_object(self, **kwargs):
            requests.append(kwargs)
            return {"Body": body}

    output = assets._thumbnail(Client(), "r2://uploads/post.png")
    assert requests[0]["Key"] == "uploads/post.png"
    assert requests[0]["Range"] == "bytes=0-4194303"
    assert body.closed
    with Image.open(io.BytesIO(output)) as thumbnail:
        assert thumbnail.size == (320, 320)
        assert thumbnail.format == "JPEG"


def test_untrusted_references_never_fetch_and_broken_images_are_optional():
    requests = []

    class Client:
        def get_object(self, **kwargs):
            requests.append(kwargs)
            return {"Body": io.BytesIO(b"not an image")}

    for reference in ("https://example.com/image.png", "r2://uploads/../secret.png", "/etc/passwd"):
        assert assets._thumbnail(Client(), reference) is None
    assert not requests
    assert assets._thumbnail(Client(), "r2://uploads/broken.png") is None
    assert len(requests) == 1


def test_prepare_deduplicates_existing_assets_and_attaches_only_main_images(monkeypatch):
    import boto3
    monkeypatch.setattr(assets.media_storage, "r2_enabled", lambda: True)
    requests = []
    bodies = []

    class Client:
        closed = False

        def get_object(self, **kwargs):
            requests.append(kwargs)
            body = io.BytesIO(image_bytes())
            bodies.append(body)
            return {"Body": body}

        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)
    report = {"account": {"avatar_path": "r2://uploads/avatar.png"}, "best_posts": {
        "all_time": [{"cover_path": "r2://uploads/post.png"}],
        "last_30_days": [{"cover_path": "r2://uploads/post.png"}, {"cover_path": "https://example.com/ignored.jpg"}],
    }}
    assets.prepare_media_kit_assets(report)
    assert len(requests) == 2
    assert all(body.closed for body in bodies)
    assert client.closed
    assert report["account"]["avatar_bytes"].startswith(b"\xff\xd8")
    assert report["best_posts"]["all_time"][0]["thumbnail_bytes"] == report["best_posts"]["last_30_days"][0]["thumbnail_bytes"]
    assert "thumbnail_bytes" not in report["best_posts"]["last_30_days"][1]


def test_public_showcase_assets_never_fall_back_to_internal_post_images(monkeypatch):
    import boto3
    from types import SimpleNamespace
    requested = []
    monkeypatch.setattr(assets.media_storage, "r2_enabled", lambda: True)
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(assets, "_thumbnail", lambda client, reference: requested.append(reference) or b"public-image")
    report = {"account": {},
              "best_posts": {"all_time": [{"cover_path": "r2://uploads/private.png"}]},
              "public_best_posts": {"all_time": [{"cover_path": "r2://uploads/public.png"}], "last_30_days": []}}
    assets.prepare_media_kit_assets(report)
    assert requested == ["r2://uploads/public.png"]
    assert "thumbnail_bytes" not in report["best_posts"]["all_time"][0]
    requested.clear()
    report["public_best_posts"] = {"all_time": [], "last_30_days": []}
    assets.prepare_media_kit_assets(report)
    assert requested == []
