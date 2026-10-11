"""Windows EXE 的托盘入口；网页关闭后仍可打开工作台或退出后台。"""

from threading import Event, Thread
from typing import Callable
import webbrowser
import sys


def set_windows_process_icons() -> list[int]:
    """从当前 EXE 提取图标并设置本进程窗口图标，返回需释放的句柄。"""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return []
    import ctypes
    from ctypes import wintypes
    import os

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    shell.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int,
                                   ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON), wintypes.UINT]
    shell.ExtractIconExW.restype = wintypes.UINT
    large, small = wintypes.HICON(), wintypes.HICON()
    if not shell.ExtractIconExW(sys.executable, 0, ctypes.byref(large), ctypes.byref(small), 1):
        return []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                       wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_size_t)]
    user.SendMessageTimeoutW.restype = wintypes.LPARAM

    @callback_type
    def update_window(hwnd, parameter):
        """仅设置本进程窗口，避免修改浏览器或其他程序的图标。"""
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            for kind, handle in ((0, small.value), (1, large.value)):
                if handle:
                    result = ctypes.c_size_t()
                    # WM_SETICON；超时返回，避免某个窗口不响应导致退出卡住。
                    user.SendMessageTimeoutW(hwnd, 0x0080, kind, handle, 0x0002, 500, ctypes.byref(result))
        return True

    user.EnumWindows(update_window, 0)
    return [handle for handle in (large.value, small.value) if handle]


def release_windows_icons(handles: list[int]) -> None:
    """托盘窗口销毁后释放提取的图标，防止句柄残留。"""
    if not handles:
        return
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.DestroyIcon.argtypes = [wintypes.HICON]
    user.DestroyIcon.restype = wintypes.BOOL
    for handle in set(handles):
        user.DestroyIcon(handle)


class WorkbenchTray:
    def __init__(self, url: str, shutdown: Callable[[], None]) -> None:
        """创建托盘菜单，延迟导入桌面依赖以保持源码跨平台启动。"""
        import pystray
        from app_icon import create_app_icon

        self.url = url
        self.shutdown = shutdown
        self.exiting = Event()
        self.process_icons: list[int] = []
        image = create_app_icon(64)
        self.icon = pystray.Icon(
            "douyin-workbench", image, "抖音评论工作台",
            menu=pystray.Menu(
                pystray.MenuItem("打开工作台", self.open_workbench, default=True),
                pystray.MenuItem("退出", self.request_exit),
            ),
        )

    def open_workbench(self, icon=None, item=None) -> None:
        """重新打开本机工作台，不重复启动服务或采集任务。"""
        if not self.exiting.is_set():
            webbrowser.open(self.url)

    def request_exit(self, icon=None, item=None) -> None:
        """只请求一次退出，在独立线程停服务，避免阻塞托盘消息循环。"""
        if self.exiting.is_set():
            return
        self.exiting.set()
        # HTTP shutdown 不能在 serve_forever 所在线程直接调用，否则死锁。
        Thread(target=self.shutdown, name="workbench-exit", daemon=True).start()

    def start(self) -> None:
        """启动 Windows 托盘循环，使 HTTP 服务继续在主线程运行。"""
        self.icon.run_detached(setup=self._setup)

    def _setup(self, icon) -> None:
        """托盘窗口创建完成后再设置窗口图标，避免启动时找不到窗口。"""
        icon.visible = True
        self.process_icons = set_windows_process_icons()

    def stop(self) -> None:
        """移除托盘图标，完成应用退出。"""
        self.exiting.set()
        self.icon.stop()
        release_windows_icons(self.process_icons)
        self.process_icons = []
