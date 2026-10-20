"""Persist reusable comment-reply filters separately from video share texts."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Any
import json

try:
    from .comment_reply_filter import validate_reply_filter
except ImportError:
    from comment_reply_filter import validate_reply_filter


DEFAULT_REPLY_SETTINGS = {"region": "", "keywords": [], "source_mode": "page_first",
                          "mode": "manual", "reply_text": "", "max_minutes": 30,
                          "concurrent": False, "concurrency_limit": 2, "retry_limit": 3}


def validate_retry_limit(payload: dict[str, Any]) -> int:
    """Keep retry budgets explicit; booleans and coerced fractional values are unsafe."""
    value = payload.get("retry_limit", 3)
    if type(value) is not int or not 0 <= value <= 10:
        raise ValueError("失败重试次数须为 0 至 10 的整数")
    return value


def validate_reply_concurrency(payload: dict[str, Any]) -> tuple[bool, int]:
    """Default legacy schemes to serial execution and reject coerced slot values."""
    concurrent = payload.get("concurrent", False)
    if type(concurrent) is not bool:
        raise ValueError("并发开关须为布尔值")
    concurrency_limit = payload.get("concurrency_limit", 2)
    if type(concurrency_limit) is not int or not 1 <= concurrency_limit <= 100:
        raise ValueError("并发数量须为 1 至 100 的整数")
    return concurrent, concurrency_limit


def validate_reply_settings(payload: Any) -> dict[str, Any]:
    """Normalize only task-wide options; reply mode and draft apply to the whole batch."""
    if not isinstance(payload, dict):
        raise ValueError("回复配置必须是 JSON 对象")
    concurrent, concurrency_limit = validate_reply_concurrency(payload)
    region = payload.get("region", "")
    keywords = payload.get("keywords", [])
    if not isinstance(region, str):
        raise ValueError("地区须为文字")
    if region.strip() or keywords:
        region, keywords = validate_reply_filter(region, keywords)
    else:
        region, keywords = "", []
    source_mode = payload.get("source_mode", "page_first")
    if source_mode != "page_first":
        raise ValueError("接口优先尚未开放；请选择页面元素优先")
    mode = payload.get("mode", "manual")
    if mode not in {"manual", "automatic"}:
        raise ValueError("回复方式无效")
    reply_text = payload.get("reply_text", "")
    if not isinstance(reply_text, str):
        raise ValueError("预设回复文字须为文字")
    reply_text = reply_text.strip()
    if len(reply_text) > 500:
        raise ValueError("预设回复文字不能超过 500 个字符")
    # 与启动校验一致：自动回复没有文案时不能保存为方案。
    if mode == "automatic" and not reply_text:
        raise ValueError("预设文字自动回复需要先填写回复内容")
    max_minutes = payload.get("max_minutes", 30)
    if type(max_minutes) is not int or not 1 <= max_minutes <= 240:
        raise ValueError("运行时长须为 1 至 240 分钟")
    return {"region": region, "keywords": keywords, "source_mode": source_mode,
            "mode": mode, "reply_text": reply_text, "max_minutes": max_minutes,
            "concurrent": concurrent, "concurrency_limit": concurrency_limit,
            "retry_limit": validate_retry_limit(payload)}


class ReplyPresetStore:
    """Keep default/selected reply settings and named presets in one atomic JSON file."""

    def __init__(self, path: Path) -> None:
        """Load valid schemes while retaining the built-in default on damaged data."""
        self.path = path
        self.lock = Lock()
        self.settings = validate_reply_settings(DEFAULT_REPLY_SETTINGS)
        self.default_settings = validate_reply_settings(DEFAULT_REPLY_SETTINGS)
        self.presets: dict[str, dict[str, Any]] = {}
        self.selected = ""
        self.default_enabled = True
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("回复方案文件格式无效")
            self.settings = validate_reply_settings(saved.get("settings", DEFAULT_REPLY_SETTINGS))
            self.default_settings = validate_reply_settings(saved.get("default_settings", self.settings))
            for entry in saved.get("presets", []):
                try:
                    name = self._name(entry.get("name"))
                    self.presets[name] = validate_reply_settings(entry.get("settings"))
                except (ValueError, TypeError, AttributeError):
                    continue
            selected = saved.get("selected", "")
            if selected in self.presets:
                self.selected = selected
                self.default_enabled = saved.get("default_enabled", True) is not False
                self.settings = validate_reply_settings(self.presets[selected])
            else:
                # A missing selected scheme must never leave a different scheme active.
                self.settings = validate_reply_settings(self.default_settings)
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    @staticmethod
    def _name(value: Any) -> str:
        """Reject empty, duplicate-prone or oversized user names."""
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 50:
            raise ValueError("配置名称需为 1–50 个字符")
        return value.strip()

    def snapshot(self) -> dict[str, Any]:
        """Return copies so callers cannot mutate the persisted scheme state."""
        with self.lock:
            return {"selected": self.selected, "default_enabled": self.default_enabled,
                    "defaults": validate_reply_settings(DEFAULT_REPLY_SETTINGS),
                    "default_settings": validate_reply_settings(self.default_settings),
                    "settings": validate_reply_settings(self.settings),
                    "presets": [{"name": name, "settings": validate_reply_settings(settings)}
                                for name, settings in self.presets.items()]}

    def manage(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply capture-style create/update/select/delete/custom semantics atomically."""
        if not isinstance(payload, dict):
            raise ValueError("方案操作必须是 JSON 对象")
        action = payload.get("action")
        if action not in {"create", "update", "select", "delete", "custom"}:
            raise ValueError("无效的配置方案操作")
        name = ("" if action == "custom" or (action == "delete" and payload.get("name") == "")
                else self._name(payload.get("name")))
        supplied = (validate_reply_settings(payload.get("settings"))
                    if action in {"create", "update"} or
                    (action == "custom" and payload.get("settings") is not None) else None)
        with self.lock:
            presets = {key: validate_reply_settings(value) for key, value in self.presets.items()}
            selected, default_enabled = self.selected, self.default_enabled
            settings = validate_reply_settings(self.settings)
            default_settings = validate_reply_settings(self.default_settings)
            if action == "delete":
                if name == selected:
                    raise ValueError("当前使用的配置不能删除，请先应用其他方案")
                if len(presets) + int(default_enabled) <= 1:
                    raise ValueError("至少需要保留一个配置方案")
            if action == "custom":
                if not default_enabled:
                    raise ValueError("默认配置已删除，请刷新后选择其他方案")
                selected = ""
                if supplied is not None:
                    default_settings = supplied
                settings = validate_reply_settings(default_settings)
            elif action == "create":
                if name in presets:
                    raise ValueError("同名配置已存在，请更换名称")
                presets[name] = supplied
            elif action == "delete" and name == "":
                if not default_enabled:
                    raise ValueError("默认配置已删除，请刷新后重试")
                default_enabled = False
            else:
                if name not in presets:
                    raise ValueError("配置方案不存在，请刷新后重试")
                if action == "update":
                    presets[name] = supplied
                    if selected == name:
                        settings = supplied
                elif action == "select":
                    selected, settings = name, validate_reply_settings(presets[name])
                else:
                    del presets[name]
            document = {"selected": selected, "default_enabled": default_enabled,
                        "settings": settings, "default_settings": default_settings,
                        "presets": [{"name": key, "settings": value} for key, value in presets.items()]}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)
            self.selected, self.default_enabled = selected, default_enabled
            self.settings, self.default_settings, self.presets = settings, default_settings, presets
            return {**document, "defaults": validate_reply_settings(DEFAULT_REPLY_SETTINGS)}
