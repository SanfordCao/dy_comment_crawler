"""Independent, page-scrolling comment reply task for the local workbench."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable
from urllib.parse import unquote, urlparse
import asyncio
import json
import re
import time

try:
    from .comment_reply_filter import REGIONS, matches_reply_filter, validate_reply_filter
    from .comments import CommentStore, get_aweme_id_from_url
    from .crawler import CommentResponseCollector, ResponseState
    from .output import create_run_directory
    from .video_links import parse_share_texts, resolve_share_rows
except ImportError:
    from comment_reply_filter import REGIONS, matches_reply_filter, validate_reply_filter
    from comments import CommentStore, get_aweme_id_from_url
    from crawler import CommentResponseCollector, ResponseState
    from output import create_run_directory
    from video_links import parse_share_texts, resolve_share_rows


DEFAULT_XPATHS = {
    "comment_card_xpath": "//*[@data-e2e='comment-item'][.//*[@id='tooltip_{comment_id}']]",
    "reply_button_xpath": "",
    "reply_editor_xpath": "",
    "reply_send_xpath": "",
}
_PUBLISH_PATH = re.compile(r"/comment/(?:publish|post|create)/", re.IGNORECASE)


class CommentReplyWorkbench:
    """Run one video in its own tab and save only comments matching its filters."""

    def __init__(self, browser_session: Any, output_root: Path,
                 license_check: Callable[[], Any], settings_path: Path) -> None:
        """Keep the reply browser task separate from the ordinary capture queue."""
        self.browser_session = browser_session
        self.output_root = output_root
        self.license_check = license_check
        self.settings_path = settings_path
        self.lock = Lock()
        self.stop_event = Event()
        self.worker: Thread | None = None
        self.status = "idle"
        self.message = "等待开始"
        self.job_id = ""
        self.run_directory: Path | None = None
        self.video_url = ""
        self.settings: dict[str, Any] = {}
        self.xpaths = dict(DEFAULT_XPATHS)
        self.items: dict[str, dict[str, Any]] = {}
        self.waiting_comment_id = ""
        self.manual_action: tuple[str, str] | None = None
        self.scanned_ids: set[str] = set()
        self.revision = 0
        self.error = ""
        self.logs: list[str] = []
        try:
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.xpaths.update({key: str(saved[key]) for key in DEFAULT_XPATHS
                                    if isinstance(saved.get(key), str)})
        except (OSError, ValueError):
            pass

    def get_xpaths(self) -> dict[str, str]:
        """Return editable XPath settings without exposing browser credentials."""
        with self.lock:
            return dict(self.xpaths)

    def save_xpaths(self, payload: dict[str, Any]) -> dict[str, str]:
        """Persist all four locators atomically; no button click happens here."""
        if not isinstance(payload, dict):
            raise ValueError("XPath 配置格式无效")
        values = {}
        for key in DEFAULT_XPATHS:
            value = payload.get(key, "")
            if not isinstance(value, str) or len(value) > 1000:
                raise ValueError(f"{key} 须为不超过 1000 字符的 XPath")
            values[key] = value.strip()
        if values["comment_card_xpath"] and "{comment_id}" not in values["comment_card_xpath"]:
            raise ValueError("评论容器 XPath 须包含 {comment_id} 占位符")
        for key, label in (("reply_button_xpath", "回复按钮"),
                           ("reply_editor_xpath", "回复输入框"),
                           ("reply_send_xpath", "发送按钮")):
            if values[key] and not values[key].startswith(".//"):
                raise ValueError(f"{label} XPath 须以 .// 开头，相对于目标评论容器定位")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise RuntimeError("回复任务运行中不能修改 XPath")
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.settings_path)
            self.xpaths = values
            return dict(values)

    def start(self, payload: dict[str, Any], capture_active: bool) -> dict[str, Any]:
        """Keep the former single-video entry point for callers and isolated tests."""
        if capture_active:
            raise RuntimeError("请先结束普通评论采集，再启动独立评论回复任务")
        rows = parse_share_texts([payload.get("share_text", "")])
        region, keywords = validate_reply_filter(payload.get("region", ""),
                                                 payload.get("keywords", payload.get("keyword", "")))
        mode = payload.get("mode", "manual")
        if mode not in {"manual", "automatic"}:
            raise ValueError("回复模式无效")
        reply_text = str(payload.get("reply_text", "")).strip()
        if len(reply_text) > 500:
            raise ValueError("预设回复不能超过 500 个字符")
        if mode == "automatic" and not reply_text:
            raise ValueError("自动回复模式需要先填写回复内容")
        max_minutes = payload.get("max_minutes", 30)
        if type(max_minutes) is not int or not 1 <= max_minutes <= 240:
            raise ValueError("运行时长须为 1 至 240 分钟")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise RuntimeError("已有评论回复任务正在运行")
            missing = [key for key, value in self.xpaths.items() if not value]
            if missing:
                raise ValueError("请先在回复 XPath 配置中填写：" + "、".join(missing))
            xpaths = dict(self.xpaths)
        self.license_check()
        # 分享短链必须先解析成功，不能让无效输入创建空任务目录。
        resolved = self.browser_session.run(resolve_share_rows(rows), timeout=75)
        settings = {"region": region, "keywords": keywords, "mode": mode,
                    "reply_text": reply_text, "max_minutes": max_minutes,
                    "share_text": rows[0]["share_text"]}
        return self.start_resolved(resolved[0], settings, xpaths)

    def start_resolved(self, row: dict[str, Any], settings: dict[str, Any],
                       xpaths: dict[str, str], job_id: str = "") -> dict[str, Any]:
        """Create one durable output directory and launch one isolated browser page."""
        video_url = row["resolved_url"]
        aweme_id = get_aweme_id_from_url(video_url)
        if not aweme_id:
            raise ValueError("分享文本未解析出视频 ID")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise RuntimeError("已有评论回复任务正在运行")
            self.license_check()
            self.stop_event.clear()
            self.status = "starting"
            self.message = "正在打开视频"
            self.error = ""
            self.job_id = job_id or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            self.video_url = video_url
            self.settings = dict(settings)
            self.items = {}
            self.scanned_ids = set()
            self.revision = 0
            self.waiting_comment_id = ""
            self.manual_action = None
            self.logs = []
            self.run_directory = create_run_directory(self.output_root, aweme_id)
            self._log_locked("任务已建立，等待打开视频")
            self._save_locked()
            self.worker = Thread(target=self._run_worker, args=(xpaths,), daemon=True,
                                 name="douyin-comment-reply")
            self.worker.start()
            return {"job_id": self.job_id, "run_name": self.run_directory.name}

    def stop(self, job_id: str) -> None:
        """Request a graceful stop; never click a pending publish button."""
        with self.lock:
            if job_id != self.job_id or self.status not in {"starting", "running", "waiting_for_reply"}:
                raise RuntimeError("没有可停止的评论回复任务")
            self.stop_event.set()
            self.message = "正在停止回复任务"
            self.status = "stopping"
            self._log_locked(self.message)

    def manual_reply(self, job_id: str, comment_id: str, text: str = "", *, skip: bool = False) -> None:
        """Hand one confirmed draft to the waiting browser task or skip the comment."""
        value = str(text).strip()
        if not skip and (not value or len(value) > 500):
            raise ValueError("回复内容须为 1 至 500 个字符")
        with self.lock:
            if job_id != self.job_id or comment_id != self.waiting_comment_id:
                raise RuntimeError("待回复评论已变化，请刷新任务状态")
            if self.manual_action is not None:
                raise RuntimeError("本条回复已提交，不能重复发送")
            self.manual_action = ("skip" if skip else "send", value)

    def snapshot(self, offset: int = 0, since: int = 0) -> dict[str, Any]:
        """Return paged matches and the single item waiting for manual input."""
        if offset < 0 or since < 0:
            raise ValueError("无效的队列偏移量")
        with self.lock:
            items = list(self.items.values())
            waiting = self.items.get(self.waiting_comment_id)
            changes = sorted(
                (dict(item) for item in items if item.get("revision", 0) > since),
                key=lambda item: item["revision"],
            )[:200]
            return {"job_id": self.job_id, "status": self.status, "message": self.message,
                    "video_url": self.video_url, "run_name": self.run_directory.name if self.run_directory else "",
                    "settings": dict(self.settings), "waiting": dict(waiting) if waiting else None,
                    "offset": offset, "total": len(items),
                    "items": [dict(item) for item in items[offset:offset + 200]],
                    "changes": changes,
                    "next_revision": changes[-1]["revision"] if changes else since,
                    "revision": self.revision,
                    "error": self.error, "regions": list(REGIONS), "logs": list(self.logs)}

    def progress_snapshot(self) -> dict[str, Any]:
        """Expose compact queue state without copying all matched comments each poll."""
        with self.lock:
            waiting = self.items.get(self.waiting_comment_id)
            return {"status": self.status, "message": self.message,
                    "run_name": self.run_directory.name if self.run_directory else "",
                    "total": len(self.items), "waiting": dict(waiting) if waiting else None,
                    "error": self.error, "logs": list(self.logs)}

    def _observe(self, comment: dict[str, Any]) -> None:
        """Retain only matched API comments; the full comment stream stays in memory."""
        cid = str(comment.get("id", ""))
        if not re.fullmatch(r"\d{1,30}", cid):
            return
        with self.lock:
            if cid in self.items or not matches_reply_filter(
                comment, self.settings["region"], self.settings["keywords"]
            ):
                return
            self.revision += 1
            self.items[cid] = {"comment": comment, "comment_id": cid,
                               "reply_text": "", "status": "matched", "error": "",
                               "matched_at": datetime.now(timezone.utc).isoformat(),
                               "sent_at": "", "revision": self.revision}
            self._log_locked(f"匹配评论 {cid}：{comment.get('author', {}).get('nickname', '匿名用户')}")

    def _log_locked(self, message: str) -> None:
        """Keep recent UI lines and append a full per-video log beside replies.json."""
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        self.logs.append(line)
        if len(self.logs) > 500:
            del self.logs[:-500]
        if self.run_directory is not None:
            with (self.run_directory / "reply.log").open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    def _save_locked(self) -> None:
        """Atomically save only matching comments and their reply outcomes."""
        if self.run_directory is None:
            return
        document = {"job_id": self.job_id, "status": self.status,
                    "video_url": self.video_url, "filters": dict(self.settings),
                    "message": self.message, "error": self.error,
                    "matches": list(self.items.values())}
        path = self.run_directory / "replies.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _set_status(self, status: str, message: str) -> None:
        """Update page-visible state and durable output in one locked step."""
        with self.lock:
            self.status, self.message = status, message
            self._log_locked(message)
            self._save_locked()

    def _run_worker(self, xpaths: dict[str, str]) -> None:
        """Keep browser operations on its dedicated event loop and preserve failures."""
        try:
            self.browser_session.run(self._run_async(xpaths))
        except Exception as error:
            with self.lock:
                self.error = str(error)
                self.status = "stopped" if self.stop_event.is_set() else "failed"
                self.message = "任务已停止" if self.stop_event.is_set() else f"回复任务失败：{error}"
                self._log_locked(self.message)
                self._save_locked()

    async def _run_async(self, xpaths: dict[str, str]) -> None:
        """Scroll the independent video page, match API comments, then reply via XPath."""
        context = await self.browser_session.get_context()
        page = await context.new_page()
        aweme_id = get_aweme_id_from_url(self.video_url)
        store = CommentStore(on_added=self._observe)
        response_state = ResponseState()
        collector = CommentResponseCollector(page, store, response_state, aweme_id)
        collector.attach()
        started = asyncio.get_running_loop().time()
        idle = 0
        last_save = started
        try:
            await page.goto(self.video_url, wait_until="domcontentloaded", timeout=45_000)
            await page.locator('[data-e2e="comment-list"]').wait_for(state="attached", timeout=30_000)
            self._set_status("running", "正在滚动评论区并筛选")
            while not self.stop_event.is_set():
                if asyncio.get_running_loop().time() - started >= self.settings["max_minutes"] * 60:
                    self._set_status("complete", "达到设定运行时长")
                    break
                await collector.drain()
                discovered = await self._scan_visible(page, store, xpaths)
                if self.stop_event.is_set():
                    break
                previous_total = len(store)
                scroll = await self._scroll_comments(page)
                await asyncio.sleep(0.8)
                await collector.drain()
                if get_aweme_id_from_url(page.url) != aweme_id:
                    raise RuntimeError("页面切换到了其他视频，已停止回复，防止回复错视频")
                if discovered or len(store) > previous_total:
                    idle = 0
                else:
                    idle += 1
                if asyncio.get_running_loop().time() - last_save >= 3:
                    with self.lock:
                        self._save_locked()
                    last_save = asyncio.get_running_loop().time()
                if scroll["at_end"] and idle >= 8:
                    if response_state.top_level_response_count == 0:
                        raise RuntimeError("未捕获到评论列表响应，不能认定评论已加载完整")
                    if response_state.has_more is True:
                        raise RuntimeError("评论接口仍显示还有更多，但页面已无法继续滚动；结果不完整")
                    self._set_status("complete", "评论区连续多轮无新增，回复任务完成")
                    break
            collector.detach()
            await collector.drain()
            if self.stop_event.is_set():
                self._set_status("stopped", "回复任务已停止")
            with self.lock:
                for item in self.items.values():
                    if item["status"] == "matched":
                        item["status"] = "stopped" if self.stop_event.is_set() else "not_found_on_page"
                        item["error"] = ("任务停止，未发送" if self.stop_event.is_set()
                                         else "接口已捕获评论，但未在页面找到其评论 ID，未发送")
                        self.revision += 1
                        item["revision"] = self.revision
                self._save_locked()
        finally:
            collector.detach()
            await collector.drain()
            await page.close()

    async def _scan_visible(self, page: Any, store: CommentStore,
                            xpaths: dict[str, str]) -> int:
        """Process DOM cards by comment ID, verifying author sec_uid before any click."""
        rows = await page.evaluate("""() => [...document.querySelectorAll('[data-e2e="comment-item"]')]
            .map(card => {
              const id = card.querySelector('[data-e2e="video-comment-more"] [id^="tooltip_"]')?.id || '';
              const author = card.querySelector('a[href*="/user/"]')?.getAttribute('href') || '';
              return {comment_id: id.startsWith('tooltip_') ? id.slice(8) : '', author};
            }).filter(row => row.comment_id)""")
        new_count = 0
        for row in rows:
            cid = row["comment_id"]
            if cid in self.scanned_ids:
                continue
            comment = store.get(cid)
            if comment is None:
                continue
            self.scanned_ids.add(cid)
            new_count += 1
            with self.lock:
                matched = cid in self.items
            if not matched:
                continue
            sec_uid = str(comment.get("author", {}).get("sec_uid", ""))
            # Protocol-relative links need a scheme to parse a real /user/<sec_uid> path.
            author_url = "https:" + row["author"] if row["author"].startswith("//") else row["author"]
            author_path = urlparse(author_url).path
            if not sec_uid or unquote(author_path).rstrip("/") != f"/user/{sec_uid}":
                self._update_item(cid, "identity_unverified", "页面作者 sec_uid 与接口不一致，未发送")
                continue
            await self._handle_match(page, cid, xpaths)
            if self.stop_event.is_set():
                break
        return new_count

    async def _scroll_comments(self, page: Any) -> dict[str, Any]:
        """Move the actual comment scroller without wheel events that switch videos."""
        result = await page.evaluate("""() => {
          const list = document.querySelector('[data-e2e="comment-list"]');
          if (!list) return {found:false};
          let node = list;
          while (node && node !== document.body && node !== document.documentElement) {
            const style = getComputedStyle(node);
            if (/(auto|scroll|overlay)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 2) {
              const before = node.scrollTop;
              node.scrollTop += Math.max(450, Math.round(node.clientHeight * .7));
              return {found:true, before, after:node.scrollTop,
                      at_end:node.scrollTop + node.clientHeight >= node.scrollHeight - 4};
            }
            node = node.parentElement;
          }
          const before = window.scrollY;
          window.scrollBy(0, Math.max(450, Math.round(innerHeight * .7)));
          return {found:true, before, after:window.scrollY,
                  at_end:window.scrollY + innerHeight >= document.documentElement.scrollHeight - 4};
        }""")
        if not result.get("found"):
            raise RuntimeError("未找到评论列表，停止滚动以避免切换视频")
        return result

    def _update_item(self, cid: str, status: str, error: str = "", reply_text: str = "") -> None:
        """Persist a single matched comment's status without touching unrelated data."""
        with self.lock:
            item = self.items[cid]
            item["status"], item["error"] = status, error
            self._log_locked(f"评论 {cid}：{status}" + (f"，{error}" if error else ""))
            self.revision += 1
            item["revision"] = self.revision
            if reply_text:
                item["reply_text"] = reply_text
            if status == "sent":
                item["sent_at"] = datetime.now(timezone.utc).isoformat()
            self._save_locked()

    async def _handle_match(self, page: Any, cid: str, xpaths: dict[str, str]) -> None:
        """Wait for a manual draft or use the preset, then issue one XPath-based send."""
        if self.settings["mode"] == "manual":
            with self.lock:
                self.waiting_comment_id = cid
                self.manual_action = None
                self.items[cid]["status"] = "waiting_for_reply"
                self.revision += 1
                self.items[cid]["revision"] = self.revision
                self.status = "waiting_for_reply"
                self.message = "等待输入回复内容"
                self._log_locked(f"等待人工回复评论 {cid}")
                self._save_locked()
            while not self.stop_event.is_set():
                await asyncio.sleep(0.25)
                with self.lock:
                    action = self.manual_action
                if action is not None:
                    break
            with self.lock:
                self.waiting_comment_id = ""
                self.manual_action = None
                if not self.stop_event.is_set():
                    self.status = "running"
                    self.message = "正在滚动评论区并筛选"
            if self.stop_event.is_set():
                self._update_item(cid, "stopped", "任务停止，未发送")
                return
            if action[0] == "skip":
                self._update_item(cid, "skipped", "用户跳过")
                return
            reply_text = action[1]
        else:
            reply_text = self.settings["reply_text"]
        try:
            self.license_check()
            await self._publish_once(page, cid, reply_text, xpaths)
        except Exception as error:
            # 发布按钮一旦有可能被点击，就不做重试；人工核查后再决定后续操作。
            with self.lock:
                clicked_or_sending = self.items[cid]["status"] == "sending"
            self._update_item(cid, "uncertain" if clicked_or_sending else "failed", str(error), reply_text)

    async def _publish_once(self, page: Any, cid: str, text: str,
                            xpaths: dict[str, str]) -> None:
        """Click one exact card, fill DraftJS, submit once and verify a publish response."""
        card_xpath = xpaths["comment_card_xpath"].replace("{comment_id}", cid)
        card = await self._unique_visible(page.locator("xpath=" + card_xpath), "目标评论")
        # 配置 XPath 可能过宽或页面已重绘；真正点击前再核对卡片的两个身份字段。
        identity = await card.evaluate("""card => {
          const tooltip = card.querySelector('[data-e2e="video-comment-more"] [id^="tooltip_"]');
          const author = card.querySelector('a[href*="/user/"]');
          return {comment_id: tooltip?.id?.slice(8) || '',
                  author: author?.getAttribute('href') || ''};
        }""")
        with self.lock:
            expected_sec_uid = str(self.items[cid]["comment"].get("author", {}).get("sec_uid", ""))
        self._verify_card_identity(cid, expected_sec_uid, identity)
        button = await self._unique_visible(card.locator("xpath=" + xpaths["reply_button_xpath"]), "回复按钮")
        await button.click(timeout=10_000)
        editor = await self._unique_visible(card.locator("xpath=" + xpaths["reply_editor_xpath"]), "回复输入框")
        await editor.click(timeout=10_000)
        await page.keyboard.insert_text(text)
        entered = (await editor.inner_text(timeout=10_000)).strip()
        if entered != text:
            raise RuntimeError("输入框内容与待发送文字不一致，未点击发布")
        send = await self._unique_visible(card.locator("xpath=" + xpaths["reply_send_xpath"]), "发送按钮")
        if not await send.is_enabled():
            raise RuntimeError("发送按钮不可用，未点击发布")
        self._update_item(cid, "sending", reply_text=text)
        loop = asyncio.get_running_loop()
        response_future: asyncio.Future[Any] = loop.create_future()

        def on_response(response: Any) -> None:
            """Accept only a POST to a comment publishing path, not unrelated traffic."""
            if (not response_future.done() and response.request.method == "POST"
                    and _PUBLISH_PATH.search(urlparse(response.url).path)):
                response_future.set_result(response)

        page.on("response", on_response)
        try:
            await send.click(timeout=10_000)
            try:
                response = await asyncio.wait_for(response_future, timeout=10)
            except TimeoutError as error:
                raise RuntimeError("点击发布后未捕获到可验证的评论发布响应；结果不确定，不会重试") from error
            try:
                payload = await response.json()
            except Exception as error:
                raise RuntimeError("发布响应无法解析；结果不确定，不会重试") from error
            if response.status >= 400 or not isinstance(payload, dict):
                raise RuntimeError(f"发布接口 HTTP {response.status}；不会重试")
            if payload.get("status_code") not in (0, "0"):
                raise RuntimeError(f"发布接口状态 {payload.get('status_code')}；不会重试")
            reply = payload.get("comment") or payload.get("data")
            if not isinstance(reply, dict) or not (reply.get("cid") or reply.get("comment_id")):
                raise RuntimeError("接口未返回新评论 ID，无法确认发布结果；不会重试")
            self._update_item(cid, "sent", reply_text=text)
        finally:
            page.remove_listener("response", on_response)
            response_future.cancel()

    @staticmethod
    def _verify_card_identity(cid: str, sec_uid: str, identity: dict[str, str]) -> None:
        """Reject an XPath match whose comment ID or author changed before clicking."""
        author_url = identity.get("author", "")
        if author_url.startswith("//"):
            author_url = "https:" + author_url
        author_path = unquote(urlparse(author_url).path).rstrip("/")
        if identity.get("comment_id") != cid or not sec_uid or author_path != f"/user/{sec_uid}":
            raise RuntimeError("目标评论卡片的评论 ID 或作者 sec_uid 不匹配，未点击回复")

    @staticmethod
    async def _unique_visible(locator: Any, label: str) -> Any:
        """Reject hidden duplicates instead of clicking the first ambiguous match."""
        await locator.first.wait_for(state="attached", timeout=10_000)
        visible = [locator.nth(index) for index in range(await locator.count())
                   if await locator.nth(index).is_visible()]
        if len(visible) != 1:
            raise RuntimeError(f"{label}可见匹配 {len(visible)} 个，需要唯一目标")
        return visible[0]


class CommentReplyCoordinator:
    """Validate a batch, then run at most two independent video reply pages at once."""

    def __init__(self, browser_session: Any, output_root: Path,
                 license_check: Callable[[], Any], settings_path: Path) -> None:
        """Share XPath configuration while keeping every video's state and output separate."""
        self.browser_session = browser_session
        self.output_root = output_root
        self.license_check = license_check
        self.settings_path = settings_path
        self.config = CommentReplyWorkbench(browser_session, output_root, license_check, settings_path)
        self.lock = Lock()
        self.worker: Thread | None = None
        self.starting = False
        self.batch_id = ""
        self.entries: list[dict[str, Any]] = []
        self.region = ""
        self.keywords: list[str] = []
        self.max_minutes = 30
        self.source_mode = "page_first"

    def get_xpaths(self) -> dict[str, str]:
        """Read the shared reply locators for the advanced configuration panel."""
        return self.config.get_xpaths()

    def save_xpaths(self, payload: dict[str, Any]) -> dict[str, str]:
        """Reject configuration changes while any queued or running video may use it."""
        with self.lock:
            if self.starting or (self.worker and self.worker.is_alive()):
                raise RuntimeError("回复任务运行中不能修改 XPath")
        return self.config.save_xpaths(payload)

    def start(self, payload: dict[str, Any], capture_active: bool) -> dict[str, Any]:
        """Validate all share texts and per-video choices before creating any task."""
        if capture_active:
            raise RuntimeError("请先结束普通评论采集，再启动独立评论回复任务")
        videos = payload.get("videos")
        if not isinstance(videos, list) or not videos or len(videos) > 100:
            raise ValueError("请填写 1 至 100 条视频分享文本")
        with self.lock:
            if self.starting or (self.worker and self.worker.is_alive()):
                raise RuntimeError("已有评论回复任务正在运行")
            self.starting = True
        try:
            rows = parse_share_texts([video.get("share_text", "") if isinstance(video, dict) else ""
                                     for video in videos])
            region, keywords = validate_reply_filter(payload.get("region", ""),
                                                     payload.get("keywords", payload.get("keyword", "")))
            max_minutes = payload.get("max_minutes", 30)
            if type(max_minutes) is not int or not 1 <= max_minutes <= 240:
                raise ValueError("运行时长须为 1 至 240 分钟")
            source_mode = payload.get("source_mode", "page_first")
            if source_mode != "page_first":
                raise ValueError("接口优先尚未开放；请选择页面元素优先")
            settings_by_row = {}
            for row in rows:
                option = videos[row["row"] - 1]
                mode = option.get("mode", "manual")
                reply_text = option.get("reply_text", "")
                if mode not in {"manual", "automatic"} or not isinstance(reply_text, str):
                    raise ValueError(f"第 {row['row']} 个视频的回复设置无效")
                reply_text = reply_text.strip()
                if len(reply_text) > 500 or (mode == "automatic" and not reply_text):
                    raise ValueError(f"第 {row['row']} 个视频的预设回复须为 1 至 500 字")
                settings_by_row[row["row"]] = {"region": region, "keywords": keywords,
                                               "mode": mode, "reply_text": reply_text,
                                               "max_minutes": max_minutes,
                                               "source_mode": source_mode,
                                               "share_text": row["share_text"]}
            xpaths = self.get_xpaths()
            missing = [key for key, value in xpaths.items() if not value]
            if missing:
                raise ValueError("请先在回复 XPath 配置中填写：" + "、".join(missing))
            self.license_check()
            timeout = 45 + 30 * ((len(rows) + 2) // 3)
            resolved = self.browser_session.run(resolve_share_rows(rows), timeout=timeout)
            batch_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            entries = [{"job_id": f"{batch_id}-{index + 1}", "row": row,
                        "settings": settings_by_row[row["row"]], "status": "pending",
                        "message": "等待运行", "job": None}
                       for index, row in enumerate(resolved)]
            self.license_check()
            with self.lock:
                self.batch_id = batch_id
                self.entries = entries
                self.region, self.keywords, self.max_minutes = region, keywords, max_minutes
                self.source_mode = source_mode
                self.worker = Thread(target=self._schedule, args=(xpaths,), daemon=True,
                                     name="douyin-reply-batch")
                self.worker.start()
            return {"batch_id": batch_id, "count": len(entries)}
        finally:
            with self.lock:
                self.starting = False

    def _schedule(self, xpaths: dict[str, str]) -> None:
        """Dispatch up to two browser pages; finished pages release slots for queued videos."""
        while True:
            with self.lock:
                active = [item for item in self.entries if item["job"] is not None
                          and item["job"].worker is not None and item["job"].worker.is_alive()]
                pending = [item for item in self.entries if item["status"] == "pending"]
                if not active and not pending:
                    return
                available = max(0, 2 - len(active))
                to_start = pending[:available]
                for item in to_start:
                    item["status"] = "starting"
            for item in to_start:
                try:
                    with self.lock:
                        if item["status"] != "starting":
                            continue
                    self.license_check()
                    job = CommentReplyWorkbench(self.browser_session, self.output_root,
                                                self.license_check, self.settings_path)
                    job.start_resolved(item["row"], item["settings"], xpaths, item["job_id"])
                    with self.lock:
                        item["job"] = job
                        stopped_while_launching = item["status"] == "stopped"
                        item["status"] = "running" if not stopped_while_launching else "stopped"
                    if stopped_while_launching:
                        job.stop(item["job_id"])
                except Exception as error:
                    with self.lock:
                        item["status"] = "failed"
                        item["message"] = f"启动失败：{error}"
            time.sleep(0.25)

    def snapshot(self, offset: int = 0, since: int = 0) -> dict[str, Any]:
        """Return independent per-video logs and the first pending manual reply."""
        with self.lock:
            entries = list(self.entries)
            batch_id = self.batch_id
            starting = self.starting
            region, keywords, max_minutes = self.region, list(self.keywords), self.max_minutes
            source_mode = self.source_mode
            worker_alive = self.worker is not None and self.worker.is_alive()
        tasks = []
        waiting = None
        for item in entries:
            job = item["job"]
            state = job.progress_snapshot() if job is not None else {}
            task = {"job_id": item["job_id"], "status": state.get("status", item["status"]),
                    "message": state.get("message", item["message"]),
                    "video_url": item["row"]["resolved_url"],
                    "share_text": item["settings"]["share_text"],
                    "mode": item["settings"]["mode"],
                    "reply_text": item["settings"]["reply_text"],
                    "run_name": state.get("run_name", ""), "total": state.get("total", 0),
                    "logs": state.get("logs", []), "error": state.get("error", "")}
            tasks.append(task)
            if waiting is None and state.get("waiting"):
                waiting = {**state["waiting"], "job_id": item["job_id"]}
        return {"batch_id": batch_id, "status": "starting" if starting else
                "running" if worker_alive else "complete" if entries else "idle",
                "tasks": tasks, "waiting": waiting, "regions": list(REGIONS),
                "settings": {"region": region, "keywords": keywords, "max_minutes": max_minutes,
                             "source_mode": source_mode}}

    def stop(self, job_id: str) -> None:
        """Stop one video by ID, or the full batch when its batch ID is supplied."""
        with self.lock:
            targets = list(self.entries) if job_id == self.batch_id else [item for item in self.entries
                                                                            if item["job_id"] == job_id]
            if not targets:
                raise RuntimeError("没有可停止的评论回复任务")
            for item in targets:
                if item["job"] is None and item["status"] in {"pending", "starting"}:
                    item["status"], item["message"] = "stopped", "任务已停止"
                elif item["job"] is not None:
                    state = item["job"].snapshot()
                    if state["status"] in {"starting", "running", "waiting_for_reply"}:
                        item["job"].stop(item["job_id"])

    def manual_reply(self, job_id: str, comment_id: str, text: str = "", *, skip: bool = False) -> None:
        """Route one explicit manual decision to exactly its video's worker."""
        with self.lock:
            job = next((item["job"] for item in self.entries if item["job_id"] == job_id), None)
        if job is None:
            raise RuntimeError("目标回复任务不存在或尚未启动")
        job.manual_reply(job_id, comment_id, text, skip=skip)
