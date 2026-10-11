from __future__ import annotations

from argparse import ArgumentParser
from contextlib import redirect_stdout
from contextvars import ContextVar
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import TextIOBase
from pathlib import Path
from threading import Event, Lock, Thread
from tempfile import TemporaryDirectory
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse, parse_qs
from uuid import uuid4
import asyncio
import json
import mimetypes
import os
import re
import shutil
import sys
import traceback
import webbrowser

try:
    from .crawler import CrawlerConfig, crawl_douyin_comments
    from .browser_session import BrowserSession
    from .private_chat import open_user_chat, send_user_message, validated_message, validated_sec_uid
    from .private_chat_batch import PrivateChatBatch
    from .export_comments_excel import export_comments_to_excel, export_comment_rows
    from .output import read_result_json
except ImportError:
    from crawler import CrawlerConfig, crawl_douyin_comments
    from browser_session import BrowserSession
    from private_chat import open_user_chat, send_user_message, validated_message, validated_sec_uid
    from private_chat_batch import PrivateChatBatch
    from export_comments_excel import export_comments_to_excel, export_comment_rows
from output import read_result_json


CAPTURE_DEFAULTS = {
    "comment_scope": "top_level", "fetch_mode": "network_first",
    "max_comments": 0, "max_minutes": 30, "scroll_direction": "down",
    "min_delay_ms": 800, "max_delay_ms": 1400,
    "challenge_wait_minutes": 30, "fetch_douyin_id": False, "concurrent": False,
    "concurrency_limit": 2,
}


def load_console_html() -> bytes:
    return Path(__file__).with_name("web_console.html").read_bytes()


def excel_download_filename(result: dict[str, Any]) -> str:
    author = result.get("video_publisher_nickname") or "作者未知"
    title = result.get("video_title") or "标题未知"
    # Keep the visible task name, replacing characters invalid on Windows.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", f"{author}-{title}")
    name = name.strip().rstrip(". ")[:150].rstrip(". ")
    return f"{name or '作者未知-标题未知'}.xlsx"


class _LogWriter(TextIOBase):
    def __init__(self, sink: Callable[[str], None]) -> None:
        self.sink = sink
        self.buffer = ""

    def write(self, value: str) -> int:
        self.buffer += value
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                self.sink(line)
        return len(value)

    def flush(self) -> None:
        if self.buffer.strip():
            self.sink(self.buffer)
        self.buffer = ""


_task_writer: ContextVar[_LogWriter | None] = ContextVar("douyin_task_writer", default=None)


class _TaskStdout(TextIOBase):
    def __init__(self, fallback: TextIOBase) -> None:
        self.fallback = fallback

    def write(self, value: str) -> int:
        writer = _task_writer.get()
        return (writer if writer is not None else self.fallback).write(value)

    def flush(self) -> None:
        writer = _task_writer.get()
        (writer if writer is not None else self.fallback).flush()


class ConsoleState:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.output_root = self.project_root / "output"
        self.profile_dir = self.project_root / ".douyin-browser-profile"
        self.browser_session = BrowserSession(self.profile_dir)
        self.chat_lock = Lock()
        self.private_chat_session: dict[str, Any] | None = None
        self.private_chat_batch = PrivateChatBatch(self.browser_session, self.chat_lock)
        self.settings_path = self.project_root / "src" / "config" / "web_console_settings.json"
        legacy_settings_path = self.project_root / "web_console_settings.json"
        self.xpath_settings = {
            "pause_xpath": "",
            "detail_xpath": (
                "/html/body/div[2]/div[1]/div[4]/div[3]/div/div[3]/div[1]/"
                "div[1]/div/div[1]/div/div[4]/div/div/div[1]/div[1]/div[1]/div[1]/span"
            ),
            "author_xpath": "",
            "title_xpath": "",
            "comment_xpath": "",
        }
        settings_source = (
            self.settings_path
            if self.settings_path.is_file()
            else legacy_settings_path
        )
        try:
            saved_settings = json.loads(settings_source.read_text(encoding="utf-8"))
            if isinstance(saved_settings, dict):
                for key in self.xpath_settings:
                    value = saved_settings.get(key)
                    if isinstance(value, str):
                        self.xpath_settings[key] = value
        except (OSError, json.JSONDecodeError):
            pass
        if settings_source == legacy_settings_path and legacy_settings_path.is_file():
            try:
                self.settings_path.parent.mkdir(parents=True, exist_ok=True)
                if not self.settings_path.exists():
                    legacy_settings_path.replace(self.settings_path)
            except OSError:
                pass
        self.lock = Lock()
        self.status = "idle"
        self.message = "等待开始"
        self.logs: list[str] = []
        self.run_name: str | None = None
        self.summary: dict[str, Any] | None = None
        self.worker: Thread | None = None
        self.stop_event = Event()
        self.task_controls: dict[str, dict[str, Any]] = {}
        self.batch_path = self.project_root / "src" / "config" / "batch_state.json"
        self.capture_settings_path = self.settings_path.with_name("capture_settings.json")
        self.capture_settings = dict(CAPTURE_DEFAULTS)
        try:
            self.capture_settings = self._validated_capture_settings(
                json.loads(self.capture_settings_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            pass
        self.batch: dict[str, Any] | None = None
        try:
            saved = json.loads(self.batch_path.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and isinstance(saved.get("items"), list):
                if not self.capture_settings_path.exists() and isinstance(saved.get("options"), dict):
                    self.capture_settings = self._validated_capture_settings(saved["options"])
                self.batch = saved
                if saved.get("status") in {"running", "stopping"}:
                    saved["status"] = "interrupted"
                    for item in saved["items"]:
                        if item["status"] in {"running", "waiting_for_user", "pending"}:
                            item["status"] = "interrupted"
                self._save_batch_locked()
        except (OSError, ValueError, TypeError, KeyError):
            pass
        if not self.capture_settings_path.exists():
            self.save_capture_settings(self.capture_settings)
        self.capture_presets_path = self.settings_path.with_name("capture_presets.json")
        self.capture_presets: dict[str, dict[str, Any]] = {}
        self.selected_capture_preset = ""
        self.default_capture_preset_enabled = True
        try:
            saved_presets = json.loads(self.capture_presets_path.read_text(encoding="utf-8"))
            # 逐套校验，某一套配置损坏不会影响其他有效方案。
            for entry in saved_presets.get("presets", []):
                try:
                    name = self._validated_preset_name(entry.get("name"))
                    self.capture_presets[name] = self._validated_capture_settings(entry.get("settings"))
                except (ValueError, TypeError, AttributeError):
                    continue
            selected = saved_presets.get("selected", "")
            if selected in self.capture_presets:
                self.selected_capture_preset = selected
                self.default_capture_preset_enabled = saved_presets.get("default_enabled", True) is not False
            # 当前方案缺失时保留默认方案，避免损坏配置导致没有可用方案。
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    @staticmethod
    def _validated_preset_name(value: Any) -> str:
        """校验命名方案名称，返回去空白名称；默认方案使用独立空名称标识。"""
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 50:
            raise ValueError("配置名称需为 1–50 个字符")
        return value.strip()

    def _capture_presets_locked(self) -> dict[str, Any]:
        """返回方案列表、当前标记和系统默认参数副本，供页面安全创建草稿。"""
        # 返回独立副本，避免页面请求修改内部配置引用。
        return {"selected": self.selected_capture_preset, "default_enabled": self.default_capture_preset_enabled,
                "defaults": dict(CAPTURE_DEFAULTS),
                "presets": [{"name": name, "settings": dict(settings)}
                            for name, settings in self.capture_presets.items()]}

    def get_capture_presets(self) -> dict[str, Any]:
        """在锁内读取配置列表，包含默认方案是否仍存在。"""
        with self.lock:
            return self._capture_presets_locked()

    def manage_capture_preset(self, payload: dict[str, Any]) -> dict[str, Any]:
        """处理方案操作，返回方案列表及当前使用参数；编辑其他方案不会切换当前配置。"""
        action = payload.get("action")
        if action not in {"create", "update", "select", "delete", "custom"}:
            raise ValueError("无效的配置方案操作")
        name = ("" if action == "custom" or (action == "delete" and payload.get("name") == "")
                else self._validated_preset_name(payload.get("name")))
        # 自定义草稿在修改方案标记前校验，非法输入不能导致当前方案被清空。
        custom_settings = (self._validated_capture_settings(payload["settings"])
                           if action == "custom" and payload.get("settings") is not None else None)
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                raise RuntimeError("采集运行期间不能切换或管理配置方案")
            # 先构造新副本，再写文件；保存失败时不改内存中的方案。
            presets = {key: dict(value) for key, value in self.capture_presets.items()}
            selected = self.selected_capture_preset
            default_enabled = self.default_capture_preset_enabled
            # 后端同样约束删除，避免绕过页面按钮误删当前使用方案。
            if action == "delete":
                if name == selected:
                    raise ValueError("当前使用的配置不能删除，请先应用其他方案")
                if len(presets) + int(default_enabled) <= 1:
                    raise ValueError("至少需要保留一个配置方案")
            if action == "custom":
                if not default_enabled:
                    raise ValueError("默认配置已删除，请刷新后选择其他方案")
                # 切回自定义只解除方案关联，保留当前参数及所有命名方案。
                selected = ""
            elif action == "create":
                if name in presets:
                    raise ValueError("同名配置已存在，请更换名称或更新当前方案")
                presets[name] = self._validated_capture_settings(payload.get("settings"))
                # 新建仅保存；用户之后点击“应用”才切换实际采集配置。
            elif action == "delete" and name == "":
                if not default_enabled:
                    raise ValueError("默认配置已删除，请刷新后重试")
                default_enabled = False
            else:
                if name not in presets:
                    raise ValueError("配置方案不存在，请刷新后重试")
                if action == "update":
                    presets[name] = self._validated_capture_settings(payload.get("settings"))
                elif action == "select":
                    selected = name
                else:
                    del presets[name]
            document = {"selected": selected, "default_enabled": default_enabled,
                        "presets": [{"name": key, "settings": value} for key, value in presets.items()]}
            self.capture_presets_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.capture_presets_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.capture_presets_path)
            self.capture_presets, self.selected_capture_preset = presets, selected
            self.default_capture_preset_enabled = default_enabled
            # 只有明确使用方案或更新正在使用的方案，才改实际采集参数。
            # 查看/编辑其他方案不应悄悄改变下一次采集的配置。
            if custom_settings is not None:
                self._save_capture_settings_locked(custom_settings)
            elif action == "select" or (action == "update" and selected == name):
                self._save_capture_settings_locked(dict(presets[name]))
            return {**self._capture_presets_locked(), "settings": dict(self.capture_settings)}

    @staticmethod
    def _validated_capture_settings(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("采集配置必须是 JSON 对象")
        settings = {key: payload.get(key, default) for key, default in CAPTURE_DEFAULTS.items()}
        for key, choices in {
            "comment_scope": {"top_level", "all"},
            "fetch_mode": {"network_first", "scroll_only"},
            "scroll_direction": {"up", "down"},
        }.items():
            if settings[key] not in choices:
                raise ValueError(f"无效的采集配置：{key}")
        for key in ("fetch_douyin_id", "concurrent"):
            if not isinstance(settings[key], bool):
                raise ValueError(f"{key} 必须为布尔值")
        for key, minimum in {"max_comments": 0, "max_minutes": 1,
                             "min_delay_ms": 500, "max_delay_ms": 500,
                             "challenge_wait_minutes": 1, "concurrency_limit": 1}.items():
            if type(settings[key]) is not int or settings[key] < minimum:
                raise ValueError(f"{key} 必须是大于等于 {minimum} 的整数")
        # 队列最多允许 100 个视频，并发数量使用同一上限，避免误填超大值。
        if settings["concurrency_limit"] > 100:
            raise ValueError("并发数量不能超过 100")
        if settings["max_delay_ms"] < settings["min_delay_ms"]:
            raise ValueError("最长滚动等待不能小于最短滚动等待")
        return settings

    def get_capture_settings(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.capture_settings)

    def _save_capture_settings_locked(self, settings: dict[str, Any]) -> None:
        self.capture_settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.capture_settings_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.capture_settings_path)
        self.capture_settings = settings

    def save_capture_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self._validated_capture_settings(payload)
        with self.lock:
            self._save_capture_settings_locked(settings)
        return dict(settings)

    def _save_batch_locked(self) -> None:
        if self.batch is None:
            return
        self.batch.pop("mode", None)
        for item in self.batch.get("items", []):
            item.pop("summary", None)
        options = self.batch.get("options", {})
        for key in self.xpath_settings:
            options.pop(key, None)
        for key, default in {"idle_rounds": 8, "scroll_pixels": 700}.items():
            if options.get(key) == default:
                options.pop(key)
        # Keep terminal items in memory for scheduler indices and final totals,
        # but never persist them as queued work. Results remain in output/.
        saved = json.loads(json.dumps(self.batch))
        saved.pop("options", None)
        queue_statuses = {"pending", "running", "waiting_for_user", "stopping"}
        saved["items"] = [item for item in saved.get("items", []) if item.get("status") in queue_statuses]
        saved["current_index"] = 0
        previous = []
        for batch in saved.get("unfinished_batches", []):
            batch.pop("options", None)
            batch["items"] = [item for item in batch.get("items", []) if item.get("status") in queue_statuses]
            batch["current_index"] = 0
            if batch["items"]:
                previous.append(batch)
        if previous:
            saved["unfinished_batches"] = previous
        else:
            saved.pop("unfinished_batches", None)
        if not saved.get("items") and not previous:
            self.batch_path.unlink(missing_ok=True)
            return
        self.batch_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.batch_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.batch_path)

    @staticmethod
    def video_urls(options: dict[str, Any]) -> list[str]:
        raw = options.get("urls", [options.get("url", "")])
        if not isinstance(raw, list) or not raw or len(raw) > 100:
            raise ValueError("请填写 1 至 100 个视频链接")
        urls, seen = [], set()
        for value in raw:
            if not isinstance(value, str):
                raise ValueError("视频链接必须是文本")
            url = value.strip()
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").lower()
            if len(url) > 4096 or parsed.scheme not in {"http", "https"} or not (
                hostname == "douyin.com" or hostname.endswith(".douyin.com")
            ) or parsed.username or parsed.password:
                raise ValueError(f"请输入有效的 douyin.com 视频地址：{url[:80]}")
            video_id = parse_qs(parsed.query).get("modal_id", [None])[0]
            match = re.search(r"/video/(\d+)", parsed.path)
            key = (match.group(1) if match else video_id) or url
            if key not in seen:
                seen.add(key)
                urls.append(url)
        return urls

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            line = f"[{timestamp}] {message}"
            self.logs.append(line)
            self.logs = self.logs[-500:]

    def clear_logs(self) -> None:
        with self.lock:
            self.logs.clear()

    def get_xpath_settings(self) -> dict[str, str]:
        with self.lock:
            return dict(self.xpath_settings)

    def save_xpath_settings(self, payload: dict[str, Any]) -> dict[str, str]:
        settings: dict[str, str] = {}
        for key in ("pause_xpath", "detail_xpath", "author_xpath", "title_xpath", "comment_xpath"):
            value = payload.get(key, "")
            if not isinstance(value, str) or len(value) > 4_096:
                raise ValueError(f"{key} 必须是 4096 字符以内的 XPath 文本")
            settings[key] = value.strip()
        with self.lock:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(
                json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.xpath_settings = settings
            return dict(settings)

    def initialize_page(self) -> dict[str, Any] | None:
        """初始化页面显示，空闲时清理显示缓存；运行中的每任务日志仍保留。"""
        with self.lock:
            active = bool(self.batch and self.batch.get("status") in {"running", "stopping"}) or self.status in {
                "starting",
                "running",
                "waiting_for_user",
                "stopping",
            }
            self.logs.clear()
            if not active:
                # 只清显示缓存，不修改任务文件中的完整日志。
                for control in self.task_controls.values():
                    control["logs"].clear()
            if not active and self.status in {"succeeded", "stopped", "failed"}:
                self.status = "idle"
                self.message = "等待开始"
        if not active:
            return None

        candidates: list[Path] = []
        with self.lock:
            active_names = [item.get("run_name") for item in (self.batch or {}).get("items", [])
                            if item["status"] in {"running", "waiting_for_user", "stopping"}]
        for name in active_names:
            if name:
                try:
                    candidates.append(self.resolve_run(name))
                except (ValueError, FileNotFoundError):
                    pass
        if self.run_name:
            try:
                candidates.append(self.resolve_run(self.run_name))
            except (ValueError, FileNotFoundError):
                pass
        if not self.batch and self.output_root.is_dir():
            candidates.extend(
                path
                for path in sorted(self.output_root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
                if path.is_dir() and path not in candidates
            )

        for directory in candidates:
            try:
                result = read_result_json(directory)
            except (OSError, json.JSONDecodeError, AttributeError):
                continue
            if result.get("status") != "running":
                continue
            summary = result.get("summary") or {}
            comments = result.get("comments") or []
            self.run_name = directory.name
            self.summary = summary
            self.log(f"[刷新恢复] 已从 {directory.name} 的 JSON 检查点恢复采集信息")
            self.log(
                "[刷新恢复] 视频："
                f"{result.get('video_publisher_nickname') or '作者未知'} · "
                f"{result.get('video_title') or '标题未知'}；"
                f"已保存 {summary.get('comment_count', len(comments))} 条评论"
            )
            return {
                key: result.get(key)
                for key in (
                    "status",
                    "source_url",
                    "video_publisher_nickname",
                    "video_title",
                    "settings",
                    "summary",
                )
            }
        return None

    def snapshot(self) -> dict[str, Any]:
        """返回批次状态及独立任务日志副本，防止页面混合并发任务输出。"""
        with self.lock:
            status, message = self.status, self.message
            if self.batch and self.batch.get("status") in {"running", "stopping"}:
                if self.stop_event.is_set():
                    status, message = "stopping", "正在停止整批任务"
                elif status in {"succeeded", "failed", "stopped"}:
                    status, message = "running", "准备采集下一个视频"
            task_logs = []
            for index, item in enumerate((self.batch or {}).get("items", [])):
                logs = self.task_controls.get(item["url"], {}).get("logs", [])
                if logs or (self.batch or {}).get("status") in {"running", "stopping"}:
                    task_logs.append({"key": f'{self.batch["id"]}:{item["url"]}',
                                      "index": index + 1, "url": item["url"],
                                      "status": item["status"], "run_name": item.get("run_name"),
                                      "logs": list(logs)})
            return {
                "status": status,
                "message": message,
                "logs": list(self.logs),
                "run_name": self.run_name,
                "summary": self.summary,
                "task_logs": task_logs,
                "batch": json.loads(json.dumps(self.batch)) if self.batch else None,
            }

    def start(self, options: dict[str, Any]) -> None:
        options = {**self.get_capture_settings(), **options}
        urls = self.video_urls(options)
        capture_settings = self._validated_capture_settings(options)
        if options.get("comment_scope", "top_level") not in {"top_level", "all"}:
            raise ValueError("评论层级选项无效")
        if options.get("fetch_mode", "network_first") not in {"network_first", "scroll_only"}:
            raise ValueError("一级评论加载方式无效")
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                raise RuntimeError("已有抓取任务正在运行")
            self._save_capture_settings_locked(capture_settings)
            self.status = "starting"
            self.message = "正在启动 Chrome"
            self.logs = []
            self.run_name = None
            self.summary = None
            self.stop_event.clear()
            self.task_controls = {url: {"stop": Event(), "continue": Event(), "logs": [], "log_path": None} for url in urls}
            unfinished = []
            if self.batch:
                unfinished = json.loads(json.dumps(self.batch.get("unfinished_batches", [])))
                remaining = [item for item in self.batch.get("items", [])
                             if item.get("status") in {"pending", "running", "waiting_for_user", "stopping"}]
                if remaining:
                    previous = {key: value for key, value in self.batch.items() if key != "unfinished_batches"}
                    previous["items"] = json.loads(json.dumps(remaining))
                    previous["current_index"] = 0
                    unfinished.append(previous)
            self.batch = {
                "id": datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8],
                "status": "running", "current_index": 0,
                "options": {key: value for key, value in options.items() if key not in {"url", "urls"}},
                "items": [{"url": url, "status": "pending", "run_name": None, "error": ""} for url in urls],
            }
            if unfinished:
                self.batch["unfinished_batches"] = unfinished
            self._save_batch_locked()
            self.worker = Thread(
                target=self._run_queue,
                args=(options, urls),
                daemon=True,
                name="douyin-crawler",
            )
            self.worker.start()

    def _refresh_running_status_locked(self) -> None:
        if self.stop_event.is_set():
            self.status, self.message = "stopping", "正在停止整批任务"
            return
        waiting = [i for i in self.batch["items"] if i["status"] == "waiting_for_user"]
        active = sum(i["status"] in {"running", "waiting_for_user", "stopping"} for i in self.batch["items"])
        self.status = "waiting_for_user" if waiting else "running"
        self.message = (f"{len(waiting)} 个视频需要登录或验证，完成后点击继续" if waiting else
                        f"正在采集 {active} 个视频")

    def _task_log(self, index: int, message: str) -> None:
        with self.lock:
            item = self.batch["items"][index]
            control = self.task_controls[item["url"]]
            line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
            control["logs"].append(line)
            control["logs"] = control["logs"][-500:]
            self.logs.append(f"[视频 {index + 1}] {line}")
            self.logs = self.logs[-500:]
            if control["log_path"] is not None:
                try:
                    with control["log_path"].open("a", encoding="utf-8") as file:
                        file.write(line + "\n")
                except OSError:
                    pass

    def _register_task_run(self, index: int, directory: Path) -> None:
        with self.lock:
            item = self.batch["items"][index]
            control = self.task_controls[item["url"]]
            item["run_name"] = directory.name
            self.run_name = directory.name
            control["log_path"] = directory / "run.log"
            control["log_path"].write_text("\n".join(control["logs"]) + "\n", encoding="utf-8")
            self._save_batch_locked()

    async def _run_video(self, index: int, options: dict[str, Any], context: Any,
                         first_login: list[bool]) -> None:
        item = self.batch["items"][index]
        control = self.task_controls[item["url"]]
        stopped = lambda: self.stop_event.is_set() or control["stop"].is_set()
        writer = _LogWriter(lambda message: self._task_log(index, message))
        token = _task_writer.set(writer)

        async def manual_continue(reason: str, minutes: int) -> None:
            if stopped():
                return
            with self.lock:
                item["status"] = "waiting_for_user"
                self._refresh_running_status_locked()
                self._save_batch_locked()
            print(f"等待人工处理：{reason}（最多 {minutes} 分钟）")
            completed = await asyncio.to_thread(control["continue"].wait, minutes * 60)
            control["continue"].clear()
            if not completed:
                raise RuntimeError("等待网页确认登录/验证超时")
            with self.lock:
                if not stopped():
                    first_login[0] = False
                    item["status"] = "running"
                self._refresh_running_status_locked()
                self._save_batch_locked()

        try:
            options = {**options, **self.get_xpath_settings()}
            config = CrawlerConfig(
                url=item["url"], output_root=self.output_root, profile_dir=self.profile_dir,
                browser_context=context, require_initial_login=first_login[0],
                scroll_direction=str(options.get("scroll_direction", "down")),
                comment_scope=str(options.get("comment_scope", "top_level")),
                fetch_mode=str(options.get("fetch_mode", "network_first")),
                fetch_douyin_id=options.get("fetch_douyin_id") is True,
                idle_rounds=int(options.get("idle_rounds", 8)),
                max_comments=int(options.get("max_comments", 0)),
                max_minutes=int(options.get("max_minutes", 30)),
                min_delay_ms=int(options.get("min_delay_ms", 800)),
                max_delay_ms=int(options.get("max_delay_ms", 1400)),
                scroll_pixels=int(options.get("scroll_pixels", 700)),
                challenge_wait_minutes=int(options.get("challenge_wait_minutes", 30)),
                pause_xpath=options["pause_xpath"], detail_xpath=options["detail_xpath"],
                author_xpath=options["author_xpath"], title_xpath=options["title_xpath"],
                comment_xpath=options["comment_xpath"], manual_continue=manual_continue,
                stop_requested=stopped,
                run_directory_ready=lambda directory: self._register_task_run(index, directory),
            )
            print(f"开始采集：{item['url']}")
            directory, result = await crawl_douyin_comments(config)
            with self.lock:
                item["run_name"] = directory.name
                item["status"] = "stopped" if stopped() else "succeeded"
                self.summary = result.get("summary")
                self.run_name = directory.name
            print("任务已停止，保留已有数据" if stopped() else "评论采集完成")
        except BaseException as error:
            print(f"抓取失败：{error}")
            print(traceback.format_exc(limit=4))
            with self.lock:
                item["status"] = "stopped" if stopped() else "failed"
                item["error"] = "" if stopped() else str(error)
                directory = getattr(error, "run_directory", None)
                if directory:
                    item["run_name"] = directory.name
        finally:
            writer.flush()
            _task_writer.reset(token)
            with self.lock:
                self._refresh_running_status_locked()
                self._save_batch_locked()

    async def _run_batch_async(self, options: dict[str, Any]) -> None:
        """在共享浏览器事件循环派发任务，结束仅关闭任务页，保留登录与私聊页。"""
        # 关闭开关时始终串行；开启后按本批次启动时的配置派发任务。
        limit = int(options.get("concurrency_limit", 2)) if options.get("concurrent") is True else 1
        first_login = [not self.profile_dir.exists() or not any(self.profile_dir.iterdir())]
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        context = await self.browser_session.get_context()
        running: set[asyncio.Task] = set()
        pending = list(range(len(self.batch["items"])))
        try:
            while pending or running:
                with self.lock:
                    waiting = any(i["status"] == "waiting_for_user" for i in self.batch["items"])
                    while (pending and len(running) < limit and not self.stop_event.is_set()
                           and not waiting and not (first_login[0] and running)):
                        index = pending.pop(0)
                        item = self.batch["items"][index]
                        if item["status"] != "pending":
                            continue
                        self.batch["current_index"] = index
                        item["status"] = "running"
                        running.add(asyncio.create_task(self._run_video(index, options, context, first_login)))
                        self._refresh_running_status_locked()
                        self._save_batch_locked()
                if not running:
                    break
                done, _ = await asyncio.wait(running, timeout=.15, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                running.difference_update(done)
                if done and pending:
                    await asyncio.sleep(1)
        finally:
            if running:
                self.stop_event.set()
                for control in self.task_controls.values():
                    control["continue"].set()
                await asyncio.gather(*running, return_exceptions=True)

    def _run_queue(self, options: dict[str, Any], urls: list[str]) -> None:
        """工作线程等待共享浏览器中的批次，保持原有日志路由和任务终态处理。"""
        try:
            self.log(f"并发采集已开启（最多 {options.get('concurrency_limit', 2)} 个视频）"
                     if options.get("concurrent") is True else "按顺序采集视频")
            with redirect_stdout(_TaskStdout(sys.stdout)):
                self.browser_session.run(self._run_batch_async(options))
        except Exception as error:
            self.log(f"批次异常：{error}")
            with self.lock:
                for item in self.batch["items"]:
                    if item["status"] in {"pending", "running", "waiting_for_user", "stopping"}:
                        item.update(status="stopped" if self.stop_event.is_set() else "failed", error=str(error))
        finally:
            with self.lock:
                for item in self.batch["items"]:
                    if item["status"] == "pending":
                        item["status"] = "cancelled"
                failures = sum(i["status"] == "failed" for i in self.batch["items"])
                successes = sum(i["status"] == "succeeded" for i in self.batch["items"])
                stopped_count = sum(i["status"] == "stopped" for i in self.batch["items"])
                self.status = ("stopped" if self.stop_event.is_set() else "succeeded" if successes
                               else "failed" if failures else "stopped")
                self.batch["status"] = self.status
                self.message = (f"采集结束：完成 {successes} 个，失败 {failures} 个，停止 {stopped_count} 个"
                                if not self.stop_event.is_set() else "整批任务已停止，已保留采集结果")
                if len(self.batch["items"]) == 1:
                    item = self.batch["items"][0]
                    self.message = ("任务已停止，已保留采集结果" if self.status == "stopped" else
                                    item["error"] if failures else "评论采集完成")
                self._save_batch_locked()

    def continue_after_login(self) -> None:
        with self.lock:
            waiting = [i for i in self.batch["items"] if i["status"] == "waiting_for_user"] if self.batch else []
            if not waiting:
                raise RuntimeError("当前没有等待登录/验证的任务")
            for item in waiting:
                self.task_controls[item["url"]]["continue"].set()

    def stop_task(self, batch_id: str, url: str) -> None:
        with self.lock:
            if not self.worker or not self.worker.is_alive() or not self.batch:
                raise RuntimeError("当前没有正在运行的队列")
            if self.batch["id"] != batch_id or self.stop_event.is_set():
                raise RuntimeError("该任务不属于当前运行队列，或整批任务正在停止")
            item = next((i for i in self.batch["items"] if i["url"] == url), None)
            if not item or item["status"] not in {"pending", "running", "waiting_for_user"}:
                raise RuntimeError("该任务已结束或正在停止")
            if item["status"] == "pending":
                item["status"] = "stopped"
            else:
                item["status"] = "stopping"
                control = self.task_controls[url]
                control["stop"].set()
                control["continue"].set()
            self._refresh_running_status_locked()
            self._save_batch_locked()

    def stop(self) -> None:
        with self.lock:
            if not self.worker or not self.worker.is_alive():
                raise RuntimeError("当前没有正在运行的任务")
            self.stop_event.set()
            self.batch["status"] = "stopping"
            for control in self.task_controls.values():
                control["continue"].set()
            self._refresh_running_status_locked()
            self._save_batch_locked()

    def resolve_run(self, name: str) -> Path:
        if not name or name in {".", ".."} or Path(name).name != name:
            raise ValueError("无效的结果目录")
        path = (self.output_root / name).resolve()
        if path.parent != self.output_root.resolve() or not path.is_dir():
            raise FileNotFoundError("结果目录不存在")
        return path

    def _is_run_active_locked(self, name: str) -> bool:
        return any(
            item.get("run_name") == name
            and item.get("status") in {"running", "waiting_for_user", "stopping"}
            for item in (self.batch or {}).get("items", [])
        )

    def delete_run(self, name: str) -> None:
        with self.lock:
            if self._is_run_active_locked(name):
                raise RuntimeError("该任务正在采集或停止中，不能删除")
            if not self.output_root.exists():
                raise FileNotFoundError("结果目录不存在")
            if (
                self.output_root.is_symlink()
                or self.output_root.resolve().parent != self.project_root
            ):
                raise ValueError("结果根目录位置异常，拒绝删除")
            candidate = self.output_root / name
            if candidate.is_symlink():
                raise ValueError("不允许删除符号链接目录")
            path = self.resolve_run(name)
            if not (path / "comments.json").is_file():
                raise ValueError("该目录不是有效的采集结果目录")
            shutil.rmtree(path)

    def open_private_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """从保存的评论读取目标身份，串行打开私信并生成一次性发送凭据。"""
        if not self.chat_lock.acquire(blocking=False):
            raise RuntimeError("正在打开另一位用户的私信，请稍后再试")
        try:
            # 打开新目标立即使旧凭据失效，避免旧弹窗向之前的收件人发送。
            self.private_chat_session = None
            with self.lock:
                result = read_result_json(self.resolve_run(str(payload.get("run_name", ""))))
                comment_id = str(payload.get("comment_id", ""))
                comment = next((item for item in result.get("comments", []) if str(item.get("id", "")) == comment_id), None)
                if not comment_id or not comment:
                    raise ValueError("评论不存在，请刷新结果后再试")
                author = dict(comment.get("author") or {})
                validated_sec_uid(author.get("sec_uid"))
            return self.browser_session.run(self._open_private_chat_async(author), timeout=65)
        finally:
            self.chat_lock.release()

    async def _open_private_chat_async(self, author: dict[str, Any]) -> dict[str, Any]:
        """在采集同一浏览器上下文创建独立标签页，整个打开过程最多 60 秒。"""
        context = await self.browser_session.get_context()

        def remember_chat(page: Any, sec_uid: str) -> None:
            """只在浏览器已核对会话 sec_uid 后保存页面，不接受客户端提供的收件人。"""
            self.private_chat_session = {"page": page, "sec_uid": sec_uid,
                                         "token": uuid4().hex, "used": False}

        try:
            result = await asyncio.wait_for(open_user_chat(context, author, remember_chat), timeout=60)
            if self.private_chat_session:
                result["chat_token"] = self.private_chat_session["token"]
            return result
        except TimeoutError as error:
            raise RuntimeError("私信界面加载超时，请在浏览器中检查；不会自动重试或发送") from error

    def send_private_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """消费一次性会话凭据并发送一条文本，重复请求及并发请求不会再次点击发送。"""
        message = validated_message(payload.get("message"))
        if not self.chat_lock.acquire(blocking=False):
            raise RuntimeError("正在处理另一项私信操作，请稍后再试")
        try:
            session = self.private_chat_session
            if not session or not isinstance(payload.get("chat_token"), str) or payload["chat_token"] != session["token"]:
                raise RuntimeError("私信会话已失效，请先打开目标用户的私信")
            if session["used"]:
                raise RuntimeError("该发送请求已处理，不能重复发送；请到抖音核对结果")
            # 在排入浏览器循环前消费凭据，HTTP 超时或重复点击也不会补发。
            session["used"] = True
            try:
                return self.browser_session.run(send_user_message(session["page"], session["sec_uid"], message), timeout=35)
            except RuntimeError:
                raise
            except Exception as error:
                raise RuntimeError("发送操作异常，结果可能不确定，请在抖音核对；不会自动重试") from error
        finally:
            self.chat_lock.release()

    def start_private_chat_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """从勾选的保存评论提取作者，不接受客户端伪造的 sec_uid 或昵称。"""
        ids = payload.get("comment_ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(value, str) or not value for value in ids):
            raise ValueError("请选择有效评论中的用户")
        with self.lock:
            result = read_result_json(self.resolve_run(str(payload.get("run_name", ""))))
            comments = {str(item.get("id", "")): item for item in result.get("comments", [])}
            if any(value not in comments for value in ids):
                raise ValueError("部分评论不存在，请刷新结果并重新选择")
            authors = [dict(comments[value].get("author") or {}) for value in dict.fromkeys(ids)]
        result = self.private_chat_batch.start(authors, payload.get("message"), payload.get("request_id"))
        # 批量拥有串行锁时使之前的单条发送凭据失效，结束后不能误用旧会话。
        self.private_chat_session = None
        return result

    def export_merged_runs(self, names: Any) -> bytes:
        """合并所选任务评论为一个 Excel；锁内校验活动状态，临时文件自动清理。"""
        if not isinstance(names, list) or not names or len(names) > 1000 or any(not isinstance(name, str) for name in names):
            raise ValueError("请选择 1 至 1000 个有效任务")
        with self.lock:
            runs = []
            for name in dict.fromkeys(names):
                if self._is_run_active_locked(name):
                    raise RuntimeError("所选任务正在采集或停止中，不能导出")
                runs.append((name, read_result_json(self.resolve_run(name))))
            comments = []
            for name, result in runs:
                comments.extend({**comment, "_run_id": name,
                                 "_video_author": result.get("video_publisher_nickname") or "作者未知",
                                 "_video_title": result.get("video_title") or "标题未知"}
                                for comment in result.get("comments", []) if isinstance(comment, dict))
            # 不在采集目录中留下合并中间文件，不修改原始评论。
            with TemporaryDirectory(prefix="douyin-export-") as directory:
                path = Path(directory) / "merged.xlsx"
                export_comment_rows(comments, path, include_source=True)
                return path.read_bytes()

    def list_runs(self) -> list[dict[str, Any]]:
        if not self.output_root.exists():
            return []
        runs: list[dict[str, Any]] = []
        with self.lock:
            active_names = {
                item.get("run_name")
                for item in (self.batch or {}).get("items", [])
                if item.get("status") in {"running", "waiting_for_user", "stopping"}
            }
        for directory in sorted(self.output_root.iterdir(), reverse=True):
            json_path = directory / "comments.json"
            if not directory.is_dir() or not json_path.is_file():
                continue
            try:
                payload = read_result_json(directory)
                summary = payload.get("summary") or {}
                runs.append(
                    {
                        "name": directory.name,
                        "is_active": directory.name in active_names,
                        "video_publisher_nickname": payload.get(
                            "video_publisher_nickname", ""
                        ),
                        "video_title": payload.get("video_title", ""),
                        "status": payload.get("status", "unknown"),
                        "source_url": payload.get("source_url", ""),
                        "comment_count": summary.get("comment_count", 0),
                        "image_count": summary.get("image_count", 0),
                        "started_at": payload.get("started_at", ""),
                    }
                )
            except (OSError, json.JSONDecodeError):
                continue
        return runs


class ConsoleServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], state: ConsoleState) -> None:
        """创建基础版工作台服务，不加载授权模块或密钥。"""
        super().__init__(address, ConsoleRequestHandler)
        self.state = state


class ConsoleRequestHandler(BaseHTTPRequestHandler):
    server: ConsoleServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        disposition: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send_bytes(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 64 * 1024:
            raise ValueError("请求内容为空或过大")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求格式错误")
        return payload

    def do_GET(self) -> None:
        """读取工作台页面、任务状态及采集结果。"""
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/private-chat/batch":
                self._json(200, self.server.state.private_chat_batch.snapshot())
                return
            if path in {"/", "/index.html"}:
                body = load_console_html()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'; img-src 'self' data:; "
                    "connect-src 'self'; base-uri 'none'; form-action 'none'; "
                    "frame-ancestors 'none'",
                )
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/status":
                self._json(200, self.server.state.snapshot())
                return
            if path == "/api/settings":
                self._json(200, self.server.state.get_xpath_settings())
                return
            if path == "/api/capture-settings":
                self._json(200, self.server.state.get_capture_settings())
                return
            if path == "/api/capture-presets":
                self._json(200, self.server.state.get_capture_presets())
                return
            if path == "/api/runs":
                self._json(200, self.server.state.list_runs())
                return
            parts = [unquote(part) for part in path.split("/") if part]
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "comments":
                run = self.server.state.resolve_run(parts[2])
                body = json.dumps(
                    read_result_json(run), ensure_ascii=False, indent=2
                ).encode("utf-8")
                self._send_bytes(200, body, "application/json; charset=utf-8")
                return
            if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3] == "files":
                run = self.server.state.resolve_run(parts[2])
                filename = parts[4]
                relative_path = Path(filename)
                if relative_path.is_absolute() or not filename or ".." in relative_path.parts:
                    raise ValueError("无效文件名")
                file_path = (run / relative_path).resolve()
                if not file_path.is_relative_to(run.resolve()) or not file_path.is_file():
                    raise FileNotFoundError("图片不存在")
                media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                self._send_bytes(200, file_path.read_bytes(), media_type)
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "excel":
                with self.server.state.lock:
                    if self.server.state._is_run_active_locked(parts[2]):
                        raise RuntimeError("该任务正在采集或停止中，不能导出")
                    run = self.server.state.resolve_run(parts[2])
                    filename = excel_download_filename(read_result_json(run))
                    output_path = run / "comments.xlsx"
                    export_comments_to_excel(run / "comments.json", output_path)
                    body = output_path.read_bytes()
                self._send_bytes(
                    200,
                    body,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    'attachment; filename="comments.xlsx"; '
                    f"filename*=UTF-8''{quote(filename, safe='')}",
                )
                return
            self.send_error(404)
        except (ValueError, FileNotFoundError, OSError, RuntimeError) as error:
            self._json(400, {"error": str(error)})

    def do_POST(self) -> None:
        """处理工作台操作，保留各接口原有的参数及跨站校验。"""
        try:
            if self.path in {"/api/private-chat/batch/start", "/api/private-chat/batch/stop"}:
                origin = self.headers.get("Origin")
                if origin and (urlparse(origin).scheme not in {"http", "https"}
                               or urlparse(origin).netloc != self.headers.get("Host")):
                    raise ValueError("不允许跨站操作批量私信")
                payload = self._read_json()
                if self.path.endswith("/start"):
                    self._json(202, self.server.state.start_private_chat_batch(payload))
                else:
                    self.server.state.private_chat_batch.stop(str(payload.get("job_id", "")))
                    self._json(200, self.server.state.private_chat_batch.snapshot())
                return
            if self.path == "/api/private-chat/open":
                self._json(200, {"ok": True, **self.server.state.open_private_chat(self._read_json())})
                return
            if self.path == "/api/private-chat/send":
                # 浏览器跨站请求不能使用本机发送接口；只允许控制台同源调用。
                origin = self.headers.get("Origin")
                if origin and (urlparse(origin).scheme not in {"http", "https"}
                               or urlparse(origin).netloc != self.headers.get("Host")):
                    raise ValueError("不允许跨站发送私信")
                self._json(200, {"ok": True, **self.server.state.send_private_chat(self._read_json())})
                return
            if self.path == "/api/runs/export-merged":
                body = self.server.state.export_merged_runs(self._read_json().get("names"))
                filename = f"评论合并导出-{datetime.now().strftime('%Y%m%d-%H%M%S')}.xlsx"
                self._send_bytes(200, body, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 'attachment; filename="merged.xlsx"; ' + f"filename*=UTF-8''{quote(filename, safe='')}")
                return
            if self.path == "/api/crawl/start":
                options = self._read_json()
                if options.get("comment_scope", "top_level") not in {
                    "top_level",
                    "all",
                }:
                    raise ValueError("评论层级选项无效")
                if options.get("fetch_mode", "network_first") not in {
                    "network_first",
                    "scroll_only",
                }:
                    raise ValueError("一级评论加载方式无效")
                self.server.state.start(options)
                self._json(202, {"ok": True})
                return
            if self.path == "/api/settings":
                settings = self.server.state.save_xpath_settings(self._read_json())
                self._json(200, {"ok": True, **settings})
                return
            if self.path == "/api/capture-settings":
                settings = self.server.state.save_capture_settings(self._read_json())
                self._json(200, {"ok": True, **settings})
                return
            if self.path == "/api/capture-presets":
                result = self.server.state.manage_capture_preset(self._read_json())
                self._json(200, {"ok": True, **result})
                return
            if self.path == "/api/crawl/continue":
                self.server.state.continue_after_login()
                self._json(200, {"ok": True})
                return
            if self.path == "/api/crawl/stop":
                self.server.state.stop()
                self._json(200, {"ok": True})
                return
            if self.path == "/api/crawl/stop-task":
                payload = self._read_json()
                self.server.state.stop_task(str(payload.get("batch_id", "")), str(payload.get("url", "")))
                self._json(200, {"ok": True})
                return
            if self.path == "/api/logs/clear":
                self.server.state.clear_logs()
                self._json(200, {"ok": True})
                return
            if self.path == "/api/page/initialize":
                run = self.server.state.initialize_page()
                with self.server.state.lock:
                    try:
                        batch = json.loads(self.server.state.batch_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        batch = None
                self._json(200, {"ok": True, "run": run, "batch": batch})
                return
            if self.path == "/api/runs/delete":
                payload = self._read_json()
                name = str(payload.get("name", ""))
                self.server.state.delete_run(name)
                self._json(200, {"ok": True})
                return
            self.send_error(404)
        except (ValueError, FileNotFoundError, OSError, RuntimeError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})


def workbench_data_dir() -> Path:
    """返回 EXE 的持久化目录，避免数据写入单文件包的临时解压目录。"""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path.home() / "Library" / "Application Support"
    return base / "DouyinCommentWorkbench"


def create_console_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    project_root: Path | None = None,
) -> ConsoleServer:
    """直接创建基础版服务；源码使用项目目录，EXE 使用用户可写目录。"""
    root = project_root or (workbench_data_dir() if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1])
    return ConsoleServer((host, port), ConsoleState(root))


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="启动抖音评论工具网页控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    return parser


def main() -> None:
    """直接打开网页工作台，退出时清理任务及共享浏览器。"""
    args = build_parser().parse_args()
    server = create_console_server(args.host, args.port)
    url = f"http://{args.host}:{server.server_address[1]}/"
    print(f"抖音评论工具已启动：{url}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.state.stop()
        server.state.private_chat_batch.stop()
        if server.state.private_chat_batch.worker is not None:
            server.state.private_chat_batch.worker.join(timeout=5)
        if server.state.worker is not None:
            server.state.worker.join(timeout=5)
        try:
            server.state.browser_session.close()
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
