from __future__ import annotations

import unittest

from douyin_comment_crawler.comments import (
    CommentStore,
    extract_pagination,
    extract_top_level_comment_array,
    get_aweme_id_from_url,
    is_top_level_comment_response_url,
    parse_top_level_comment,
)


class CommentTests(unittest.TestCase):
    def test_accepts_top_level_endpoint_and_rejects_replies(self) -> None:
        self.assertTrue(
            is_top_level_comment_response_url(
                "https://www.douyin.com/aweme/v1/web/comment/list/"
                "?aweme_id=123&cursor=0"
            )
        )
        self.assertFalse(
            is_top_level_comment_response_url(
                "https://www.douyin.com/aweme/v1/web/comment/list/reply/"
                "?comment_id=456"
            )
        )
        self.assertFalse(
            is_top_level_comment_response_url("https://example.com/comment/list/")
        )

    def test_extracts_video_id_from_supported_urls(self) -> None:
        video_id = "7666731321595551205"
        self.assertEqual(
            get_aweme_id_from_url(
                f"https://www.douyin.com/user/self?modal_id={video_id}"
            ),
            video_id,
        )
        self.assertEqual(
            get_aweme_id_from_url(f"https://www.douyin.com/video/{video_id}"),
            video_id,
        )
        self.assertEqual(
            get_aweme_id_from_url(
                "https://www.douyin.com/aweme/v1/web/comment/list/"
                f"?aweme_id={video_id}"
            ),
            video_id,
        )

    def test_parses_text_author_counts_and_one_best_url_per_image(self) -> None:
        comment = parse_top_level_comment(
            {
                "cid": "c-1",
                "aweme_id": "a-1",
                "text": "文字评论",
                "create_time": 1_700_000_000,
                "digg_count": 9,
                "reply_comment_total": 3,
                "ip_label": "天津",
                "user": {
                    "uid": "u-1",
                    "sec_uid": "sec-1",
                    "nickname": "测试用户",
                    "unique_id": "tester",
                },
                "image_list": [
                    {
                        "origin_url": {
                            "url_list": ["http://p3.douyinpic.com/a.jpeg"]
                        },
                        "download_url": {
                            "url_list": ["https://p3.douyinpic.com/a.webp"]
                        },
                    }
                ],
                "reply_comment": [{"cid": "nested-reply-must-not-be-read"}],
            },
            "fallback",
        )
        assert comment is not None
        self.assertEqual(comment["id"], "c-1")
        self.assertEqual(comment["text"], "文字评论")
        self.assertEqual(comment["author"]["nickname"], "测试用户")
        self.assertEqual(comment["reply_count"], 3)
        self.assertEqual(
            comment["source_image_urls"],
            ["https://p3.douyinpic.com/a.jpeg"],
        )
        self.assertNotIn("reply_comment", comment)

    def test_deduplicates_and_merges_late_images(self) -> None:
        store = CommentStore()
        self.assertTrue(store.add({"cid": "1", "text": "hello"}, "99"))
        self.assertFalse(
            store.add(
                {
                    "cid": "1",
                    "text": "hello",
                    "image_list": [
                        {"url_list": ["https://example.com/image.png"]}
                    ],
                },
                "99",
            )
        )
        self.assertEqual(len(store), 1)
        self.assertEqual(
            store.values()[0]["source_image_urls"],
            ["https://example.com/image.png"],
        )

    def test_reads_only_top_level_payload_and_pagination(self) -> None:
        payload = {
            "comments": [{"cid": "top"}],
            "has_more": 0,
            "cursor": "20",
            "reply_comment": [{"cid": "reply"}],
        }
        self.assertEqual(extract_top_level_comment_array(payload), [{"cid": "top"}])
        self.assertEqual(
            extract_pagination(payload),
            {"has_more": False, "cursor": "20"},
        )


if __name__ == "__main__":
    unittest.main()

