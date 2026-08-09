from __future__ import annotations

import unittest

from douyin_comment_crawler.cli import arguments_for_pycharm


class CliTests(unittest.TestCase):
    def test_prompts_for_url_when_pycharm_has_no_parameters(self) -> None:
        arguments = arguments_for_pycharm(
            [],
            input_function=lambda _: " https://www.douyin.com/video/123 ",
        )
        self.assertEqual(
            arguments,
            ["--url", "https://www.douyin.com/video/123"],
        )

    def test_preserves_pycharm_script_parameters(self) -> None:
        arguments = ["--url", "https://www.douyin.com/video/456"]
        self.assertIs(arguments_for_pycharm(arguments), arguments)


if __name__ == "__main__":
    unittest.main()
