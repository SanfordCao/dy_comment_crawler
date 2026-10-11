"""激活窗口：导出申请、签名验签与联网校时，授权通过后启动工作台。"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from tkinter import filedialog
from typing import Any
from offline_license import LicenseManager

APP_ID = "douyin-comment-crawler"


def device_code(system: str, identifier: str) -> str:
    """对系统标识做产品隔离哈希，不导出原始系统或硬件标识。"""
    identifier = identifier.strip()
    if not identifier or identifier.lower() in {"unknown", "none"}:
        raise ValueError("无法读取可靠的设备标识，请联系管理员")
    digest = hashlib.sha256(f"{APP_ID}:v1:{system}:{identifier}".encode()).hexdigest().upper()
    return "DY1-" + "-".join(digest[i:i + 8] for i in range(0, len(digest), 8))


def current_device_code() -> str:
    """Windows 读取 MachineGuid、Mac 读取平台 UUID；读取失败不生成随机替代码。"""
    system = platform.system()
    if system == "Windows":
        import winreg

        # 显式使用 64 位注册表视图，避免不同 Python 位数读出不同结果。
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            identifier, _ = winreg.QueryValueEx(key, "MachineGuid")
    elif system == "Darwin":
        result = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                capture_output=True, text=True, check=True, timeout=5)
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([^\"]+)"', result.stdout)
        if not match:
            raise ValueError("未能读取 Mac 设备标识")
        identifier = match.group(1)
    else:
        raise ValueError("当前界面仅支持 Windows 和 macOS")
    return device_code(system, str(identifier))


def authorization_request(code: str) -> dict[str, Any]:
    """构建可发送给管理员的申请；不包含用户名、登录信息或原始硬件信息。"""
    return {"schema_version": 1, "product": APP_ID, "device_code": code,
            "system": platform.system(), "created_at": datetime.now(timezone.utc).isoformat()}


def ensure_activation(manager: LicenseManager) -> bool:
    """已有有效授权直接通过，否则弹出窗口；用户退出时不启动网页。"""
    try:
        manager.require()
        return True
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        initial_error = str(error)
    try:
        import customtkinter as ctk
    except ImportError as error:
        raise SystemExit("缺少 CustomTkinter，请在 PyCharm 当前解释器中安装 customtkinter") from error

    class ActivationWindow(ctk.CTk):
        """使用系统文件对话框的激活窗口；所有提示在窗口内显示。"""

        def __init__(self) -> None:
            """先构建界面再异步读取设备信息，防止系统查询阻塞首屏。"""
            super().__init__()
            self.code = ""
            self.accepted = False
            self.verifying = False
            self.verify_result = None
            self.device_result: tuple[str, str] | None = None
            self.title("抖音评论工作台 · 设备授权")
            self.geometry("760x700")
            self.minsize(680, 650)
            self.configure(fg_color="#F3F6F3")
            self.grid_columnconfigure(0, weight=1)
            self.grid_rowconfigure(0, weight=1)
            self._build_ui()
            Thread(target=self._read_device, daemon=True, name="activation-device").start()
            self.after(100, self._poll_device)

        def _build_ui(self) -> None:
            """构建说明、设备码、文件操作、授权预览和状态区域。"""
            card = ctk.CTkScrollableFrame(self, fg_color="#FFFFFF", corner_radius=20)
            card.grid(row=0, column=0, sticky="nsew", padx=24, pady=(24, 12))
            card.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(card, text="设备授权", font=ctk.CTkFont(size=28, weight="bold"),
                         text_color="#172820", anchor="w").grid(row=0, column=0, sticky="ew", padx=24, pady=(24, 8))
            ctk.CTkLabel(card, text="导出申请给管理员，收到授权文件后导入本机。\n一份授权绑定一台设备，启动时需联网获取 NTP 时间。",
                         justify="left", anchor="w", font=ctk.CTkFont(size=15), text_color="#65746B").grid(
                row=1, column=0, sticky="ew", padx=24, pady=(0, 20))
            self.badge = ctk.CTkLabel(card, text="未激活", fg_color="#FFF3DC",
                                       text_color="#855C19", corner_radius=8, height=34)
            self.badge.grid(row=2, column=0, sticky="w", padx=24, pady=(0, 18))
            ctk.CTkLabel(card, text="当前设备码", anchor="w", font=ctk.CTkFont(size=16, weight="bold")).grid(
                row=3, column=0, sticky="ew", padx=24)
            self.device_text = ctk.CTkTextbox(card, height=82, fg_color="#F1F5F2", text_color="#31463A",
                                               font=ctk.CTkFont(size=14), wrap="char")
            self.device_text.grid(row=4, column=0, sticky="ew", padx=24, pady=(8, 8))
            self._set_device_text("正在读取设备信息…")
            actions = ctk.CTkFrame(card, fg_color="transparent")
            actions.grid(row=6, column=0, sticky="ew", padx=24, pady=20)
            actions.grid_columnconfigure((0, 1), weight=1)
            self.export_button = ctk.CTkButton(actions, text="1  导出授权申请", command=self._export,
                                                  state="disabled", height=44, fg_color="#E8EFEB", text_color="#254638", hover_color="#D9E6DE")
            self.export_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
            self.import_button = ctk.CTkButton(actions, text="2  导入授权文件", command=self._import,
                                                  state="disabled", height=44, fg_color="#12796C", hover_color="#0D665B")
            self.import_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))
            self.preview = ctk.CTkLabel(card, text="尚未导入授权文件", anchor="w", justify="left",
                                           fg_color="#F1F5F2", corner_radius=12, height=95, wraplength=540)
            self.preview.grid(row=7, column=0, sticky="ew", padx=24, pady=(0, 16))
            self.status = ctk.CTkLabel(card, text=initial_error,
                                          anchor="w", justify="left", wraplength=540, text_color="#65746B")
            self.status.grid(row=8, column=0, sticky="ew", padx=24, pady=(0, 12))
            self.enter_button = ctk.CTkButton(card, text="进入工作台", command=self._enter, state="disabled", height=42)
            self.enter_button.grid(row=9, column=0, sticky="ew", padx=24, pady=(0, 24))
            self.retry_button = ctk.CTkButton(card, text="重新验证已有授权", command=self._enter, height=36)
            self.retry_button.grid(row=10, column=0, sticky="ew", padx=24, pady=(0, 24))
            ctk.CTkButton(self, text="退出", command=self.destroy, width=90, fg_color="#E0E8E2",
                          text_color="#354A3D", hover_color="#D1DDD4").grid(row=1, column=0, sticky="e", padx=24, pady=(0, 16))

        def _set_device_text(self, text: str) -> None:
            """更新只读设备码，防止用户修改后导出错误申请。"""
            self.device_text.configure(state="normal")
            self.device_text.delete("1.0", "end")
            self.device_text.insert("1.0", text)
            self.device_text.configure(state="disabled")

        def _read_device(self) -> None:
            """后台线程只查询信息，不调用 Tk；结果交给主线程显示。"""
            try:
                self.device_result = (manager.device(), "")
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self.device_result = ("", f"读取设备信息失败：{error}")

        def _poll_device(self) -> None:
            """在 UI 主线程获取设备结果，失败时禁用依赖设备身份的操作。"""
            if self.device_result is None:
                self.after(100, self._poll_device)
                return
            self.code, error = self.device_result
            self._set_device_text(self.code or "设备信息不可用")
            if error:
                self._notice(error, True)
            else:
                for button in (self.export_button, self.import_button):
                    button.configure(state="normal")

        def _notice(self, text: str, error: bool = False) -> None:
            """在窗口内展示操作反馈，无须用户关闭额外消息弹框。"""
            self.status.configure(text=text, text_color="#A5322D" if error else "#12796C")

        def _enter(self) -> None:
            """进入前再次核对到期与设备，避免导入后长时间停留绕过期限。"""
            self._start_verify(None, True)

        def _export(self) -> None:
            """通过系统另存为对话框导出申请，取消时不写入任何文件。"""
            path = filedialog.asksaveasfilename(parent=self, title="导出授权申请", defaultextension=".json",
                                              initialfile=f"授权申请-{self.code[4:12]}.json",
                                              filetypes=[("授权申请", "*.json")])
            if not path:
                return
            try:
                Path(path).write_text(json.dumps(authorization_request(self.code), ensure_ascii=False, indent=2), encoding="utf-8")
                self._notice("申请已导出，请将该文件发送给管理员。")
            except OSError as error:
                self._notice(f"导出失败：{error}", True)

        def _import(self) -> None:
            """验证并保存授权，失败不开放工作台或覆盖已有授权。"""
            path = filedialog.askopenfilename(parent=self, title="导入授权文件", filetypes=[("授权文件", "*.lic")])
            if not path:
                return
            self._start_verify(Path(path), False)

        def _start_verify(self, path: Path | None, enter: bool) -> None:
            """后台校验网络与签名，网络超时期间保持窗口可响应。"""
            if self.verifying:
                return
            self.verifying = True
            self.verify_result = None
            self.badge.configure(text="未激活")
            for button in (self.import_button, self.enter_button, self.retry_button):
                button.configure(state="disabled")
            self.preview.configure(text="尚未导入有效授权文件")
            self._notice("正在验证签名、设备与网络时间，请稍候…")

            def verify() -> None:
                """工作线程仅回传校验结果，不在后台操作 Tk 控件。"""
                try:
                    payload = manager.import_license(path) if path else manager.require()
                    self.verify_result = (payload, "")
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    self.verify_result = (None, str(error))

            Thread(target=verify, name="activation-verify", daemon=True).start()
            self.after(100, lambda: self._poll_verify(enter))

        def _poll_verify(self, enter: bool) -> None:
            """在主线程回显授权结果，只有验证成功才能进入工作台。"""
            if self.verify_result is None:
                self.after(100, lambda: self._poll_verify(enter))
                return
            payload, error = self.verify_result
            self.verifying = False
            self.import_button.configure(state="normal" if self.code else "disabled")
            self.retry_button.configure(state="normal")
            if error:
                self._notice(error, True)
                return
            if enter:
                self.accepted = True
                self.destroy()
            else:
                # 限制未验证文件中的文本长度，避免异常内容撑破窗口布局。
                owner = str(payload.get("customer") or "未填写")[:80]
                expires = str(payload.get("expires_at") or "未填写")[:80]
                license_id = str(payload.get("license_id") or "未填写")[:80]
                self.preview.configure(text=f"客户：{owner}\n授权编号：{license_id}\n到期时间：{expires}")
                self.badge.configure(text="授权有效", fg_color="#E7F5ED", text_color="#12796C")
                self.enter_button.configure(state="normal")
                self._notice("授权验证通过并已保存，点击进入工作台。")

    ctk.set_appearance_mode("light")
    window = ActivationWindow()
    window.mainloop()
    return window.accepted


def main() -> None:
    """统一转到含签名认证的工作台入口，避免重复弹出激活窗口。"""
    from web_console import main as start_workbench
    start_workbench()


if __name__ == "__main__":
    main()
