import unittest

from app.instagram_import import _extract_embedded_image, _extract_json_ld, _extract_meta


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


if __name__ == "__main__":
    unittest.main()
