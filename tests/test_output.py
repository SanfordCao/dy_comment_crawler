from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from io import BytesIO
import json
import unittest

from PIL import Image

from douyin_comment_crawler.output import (
    create_run_directory,
    extension_from_content_type,
    normalize_downloaded_image,
    write_result_json,
)


class OutputTests(unittest.TestCase):
    def test_creates_fresh_directory_per_run(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = datetime(2026, 7, 31, 12, 34, 56)
            first = create_run_directory(root, "123", now)
            second = create_run_directory(root, "123", now)
            self.assertNotEqual(first, second)
            self.assertRegex(first.name, r"-123$")
            self.assertRegex(second.name, r"-123-1$")

    def test_writes_readable_json_with_image_relationship(self) -> None:
        with TemporaryDirectory() as temporary:
            run_directory = create_run_directory(Path(temporary), "321")
            json_path = write_result_json(
                run_directory,
                {
                    "comments": [
                        {
                            "id": "1",
                            "text": "hello",
                            "images": [{"file": "comment-1-01.jpg"}],
                        }
                    ]
                },
            )
            parsed = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(
                parsed["comments"][0]["images"][0]["file"],
                "comment-1-01.jpg",
            )

    def test_extension_prefers_content_type(self) -> None:
        self.assertEqual(
            extension_from_content_type("image/webp", "https://x/a.jpg"),
            ".webp",
        )
        self.assertEqual(
            extension_from_content_type("", "https://x/a.jpeg?x=1"),
            ".jpg",
        )
        self.assertEqual(
            extension_from_content_type("", "https://x/no-extension"),
            ".jpg",
        )

    def test_converts_webp_to_viewable_jpeg(self) -> None:
        source = BytesIO()
        Image.new("RGBA", (4, 4), (255, 0, 0, 128)).save(source, format="WEBP")
        content, extension, content_type, converted_from = normalize_downloaded_image(
            source.getvalue(),
            "image/webp",
            "https://x/comment.webp",
        )
        self.assertEqual(extension, ".jpg")
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(converted_from, "image/webp")
        self.assertTrue(content.startswith(b"\xff\xd8"))

    def test_leaves_jpeg_unchanged(self) -> None:
        content = b"\xff\xd8fake-jpeg"
        normalized = normalize_downloaded_image(
            content,
            "image/jpeg",
            "https://x/comment.jpg",
        )
        self.assertEqual(normalized, (content, ".jpg", "image/jpeg", None))


if __name__ == "__main__":
    unittest.main()
