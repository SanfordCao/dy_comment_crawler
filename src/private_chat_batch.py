"""用户明确确认后的批量私信：按账号去重、串行执行、不自动补发。"""

from __future__ import annotations

import asyncio
import re
from threading import Event, Lock, Thread
from typing import Any, Callable
from uuid import uuid4

try:
    from .private_chat import open_user_chat, send_user_message, validated_message, validated_sec_uid
except ImportError:
    from private_chat import open_user_chat, send_user_message, validated_message, validated_sec_uid


class PrivateChatBatch:
    """管理一个内存批次，与单条私信共享互斥锁，不影响评论采集调度。"""

    def __init__(self, browser_session: Any, chat_lock: Any,
                 private_chat_xpath: Callable[[], str] | None = None,
                 send_message_xpath: Callable[[], str] | None = None,
                 close_chat_panel_xpath: Callable[[], str] | None = None) -> None:
        """初始化状态、停止信号和请求去重；不启动浏览器或发送消息。"""
        self.browser_session = browser_session
        self.chat_lock = chat_lock
        # 使用全局配置读取函数，避免批量私信另存一份 XPath 导致配置不一致。
        self.private_chat_xpath = private_chat_xpath or (lambda: "")
        self.send_message_xpath = send_message_xpath or (lambda: "")
        self.close_chat_panel_xpath = close_chat_panel_xpath or (lambda: "")
        self.lock = Lock()
        self.stop_event = Event()
        self.worker: Thread | None = None
        self.job: dict[str, Any] | None = None
        self.requests: dict[str, str] = {}

    def _snapshot_locked(self) -> dict[str, Any]:
        """复制展示数据，不暴露消息内容或内部对象引用。"""
        if self.job is None:
            return {"status": "idle", "items": []}
        return {**self.job, "items": [dict(item) for item in self.job["items"]]}

    def snapshot(self) -> dict[str, Any]:
        """供页面轮询批次进度，刷新不会重新发送。"""
        with self.lock:
            return self._snapshot_locked()

    def start(self, authors: list[dict[str, Any]], message: Any, request_id: Any) -> dict[str, Any]:
        """验证收件人及内容，取得串行锁后只启动一次后台执行。"""
        message = validated_message(message)
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", request_id):
            raise ValueError("批量请求标识无效，请重新打开批量私信面板")
        targets: dict[str, dict[str, Any]] = {}
        for author in authors:
            targets.setdefault(validated_sec_uid(author.get("sec_uid")), dict(author))
        if not targets:
            raise ValueError("请至少选择一位用户")
        with self.lock:
            if request_id in self.requests:
                if self.job and self.job["id"] == self.requests[request_id]:
                    return self._snapshot_locked()
                raise RuntimeError("该批量请求已处理，不能再次发送")
            if len(self.requests) >= 1000:
                raise RuntimeError("本次程序会话已达到批次数限制，请先核对结果")
            if not self.chat_lock.acquire(blocking=False):
                raise RuntimeError("正在处理其他私信操作，请稍后再试")
            self.stop_event = Event()
            job_id = uuid4().hex
            self.requests[request_id] = job_id
            self.job = {"id": job_id, "request_id": request_id, "status": "running", "message": "正在逐个发送",
                        "items": [{"sec_uid": uid, "nickname": str(author.get("nickname") or "匿名用户"),
                                   "unique_id": str(author.get("unique_id") or ""), "status": "pending", "detail": "待发送"}
                                  for uid, author in targets.items()]}
            self.worker = Thread(target=self._run, args=(list(targets.values()), message),
                                 daemon=True, name="douyin-private-chat-batch")
            try:
                self.worker.start()
            except Exception as error:
                self.job["status"] = "failed"
                self.job["message"] = "未能启动批量任务，未发送消息"
                self.chat_lock.release()
                raise RuntimeError(self.job["message"]) from error
            return self._snapshot_locked()

    def stop(self, job_id: str | None = None) -> None:
        """停止后续派发；已点击发送的当前消息不能安全撤销或补发。"""
        with self.lock:
            if job_id is not None and (not self.job or self.job["id"] != job_id):
                raise ValueError("批量任务已变化，请刷新进度")
            if self.job and self.job["status"] in {"running", "stopping"}:
                self.stop_event.set()
                self.job["status"] = "stopping"
                self.job["message"] = "正在停止；当前操作结束后不再发送下一位"

    async def _send_one(self, author: dict[str, Any], message: str) -> dict[str, Any]:
        """打开并核验单个会话，在停止信号到达时禁止新的发送动作。"""
        context = await self.browser_session.get_context()
        ready: dict[str, Any] = {}

        def remember(page: Any, sec_uid: str) -> None:
            """只接收经过会话身份核验的页面，昵称不参与身份比较。"""
            ready.update(page=page, sec_uid=sec_uid)

        await open_user_chat(context, author, remember,
                             private_chat_xpath=self.private_chat_xpath(),
                             close_chat_panel_xpath=self.close_chat_panel_xpath())
        if self.stop_event.is_set():
            return {"status": "cancelled", "message": "已停止，未发送"}
        if not ready:
            raise RuntimeError("未能核对聊天对象 sec_uid，已停止后续发送")
        return await send_user_message(ready["page"], ready["sec_uid"], message,
                                       send_message_xpath=self.send_message_xpath())

    def _run(self, authors: list[dict[str, Any]], message: str) -> None:
        """逐位发送相同内容；单人失败或结果不确定时记录原因并继续下一位。"""
        terminal = "completed"
        try:
            for index, author in enumerate(authors):
                if self.stop_event.is_set():
                    terminal = "stopped"
                    break
                with self.lock:
                    self.job["items"][index].update(status="running", detail="正在打开并核对会话")
                try:
                    result = self.browser_session.run(self._send_one(author, message), timeout=100)
                    with self.lock:
                        # 保存提交确认信息，让页面区分普通提交与需要人工核对的结果。
                        self.job["items"][index].update(status=result["status"], detail=result["message"],
                                                       page_closed=result.get("page_closed"))
                    if result["status"] == "cancelled":
                        terminal = "stopped"
                        break
                    # 已点击发送但结果未确认时保留页面供人工核对，不对该用户补发。
                    # 下一位会新开主页并重新核对 sec_uid，因此可以继续处理。
                except Exception as error:
                    detail = str(error)
                    # 单人失败不影响队列；已知权限限制归类为跳过，其余保留异常供核对。
                    # 无论发送按钮是否曾被点击，都不对同一用户自动重试。
                    skip = any(text in detail for text in ("该账号为私密账号", "该用户当前无法私信", "该用户的私信入口不可用"))
                    with self.lock:
                        self.job["items"][index].update(status="skipped" if skip else "failed", detail=detail)
                # 不额外等待固定间隔；下一轮仍先检查停止信号，再串行处理下一位。
        finally:
            with self.lock:
                for item in self.job["items"]:
                    if item["status"] in {"pending", "running"}:
                        item.update(status="cancelled", detail="未继续发送")
                self.job["status"] = terminal
                needs_review = any(item["status"] in {"failed", "skipped"}
                                   or item.get("page_closed") is False for item in self.job["items"])
                self.job["message"] = ("已停止，剩余用户未发送" if terminal == "stopped" else
                                       "批量操作已结束；失败用户已跳过，结果不确定的用户请人工核对，不会自动重发"
                                       if needs_review else "批量操作已结束，请以抖音实际发送记录为准")
            self.chat_lock.release()
