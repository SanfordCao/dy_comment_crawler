from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
import argparse
import asyncio
import sys

try:
    from .crawler import CrawlerConfig, crawl_douyin_comments
except ImportError:  # Support direct execution from the package directory.
    from crawler import CrawlerConfig, crawl_douyin_comments


def _integer_at_least(minimum: int):
    def parse(value: str) -> int:
        number = int(value)
        if number < minimum:
            raise argparse.ArgumentTypeError(f"必须不小于 {minimum}")
        return number

    return parse


def _douyin_url(value: str) -> str:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise argparse.ArgumentTypeError("必须是 http/https 地址")
    if hostname != "douyin.com" and not hostname.endswith(".douyin.com"):
        raise argparse.ArgumentTypeError("必须是 douyin.com 地址")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="douyin-comments",
        description="通过 Playwright 抓取抖音一级评论文字及评论图片",
    )
    parser.add_argument("--url", required=True, type=_douyin_url, help="抖音视频地址")
    parser.add_argument(
        "--cdp",
        default=None,
        help="可选：连接现有 Chrome 的 CDP WebSocket/HTTP 地址",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path(".douyin-browser-profile"),
        help="默认浏览器登录状态目录",
    )
    parser.add_argument("--output", type=Path, default=Path("output"), help="输出根目录")
    parser.add_argument(
        "--scroll-direction",
        choices=("up", "down"),
        default="down",
        help="评论懒加载滚动方向，默认 down",
    )
    parser.add_argument(
        "--idle-rounds",
        type=_integer_at_least(2),
        default=8,
        help="连续无新增后停止的轮数",
    )
    parser.add_argument(
        "--max-comments",
        type=_integer_at_least(0),
        default=0,
        help="评论上限，0 表示不限制",
    )
    parser.add_argument(
        "--max-minutes",
        type=_integer_at_least(1),
        default=30,
        help="最长运行分钟数",
    )
    parser.add_argument(
        "--min-delay-ms",
        type=_integer_at_least(500),
        default=1_200,
        help="每次滚动最短等待",
    )
    parser.add_argument(
        "--max-delay-ms",
        type=_integer_at_least(500),
        default=2_200,
        help="每次滚动最长等待",
    )
    parser.add_argument(
        "--scroll-pixels",
        type=_integer_at_least(100),
        default=700,
        help="每次滚动像素",
    )
    parser.add_argument(
        "--challenge-wait-minutes",
        type=_integer_at_least(1),
        default=30,
        help="等待人工登录/验证的分钟数，默认 30",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="遇到登录或验证码直接失败",
    )
    parser.add_argument(
        "--keep-page",
        action="store_true",
        help="执行完成后保留程序创建的标签页",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> CrawlerConfig:
    if args.max_delay_ms < args.min_delay_ms:
        raise ValueError("--max-delay-ms 不能小于 --min-delay-ms")
    return CrawlerConfig(
        url=args.url,
        cdp_url=args.cdp,
        output_root=args.output.resolve(),
        profile_dir=args.profile_dir.resolve(),
        scroll_direction=args.scroll_direction,
        idle_rounds=args.idle_rounds,
        max_comments=args.max_comments,
        max_minutes=args.max_minutes,
        min_delay_ms=args.min_delay_ms,
        max_delay_ms=args.max_delay_ms,
        scroll_pixels=args.scroll_pixels,
        challenge_wait_minutes=args.challenge_wait_minutes,
        non_interactive=args.non_interactive,
        close_page=not args.keep_page,
    )


def arguments_for_pycharm(
    arguments: list[str],
    input_function=input,
) -> list[str]:
    """Prompt for the URL when PyCharm runs __main__.py without parameters."""
    if arguments:
        return arguments
    url = input_function("请输入抖音视频 URL，然后按回车：").strip()
    return ["--url", url] if url else []


def main() -> None:
    parser = build_parser()
    args = parser.parse_args(arguments_for_pycharm(sys.argv[1:]))
    try:
        config = config_from_args(args)
    except ValueError as error:
        parser.error(str(error))

    print(
        "提示：请只抓取你有权访问的公开内容，并遵守平台规则。"
        "程序不会绕过验证码；出现验证时请手动处理。"
    )
    try:
        run_directory, result = asyncio.run(crawl_douyin_comments(config))
    except Exception as error:
        print(f"\n抓取失败：{error}")
        if run_directory := getattr(error, "run_directory", None):
            print(f"失败信息和已抓取数据已保存到：{run_directory}")
        raise SystemExit(1) from error

    print(
        f"\n完成：{result['summary']['comment_count']} 条一级评论，"
        f"{result['summary']['image_count']} 张图片\n"
        f"输出目录：{run_directory}"
    )
