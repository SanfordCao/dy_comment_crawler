"""从配置指定的响应接口提取当前视频信息，不依赖页面 XPath。"""

from __future__ import annotations

import asyncio
import re
from contextvars import copy_context
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

METADATA_DEFAULTS = {
    "author_api_path": "/aweme/v1/web/aweme/detail/",
    "author_json_path": "aweme_detail.author.nickname",
    "title_api_path": "/aweme/v1/web/aweme/detail/",
    "title_json_path": "aweme_detail.item_title",
}


def validate_metadata_settings(payload: dict[str, Any]) -> dict[str, str]:
    """验证路径格式，空值代表停用该字段；不执行配置中的代码。"""
    settings = {}
    for key, default in METADATA_DEFAULTS.items():
        value = payload.get(key, default)
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError(f"{key} 必须是 4096 字符以内的路径文本")
        value = value.strip()
        if value and key.endswith("api_path"):
            if not value.startswith("/") or any(char in value for char in "?#\r\n"):
                raise ValueError(f"{key} 请填写以 / 开头的接口关键路径，不含域名或查询参数")
        elif value and not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", value):
            raise ValueError(f"{key} 请填写点分 JSON 路径，例如 aweme_detail.author.nickname")
        settings[key] = value
    return settings


def read_json_path(payload: Any, path: str) -> str:
    """逐段读取对象键或数组索引，只接受非空字符串，防止对象被误保存为标题。"""
    if not path:
        return ""
    value = payload
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            return ""
    return value.strip() if isinstance(value, str) else ""


class VideoMetadataCollector:
    """每个视频页独立监听，先过滤接口路径，再解码 JSON 并核对目标视频 ID。"""

    def __init__(self, page: Any, aweme_id: str, settings: dict[str, str],
                 result: dict[str, Any], warnings: set[str]) -> None:
        """保存本任务输出引用，晚到的有效响应也能更新后续检查点。"""
        self.page, self.aweme_id = page, aweme_id
        self.settings = validate_metadata_settings(settings)
        self.result, self.warnings = result, warnings
        self.pending: set[asyncio.Task] = set()
        self._log_context = copy_context()
        self._listener = self._schedule

    def attach(self) -> None:
        """导航前注册，避免遗漏打开视频时立即返回的详情响应。"""
        self.page.on("response", self._listener)
        for prefix, label in (("author", "作者昵称"), ("title", "视频标题")):
            print(f"[视频接口配置] {label}：接口={self.settings[prefix + '_api_path'] or '未配置'}；"
                  f"JSON路径={self.settings[prefix + '_json_path'] or '未配置'}")

    def _schedule(self, response: Any) -> None:
        """非目标接口不创建任务、不读取响应体，同一响应只解码一次。"""
        path = urlsplit(response.url).path
        prefixes = [prefix for prefix in ("author", "title")
                    if self.settings[prefix + "_api_path"]
                    and self.settings[prefix + "_api_path"] in path
                    and self.settings[prefix + "_json_path"]]
        if not prefixes:
            return
        task = self._log_context.run(asyncio.create_task, self._handle(response, prefixes))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def _handle(self, response: Any, prefixes: list[str]) -> None:
        """响应必须明确对应当前视频；不从相关视频列表猜测、不回退 HTML。"""
        try:
            if response.status != 200:
                self.warnings.add(f"视频信息接口返回 HTTP {response.status}")
                return
            payload = await response.json()
            if not isinstance(payload, dict):
                return
            if payload.get("status_code", 0) not in (0, "0"):
                self.warnings.add("视频信息接口返回非成功状态")
                return
            detail = payload.get("aweme_detail")
            target = detail if isinstance(detail, dict) else payload
            payload_id = str(target.get("aweme_id") or target.get("awemeId") or "")
            query_ids = parse_qs(urlsplit(response.url).query).get("aweme_id", [])
            if payload_id != self.aweme_id or any(value != self.aweme_id for value in query_ids):
                return
            for prefix in prefixes:
                field = "video_publisher_nickname" if prefix == "author" else "video_title"
                label = "作者昵称" if prefix == "author" else "视频标题"
                value = read_json_path(payload, self.settings[prefix + "_json_path"])
                if not value:
                    self.warnings.add(f"{label} JSON路径未取得文字：{self.settings[prefix + '_json_path']}")
                    continue
                if self.result.get(field) != value:
                    self.result[field] = value
                    print(f"[视频信息接口] {label}：{value}")
        except Exception as error:
            self.warnings.add(f"视频信息响应解析失败：{type(error).__name__}")

    async def wait(self, timeout_ms: int, stop_requested: Callable[[], bool] | None = None) -> None:
        """有限等待已配置字段；没有响应时继续采集，避免缺失信息让任务卡住。"""
        fields = [(prefix, field) for prefix, field in
                  (("author", "video_publisher_nickname"), ("title", "video_title"))
                  if self.settings[prefix + "_api_path"] and self.settings[prefix + "_json_path"]]
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while any(not self.result.get(field) for _, field in fields):
            if stop_requested and stop_requested():
                return
            if asyncio.get_running_loop().time() >= deadline:
                for prefix, field in fields:
                    if not self.result.get(field):
                        label = "作者昵称" if prefix == "author" else "视频标题"
                        warning = f"{label}未从配置接口获取，请检查接口关键路径和 JSON路径"
                        self.warnings.add(warning)
                        print(f"[视频信息] {warning}；不使用 HTML 回退")
                return
            await asyncio.sleep(0.1)

    async def close(self) -> None:
        """移除监听并取消未完成解析，确保页面关闭后不再修改任务结果。"""
        self.page.remove_listener("response", self._listener)
        tasks = tuple(self.pending)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
