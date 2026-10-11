"""Windows EXE 的托盘入口；网页关闭后仍可打开工作台或退出后台。"""

from threading import Event, Thread
from typing import Callable
import webbrowser


class WorkbenchTray:
    def __init__(self, url: str, shutdown: Callable[[], None]) -> None:
        """创建托盘菜单，延迟导入桌面依赖以保持源码跨平台启动。"""
        import pystray
        from app_icon import create_app_icon

        self.url = url
        self.shutdown = shutdown
        self.exiting = Event()
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
        self.icon.run_detached()

    def stop(self) -> None:
        """移除托盘图标，完成应用退出。"""
        self.exiting.set()
        self.icon.stop()
