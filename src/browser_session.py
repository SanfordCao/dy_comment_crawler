"""程序内共享当前使用者的 Chrome 登录会话；不读取或分发登录凭据。"""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Coroutine

from playwright.async_api import async_playwright


class BrowserSession:
    """让采集与私聊在同一事件循环使用一个浏览器，避免抢占用户目录。"""

    def __init__(self, profile_dir: Path) -> None:
        """保存用户目录，延迟启动线程和浏览器，空闲打开控制台不启动 Chrome。"""
        self.profile_dir = profile_dir
        self.loop = asyncio.new_event_loop()
        self._ready = Event()
        self._thread_lock = Lock()
        self._thread: Thread | None = None
        self._context = None
        self._playwright = None
        self._launch_lock = asyncio.Lock()

    def _serve(self) -> None:
        """浏览器专用线程保持事件循环运行，任务结束不关闭人工私聊标签页。"""
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()
        self.loop.close()

    def run(self, coroutine: Coroutine[Any, Any, Any], timeout: float | None = None) -> Any:
        """从控制台线程提交浏览器操作；超时取消操作，不持续点击或等待。"""
        with self._thread_lock:
            if self.loop.is_closed():
                coroutine.close()
                raise RuntimeError("浏览器服务已关闭，请重启程序")
            if self._thread is None:
                self._thread = Thread(target=self._serve, daemon=True, name="douyin-browser")
                self._thread.start()
        self._ready.wait()
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout)
        except FutureTimeout as error:
            future.cancel()
            raise RuntimeError("浏览器操作超时，请检查登录、验证或页面加载状态") from error

    async def get_context(self) -> Any:
        """仅在浏览器线程调用；复用当前登录会话，关闭后下次人工操作重新启动。"""
        async with self._launch_lock:
            if self._context is not None:
                return self._context
            if self._playwright is not None:
                await self._playwright.stop()
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._playwright = await async_playwright().start()
            try:
                context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir), channel="chrome", headless=False,
                    # 不模拟固定分辨率，让最大化、缩放和不同屏幕自然改变页面视口。
                    no_viewport=True, timeout=30000)
            except Exception as error:
                raise RuntimeError("无法打开采集浏览器，请确认已安装 Chrome，且用户目录未被其他程序占用") from error
            self._context = context
            context.on("close", lambda _: self._clear_context(context))
            return context

    def _clear_context(self, context: Any) -> None:
        """忽略旧浏览器的关闭事件，不清理后来新启动的会话。"""
        if self._context is context:
            self._context = None

    async def _close(self) -> None:
        """程序退出时关闭共享浏览器，取消剩余操作并等待资源清理。"""
        if self._context is not None:
            await self._context.close()
        if self._playwright is not None:
            await self._playwright.stop()
        pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def close(self) -> None:
        """停止服务；只在程序退出时调用，不作为停止采集的动作。"""
        if self._thread is None:
            self.loop.close()
            return
        try:
            self.run(self._close(), timeout=10)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout=5)
