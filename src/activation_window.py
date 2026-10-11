"""激活窗口：复制设备码、签名验签与联网校时，授权通过后启动工作台。"""

from __future__ import annotations

import hashlib
import platform
import re
import subprocess
from datetime import datetime, timedelta, timezone
from threading import Thread
from tkinter import TclError, messagebox
from typing import Any
from offline_license import LicenseManager, utc_time

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


def show_authorization_error(parent: Any, error: str) -> None:
    """主线程弹出授权失败原因；关闭提示不会开放工作台。"""
    messagebox.showerror("授权验证失败", f"{error}\n\n当前未通过授权验证，无法使用程序。\n请粘贴管理员签发的有效授权码。", parent=parent)


def authorization_expiry_text(payload: dict[str, Any]) -> str:
    """统一按北京时间展示已验证的截止时刻，避免系统时区造成误解。"""
    expires = utc_time(payload["expires_at"]).astimezone(timezone(timedelta(hours=8)))
    return expires.strftime("%Y-%m-%d %H:%M:%S")


def show_authorization_success(parent: Any, payload: dict[str, Any]) -> None:
    """导入并保存成功后说明有效期，正常启动不重复弹出。"""
    expires = authorization_expiry_text(payload)
    messagebox.showinfo("授权导入成功", f"授权已验证并保存。\n\n有效期截止（北京时间）：{expires}\n该时刻起授权失效。\n\n点击“进入工作台”即可使用。", parent=parent)


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
        """两步激活窗口：先申请设备授权，再输入授权码完成激活。"""

        def __init__(self) -> None:
            """先构建界面再异步读取设备信息，防止系统查询阻塞首屏。"""
            super().__init__()
            self.code = ""
            self.accepted = False
            self.verifying = False
            self.verify_result = None
            self.device_result: tuple[str, str] | None = None
            self.title("抖音评论工作台 · 设备授权")
            self.geometry("760x760")
            self.minsize(680, 700)
            self.configure(fg_color="#F3F6F3")
            self.grid_columnconfigure(0, weight=1)
            self.grid_rowconfigure(0, weight=1)
            self._build_ui()
            Thread(target=self._read_device, daemon=True, name="activation-device").start()
            self.after(100, self._poll_device)
            # 初次使用没有授权无需重复弹窗，已有文件验证失败才单独说明。
            if (manager.data_dir / "license.lic").exists():
                self.after(200, lambda: show_authorization_error(self, initial_error))

        def _build_ui(self) -> None:
            """构建说明、设备码、文件操作、授权预览和状态区域。"""
            card = ctk.CTkScrollableFrame(self, fg_color="#FFFFFF", corner_radius=20)
            card.grid(row=0, column=0, sticky="nsew", padx=24, pady=(24, 12))
            card.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(card, text="设备授权", font=ctk.CTkFont(size=28, weight="bold"),
                         text_color="#172820", anchor="w").grid(row=0, column=0, sticky="ew", padx=24, pady=(24, 8))
            self.step_description = ctk.CTkLabel(card, text="第 1 步 / 共 2 步：申请授权\n复制设备码发送给管理员，收到授权码后点击“下一步”。\n一份授权绑定一台设备，启动时需联网获取 NTP 时间。",
                         justify="left", anchor="w", wraplength=540, font=ctk.CTkFont(size=15), text_color="#65746B")
            self.step_description.grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 20))
            self.badge = ctk.CTkLabel(card, text="未激活", fg_color="#FFF3DC",
                                       text_color="#855C19", corner_radius=8, height=34)
            self.badge.grid(row=2, column=0, sticky="w", padx=24, pady=(0, 18))
            self.request_frame = ctk.CTkFrame(card, fg_color="transparent")
            self.request_frame.grid(row=3, column=0, sticky="ew", padx=24)
            self.request_frame.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(self.request_frame, text="当前设备码", anchor="w", font=ctk.CTkFont(size=16, weight="bold")).grid(
                row=0, column=0, sticky="ew")
            self.device_text = ctk.CTkTextbox(self.request_frame, height=82, fg_color="#F1F5F2", text_color="#31463A",
                                               font=ctk.CTkFont(size=14), wrap="char")
            self.device_text.grid(row=1, column=0, sticky="ew", pady=(8, 8))
            self._set_device_text("正在读取设备信息…")
            actions = ctk.CTkFrame(self.request_frame, fg_color="transparent")
            actions.grid(row=2, column=0, sticky="ew", pady=20)
            actions.grid_columnconfigure((0, 1), weight=1)
            self.copy_button = ctk.CTkButton(actions, text="复制设备码", command=self._copy_device_code,
                                                  state="disabled", height=44, fg_color="#E8EFEB", text_color="#254638", hover_color="#D9E6DE")
            self.copy_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
            self.next_button = ctk.CTkButton(actions, text="下一步", command=lambda: self._show_step(2),
                                                  state="disabled", height=44, fg_color="#12796C", hover_color="#0D665B")
            self.next_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))
            self.activation_frame = ctk.CTkFrame(card, fg_color="transparent")
            self.activation_frame.grid(row=3, column=0, sticky="ew", padx=24)
            self.activation_frame.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(self.activation_frame, text="授权码（粘贴完整签名授权码）", anchor="w", font=ctk.CTkFont(size=16, weight="bold")).grid(
                row=0, column=0, sticky="ew", pady=(0, 8))
            self.authorization_text = ctk.CTkTextbox(self.activation_frame, height=100, wrap="char", fg_color="#F1F5F2")
            self.authorization_text.grid(row=1, column=0, sticky="ew")
            activation_actions = ctk.CTkFrame(self.activation_frame, fg_color="transparent")
            activation_actions.grid(row=2, column=0, sticky="ew", pady=20)
            activation_actions.grid_columnconfigure((0, 1), weight=1)
            self.back_button = ctk.CTkButton(activation_actions, text="上一步", command=lambda: self._show_step(1),
                                                  height=44, fg_color="#E8EFEB", text_color="#254638", hover_color="#D9E6DE")
            self.back_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
            self.import_button = ctk.CTkButton(activation_actions, text="验证并激活", command=self._import,
                                                  state="disabled", height=44, fg_color="#12796C", hover_color="#0D665B")
            self.import_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))
            self.preview = ctk.CTkLabel(self.activation_frame, text="尚未激活", anchor="w", justify="left",
                                           fg_color="#F1F5F2", corner_radius=12, height=95, wraplength=540)
            self.preview.grid(row=3, column=0, sticky="ew", pady=(0, 16))
            self.status = ctk.CTkLabel(card, text=initial_error,
                                          anchor="w", justify="left", wraplength=540, text_color="#65746B")
            self.status.grid(row=4, column=0, sticky="ew", padx=24, pady=(0, 12))
            self.enter_button = ctk.CTkButton(self.activation_frame, text="进入工作台", command=self._enter, state="disabled", height=42)
            self.enter_button.grid(row=4, column=0, sticky="ew", pady=(0, 24))
            self.retry_button = ctk.CTkButton(self.activation_frame, text="重新验证已有授权", command=self._enter, height=36)
            self.retry_button.grid(row=5, column=0, sticky="ew", pady=(0, 24))
            # 首屏仅展示申请步骤；隐藏父容器，避免授权输入和按钮提前出现。
            self.activation_frame.grid_remove()
            self.step = 1
            ctk.CTkButton(self, text="退出", command=self.destroy, width=90, fg_color="#E0E8E2",
                          text_color="#354A3D", hover_color="#D1DDD4").grid(row=1, column=0, sticky="e", padx=24, pady=(0, 16))

        def _show_step(self, step: int) -> None:
            """仅切换展示步骤，保留输入草稿；校验期间禁止切换避免状态混乱。"""
            if self.verifying or step not in (1, 2):
                return
            if step == 2 and not self.code:
                self._notice("设备码尚未获取，请稍候再继续。", True)
                return
            self.step = step
            if step == 1:
                self.activation_frame.grid_remove()
                self.request_frame.grid()
                self.step_description.configure(text="第 1 步 / 共 2 步：申请授权\n复制设备码发送给管理员，收到授权码后点击“下一步”。\n一份授权绑定一台设备，启动时需联网获取 NTP 时间。")
            else:
                self.request_frame.grid_remove()
                self.activation_frame.grid()
                self.step_description.configure(text="第 2 步 / 共 2 步：输入授权\n粘贴管理员提供的完整授权码，点击“验证并激活”。\n验证成功后会显示有效期，并保存本机授权。")
                self.authorization_text.focus_set()

        def _set_device_text(self, text: str) -> None:
            """更新只读设备码，复制时使用已读取值而非用户编辑文本。"""
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
                for button in (self.copy_button, self.next_button, self.import_button):
                    button.configure(state="normal")

        def _notice(self, text: str, error: bool = False) -> None:
            """在窗口内展示操作反馈，无须用户关闭额外消息弹框。"""
            self.status.configure(text=text, text_color="#A5322D" if error else "#12796C")

        def _enter(self) -> None:
            """进入前再次核对到期与设备，避免导入后长时间停留绕过期限。"""
            self._start_verify(None, True)

        def _copy_device_code(self) -> None:
            """复制真实设备码到系统剪贴板，不再生成申请文件。"""
            if not self.code:
                self._notice("设备码尚未获取，请稍候再复制。", True)
                return
            try:
                self.clipboard_clear()
                self.clipboard_append(self.code)
                self.update_idletasks()
                self._notice("设备码已复制，请粘贴发送给管理员。")
            except TclError:
                self._notice("复制失败，请从设备码区域手动选择并复制。", True)

        def _import(self) -> None:
            """读取用户粘贴的授权码，不再打开文件选择对话框。"""
            code = self.authorization_text.get("1.0", "end").strip()
            if not code:
                show_authorization_error(self, "请先粘贴完整授权码")
                return
            self._start_verify(code, False)

        def _start_verify(self, code: str | None, enter: bool) -> None:
            """后台校验网络与签名，网络超时期间保持窗口可响应。"""
            if self.verifying:
                return
            self.verifying = True
            self.verify_result = None
            self.badge.configure(text="未激活")
            for button in (self.import_button, self.enter_button, self.retry_button, self.back_button):
                button.configure(state="disabled")
            self.authorization_text.configure(state="disabled")
            self.preview.configure(text="尚未通过授权验证")
            self._notice("正在验证签名、设备与网络时间，请稍候…")

            def verify() -> None:
                """工作线程仅回传校验结果，不在后台操作 Tk 控件。"""
                try:
                    payload = manager.import_code(code) if code is not None else manager.require()
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
            self.back_button.configure(state="normal")
            self.authorization_text.configure(state="normal")
            if error:
                self._notice(error, True)
                show_authorization_error(self, error)
                return
            if enter:
                self.accepted = True
                self.destroy()
            else:
                # 限制未验证文件中的文本长度，避免异常内容撑破窗口布局。
                owner = str(payload.get("customer") or "未填写")[:80]
                expires = authorization_expiry_text(payload)
                license_id = str(payload.get("license_id") or "未填写")[:80]
                self.preview.configure(text=f"客户：{owner}\n授权编号：{license_id}\n到期时间（北京时间）：{expires}")
                self.badge.configure(text="授权有效", fg_color="#E7F5ED", text_color="#12796C")
                self.enter_button.configure(state="normal")
                self._notice("授权验证通过并已保存，点击进入工作台。")
                self.authorization_text.delete("1.0", "end")
                # 此分支只用于主动导入，已有授权直接启动不需要再次提醒。
                show_authorization_success(self, payload)

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
