from __future__ import annotations
from contextvars import copy_context

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlsplit, urlunsplit
import asyncio
import hashlib
import random
import re

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Response,
    async_playwright,
)

try:
    from .video_metadata import VideoMetadataCollector, METADATA_DEFAULTS
    from .comments import (
        CommentStore,
        extract_pagination,
        extract_top_level_comment_array,
        get_aweme_id_from_url,
        is_reply_comment_response_url,
        is_top_level_comment_response_url,
    )
    from .output import (
        create_run_directory,
        normalize_downloaded_image,
        safe_path_part,
        write_result_json,
    )
except ImportError:  # Support direct execution from the package directory.
    from video_metadata import VideoMetadataCollector, METADATA_DEFAULTS
    from comments import (
        CommentStore,
        extract_pagination,
        extract_top_level_comment_array,
        get_aweme_id_from_url,
        is_reply_comment_response_url,
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
    browser_context: BrowserContext | None = None
    require_initial_login: bool = False
    scroll_direction: str = "down"
    comment_scope: str = "top_level"
    fetch_mode: str = "network_first"
    fetch_douyin_id: bool = False
    retry_limit: int = 3
    idle_rounds: int = 8
    max_comments: int = 0
    max_minutes: int = 30
    min_delay_ms: int = 800
    max_delay_ms: int = 1_400
    scroll_pixels: int = 700
    challenge_wait_minutes: int = 30
    pause_xpath: str = ""
    author_api_path: str = METADATA_DEFAULTS["author_api_path"]
    author_json_path: str = METADATA_DEFAULTS["author_json_path"]
    title_api_path: str = METADATA_DEFAULTS["title_api_path"]
    title_json_path: str = METADATA_DEFAULTS["title_json_path"]
    non_interactive: bool = False
    close_page: bool = True
    action_timeout_ms: int = 10_000
    navigation_timeout_ms: int = 45_000
    connection_timeout_ms: int = 60_000
    max_image_bytes: int = 25 * 1024 * 1024
    manual_continue: Callable[[str, int], Awaitable[None]] | None = None
    stop_requested: Callable[[], bool] | None = None
    run_directory_ready: Callable[[Path], None] | None = None

    def __post_init__(self) -> None:
        """Reject invalid retry budgets even when constructed outside the UI."""
        if type(self.retry_limit) is not int or not 0 <= self.retry_limit <= 10:
            raise ValueError("失败重试次数必须是 0～10 的整数")


@dataclass(slots=True)
class ResponseState:
    response_count: int = 0
    top_level_response_count: int = 0
    reply_response_count: int = 0
    top_level_request_url: str | None = None
    has_more: bool | None = None
    top_level_comments_null: bool = False
    cursor: str | None = None
    last_response_at: float = 0.0
    last_added: int = 0
    warnings: set[str] = field(default_factory=set)
    load_failures: dict[str, int] = field(default_factory=dict)
    reply_received: dict[str, int] = field(default_factory=dict)
    reply_progress: dict[str, tuple[str | None, int, bool]] = field(default_factory=dict)
    skipped_reply_ids: set[str] = field(default_factory=set)


def _record_load_failure(state: ResponseState, config: CrawlerConfig, key: str, reason: str) -> bool:
    """Consume one failed attempt; preserve budgets across API/DOM fallback."""
    count = state.load_failures.get(key, 0) + 1
    state.load_failures[key] = count
    exhausted = count > config.retry_limit
    action = "已达上限，跳过失败项／停止当前视频，结果可能不完整" if exhausted else f"准备重试 {count}/{config.retry_limit}"
    message = f"[失败重试][{key}] {reason}；{action}"
    print(message)
    if exhausted:
        state.warnings.add(message)
    return exhausted


async def _randomized_pause(config: CrawlerConfig, scale: float = 1.0) -> None:
    minimum = max(100, round(config.min_delay_ms * scale))
    maximum = max(minimum, round(config.max_delay_ms * scale))
    await asyncio.sleep(random.randint(minimum, maximum) / 1_000)


# Use the same visibility rule for preparation, scanning and scrolling.
_VISIBLE_COMMENT_LIST = """[...document.querySelectorAll('[data-e2e="comment-list"]')].find(e => {
  const r = e.getBoundingClientRect(), s = getComputedStyle(e);
  return r.width > 0 && r.height > 0 && s.display !== 'none' &&
    s.visibility !== 'hidden' && s.visibility !== 'collapse' &&
    e.checkVisibility?.({checkOpacity:true, checkVisibilityCSS:true}) !== false;
})"""
_NOTE_DETAIL = r"""(/^\/note\/\d+/.test(location.pathname) ||
  !!document.querySelector('[data-e2e="note-detail"], [data-e2e="note-detail-container"], #note-detail'))"""


async def _verify_work_page(page: Page, aweme_id: str) -> None:
    parsed = urlparse(page.url)
    host = (parsed.hostname or "").lower()
    official = host == "douyin.com" or host.endswith(".douyin.com")
    if not official or not aweme_id or get_aweme_id_from_url(page.url) != aweme_id:
        raise RuntimeError("页面作品 ID 与目标不一致，已停止操作，防止操作其他作品")


async def _prepare_comment_panel(page: Page, aweme_id: str, retry_limit: int,
                                 stop_requested: Callable[[], bool] | None,
                                 state: ResponseState,
                                 wait_access: Callable[[], Awaitable[Any]] | None = None) -> None:
    """Open a note's comments once per document; retries only wait for visibility."""
    for attempt in range(retry_limit + 1):
        for tick in range(30):
            if state.top_level_comments_null:
                return
            if stop_requested and stop_requested():
                raise asyncio.CancelledError
            if wait_access is not None:
                access_task = asyncio.create_task(wait_access())
                try:
                    while not access_task.done():
                        if state.top_level_comments_null:
                            return
                        if stop_requested and stop_requested():
                            raise asyncio.CancelledError
                        await asyncio.wait({access_task}, timeout=0.1)
                    await access_task
                finally:
                    if not access_task.done():
                        access_task.cancel()
                    await asyncio.gather(access_task, return_exceptions=True)
            if state.top_level_comments_null:
                return
            await _verify_work_page(page, aweme_id)
            if await page.evaluate("() => !!(" + _VISIBLE_COMMENT_LIST + ")"):
                return
            if attempt == 0 and tick < 5:
                await page.wait_for_timeout(100)
                continue
            toggle = await page.evaluate("""() => {
              if (document.readyState === "loading" || !(NOTE_DETAIL) || window.__douyinCommentsOpened) return false;
              if (VISIBLE_LIST) return false;
              window.__douyinCommentsOpened = true;
              const active = document.activeElement;
              if (active && active !== document.body) active.blur();
              return true;
            }""".replace("NOTE_DETAIL", _NOTE_DETAIL).replace("VISIBLE_LIST", _VISIBLE_COMMENT_LIST))
            if toggle:
                if state.top_level_comments_null:
                    return
                await _verify_work_page(page, aweme_id)
                await page.keyboard.press("x")
            await page.wait_for_timeout(100)
    if not state.top_level_comments_null:
        raise RuntimeError(f"未找到可见评论列表，已用尽 {retry_limit} 次重试")


async def _goto_with_retries(page: Page, config: CrawlerConfig, state: ResponseState) -> None:
    """Bound initial video navigation failures using the page's retry budget."""
    for attempt in range(config.retry_limit + 1):
        if state.top_level_comments_null:
            return
        if config.stop_requested is not None and config.stop_requested():
            raise asyncio.CancelledError
        try:
            await page.goto(
                config.url,
                wait_until="domcontentloaded",
                timeout=config.navigation_timeout_ms,
            )
            state.load_failures.pop("navigation", None)
            return
        except Exception as error:
            if state.top_level_comments_null:
                return
            if config.stop_requested is not None and config.stop_requested():
                raise asyncio.CancelledError from error
            exhausted = _record_load_failure(state, config, "navigation", str(error))
            if exhausted:
                raise RuntimeError(
                    f"视频页面加载失败，已用尽 {config.retry_limit} 次重试：{error}"
                ) from error
            await _randomized_pause(config)


def _initial_result(
    config: CrawlerConfig,
    run_directory: Path,
    aweme_id: str,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    return {
        "schema_version": 2,
        "status": "running",
        "source_url": config.url,
        "aweme_id": aweme_id or None,
        "video_publisher_nickname": "",
        "video_title": "",
        "started_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "finished_at": None,
        "run_directory": str(run_directory),
        "settings": {
            "browser_mode": "cdp" if config.cdp_url else "persistent-profile",
            "scroll_direction": config.scroll_direction,
            "comment_scope": config.comment_scope,
            "fetch_mode": config.fetch_mode,
            "fetch_douyin_id": config.fetch_douyin_id,
            "retry_limit": config.retry_limit,
            "idle_rounds": config.idle_rounds,
            "max_comments": config.max_comments or None,
            "max_minutes": config.max_minutes,
            "delay_ms": [config.min_delay_ms, config.max_delay_ms],
            "challenge_wait_minutes": config.challenge_wait_minutes,
            "pause_xpath": config.pause_xpath,
            **{key: getattr(config, key) for key in METADATA_DEFAULTS},
        },
        "summary": {
            "comment_count": 0,
            "image_count": 0,
            "failed_image_count": 0,
            "comment_response_count": 0,
            "top_level_response_count": 0,
            "top_level_comment_count": 0,
            "reply_comment_count": 0,
            "reply_response_count": 0,
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
    values = store.values()
    result["summary"]["top_level_comment_count"] = sum(
        1 for comment in values if comment.get("level", 1) == 1
    )
    result["summary"]["reply_comment_count"] = sum(
        1 for comment in values if comment.get("level", 1) > 1
    )
    result["summary"]["reply_response_count"] = response_state.reply_response_count
    result["summary"]["top_level_response_count"] = (
        response_state.top_level_response_count
    )
    write_result_json(run_directory, result)


class CommentResponseCollector:
    def __init__(
        self,
        page: Page,
        store: CommentStore,
        state: ResponseState,
        aweme_id: str,
        include_all_levels: bool = False,
    ) -> None:
        self.page = page
        self.store = store
        self.state = state
        self.aweme_id = aweme_id
        self.include_all_levels = include_all_levels
        self.pending: set[asyncio.Task[None]] = set()
        self._listener = self._schedule
        self._attached = False
        self._log_context = copy_context()

    def attach(self) -> None:
        """Register once so repeated setup cannot duplicate response processing."""
        if self._attached:
            return
        self.page.on("response", self._listener)
        self._attached = True

    def detach(self) -> None:
        """Release once; normal completion and finally may both request cleanup."""
        if not self._attached:
            return
        # Playwright/pyee raises KeyError when the same wrapped callback is removed
        # twice; its text is a function repr, so never let cleanup fail a finished task.
        self._attached = False
        try:
            self.page.remove_listener("response", self._listener)
        except KeyError:
            pass

    def _schedule(self, response: Response) -> None:
        response_url = response.url
        is_top_level = is_top_level_comment_response_url(response_url)
        is_reply = is_reply_comment_response_url(response_url)
        if not is_top_level and not (self.include_all_levels and is_reply):
            return
        response_aweme_id = get_aweme_id_from_url(response_url)
        if self.aweme_id and response_aweme_id and response_aweme_id != self.aweme_id:
            return

        task = self._log_context.run(asyncio.create_task, self._handle(response, is_reply=is_reply))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def _handle(self, response: Response, *, is_reply: bool) -> None:
        try:
            payload = await asyncio.wait_for(response.json(), timeout=10)
        except Exception:
            self.state.warnings.add(f"无法解析评论响应：{response.url}")
            return

        if not isinstance(payload, dict) or getattr(response, "status", 200) >= 400 or payload.get("status_code", 0) not in (0, None):
            self.state.warnings.add(f"评论接口返回错误：{response.url}")
            return
        data = payload.get("data")
        comments_null = (("comments" in payload and payload["comments"] is None)
                         or (isinstance(data, dict) and "comments" in data and data["comments"] is None))
        comments = [] if comments_null else extract_top_level_comment_array(payload)
        if is_reply:
            query = parse_qs(urlparse(response.url).query)
            parent_id = next(
                (
                    query[key][0]
                    for key in ("comment_id", "comment_id_str", "root_comment_id", "cid")
                    if query.get(key)
                ),
                "",
            )
            added = sum(
                self.store.add_tree(
                    comment,
                    self.aweme_id,
                    level=2,
                    parent_comment_id=parent_id,
                    root_comment_id=parent_id,
                )
                for comment in comments
            )
        elif self.include_all_levels:
            added = sum(
                self.store.add_tree(comment, self.aweme_id) for comment in comments
            )
        else:
            added = sum(self.store.add(comment, self.aweme_id) for comment in comments)
        pagination = extract_pagination(payload)
        if comments_null:
            pagination["has_more"] = False
            print(f"[{'回复' if is_reply else '一级评论'}响应] comments=null，跳过当前{'回复列表' if is_reply else '视频'}，不再重试")
        self.state.response_count += 1
        if is_reply:
            self.state.reply_response_count += 1
            self.state.reply_received[parent_id] = self.state.reply_received.get(parent_id, 0) + 1
            previous = self.state.reply_progress.get(parent_id, (None, 0, False))
            values = self.store.values()
            expected = next((comment.get("reply_count", 0) for comment in values if comment.get("id") == parent_id), 0)
            loaded = sum(1 for comment in values if comment.get("root_comment_id") == parent_id)
            finished = previous[2] or comments_null or (pagination["has_more"] is False and (bool(comments) or loaded >= expected))
            self.state.reply_progress[parent_id] = (pagination["cursor"] if comments or finished else previous[0], previous[1] + added, finished)
        else:
            if comments_null:
                self.state.top_level_comments_null = True
                self.state.load_failures.pop("top_level", None)
            self.state.top_level_response_count += 1
            self.state.top_level_request_url = response.url
            self.state.has_more = False if self.state.top_level_comments_null else pagination["has_more"]
            self.state.cursor = pagination["cursor"]
        self.state.last_response_at = asyncio.get_running_loop().time()
        self.state.last_added = added
        print(
            f"[{'回复' if is_reply else '一级评论'}响应] "
            f"本页 {len(comments)} 条，新增 {added} 条，"
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


async def _expand_reply_threads(page: Page, config: CrawlerConfig, state: ResponseState | None = None, collector: CommentResponseCollector | None = None) -> int:
    """Expand each reply page with an independent bounded, data-based retry budget."""
    state = state if state is not None else ResponseState()
    if config.comment_scope != "all" or "__scan_failed__" in state.skipped_reply_ids:
        return 0
    clicked = 0
    for attempt in range(40):
        if config.stop_requested and config.stop_requested():
            return clicked
        if state.top_level_comments_null:
            return clicked
        await _verify_work_page(page, get_aweme_id_from_url(config.url))
        token = f"codex-expand-{attempt}-{random.randint(100000, 999999)}"
        result = None
        try:
            result = await page.evaluate(
                """
                ({token, blocked}) => {
                  const normalize = value =>
                    (value || "").replace(/\\s+/g, "").trim();
                  const matchesExpandControl = text =>
                    text.includes("展开") && !text.includes("收起");
                  const visible = element => {
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0 &&
                      style.display !== "none" && style.visibility !== "hidden";
                  };

                  const root = VISIBLE_LIST;
                  if (!root) return {found: false};
                  const searchRoot = root;
                  const constrainToRightSide = false;

                  const elements = searchRoot.querySelectorAll(
                    'button, [role="button"], a, [data-e2e*="reply"], div, span, p'
                  );
                  for (const element of elements) {
                    const text = normalize(element.innerText || element.textContent);
                    if (!text || text.length > 40 || !matchesExpandControl(text)) continue;
                    if (!visible(element)) continue;
                    const rect = element.getBoundingClientRect();
                    if (constrainToRightSide && rect.right < window.innerWidth * 0.55) continue;
                    const hasSameTextChild = Array.from(element.children).some(
                      child => normalize(child.innerText || child.textContent) === text
                    );
                    if (hasSameTextChild) continue;
                    const target = element.closest('button, [role="button"], a') || element;
                    const card = target.closest('[data-e2e="comment-item"]');
                    const tooltip = card && (card.querySelector('[data-e2e="video-comment-more"][id^="tooltip_"]') || card.querySelector('[id^="tooltip_"]'));
                    const parentId = tooltip ? tooltip.id.slice(8) : '';
                    const key = parentId || (card ? normalize(card.innerText).slice(0, 100) : text);
                    if (blocked.includes(key) || target.dataset.douyinExpandClickedText === text) continue;
                    target.dataset.douyinExpandToken = token;
                    return {found: true, text, key, parentId, replyControl: /回复|评论|条/.test(text), tag: target.tagName.toLowerCase()};
                  }
                  return {found: false, reason: "reply-control-missing"};
                }
                """.replace("VISIBLE_LIST", _VISIBLE_COMMENT_LIST).replace("NOTE_DETAIL", _NOTE_DETAIL),
                {"token": token, "blocked": list(state.skipped_reply_ids)},
            )
            if not result or not result.get("found"):
                if clicked == 0:
                    print(
                        "[回复加载] 评论区内暂未找到新的“展开”入口："
                        f"{(result or {}).get('reason', 'unknown')}"
                    )
                break
            target = page.locator(
                f'[data-douyin-expand-token="{token}"]'
            ).first
            key = result["key"]
            parent_id = result.get("parentId", "")
            before = state.reply_progress.get(parent_id)
            if before is not None and before[2]:
                state.skipped_reply_ids.add(key)
                continue
            before_received = state.reply_received.get(parent_id, 0)
            await target.scroll_into_view_if_needed(timeout=4_000)
            await target.click(timeout=4_000)
            if not result.get("replyControl", True):
                # Full-text expansion is not a reply page: mark only that DOM
                # control and never exhaust the parent comment's reply budget.
                try:
                    await target.evaluate("(element, text) => {element.dataset.douyinExpandClickedText = text;}", result.get("text", ""))
                except PlaywrightError:
                    pass
                continue
            # A successful click is not evidence that replies loaded. Wait for
            # the matching parent response and only accept actual page progress.
            await _randomized_pause(config)
            deadline = asyncio.get_running_loop().time() + config.action_timeout_ms / 1000
            while collector is not None and state.reply_received.get(parent_id, 0) == before_received:
                if config.stop_requested and config.stop_requested():
                    return clicked
                await collector.drain()
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.2)
            if collector is not None:
                await collector.drain()
            after = state.reply_progress.get(parent_id)
            progressed = after is not None and (
                after[2]
                or (before is None and (after[1] > 0 or after[0] not in (None, "0")))
                or (before is not None and (after[1] > before[1] or after[0] != before[0]))
            )
            if progressed:
                state.load_failures.pop("reply:" + key, None)
                clicked += 1
                if after[2]:
                    state.skipped_reply_ids.add(key)
            else:
                blocking = await _find_blocking_state(page)
                if blocking:
                    await _wait_for_manual_challenge(page, config, blocking)
                    continue
                exhausted = _record_load_failure(state, config, "reply:" + key, "展开后未取得关联回复分页进展")
                if exhausted:
                    state.skipped_reply_ids.add(key)
            if config.stop_requested and config.stop_requested():
                break
            print(
                f"[回复加载] 已点击“{result.get('text', '展开')}”"
                f"（本轮第 {clicked} 个）"
            )
            # 展开回复使用独立的小间隔；后续仍会等待响应并再次扫描，
            # 因此提速不会跳过新出现的嵌套“展开”入口。
            await page.wait_for_timeout(random.randint(280, 480))
        except Exception as error:
            blocking = await _find_blocking_state(page)
            if blocking:
                await _wait_for_manual_challenge(page, config, blocking)
                continue
            print(f"[回复加载] 点击展开入口失败：{error}")
            if result and result.get("key"):
                if _record_load_failure(state, config, "reply:" + result["key"], str(error)):
                    state.skipped_reply_ids.add(result["key"])
            elif _record_load_failure(state, config, "reply_scan", str(error)):
                state.skipped_reply_ids.add("__scan_failed__")
                break
            # Keep the viewport until this parent resolves or exhausts its
            # budget; scrolling would otherwise unmount an unresolved comment.
            await _randomized_pause(config)
            continue
    return clicked


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
    if config.manual_continue is not None:
        await config.manual_continue(
            blocking_state,
            config.challenge_wait_minutes,
        )
        await page.wait_for_timeout(1_000)
        if remaining := await _find_blocking_state(page):
            raise RuntimeError(f"页面仍显示登录或风控验证：{remaining}")
        return

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




async def _pause_configured_video(page: Page, config: CrawlerConfig) -> bool:
    """输出暂停配置并安全执行暂停，避免重复点击将视频恢复播放。"""
    print(f"[按钮配置] 暂停按钮 pause_xpath = {config.pause_xpath.strip() or '（未配置）'}")
    if not config.pause_xpath.strip():
        print("[视频暂停] 未配置暂停按钮 XPath，跳过自动暂停")
        return False
    visible_video_state = """() => {
      const videos = [...document.querySelectorAll('video')].filter(v => {
        const rect = v.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && getComputedStyle(v).visibility !== 'hidden';
      });
      return videos.length ? videos.every(v => v.paused) : null;
    }"""
    try:
        if await page.evaluate(visible_video_state) is True:
            print("[视频暂停] 视频已经暂停，不重复点击")
            return True
        videos = page.locator("video:visible")
        if await videos.count():
            await videos.first.hover(timeout=config.action_timeout_ms)
        target = page.locator(f"xpath={config.pause_xpath.strip()}").first
        await target.wait_for(state="visible", timeout=config.action_timeout_ms)
        # A play/pause toggle must not resume a video that paused while loading.
        if await page.evaluate(visible_video_state) is True:
            print("[视频暂停] 视频已经暂停，不重复点击")
            return True
        await target.click(timeout=config.action_timeout_ms)
        state = await page.evaluate(visible_video_state)
        if state is not None:
            await page.wait_for_function(visible_video_state, timeout=config.action_timeout_ms)
            print("[视频暂停] 已通过配置的 XPath 点击并确认暂停")
        else:
            print("[视频暂停] 已点击暂停按钮（未找到可验证状态的视频元素）")
        return True
    except Exception as error:
        print(f"[视频暂停] 点击或暂停验证失败：{error}；继续采集")
        return False




async def _current_viewport(page: Page) -> dict[str, int] | None:
    """读取当前实际页面尺寸，兼容无固定视口及用户随时调整窗口。"""
    try:
        # no_viewport 模式的 page.viewport_size 为 None，不能据此跳过滚动。
        size = await page.evaluate("() => ({width: window.innerWidth, height: window.innerHeight})")
        width, height = int(size["width"]), int(size["height"])
        if width > 0 and height > 0:
            return {"width": width, "height": height}
    except Exception as error:
        print(f"[评论滚动] 读取实际视口失败：{error}")
    return None


async def _scroll_comment_panel(
    page: Page,
    config: CrawlerConfig,
    *,
    jump_to_end: bool = False,
) -> bool:
    delta = (
        -abs(config.scroll_pixels)
        if config.scroll_direction == "up"
        else abs(config.scroll_pixels)
    )
    viewport = await _current_viewport(page)
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
            ({x, y, delta, jumpToEnd}) => {
              let element = VISIBLE_LIST;
              if (!element) return {found: false};
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
                  element.scrollTop = jumpToEnd
                    ? element.scrollHeight
                    : element.scrollTop + delta;
                  if (!(NOTE_DETAIL)) element.dispatchEvent(
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
            """.replace("VISIBLE_LIST", _VISIBLE_COMMENT_LIST).replace("NOTE_DETAIL", _NOTE_DETAIL),
            {
                "x": probe_x,
                "y": probe_y,
                "delta": delta,
                "jumpToEnd": jump_to_end,
            },
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


async def _reset_comment_panel_to_top(page: Page) -> bool:
    """按当前窗口尺寸定位评论容器，回到顶部以继续展开回复。"""
    viewport = await _current_viewport(page)
    if not viewport:
        return False
    result = await page.evaluate(
        """
        ({x, y}) => {
          let element = VISIBLE_LIST;
          if (!element) return false;
          while (element && element !== document.body) {
            const style = window.getComputedStyle(element);
            if (
              ["auto", "scroll", "overlay"].includes(style.overflowY) &&
              element.scrollHeight > element.clientHeight
            ) {
              element.scrollTop = 0;
              element.dispatchEvent(new Event("scroll", {bubbles: true}));
              return true;
            }
            element = element.parentElement;
          }
          return false;
        }
        """.replace("VISIBLE_LIST", _VISIBLE_COMMENT_LIST).replace("NOTE_DETAIL", _NOTE_DETAIL),
        {
            "x": max(1, viewport["width"] - 150),
            "y": viewport["height"] / 2,
        },
    )
    if result:
        print("[采集阶段] 一级评论已加载完，评论列表已回到顶部")
    return bool(result)


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


def _replace_query_cursor(raw_url: str, cursor: str) -> str:
    """Keep the browser-observed request intact and replace only its cursor."""
    parts = urlsplit(raw_url)
    encoded_cursor = urlencode({"cursor": cursor})
    replaced = False
    updated: list[str] = []
    for item in parts.query.split("&") if parts.query else []:
        if item.partition("=")[0] == "cursor":
            if not replaced:
                updated.append(encoded_cursor)
                replaced = True
            continue
        updated.append(item)
    if not replaced:
        updated.append(encoded_cursor)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "&".join(updated), parts.fragment)
    )


async def _try_collect_top_level_by_network(
    *,
    page: Page,
    config: CrawlerConfig,
    store: CommentStore,
    response_state: ResponseState,
    collector: CommentResponseCollector,
    run_directory: Path,
    result: dict[str, Any],
) -> str | None:
    """Page through the observed comment/list request; return a stop reason if done."""
    if config.fetch_mode != "network_first":
        print("[一级评论接口] 已选择仅页面滚动模式")
        return None

    loop = asyncio.get_running_loop()
    wait_deadline = loop.time() + 12
    while not response_state.top_level_request_url and loop.time() < wait_deadline:
        if config.stop_requested is not None and config.stop_requested():
            return "用户从网页停止任务"
        await page.wait_for_timeout(250)
        await collector.drain()

    if response_state.top_level_comments_null:
        return "评论接口返回 comments=null，跳过当前视频"
    request_url = response_state.top_level_request_url
    if not request_url or response_state.cursor is None:
        message = "未取得可复用的 comment/list 请求或游标，已自动改用页面滚动"
        print(f"[一级评论接口] {message}")
        response_state.warnings.add(message)
        return None
    if not is_top_level_comment_response_url(request_url):
        message = "捕获的请求不是一级评论接口，已自动改用页面滚动"
        print(f"[一级评论接口] {message}")
        response_state.warnings.add(message)
        return None
    if response_state.has_more is False:
        print("[一级评论接口] 首次响应已表明没有下一页")
        return (
            "评论接口返回 has_more=false"
            if config.comment_scope != "all"
            else None
        )

    print(
        "[一级评论接口] 已捕获真实 comment/list 请求，"
        "开始在当前登录页面内按游标翻页"
    )
    attempted_cursors: set[str] = set()
    direct_pages = 0
    direct_added = 0
    last_checkpoint_at = 0.0
    deadline = (
        float("inf")
        if config.comment_scope == "all"
        else loop.time() + config.max_minutes * 60
    )

    while response_state.has_more is not False:
        if loop.time() >= deadline:
            return "达到 max-minutes 限制"
        if config.stop_requested is not None and config.stop_requested():
            return "用户从网页停止任务"
        if (
            config.comment_scope != "all"
            and config.max_comments > 0
            and len(store) >= config.max_comments
        ):
            return "达到 max-comments 限制"

        cursor = response_state.cursor
        if cursor is None or cursor in attempted_cursors:
            blocking = await _find_blocking_state(page)
            if blocking:
                await _wait_for_manual_challenge(page, config, blocking)
                attempted_cursors.discard(cursor)
                continue
            if _record_load_failure(response_state, config, "top_level", "接口游标缺失或重复"):
                return "评论加载达到失败重试上限，结果可能不完整"
            message = "接口游标缺失或重复，已自动切换页面滚动"
            print(f"[一级评论接口] {message}")
            response_state.warnings.add(message)
            return None
        attempted_cursors.add(cursor)

        base_url = response_state.top_level_request_url or request_url
        next_url = _replace_query_cursor(base_url, cursor)
        before_responses = response_state.top_level_response_count
        before_count = len(store)
        before_cursor = cursor
        await _verify_work_page(page, get_aweme_id_from_url(config.url))
        try:
            fetch_result = await asyncio.wait_for(
                page.evaluate(
                    """
                    async url => {
                      const response = await fetch(url, {
                        method: "GET",
                        credentials: "include",
                        headers: {"accept": "application/json, text/plain, */*"}
                      });
                      await response.arrayBuffer();
                      return {ok: response.ok, status: response.status};
                    }
                    """,
                    next_url,
                ),
                timeout=max(20, config.navigation_timeout_ms / 1_000),
            )
            await page.wait_for_timeout(120)
            await collector.drain()
        except Exception as error:
            message = f"接口请求失败，已自动切换页面滚动：{error}"
            print(f"[一级评论接口] {message}")
            response_state.warnings.add(message)
            blocking = await _find_blocking_state(page)
            if blocking:
                await _wait_for_manual_challenge(page, config, blocking)
                attempted_cursors.discard(cursor)
                continue
            if _record_load_failure(response_state, config, "top_level", "接口请求失败"):
                return "评论加载达到失败重试上限，结果可能不完整"
            return None

        response_advanced = (
            response_state.top_level_response_count > before_responses
        )
        cursor_advanced = (
            response_state.has_more is False
            or (
                response_state.cursor is not None
                and response_state.cursor != before_cursor
            )
        )
        if (
            not isinstance(fetch_result, dict)
            or not fetch_result.get("ok")
            or not response_advanced
            or not cursor_advanced
        ):
            status = (
                fetch_result.get("status")
                if isinstance(fetch_result, dict)
                else "unknown"
            )
            message = (
                "接口未正常推进分页"
                f"（HTTP {status}），已自动切换页面滚动"
            )
            print(f"[一级评论接口] {message}")
            response_state.warnings.add(message)
            blocking = await _find_blocking_state(page)
            if blocking:
                await _wait_for_manual_challenge(page, config, blocking)
                attempted_cursors.discard(cursor)
                continue
            if _record_load_failure(response_state, config, "top_level", "接口未推进分页"):
                return "评论加载达到失败重试上限，结果可能不完整"
            return None

        response_state.load_failures.pop("top_level", None)
        direct_pages += 1
        added = len(store) - before_count
        direct_added += max(0, added)
        print(
            f"[一级评论接口分页 {direct_pages}] 新增 {max(0, added)} 条，"
            f"累计 {len(store)} 条，has_more={response_state.has_more}"
        )
        if loop.time() - last_checkpoint_at >= 5:
            _checkpoint(run_directory, result, store, response_state)
            last_checkpoint_at = loop.time()
        if response_state.has_more is not False:
            await _randomized_pause(config, 0.55)

    print(
        f"[一级评论接口] 分页完成：主动请求 {direct_pages} 页，"
        f"新增 {direct_added} 条一级评论"
    )
    return (
        "评论接口返回 has_more=false"
        if config.comment_scope != "all"
        else None
    )


async def _comment_scroll_position(page: Page) -> float | None:
    """Read the active comment viewport, so ordinary traversal is not a failure."""
    try:
        return await page.evaluate("""() => {
          const list = VISIBLE_LIST;
          for(let e=list;e && e!==document.body;e=e.parentElement) {
            if (['auto','scroll','overlay'].includes(getComputedStyle(e).overflowY) && e.scrollHeight > e.clientHeight) return e.scrollTop;
          }
          return null;
        }""".replace("VISIBLE_LIST", _VISIBLE_COMMENT_LIST))
    except PlaywrightError:
        return None


async def _comment_panel_fits_viewport(page: Page) -> bool:
    """Recognize a visible short list that legitimately needs no scrollbar."""
    try:
        return bool(await page.evaluate("""() => {
          const list = VISIBLE_LIST;
          if (!list) return false;
          const rect = list.getBoundingClientRect();
          if (rect.top < 0 || rect.bottom > innerHeight) return false;
          for(let e=list;e && e!==document.body;e=e.parentElement) {
            if (['auto','scroll','overlay'].includes(getComputedStyle(e).overflowY) && e.scrollHeight > e.clientHeight) return false;
          }
          return true;
        }""".replace("VISIBLE_LIST", _VISIBLE_COMMENT_LIST)))
    except PlaywrightError:
        return False


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
    deadline = (
        float("inf")
        if config.comment_scope == "all"
        else loop.time() + config.max_minutes * 60
    )
    previous_count = len(store)
    idle_rounds = 0
    scroll_target_warnings = 0
    round_number = 0
    last_checkpoint_at = 0.0
    reply_phase = config.comment_scope != "all"

    while loop.time() < deadline:
        if response_state.top_level_comments_null:
            return "评论接口返回 comments=null，跳过当前视频"
        round_number += 1
        if config.stop_requested is not None and config.stop_requested():
            return "用户从网页停止任务"
        if (
            config.comment_scope != "all"
            and config.max_comments > 0
            and len(store) >= config.max_comments
        ):
            return "达到 max-comments 限制"

        await _verify_work_page(page, get_aweme_id_from_url(config.url))
        if round_number == 1 or round_number % 8 == 0:
            await _wait_for_manual_challenge(
                page,
                config,
                await _find_blocking_state(page),
            )

        if (
            config.comment_scope == "all"
            and not reply_phase
            and response_state.top_level_response_count > 0
            and response_state.has_more is False
        ):
            await collector.drain()
            if len(store) == 0:
                # A valid empty final response is normal completion, not a
                # missing-scroll-container failure in the reply scan phase.
                return "评论接口返回 has_more=false，暂无评论"
            await _reset_comment_panel_to_top(page)
            await _randomized_pause(config)
            reply_phase = True
            idle_rounds = 0
            previous_count = len(store)
            print(
                "[采集阶段] 开始从顶部扫描全部一级评论并递归展开回复"
            )

        expanded_count = 0
        if config.comment_scope == "all" and reply_phase:
            expanded_count = await _expand_reply_threads(page, config, response_state, collector)
            await collector.drain()

        # 所有层级模式先把当前 DOM 中能看到的回复入口处理完。只要本轮有
        # 展开动作就暂不滚动，避免虚拟列表卸载尚未处理的一级评论。
        found_target = True
        before_top_cursor = response_state.cursor
        await _verify_work_page(page, get_aweme_id_from_url(config.url))
        before_scroll_position = await _comment_scroll_position(page)
        if expanded_count == 0:
            found_target = await _scroll_comment_panel(
                page,
                config,
                jump_to_end=(
                    config.comment_scope == "all" and not reply_phase
                ),
            )
        if not found_target and scroll_target_warnings < 3:
            scroll_target_warnings += 1
            response_state.warnings.add(
                "右侧评论区探测点未找到带滚动条的容器；"
                "为避免切换视频，本轮未发送滚动事件"
            )

        await _randomized_pause(config)
        await collector.drain()
        if response_state.top_level_comments_null:
            return "评论接口返回 comments=null，跳过当前视频"
        after_scroll_position = await _comment_scroll_position(page)
        viewport_moved = before_scroll_position is not None and after_scroll_position is not None and after_scroll_position != before_scroll_position
        if not viewport_moved and len(store) <= previous_count and expanded_count == 0:
            blocking = await _find_blocking_state(page)
            if blocking:
                await _wait_for_manual_challenge(page, config, blocking)
                continue
        if not found_target and config.comment_scope == "all" and reply_phase:
            if response_state.has_more is False and await _comment_panel_fits_viewport(page):
                return "评论接口返回 has_more=false，评论及回复扫描完成"
            if _record_load_failure(response_state, config, "reply_viewport", "回复扫描无法找到可滚动评论容器"):
                return "回复扫描达到失败重试上限，结果可能不完整"
        elif config.comment_scope == "all" and reply_phase:
            # A usable reply-scanning viewport means earlier scan failures are
            # no longer consecutive and must not consume this video's budget.
            response_state.load_failures.pop("reply_viewport", None)
        if response_state.cursor != before_top_cursor or len(store) > previous_count:
            response_state.load_failures.pop("top_level", None)
        elif expanded_count == 0 and not viewport_moved:
            # The all-level mode has an unbounded runtime by design, so its
            # initial top-level loading phase also needs the configured retry
            # budget. Without this branch a missing/ stalled first page could
            # keep polling forever while waiting for has_more=false.
            loading_top_level = not (config.comment_scope == "all" and reply_phase)
            if loading_top_level and response_state.has_more is not False:
                empty_page = await _page_explicitly_has_no_comments(page)
                if config.comment_scope == "all" and not reply_phase and empty_page:
                    return "页面明确显示暂无评论"
                if not empty_page:
                    if _record_load_failure(response_state, config, "top_level", "页面加载未推进评论列表"):
                        return "评论加载达到失败重试上限，结果可能不完整"
        # Traversing preloaded comments is useful even without network data.
        if len(store) > previous_count or viewport_moved:
            idle_rounds = 0
            if len(store) > previous_count:
                previous_count = len(store)
                # 大量回复模式下每轮完整重写 JSON 会越来越慢。最多每 5 秒
                # 保存一次中间检查点；循环结束后还会无条件完整保存。
                if loop.time() - last_checkpoint_at >= 5:
                    _checkpoint(run_directory, result, store, response_state)
                    last_checkpoint_at = loop.time()
        else:
            idle_rounds += 1

        idle_limit = (
            max(config.idle_rounds, 30)
            if config.comment_scope == "all" and reply_phase
            else config.idle_rounds
        )
        idle_status = (
            "等待一级评论接口返回 has_more=false"
            if config.comment_scope == "all" and not reply_phase
            else f"{idle_rounds}/{idle_limit}"
        )
        print(
            f"[{'回复扫描' if reply_phase and config.comment_scope == 'all' else '一级加载'} "
            f"{round_number}] 累计 {len(store)} 条，"
            f"本轮展开 {expanded_count} 个，"
            f"连续无新增且无展开 {idle_status}"
        )

        stop_reason = None
        if config.comment_scope != "all" or reply_phase:
            stop_reason = determine_stop_reason(
                response_count=response_state.response_count,
                has_more=(
                    None
                    if config.comment_scope == "all"
                    else response_state.has_more
                ),
                idle_rounds=idle_rounds,
                idle_limit=idle_limit,
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
    semaphore = asyncio.Semaphore(3)
    images_directory = run_directory / "images"
    images_directory.mkdir(parents=True, exist_ok=True)

    async def download_for_comment(comment: dict[str, Any]) -> tuple[int, int]:
        downloaded = 0
        failed = 0
        source_urls = list(dict.fromkeys(comment["source_image_urls"]))
        for index, source_url in enumerate(source_urls, start=1):
            try:
                async with semaphore:
                    await _randomized_pause(config, 0.18)
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
                            "图片超过大小限制 "
                            f"{config.max_image_bytes} bytes"
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
                            "转换后的图片超过大小限制 "
                            f"{config.max_image_bytes} bytes"
                        )
                    fallback = hashlib.sha256(
                        comment["id"].encode()
                    ).hexdigest()[:12]
                    id_part = safe_path_part(comment["id"], fallback)
                    filename = f"comment-{id_part}-{index:02d}{extension}"
                    await asyncio.to_thread(
                        _write_exclusive,
                        images_directory / filename,
                        body,
                    )
                comment["images"].append(
                    {
                        "source_url": source_url,
                        "file": f"images/{filename}",
                        "content_type": content_type,
                        "source_content_type": source_content_type,
                        "converted_from": converted_from,
                        "bytes": len(body),
                    }
                )
                downloaded += 1
            except Exception as error:
                comment["images"].append(
                    {
                        "source_url": source_url,
                        "file": None,
                        "error": str(error),
                    }
                )
                failed += 1
        return downloaded, failed

    results = await asyncio.gather(
        *(download_for_comment(comment) for comment in comments)
    )
    return (
        sum(downloaded for downloaded, _ in results),
        sum(failed for _, failed in results),
    )


def _is_target_profile_response(response: Response, sec_uid: str) -> bool:
    """Reject unrelated responses before reading their bodies."""
    parsed = urlparse(response.url)
    return (
        parsed.hostname == "www.douyin.com"
        and parsed.path.rstrip("/") == "/aweme/v1/web/user/profile/other"
        and parse_qs(parsed.query).get("sec_user_id") == [sec_uid]
    )


class _ProfilePageFailure(RuntimeError):
    pass


class _ProfileVerificationFailure(RuntimeError):
    pass


async def _read_profile_short_id(
    page: Page, sec_uid: str, config: CrawlerConfig, *, reload: bool = False,
) -> str:
    future = asyncio.get_running_loop().create_future()
    page_failure = asyncio.get_running_loop().create_future()
    pending: set[asyncio.Task] = set()

    async def read_response(response: Response) -> None:
        try:
            payload = await response.json()
            if not isinstance(payload, dict):
                return
            user = payload.get("user")
            if not isinstance(user, dict) or user.get("sec_uid") != sec_uid:
                return
            if payload.get("status_code") not in (None, 0):
                return
            value = str(user.get("short_id") or "").strip()
            if not future.done():
                future.set_result(value if value != "0" else "")
        except Exception:
            # A malformed response may be followed by a valid one.
            pass

    def on_response(response: Response) -> None:
        if _is_target_profile_response(response, sec_uid):
            task = asyncio.create_task(read_response(response))
            pending.add(task)
            task.add_done_callback(pending.discard)

    def on_crash(*_args: Any) -> None:
        if not page_failure.done():
            page_failure.set_result("用户主页已崩溃")

    def on_close(*_args: Any) -> None:
        if not page_failure.done():
            page_failure.set_result("用户主页已关闭")

    async def query() -> str:
        profile_url = f"https://www.douyin.com/user/{quote(sec_uid, safe='')}"
        try:
            if reload:
                await page.reload(wait_until="domcontentloaded",
                                  timeout=config.navigation_timeout_ms)
            else:
                await page.goto(profile_url, wait_until="domcontentloaded",
                                timeout=config.navigation_timeout_ms)
        except Exception:
            if not future.done():
                raise
        if not future.done():
            blocker = await _find_blocking_state(page)
            if blocker:
                print("[抖音号] 用户主页需要登录或验证，等待人工处理")
                await page.bring_to_front()
                try:
                    await _wait_for_manual_challenge(page, config, blocker)
                except Exception as error:
                    raise _ProfileVerificationFailure(str(error)) from error
                await page.goto(profile_url, wait_until="domcontentloaded",
                                timeout=config.navigation_timeout_ms)
        return await asyncio.wait_for(future, config.navigation_timeout_ms / 1000)

    page.on("response", on_response)
    page.on("crash", on_crash)
    page.on("close", on_close)
    query_task = asyncio.create_task(query())
    try:
        # A renderer crash must also interrupt navigation or manual waiting,
        # not only the final response wait.
        await asyncio.wait({query_task, page_failure}, return_when=asyncio.FIRST_COMPLETED)
        if page_failure.done():
            raise _ProfilePageFailure(page_failure.result())
        return query_task.result()
    finally:
        page.remove_listener("response", on_response)
        page.remove_listener("crash", on_crash)
        page.remove_listener("close", on_close)
        query_task.cancel()
        await asyncio.gather(query_task, return_exceptions=True)
        future.cancel()
        page_failure.cancel()
        for task in tuple(pending):
            task.cancel()
        if pending:
            await asyncio.gather(*tuple(pending), return_exceptions=True)


async def _supplement_douyin_ids(
    context: BrowserContext, config: CrawlerConfig, store: CommentStore,
    run_directory: Path, result: dict[str, Any], state: ResponseState,
) -> None:
    if not config.fetch_douyin_id:
        return
    groups: dict[str, list[dict[str, Any]]] = {}
    for comment in store.values():
        author = comment["author"]
        key = author.get("sec_uid") or author.get("uid") or comment["id"]
        groups.setdefault(key, []).append(author)
    print(f"[抖音号] 开始处理 {len(groups)} 位去重用户；主页补充会增加耗时")
    profile_page = None
    success = failed = 0
    last_checkpoint = asyncio.get_running_loop().time()
    last_profile_at = 0.0

    async def close_profile_page() -> None:
        nonlocal profile_page
        old_page, profile_page = profile_page, None
        if old_page is not None:
            try:
                await asyncio.wait_for(old_page.close(), timeout=5)
            except Exception:
                pass

    async def query_with_recovery(sec_uid: str) -> str:
        """Retry read-only profile loading without retrying any publish action."""
        nonlocal profile_page
        for attempt in range(config.retry_limit + 1):
            if config.stop_requested and config.stop_requested():
                raise asyncio.CancelledError
            reload = attempt == 1 and profile_page is not None and not profile_page.is_closed()
            try:
                if profile_page is None or profile_page.is_closed():
                    profile_page = await asyncio.wait_for(context.new_page(), timeout=15)
                    profile_page.set_default_timeout(config.action_timeout_ms)
                return await _read_profile_short_id(profile_page, sec_uid, config, reload=reload)
            except (_ProfilePageFailure, PlaywrightError, TimeoutError) as error:
                if attempt >= config.retry_limit:
                    await close_profile_page()
                    raise
                action = "重新加载主页" if attempt == 0 else "新建主页标签页"
                print(f"[抖音号][自动恢复] {error}；{action}，重试 {attempt + 1}/{config.retry_limit}")
                if attempt >= 1:
                    await close_profile_page()
                deadline = asyncio.get_running_loop().time() + max(0.1, config.min_delay_ms / 1000)
                while asyncio.get_running_loop().time() < deadline:
                    if config.stop_requested and config.stop_requested():
                        raise asyncio.CancelledError
                    await asyncio.sleep(0.1)
            except _ProfileVerificationFailure:
                # Login/risk verification remains manual; never refresh repeatedly.
                raise
        raise RuntimeError("主页恢复失败")

    try:
        for authors in groups.values():
            if config.stop_requested and config.stop_requested():
                print("[抖音号] 已停止补充，保留已经获取的抖音号")
                break
            first = authors[0]
            name = first.get("nickname") or "匿名用户"
            sec_uid = first.get("sec_uid") or ""
            existing = next((a for a in authors if a.get("unique_id")), None)
            if existing:
                value, source = existing["unique_id"], "comment_api"
            elif not sec_uid:
                failed += 1
                print(f"[抖音号][未获取] {name}：缺少 sec_uid，无法访问主页")
                continue
            else:
                delay = random.randint(max(800, config.min_delay_ms),
                                       max(1400, config.max_delay_ms, config.min_delay_ms)) / 1000
                while asyncio.get_running_loop().time() < last_profile_at + delay:
                    if config.stop_requested and config.stop_requested():
                        break
                    await asyncio.sleep(0.2)
                if config.stop_requested and config.stop_requested():
                    print("[抖音号] 已停止补充，保留已经获取的抖音号")
                    break
                last_profile_at = asyncio.get_running_loop().time()
                print(f"[抖音号][主页查询] {name}；sec_uid={sec_uid}")
                task = asyncio.create_task(query_with_recovery(sec_uid))
                stopped = False
                try:
                    while not task.done():
                        await asyncio.wait({task}, timeout=0.2)
                        if config.stop_requested and config.stop_requested():
                            stopped = True
                            task.cancel()
                            break
                    if stopped:
                        print("[抖音号] 已停止补充，保留已经获取的抖音号")
                        break
                    value = task.result()
                except Exception as error:
                    failed += 1
                    warning = f"抖音号主页补充失败：{name}（{sec_uid}）；{type(error).__name__}"
                    state.warnings.add(warning)
                    print(f"[抖音号][未获取] {warning}")
                    continue
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if not value:
                    failed += 1
                    print(f"[抖音号][未获取] {name}：主页接口 short_id 为空或为 0")
                    continue
                source = "profile_api"
            for author in authors:
                if not author.get("unique_id"):
                    author["unique_id"] = value
                    author["unique_id_source"] = source
                    if source == "profile_api":
                        author["short_id"] = value
            success += 1
            label = "评论接口 unique_id" if source == "comment_api" else "主页接口 short_id"
            print(f"[抖音号][{label}] {name}：{value}；关联 {len(authors)} 条评论")
            now = asyncio.get_running_loop().time()
            if success % 20 == 0 or now - last_checkpoint >= 3:
                _checkpoint(run_directory, result, store, state)
                last_checkpoint = now
    finally:
        await close_profile_page()
        _checkpoint(run_directory, result, store, state)
    print(f"[抖音号] 处理结束：成功 {success} 位，未获取 {failed} 位")


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
    if config.run_directory_ready is not None:
        config.run_directory_ready(run_directory)

    page: Page | None = None
    collector: CommentResponseCollector | None = None
    metadata_collector: VideoMetadataCollector | None = None
    playwright_instance = None
    owned_context = False
    context: BrowserContext | None = None
    try:
        if config.browser_context is None:
            playwright_instance = await async_playwright().start()
        if config.browser_context is not None:
            context = config.browser_context
            page = await context.new_page()
        elif config.cdp_url:
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
                # 独立启动也跟随真实窗口，不与网页共享启动路径产生尺寸差异。
                no_viewport=True,
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
            include_all_levels=config.comment_scope == "all",
        )
        collector.attach()
        metadata_collector = VideoMetadataCollector(
            page, aweme_id, {key: getattr(config, key) for key in METADATA_DEFAULTS},
            result, response_state.warnings,
        )
        metadata_collector.attach()

        print(
            "[采集模式] "
            + (
                "所有层级评论：先完整加载一级评论，再从顶部展开全部回复"
                if config.comment_scope == "all"
                else "仅一级评论"
            )
        )
        if config.comment_scope == "all":
            print(
                "[完整性保护] 所有层级模式不会因评论数或运行分钟上限自动截断；"
                "确认无新增和无展开入口后才结束"
            )
        print(f"打开页面：{config.url}")
        preparation_error = ""
        await _goto_with_retries(page, config, response_state)
        await collector.drain()
        if not response_state.top_level_comments_null:
            await page.bring_to_front()
            await _randomized_pause(config, 1.2)
            await collector.drain()
            if not response_state.top_level_comments_null:
                initial_state = await _find_blocking_state(page)
                if (config.require_initial_login or (owned_context and profile_is_new)) and not initial_state:
                    initial_state = "首次使用专用浏览器配置，请先登录抖音"
                async def wait_comment_access() -> None:
                    nonlocal initial_state
                    if not response_state.top_level_comments_null:
                        blocking = initial_state or await _find_blocking_state(page)
                        initial_state = ""
                        await _wait_for_manual_challenge(page, config, blocking)

                try:
                    await _prepare_comment_panel(page, aweme_id, config.retry_limit,
                                                 config.stop_requested, response_state,
                                                 wait_access=wait_comment_access)
                except Exception as error:
                    response_state.load_failures["top_level"] = config.retry_limit + 1
                    response_state.warnings.add(str(error))
                    _checkpoint(run_directory, result, store, response_state)
                    preparation_error = str(error)
                paused = False if preparation_error or response_state.top_level_comments_null else await _pause_configured_video(page, config)
                if config.pause_xpath.strip() and not paused:
                    response_state.warnings.add("自动暂停视频失败，请检查暂停按钮 XPath")
                await collector.drain()

        if not response_state.top_level_comments_null and not preparation_error:
            # 独立视频页自动显示评论列表，无需点击图标；接口监听已在导航前注册。
            await metadata_collector.wait(config.action_timeout_ms, config.stop_requested)
            print(
                "[视频信息] 发布者："
                f"{result['video_publisher_nickname'] or '未获取到'}；"
                f"标题：{result['video_title'] or '未获取到'}"
            )
            await _randomized_pause(config)
            await collector.drain()
        _checkpoint(run_directory, result, store, response_state)

        network_stop_reason = preparation_error or await _try_collect_top_level_by_network(
            page=page,
            config=config,
            store=store,
            response_state=response_state,
            collector=collector,
            run_directory=run_directory,
            result=result,
        )
        if network_stop_reason is not None:
            result["summary"]["stop_reason"] = network_stop_reason
        else:
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
            and not preparation_error
            and result["summary"]["stop_reason"] != "页面明确显示暂无评论"
        ):
            raise RuntimeError(
                "未捕获到一级评论列表响应；请确认评论区已打开、页面可正常访问，"
                "或尝试切换 --scroll-direction"
            )

        # Freeze comments before profile navigation; do not collect unrelated data.
        collector.detach()
        await collector.drain()
        await _supplement_douyin_ids(
            context, config, store, run_directory, result, response_state,
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

        incomplete = any(count > config.retry_limit for key, count in response_state.load_failures.items() if key == "top_level" or key.startswith("reply"))
        result["status"] = "incomplete" if incomplete else "complete"
        if incomplete:
            result["summary"]["stop_reason"] += "；部分加载达到失败重试上限，结果可能不完整"
        result["finished_at"] = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        result["comments"] = store.values()
        result["summary"]["comment_count"] = len(store)
        result["summary"]["image_count"] = image_count
        result["summary"]["failed_image_count"] = failed_image_count
        result["summary"]["comment_response_count"] = response_state.response_count
        result["summary"]["top_level_comment_count"] = sum(
            1 for comment in store.values() if comment.get("level", 1) == 1
        )
        result["summary"]["reply_comment_count"] = sum(
            1 for comment in store.values() if comment.get("level", 1) > 1
        )
        result["summary"]["reply_response_count"] = response_state.reply_response_count
        result["summary"]["top_level_response_count"] = (
            response_state.top_level_response_count
        )
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
        if metadata_collector is not None:
            await metadata_collector.close()
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
