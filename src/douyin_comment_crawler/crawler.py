from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import asyncio
import hashlib
import random
import re

from playwright.async_api import (
    BrowserContext,
    Page,
    Response,
    async_playwright,
)

try:
    from .comments import (
        CommentStore,
        extract_pagination,
        extract_top_level_comment_array,
        get_aweme_id_from_url,
        is_top_level_comment_response_url,
    )
    from .output import (
        create_run_directory,
        normalize_downloaded_image,
        safe_path_part,
        write_result_json,
    )
except ImportError:  # Support direct execution from the package directory.
    from comments import (
        CommentStore,
        extract_pagination,
        extract_top_level_comment_array,
        get_aweme_id_from_url,
        is_top_level_comment_response_url,
    )
    from output import (
        create_run_directory,
        normalize_downloaded_image,
        safe_path_part,
        write_result_json,
    )

_BLOCKING_TEXT = re.compile(
    r"请完成.{0,8}验证|安全验证|滑块验证|访问过于频繁|操作过于频繁|"
    r"账号存在风险|扫码登录|验证码登录|密码登录|登录后.{0,12}(?:评论|继续)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class CrawlerConfig:
    url: str
    output_root: Path
    profile_dir: Path
    cdp_url: str | None = None
    scroll_direction: str = "down"
    idle_rounds: int = 8
    max_comments: int = 0
    max_minutes: int = 30
    min_delay_ms: int = 1_200
    max_delay_ms: int = 2_200
    scroll_pixels: int = 700
    challenge_wait_minutes: int = 30
    non_interactive: bool = False
    close_page: bool = True
    action_timeout_ms: int = 10_000
    navigation_timeout_ms: int = 45_000
    connection_timeout_ms: int = 60_000
    max_image_bytes: int = 25 * 1024 * 1024


@dataclass(slots=True)
class ResponseState:
    response_count: int = 0
    has_more: bool | None = None
    cursor: str | None = None
    last_response_at: float = 0.0
    last_added: int = 0
    warnings: set[str] = field(default_factory=set)


async def _randomized_pause(config: CrawlerConfig, scale: float = 1.0) -> None:
    minimum = max(100, round(config.min_delay_ms * scale))
    maximum = max(minimum, round(config.max_delay_ms * scale))
    await asyncio.sleep(random.randint(minimum, maximum) / 1_000)


def _initial_result(
    config: CrawlerConfig,
    run_directory: Path,
    aweme_id: str,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    return {
        "schema_version": 1,
        "status": "running",
        "source_url": config.url,
        "aweme_id": aweme_id or None,
        "started_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "finished_at": None,
        "run_directory": str(run_directory),
        "settings": {
            "browser_mode": "cdp" if config.cdp_url else "persistent-profile",
            "scroll_direction": config.scroll_direction,
            "idle_rounds": config.idle_rounds,
            "max_comments": config.max_comments or None,
            "max_minutes": config.max_minutes,
            "delay_ms": [config.min_delay_ms, config.max_delay_ms],
            "challenge_wait_minutes": config.challenge_wait_minutes,
        },
        "summary": {
            "comment_count": 0,
            "image_count": 0,
            "failed_image_count": 0,
            "comment_response_count": 0,
            "stop_reason": None,
        },
        "warnings": [],
        "comments": [],
        "error": None,
    }


def _checkpoint(
    run_directory: Path,
    result: dict[str, Any],
    store: CommentStore,
    response_state: ResponseState,
) -> None:
    result["comments"] = store.values()
    result["summary"]["comment_count"] = len(store)
    result["summary"]["comment_response_count"] = response_state.response_count
    write_result_json(run_directory, result)


class CommentResponseCollector:
    def __init__(
        self,
        page: Page,
        store: CommentStore,
        state: ResponseState,
        aweme_id: str,
    ) -> None:
        self.page = page
        self.store = store
        self.state = state
        self.aweme_id = aweme_id
        self.pending: set[asyncio.Task[None]] = set()
        self._listener = self._schedule

    def attach(self) -> None:
        self.page.on("response", self._listener)

    def detach(self) -> None:
        self.page.remove_listener("response", self._listener)

    def _schedule(self, response: Response) -> None:
        response_url = response.url
        if not is_top_level_comment_response_url(response_url):
            return
        response_aweme_id = get_aweme_id_from_url(response_url)
        if self.aweme_id and response_aweme_id and response_aweme_id != self.aweme_id:
            return

        task = asyncio.create_task(self._handle(response))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def _handle(self, response: Response) -> None:
        try:
            payload = await response.json()
        except Exception:
            self.state.warnings.add(f"无法解析评论响应：{response.url}")
            return

        comments = extract_top_level_comment_array(payload)
        added = sum(self.store.add(comment, self.aweme_id) for comment in comments)
        pagination = extract_pagination(payload)
        self.state.response_count += 1
        self.state.has_more = pagination["has_more"]
        self.state.cursor = pagination["cursor"]
        self.state.last_response_at = asyncio.get_running_loop().time()
        self.state.last_added = added
        print(
            f"[评论响应] 本页 {len(comments)} 条，新增 {added} 条，"
            f"累计 {len(self.store)} 条"
            + (
                ""
                if self.state.has_more is None
                else f"，has_more={self.state.has_more}"
            )
        )

    async def drain(self) -> None:
        while self.pending:
            await asyncio.gather(*tuple(self.pending), return_exceptions=True)


async def _find_blocking_state(page: Page) -> str:
    try:
        body_text = await page.locator("body").inner_text(timeout=5_000)
    except Exception:
        return ""
    match = _BLOCKING_TEXT.search(body_text)
    return match.group(0) if match else ""


async def _wait_for_manual_challenge(
    page: Page,
    config: CrawlerConfig,
    blocking_state: str,
) -> None:
    if not blocking_state:
        return
    if config.non_interactive:
        raise RuntimeError(f"页面出现登录或风控验证：{blocking_state}")

    print(f"\n页面出现“{blocking_state}”。请在 Chrome 中手动完成登录/验证。")
    try:
        await asyncio.wait_for(
            asyncio.to_thread(input, "完成后按回车继续："),
            timeout=config.challenge_wait_minutes * 60,
        )
    except TimeoutError as error:
        raise RuntimeError("等待人工完成登录/验证超时") from error

    await page.wait_for_timeout(1_000)
    if remaining := await _find_blocking_state(page):
        raise RuntimeError(f"页面仍显示登录或风控验证：{remaining}")


async def _try_open_comment_panel(page: Page) -> bool:
    selectors = (
        '[data-e2e="comment-icon"]',
        '[data-e2e="feed-comment-icon"]',
        'button[aria-label*="评论"]',
        '[role="button"][aria-label*="评论"]',
    )
    for selector in selectors:
        target = page.locator(selector).first
        try:
            if await target.is_visible():
                await target.click(timeout=3_000)
                await page.wait_for_timeout(800)
                return True
        except Exception:
            continue
    return False


async def _scroll_comment_panel(page: Page, config: CrawlerConfig) -> bool:
    delta = (
        -abs(config.scroll_pixels)
        if config.scroll_direction == "up"
        else abs(config.scroll_pixels)
    )
    viewport = page.viewport_size
    if not viewport:
        print("[评论滚动] 无法读取浏览器视口大小，本轮跳过")
        return False

    probe_x = max(1, viewport["width"] - 150)
    probe_y = viewport["height"] / 2
    try:
        await page.mouse.move(
            probe_x,
            probe_y,
            steps=random.randint(8, 16),
        )
        await page.wait_for_timeout(random.randint(220, 480))
        scroll_result = await page.evaluate(
            """
            ({x, y, delta}) => {
              let element = document.elementFromPoint(x, y);
              let depth = 0;
              while (
                element &&
                element !== document.body &&
                element !== document.documentElement
              ) {
                const style = window.getComputedStyle(element);
                const overflowY = style.overflowY;
                const scrollable =
                  (overflowY === "auto" ||
                    overflowY === "scroll" ||
                    overflowY === "overlay") &&
                  element.scrollHeight > element.clientHeight;

                if (scrollable) {
                  const before = element.scrollTop;
                  element.scrollTop += delta;
                  element.dispatchEvent(
                    new WheelEvent("wheel", {
                      deltaY: delta,
                      bubbles: true,
                      cancelable: true,
                      clientX: x,
                      clientY: y
                    })
                  );
                  return {
                    found: true,
                    before,
                    after: element.scrollTop,
                    depth,
                    tag: element.tagName.toLowerCase(),
                    className:
                      typeof element.className === "string"
                        ? element.className.slice(0, 120)
                        : "",
                    overflowY
                  };
                }
                element = element.parentElement;
                depth += 1;
              }
              return {found: false};
            }
            """,
            {"x": probe_x, "y": probe_y, "delta": delta},
        )
        if not scroll_result or not scroll_result.get("found"):
            print(
                "[评论滚动] 右侧探测点未找到带滚动条的评论容器，"
                "本轮跳过"
            )
            return False

        print(
            "[评论滚动] 已从右侧探测点滚动容器："
            f"x={probe_x:.0f}, y={probe_y:.0f}, "
            f"scrollTop={scroll_result['before']}"
            f"->{scroll_result['after']}, "
            f"ancestorDepth={scroll_result['depth']}"
        )
        await page.wait_for_timeout(random.randint(120, 260))
        return True
    except Exception as error:
        print(f"[评论滚动] 执行失败：{error}")
        return False


async def _page_explicitly_has_no_comments(page: Page) -> bool:
    try:
        body_text = await page.locator("body").inner_text(timeout=3_000)
    except Exception:
        return False
    return bool(re.search(r"暂无评论|还没有评论|评论区空空如也", body_text))


def determine_stop_reason(
    *,
    response_count: int,
    has_more: bool | None,
    idle_rounds: int,
    idle_limit: int,
) -> str | None:
    if response_count > 0 and has_more is False and idle_rounds >= 2:
        return "评论接口返回 has_more=false"
    if idle_rounds >= idle_limit:
        return "连续多轮滚动无新增评论"
    return None


async def _collect_by_scrolling(
    *,
    page: Page,
    config: CrawlerConfig,
    store: CommentStore,
    response_state: ResponseState,
    collector: CommentResponseCollector,
    run_directory: Path,
    result: dict[str, Any],
) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.max_minutes * 60
    previous_count = len(store)
    idle_rounds = 0
    scroll_target_warnings = 0
    round_number = 0

    while loop.time() < deadline:
        round_number += 1
        if config.max_comments > 0 and len(store) >= config.max_comments:
            return "达到 max-comments 限制"

        if round_number == 1 or round_number % 8 == 0:
            await _wait_for_manual_challenge(
                page,
                config,
                await _find_blocking_state(page),
            )

        found_target = await _scroll_comment_panel(page, config)
        if not found_target and scroll_target_warnings < 3:
            scroll_target_warnings += 1
            response_state.warnings.add(
                "右侧评论区探测点未找到带滚动条的容器；"
                "为避免切换视频，本轮未发送滚动事件"
            )

        await _randomized_pause(config)
        await collector.drain()
        if len(store) > previous_count:
            idle_rounds = 0
            previous_count = len(store)
            _checkpoint(run_directory, result, store, response_state)
        else:
            idle_rounds += 1

        print(
            f"[滚动 {round_number}] 累计 {len(store)} 条，"
            f"连续无新增 {idle_rounds}/{config.idle_rounds}"
        )

        stop_reason = determine_stop_reason(
            response_count=response_state.response_count,
            has_more=response_state.has_more,
            idle_rounds=idle_rounds,
            idle_limit=config.idle_rounds,
        )
        if stop_reason == "连续多轮滚动无新增评论":
            if (
                response_state.response_count == 0
                and await _page_explicitly_has_no_comments(page)
            ):
                return "页面明确显示暂无评论"
        if stop_reason:
            return stop_reason

    return "达到 max-minutes 限制"


def _write_exclusive(path: Path, content: bytes) -> None:
    with path.open("xb") as file:
        file.write(content)


async def _download_comment_images(
    context: BrowserContext,
    comments: list[dict[str, Any]],
    run_directory: Path,
    config: CrawlerConfig,
) -> tuple[int, int]:
    image_count = 0
    failed_image_count = 0

    for comment in comments:
        source_urls = list(dict.fromkeys(comment["source_image_urls"]))
        for index, source_url in enumerate(source_urls, start=1):
            await _randomized_pause(config, 0.45)
            try:
                response = await context.request.get(
                    source_url,
                    fail_on_status_code=False,
                    headers={"Referer": "https://www.douyin.com/"},
                    timeout=30_000,
                )
                if not response.ok:
                    raise RuntimeError(f"HTTP {response.status}")
                body = await response.body()
                if len(body) > config.max_image_bytes:
                    raise RuntimeError(
                        f"图片超过大小限制 {config.max_image_bytes} bytes"
                    )

                source_content_type = response.headers.get("content-type")
                body, extension, content_type, converted_from = (
                    normalize_downloaded_image(
                        body,
                        source_content_type,
                        source_url,
                    )
                )
                if len(body) > config.max_image_bytes:
                    raise RuntimeError(
                        f"转换后的图片超过大小限制 {config.max_image_bytes} bytes"
                    )
                fallback = hashlib.sha256(comment["id"].encode()).hexdigest()[:12]
                id_part = safe_path_part(comment["id"], fallback)
                filename = f"comment-{id_part}-{index:02d}{extension}"
                await asyncio.to_thread(
                    _write_exclusive,
                    run_directory / filename,
                    body,
                )
                comment["images"].append(
                    {
                        "source_url": source_url,
                        "file": filename,
                        "content_type": content_type,
                        "source_content_type": source_content_type,
                        "converted_from": converted_from,
                        "bytes": len(body),
                    }
                )
                image_count += 1
            except Exception as error:
                comment["images"].append(
                    {
                        "source_url": source_url,
                        "file": None,
                        "error": str(error),
                    }
                )
                failed_image_count += 1
    return image_count, failed_image_count


async def crawl_douyin_comments(
    config: CrawlerConfig,
) -> tuple[Path, dict[str, Any]]:
    from datetime import datetime, timezone

    aweme_id = get_aweme_id_from_url(config.url)
    run_directory = create_run_directory(config.output_root, aweme_id)
    result = _initial_result(config, run_directory, aweme_id)
    store = CommentStore()
    response_state = ResponseState()
    write_result_json(run_directory, result)

    page: Page | None = None
    collector: CommentResponseCollector | None = None
    playwright_instance = None
    owned_context = False
    context: BrowserContext | None = None
    try:
        playwright_instance = await async_playwright().start()
        if config.cdp_url:
            print(f"连接现有 Chrome：{config.cdp_url}")
            browser = await playwright_instance.chromium.connect_over_cdp(
                config.cdp_url,
                timeout=config.connection_timeout_ms,
            )
            if not browser.contexts:
                raise RuntimeError("Chrome 中没有可用的浏览器上下文")
            context = browser.contexts[0]
            page = await context.new_page()
        else:
            profile_is_new = not config.profile_dir.exists() or not any(
                config.profile_dir.iterdir()
            )
            config.profile_dir.mkdir(parents=True, exist_ok=True)
            print(f"启动专用 Chrome 配置：{config.profile_dir}")
            context = await playwright_instance.chromium.launch_persistent_context(
                user_data_dir=str(config.profile_dir),
                channel="chrome",
                headless=False,
            )
            owned_context = True
            if context.pages and context.pages[0].url == "about:blank":
                page = context.pages[0]
            else:
                page = await context.new_page()

        page.set_default_timeout(config.action_timeout_ms)
        collector = CommentResponseCollector(
            page,
            store,
            response_state,
            aweme_id,
        )
        collector.attach()

        print(f"打开页面：{config.url}")
        await page.goto(
            config.url,
            wait_until="domcontentloaded",
            timeout=config.navigation_timeout_ms,
        )
        await page.bring_to_front()
        await _randomized_pause(config, 1.2)
        initial_state = await _find_blocking_state(page)
        if not config.cdp_url and profile_is_new and not initial_state:
            initial_state = "首次使用专用浏览器配置，请先登录抖音"
        await _wait_for_manual_challenge(
            page,
            config,
            initial_state,
        )
        await collector.drain()

        if response_state.response_count == 0:
            await _try_open_comment_panel(page)
            await _randomized_pause(config)
            await collector.drain()
        _checkpoint(run_directory, result, store, response_state)

        result["summary"]["stop_reason"] = await _collect_by_scrolling(
            page=page,
            config=config,
            store=store,
            response_state=response_state,
            collector=collector,
            run_directory=run_directory,
            result=result,
        )
        await collector.drain()
        _checkpoint(run_directory, result, store, response_state)

        if (
            response_state.response_count == 0
            and result["summary"]["stop_reason"] != "页面明确显示暂无评论"
        ):
            raise RuntimeError(
                "未捕获到一级评论列表响应；请确认评论区已打开、页面可正常访问，"
                "或尝试切换 --scroll-direction"
            )

        image_total = sum(
            len(comment["source_image_urls"]) for comment in store.values()
        )
        print(f"开始顺序下载 {image_total} 张评论图片")
        image_count, failed_image_count = await _download_comment_images(
            context,
            store.values(),
            run_directory,
            config,
        )

        result["status"] = "complete"
        result["finished_at"] = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        result["comments"] = store.values()
        result["summary"]["comment_count"] = len(store)
        result["summary"]["image_count"] = image_count
        result["summary"]["failed_image_count"] = failed_image_count
        result["summary"]["comment_response_count"] = response_state.response_count
        result["warnings"] = sorted(response_state.warnings)
        write_result_json(run_directory, result)
        return run_directory, result
    except Exception as error:
        result["status"] = "failed"
        result["finished_at"] = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        result["error"] = {
            "name": type(error).__name__,
            "message": str(error),
        }
        result["warnings"] = sorted(response_state.warnings)
        _checkpoint(run_directory, result, store, response_state)
        setattr(error, "run_directory", run_directory)
        raise
    finally:
        if collector is not None:
            collector.detach()
        if owned_context and context is not None:
            try:
                await context.close()
            except Exception:
                pass
        elif page is not None and config.close_page:
            try:
                await page.close()
            except Exception:
                pass
        if playwright_instance is not None:
            try:
                await playwright_instance.stop()
            except Exception:
                pass
