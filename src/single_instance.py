"""Windows 单实例控制：重复启动通过事件唤起已有工作台。"""

from threading import Event, Thread
from typing import Callable
import ctypes
from ctypes import wintypes


class WindowsSingleInstance:
    def __init__(self) -> None:
        """创建会话内命名互斥量和唤起事件，内核对象随进程退出自动释放。"""
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateEventW.restype = wintypes.HANDLE
        self.kernel.SetEvent.argtypes = [wintypes.HANDLE]
        self.kernel.SetEvent.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.mutex = self.kernel.CreateMutexW(None, False, r"Local\DouyinCommentWorkbench.Instance.v1")
        if not self.mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        self.is_primary = ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
        # 自动复位事件可记住启动过程中收到的唤起请求，无需读写临时配置文件。
        self.event = self.kernel.CreateEventW(None, False, False, r"Local\DouyinCommentWorkbench.Activate.v1")
        if not self.event:
            error = ctypes.get_last_error()
            self.kernel.CloseHandle(self.mutex)
            self.mutex = None
            raise ctypes.WinError(error)
        self.stopping = Event()
        self.worker: Thread | None = None

    def activate_existing(self) -> None:
        """通知已运行进程打开自己的 URL，不创建浏览器服务或托盘。"""
        if not self.kernel.SetEvent(self.event):
            raise ctypes.WinError(ctypes.get_last_error())

    def listen(self, activate: Callable[[], None]) -> None:
        """服务准备好后监听唤起事件，重复点击不改变正在执行的任务。"""
        def watch() -> None:
            """短超时等待事件，让退出不会被监听线程阻塞。"""
            while not self.stopping.is_set():
                result = self.kernel.WaitForSingleObject(self.event, 250)
                if self.stopping.is_set():
                    return
                if result == 0:
                    activate()
                elif result != 258:  # WAIT_TIMEOUT
                    return

        self.worker = Thread(target=watch, name="workbench-activate", daemon=True)
        self.worker.start()

    def close(self) -> None:
        """停止监听并释放内核句柄，退出后允许再次启动。"""
        self.stopping.set()
        if self.worker is not None:
            self.worker.join(timeout=2)
        for attribute in ("event", "mutex"):
            handle = getattr(self, attribute, None)
            if handle:
                self.kernel.CloseHandle(handle)
                setattr(self, attribute, None)
