from __future__ import annotations

import unittest
from pathlib import Path
from douyin_comment_crawler.crawler import (
    CrawlerConfig,
    _scroll_comment_panel,
    determine_stop_reason,
)


class TerminationTests(unittest.TestCase):
    def test_defaults_to_dedicated_persistent_profile(self) -> None:
        config = CrawlerConfig(
            url="https://www.douyin.com/video/1",
            output_root=Path("output"),
            profile_dir=Path(".douyin-browser-profile"),
        )
        self.assertIsNone(config.cdp_url)
        self.assertEqual(config.connection_timeout_ms, 60_000)
        self.assertEqual(config.challenge_wait_minutes, 30)
        self.assertEqual(config.scroll_direction, "down")

    def test_stops_after_api_reports_end_and_two_idle_rounds(self) -> None:
        self.assertIsNone(
            determine_stop_reason(
                response_count=3,
                has_more=False,
                idle_rounds=1,
                idle_limit=8,
            )
        )
        self.assertEqual(
            determine_stop_reason(
                response_count=3,
                has_more=False,
                idle_rounds=2,
                idle_limit=8,
            ),
            "评论接口返回 has_more=false",
        )

    def test_falls_back_to_configured_idle_limit(self) -> None:
        self.assertEqual(
            determine_stop_reason(
                response_count=0,
                has_more=None,
                idle_rounds=8,
                idle_limit=8,
            ),
            "连续多轮滚动无新增评论",
        )

    def test_keeps_scrolling_while_more_data_is_expected(self) -> None:
        self.assertIsNone(
            determine_stop_reason(
                response_count=2,
                has_more=True,
                idle_rounds=3,
                idle_limit=8,
            )
        )


class _FakeMouse:
    def __init__(self, events: list[tuple]) -> None:
        self.events = events

    async def move(self, x: float, y: float, **kwargs) -> None:
        self.events.append(("move", x, y, kwargs.get("steps")))

    async def wheel(self, x: float, y: float) -> None:
        self.events.append(("wheel", x, y))


class _FakePage:
    def __init__(
        self,
        events: list[tuple],
        scroll_result: dict | None = None,
        viewport_size: dict | None = None,
    ) -> None:
        self.mouse = _FakeMouse(events)
        self.viewport_size = (
            {"width": 1_000, "height": 800}
            if viewport_size is None
            else viewport_size
        )
        self.events = events
        self.scroll_result = scroll_result or {
            "found": True,
            "before": 1_000,
            "after": 1_700,
            "depth": 2,
        }

    async def wait_for_timeout(self, milliseconds: int) -> None:
        self.events.append(("wait", milliseconds))

    async def evaluate(self, expression: str, options: dict) -> dict:
        self.events.append(("evaluate_scroll", options, expression))
        return self.scroll_result


class MousePlacementTests(unittest.IsolatedAsyncioTestCase):
    async def test_moves_to_right_probe_before_automatic_scroll(self) -> None:
        events: list[tuple] = []
        page = _FakePage(events)
        config = CrawlerConfig(
            url="https://www.douyin.com/video/1",
            output_root=Path("output"),
            profile_dir=Path(".douyin-browser-profile"),
        )

        found = await _scroll_comment_panel(page, config)  # type: ignore[arg-type]

        self.assertTrue(found)
        self.assertEqual(
            [event[0] for event in events],
            ["move", "wait", "evaluate_scroll", "wait"],
        )
        self.assertEqual(events[0][1], 850)
        self.assertEqual(events[0][2], 400)
        self.assertEqual(events[2][1]["x"], 850)
        self.assertEqual(events[2][1]["y"], 400)
        self.assertGreater(events[2][1]["delta"], 0)
        self.assertIn("document.elementFromPoint", events[2][2])
        self.assertIn("element.scrollTop += delta", events[2][2])
        self.assertIn('new WheelEvent("wheel"', events[2][2])

    async def test_missing_scroll_container_returns_false(self) -> None:
        events: list[tuple] = []
        page = _FakePage(events, {"found": False})
        config = CrawlerConfig(
            url="https://www.douyin.com/video/1",
            output_root=Path("output"),
            profile_dir=Path(".douyin-browser-profile"),
        )

        found = await _scroll_comment_panel(page, config)  # type: ignore[arg-type]

        self.assertFalse(found)
        self.assertEqual(
            [event[0] for event in events],
            ["move", "wait", "evaluate_scroll"],
        )

    async def test_missing_viewport_skips_without_mouse_action(self) -> None:
        events: list[tuple] = []
        page = _FakePage(events, viewport_size={})
        config = CrawlerConfig(
            url="https://www.douyin.com/video/1",
            output_root=Path("output"),
            profile_dir=Path(".douyin-browser-profile"),
        )

        found = await _scroll_comment_panel(page, config)  # type: ignore[arg-type]

        self.assertFalse(found)
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
