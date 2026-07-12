import unittest
import sys
import types
from unittest.mock import patch

from app.instagram_import import (
    _extract_embedded_image,
    _extract_json_ld,
    _extract_meta,
    _extract_profile_post_urls,
    _canonical_instagram_profile_url,
    _instagram_embed_url,
    _fetch_profile_post_urls_from_timeline,
    discover_instagram_profile_post_urls,
    fetch_instagram_post,
)


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        payload: dict | None = None,
        content: bytes = b"",
        content_type: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.content = content
        self.headers = {"content-type": content_type} if content_type else {}

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("No JSON payload")
        return self._payload


class FakeClient:
    responses: dict[str, FakeResponse] = {}
    requested_urls: list[str] = []

    def __init__(self, **_: object) -> None:
        pass

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get(self, url: str, **_: object) -> FakeResponse:
        self.requested_urls.append(url)
        return self.responses.get(url, FakeResponse(status_code=404))


class InstagramImportParserTests(unittest.TestCase):
    def test_extract_meta_accepts_content_before_property(self) -> None:
        values = _extract_meta(
            '<meta content="https://cdn.example.com/cover.jpg?x=1&amp;y=2" property="og:image">'
        )

        self.assertEqual(values["og:image"], "https://cdn.example.com/cover.jpg?x=1&y=2")

    def test_extract_json_ld_reads_instagram_image(self) -> None:
        caption, image_url = _extract_json_ld(
            """
            <script type="application/ld+json">
              {"caption": "A caption", "image": {"url": "https://cdn.example.com/cover.jpg"}}
            </script>
            """
        )

        self.assertEqual(caption, "A caption")
        self.assertEqual(image_url, "https://cdn.example.com/cover.jpg")

    def test_extract_embedded_image_decodes_json_escaped_url(self) -> None:
        image_url = _extract_embedded_image(
            '<script>{"display_url":"https:\\/\\/cdn.example.com\\/cover.jpg?x=1\\u0026y=2"}</script>'
        )

        self.assertEqual(image_url, "https://cdn.example.com/cover.jpg?x=1&y=2")

    def test_extract_profile_post_urls_dedupes_and_preserves_order(self) -> None:
        urls = _extract_profile_post_urls(
            """
            <a href="/p/ABC123/">first</a>
            <a href="/p/ABC123/?img_index=1">duplicate</a>
            <a href="/reel/XYZ789/">second</a>
            <a href="/tv/TV456/">third</a>
            """
        )

        self.assertEqual(
            urls,
            [
                "https://www.instagram.com/p/ABC123/",
                "https://www.instagram.com/reel/XYZ789/",
                "https://www.instagram.com/tv/TV456/",
            ],
        )

    def test_canonical_instagram_profile_url_accepts_username_or_url(self) -> None:
        self.assertEqual(
            _canonical_instagram_profile_url("@some.profile_1"),
            "https://www.instagram.com/some.profile_1/",
        )
        self.assertEqual(
            _canonical_instagram_profile_url("https://www.instagram.com/some.profile_1/?hl=en"),
            "https://www.instagram.com/some.profile_1/",
        )


class InstagramImportFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.responses = {}
        FakeClient.requested_urls = []
        self._httpx_module = types.ModuleType("httpx")
        self._httpx_module.Client = FakeClient
        self._httpx_patch = patch.dict(sys.modules, {"httpx": self._httpx_module})
        self._httpx_patch.start()

    def tearDown(self) -> None:
        self._httpx_patch.stop()

    def test_embed_url_preserves_post_kind_and_shortcode(self) -> None:
        self.assertEqual(
            _instagram_embed_url("https://www.instagram.com/reel/ABC_123-/"),
            "https://www.instagram.com/reel/ABC_123-/embed/captioned/",
        )

    def test_fetch_uses_api_oembed_thumbnail(self) -> None:
        post_url = "https://www.instagram.com/p/ABC123/"
        image_url = "https://cdn.example.com/oembed-cover.jpg"
        FakeClient.responses = {
            f"https://www.instagram.com/api/v1/oembed/?url=https%3A%2F%2Fwww.instagram.com%2Fp%2FABC123%2F": FakeResponse(
                payload={"title": "Caption", "thumbnail_url": image_url}
            ),
            image_url: FakeResponse(content=b"oembed", content_type="image/jpeg"),
        }

        imported = fetch_instagram_post(post_url)

        self.assertEqual(imported.image_url, image_url)
        self.assertEqual(imported.image_bytes, b"oembed")
        self.assertNotIn(post_url, FakeClient.requested_urls)

    @patch("app.instagram_import._fetch_from_instagram_api", return_value=(None, None))
    def test_fetch_uses_public_embed_when_post_page_has_no_image(self, _: object) -> None:
        post_url = "https://www.instagram.com/p/ABC123/"
        embed_url = f"{post_url}embed/captioned/"
        image_url = "https://cdn.example.com/embed-cover.jpg"
        FakeClient.responses = {
            f"https://www.instagram.com/api/v1/oembed/?url=https%3A%2F%2Fwww.instagram.com%2Fp%2FABC123%2F": FakeResponse(
                status_code=404
            ),
            post_url: FakeResponse(text="<html>Login to Instagram</html>"),
            embed_url: FakeResponse(
                text=f'<meta property="og:image" content="{image_url}">'
            ),
            image_url: FakeResponse(content=b"image", content_type="image/jpeg"),
        }

        imported = fetch_instagram_post(post_url)

        self.assertEqual(imported.image_url, image_url)
        self.assertEqual(imported.image_bytes, b"image")
        self.assertIn(embed_url, FakeClient.requested_urls)

    def test_manual_cover_url_survives_empty_oembed_thumbnail(self) -> None:
        post_url = "https://www.instagram.com/p/ABC123/"
        image_url = "https://cdn.example.com/manual-cover.jpg"
        FakeClient.responses = {
            f"https://www.instagram.com/api/v1/oembed/?url=https%3A%2F%2Fwww.instagram.com%2Fp%2FABC123%2F": FakeResponse(
                payload={"title": "Caption", "thumbnail_url": None}
            ),
            image_url: FakeResponse(content=b"manual", content_type="image/jpeg"),
        }

        imported = fetch_instagram_post(post_url, cover_image_url=image_url)

        self.assertEqual(imported.image_url, image_url)
        self.assertEqual(imported.caption, "Caption")
        self.assertNotIn(post_url, FakeClient.requested_urls)

    def test_discover_profile_posts_from_html(self) -> None:
        profile_url = "https://www.instagram.com/some.profile_1/"
        FakeClient.responses = {
            profile_url: FakeResponse(
                text="""
                    <html>
                      <a href="/p/ABC123/">First</a>
                      <a href="/p/ABC123/?img_index=1">Repeat</a>
                      <a href="/reel/XYZ789/">Second</a>
                    </html>
                """
            )
        }

        urls = discover_instagram_profile_post_urls(profile_url)

        self.assertEqual(
            urls,
            [
                "https://www.instagram.com/p/ABC123/",
                "https://www.instagram.com/reel/XYZ789/",
            ],
        )

    def test_fetch_profile_posts_from_timeline(self) -> None:
        profile_url = "https://www.instagram.com/some.profile_1/"
        timeline_url = "https://www.instagram.com/api/v1/feed/user/some.profile_1/username/?count=50"
        next_timeline_url = (
            "https://www.instagram.com/api/v1/feed/user/some.profile_1/username/?count=50&max_id=CURSOR2"
        )
        FakeClient.responses = {
            timeline_url: FakeResponse(
                payload={
                    "items": [
                        {"code": "ABC123", "product_type": "carousel_container"},
                        {"code": "XYZ789", "product_type": "clips"},
                        {"code": "ABC123", "product_type": "carousel_container"},
                    ],
                    "more_available": True,
                    "next_max_id": "CURSOR2",
                }
            ),
            next_timeline_url: FakeResponse(
                payload={
                    "items": [
                        {"code": "LMN456", "product_type": "carousel_container"},
                        {"code": "XYZ789", "product_type": "clips"},
                    ],
                    "more_available": False,
                }
            )
        }

        urls = _fetch_profile_post_urls_from_timeline(FakeClient(), profile_url, limit=12)

        self.assertEqual(
            urls,
            [
                "https://www.instagram.com/p/ABC123/",
                "https://www.instagram.com/reel/XYZ789/",
                "https://www.instagram.com/p/LMN456/",
            ],
        )


if __name__ == "__main__":
    unittest.main()
