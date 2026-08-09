from __future__ import annotations

from argparse import ArgumentParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import webbrowser


def load_viewer_html() -> bytes:
    return Path(__file__).with_name("viewer.html").read_bytes()


class ViewerRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in {"/", "/index.html"}:
            self.send_error(404)
            return

        body = load_viewer_html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; "
            "style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; "
            "img-src blob: data:; "
            "connect-src 'none'; "
            "base-uri 'none'; "
            "form-action 'none'; "
            "frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), ViewerRequestHandler)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="启动本地抖音评论查看页面")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=0, help="监听端口，0 表示自动选择")
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="启动后不自动打开浏览器",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    server = create_server(args.host, args.port)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    print(f"评论查看器已启动：{url}")
    print("关闭 PyCharm 运行进程即可停止查看器。")
    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
