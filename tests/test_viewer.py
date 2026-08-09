from __future__ import annotations

import http.client
import threading
import unittest

from douyin_comment_crawler.viewer import create_server, load_viewer_html


class ViewerTests(unittest.TestCase):
    def test_page_contains_local_directory_and_image_relationship_logic(self) -> None:
        html = load_viewer_html().decode("utf-8")

        self.assertIn("webkitdirectory", html)
        self.assertIn("comments.json", html)
        self.assertIn("comment.images", html)
        self.assertIn("URL.createObjectURL", html)
        self.assertIn("URL.revokeObjectURL", html)
        self.assertIn("text.textContent", html)

    def test_local_server_serves_viewer_with_restrictive_policy(self) -> None:
        server = create_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        try:
            connection = http.client.HTTPConnection(host, port, timeout=2)
            connection.request("GET", "/")
            response = connection.getresponse()
            body = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(response.status, 200)
        self.assertIn("抖音评论查看器", body)
        policy = response.getheader("Content-Security-Policy")
        self.assertIn("connect-src 'none'", policy or "")
