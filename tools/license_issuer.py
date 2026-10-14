"""仅管理员使用的本机授权网页；不得打包给用户或公开部署。"""
from __future__ import annotations

import base64
import hmac
import hashlib
import json
import os
import re
import secrets
import sys
import webbrowser
from argparse import ArgumentParser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from tempfile import NamedTemporaryFile
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from offline_license import (PRODUCT, DEVICE_PATTERN, LICENSE_SCHEMA_VERSION, LicenseError,
                             canonical_json, decode_license_code, encode_license_code, utc_time, _unique_object)
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def generate_key_pair(password: str) -> tuple[bytes, bytes]:
    """在内存生成 Ed25519 密钥，私钥以密码加密，不自动写入磁盘。"""
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError("私钥密码须为 12—1024 个字符，请妥善备份")
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.BestAvailableEncryption(password.encode("utf-8")))
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, public


def create_keys(directory: Path, password: str) -> None:
    """保留程序调用入口，独占创建两份密钥，绝不覆盖已有文件。"""
    private_path, public_path = directory / "license_private_key.pem", directory / "license_public_key.pem"
    if private_path.exists() or public_path.exists():
        raise ValueError("该目录已有密钥，请选择新目录，不可覆盖旧密钥")
    private, public = generate_key_pair(password)
    descriptor = os.open(private_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(private)
    with public_path.open("xb") as stream:
        stream.write(public)


def issue_license_bytes(request: dict, private_pem: bytes, password: str, expiry_date: str) -> dict:
    """按北京时间精确到秒签发；不再把所选日期延长到次日零点。"""
    code = request.get("device_code", "")
    if (request.get("product") != PRODUCT or type(request.get("schema_version")) is not int
            or request["schema_version"] != LICENSE_SCHEMA_VERSION or not isinstance(code, str) or not re.fullmatch(DEVICE_PATTERN, code)):
        raise ValueError("授权申请产品、版本或设备码无效")
    if not isinstance(password, str) or not password or len(password) > 1024:
        raise ValueError("请输入正确的私钥密码")
    if not isinstance(expiry_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?", expiry_date):
        raise ValueError("到期时间格式须为 YYYY-MM-DDTHH:MM:SS（北京时间）")
    try:
        # datetime-local 不携带时区且可能省略零秒，统一补为北京时间、秒级精度。
        expires = datetime.fromisoformat(expiry_date).replace(tzinfo=timezone(timedelta(hours=8)))
    except (ValueError, OverflowError) as error:
        raise ValueError("到期时间无效") from error
    now = datetime.now(timezone.utc)
    if expires <= now:
        raise ValueError("请选择尚未过期的时间")
    try:
        key = serialization.load_pem_private_key(private_pem, password=password.encode("utf-8"))
    except (ValueError, TypeError) as error:
        raise ValueError("私钥文件或密码不正确，请检查后重试") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("私钥类型必须为 Ed25519")
    payload = {"schema_version": LICENSE_SCHEMA_VERSION, "product": PRODUCT, "device_code": code,
               "license_id": uuid4().hex,
               "issued_at": now.isoformat(), "expires_at": expires.isoformat(timespec="seconds")}
    return {"payload": payload, "signature": base64.b64encode(key.sign(canonical_json(payload))).decode("ascii")}


def issue_license(request: dict, key_path: Path, password: str, expiry_date: str) -> dict:
    """保留文件型调用入口；网页独立管理上传的加密私钥副本。"""
    return issue_license_bytes(request, key_path.read_bytes(), password, expiry_date)


def issue_license_for_device(code: str, private_pem: bytes, password: str, expiry_date: str) -> dict:
    """直接用设备码签发；产品与版本由管理员服务固定，不由网页指定。"""
    if not isinstance(code, str) or not re.fullmatch(DEVICE_PATTERN, code.strip()):
        raise ValueError("设备码格式无效，请粘贴客户端复制的完整设备码")
    request = {"schema_version": LICENSE_SCHEMA_VERSION, "product": PRODUCT, "device_code": code.strip()}
    return issue_license_bytes(request, private_pem, password, expiry_date)


def inspect_license_code(code: str, public_key: Path | None = None) -> dict[str, str]:
    """验签后反查授权码的北京时间到期时间；已过期的签名仍可核验。"""
    data = decode_license_code(code)
    payload = data["payload"]
    if (payload.get("product") != PRODUCT or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != LICENSE_SCHEMA_VERSION
            or not isinstance(payload.get("device_code"), str)
            or not re.fullmatch(DEVICE_PATTERN, payload["device_code"])
            or not isinstance(payload.get("license_id"), str) or not payload["license_id"].strip()):
        raise ValueError("授权码的产品、版本或设备信息无效")
    issued_at, expires_at = utc_time(payload.get("issued_at")), utc_time(payload.get("expires_at"))
    if expires_at <= issued_at:
        raise ValueError("授权码的到期时间无效")
    key_path = public_key or Path(__file__).resolve().parents[1] / "src" / "licensing" / "license_public_key.pem"
    try:
        key = serialization.load_pem_public_key(key_path.read_bytes())
    except (OSError, ValueError, TypeError) as error:
        raise ValueError("无法读取发行公钥，请检查 src/licensing/license_public_key.pem") from error
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("发行公钥不是 Ed25519 类型")
    try:
        signature = base64.b64decode(data["signature"], validate=True)
        key.verify(signature, canonical_json(payload))
    except (InvalidSignature, ValueError) as error:
        raise ValueError("授权签名无效：授权码可能被修改，或与当前发行公钥不匹配") from error
    return {"license_id": payload["license_id"],
            "expires_at_beijing": expires_at.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")}


class SavedKeyStore:
    """管理员专用加密私钥存储，不保存密码，不接收任意磁盘路径。"""

    def __init__(self, directory: Path | None = None) -> None:
        """私钥放在项目的管理员专用目录，测试可显式指定临时目录。"""
        if directory is None:
            # 根据脚本位置定位项目，不受 PyCharm 工作目录影响；该目录不参与客户端打包。
            directory = Path(__file__).resolve().parent / "admin_keys"
        self.path = directory / "encrypted_private_key.pem"
        self.lock = RLock()

    def status(self) -> dict:
        """仅列出文件名称和标识，私钥内容不发送回网页。"""
        with self.lock:
            keys = []
            if self.path.is_file():
                keys.append({"id": "legacy", "name": "此前保存的加密私钥"})
            for path in sorted(self.path.parent.glob("*.json")):
                if re.fullmatch(r"[a-f0-9]{64}", path.stem):
                    record = self._record(path.stem)
                    keys.append({"id": path.stem, "name": record["name"]})
            return {"keys": keys}

    def _record(self, key_id: str) -> dict:
        """只允许固定格式的文件标识，拒绝路径穿越及损坏的存储记录。"""
        if not isinstance(key_id, str) or not re.fullmatch(r"[a-f0-9]{64}", key_id):
            raise ValueError("请选择有效的私钥文件")
        with (self.path.parent / f"{key_id}.json").open("rb") as stream:
            raw = stream.read(131073)
        if len(raw) > 131072:
            raise ValueError("私钥存储记录过大")
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(record, dict) or not isinstance(record.get("name"), str)
                or not isinstance(record.get("private_pem"), str)):
            raise ValueError("私钥存储记录损坏")
        return record

    def read(self, key_id: str) -> bytes:
        """按选择读取加密 PEM，旧缓存仍可通过单独条目选择。"""
        with self.lock:
            if key_id == "legacy":
                with self.path.open("rb") as source:
                    pem = source.read(65537)
            else:
                pem = self._record(key_id)["private_pem"].encode("ascii")
                if hashlib.sha256(pem).hexdigest() != key_id:
                    raise ValueError("私钥存储记录损坏")
            self._validate_pem(pem)
            return pem

    @staticmethod
    def _validate_pem(pem: bytes) -> None:
        """拒绝明文密钥；上传仅检查封装，密码和密钥类型在签发时验证。"""
        if (len(pem) > 65536 or not re.fullmatch(
                rb"-----BEGIN ENCRYPTED PRIVATE KEY-----\r?\n[A-Za-z0-9+/=\r\n]+-----END ENCRYPTED PRIVATE KEY-----\s*", pem)):
            raise ValueError("只允许上传加密 PEM 私钥")

    def save(self, pem: bytes, name: str) -> str:
        """保存多份加密私钥，同内容去重、同名不同内容不覆盖。"""
        self._validate_pem(pem)
        if not isinstance(name, str) or not name.strip() or len(name) > 255:
            raise ValueError("私钥文件名称无效")
        name = name.replace("\\", "/").rsplit("/", 1)[-1]
        key_id = hashlib.sha256(pem).hexdigest()
        with self.lock:
            target = self.path.parent / f"{key_id}.json"
            if target.exists():
                self.read(key_id)
                return key_id
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = None
            try:
                with NamedTemporaryFile(dir=self.path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    if sys.platform != "win32":
                        os.fchmod(stream.fileno(), 0o600)
                    # 记录仅包含文件名及仍受密码加密的 PEM，不保存签发密码。
                    stream.write(canonical_json({"name": name, "private_pem": pem.decode("ascii")}))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()
            return key_id

    def clear(self, key_id: str) -> None:
        """只删除所选管理员副本，其他密钥和原始文件均保留。"""
        with self.lock:
            if key_id == "legacy":
                self.path.unlink(missing_ok=True)
            else:
                self._record(key_id)
                (self.path.parent / f"{key_id}.json").unlink()


class IssuerServer(ThreadingHTTPServer):
    def __init__(self, port: int = 8787, key_directory: Path | None = None) -> None:
        """仅绑定回环地址，每次启动使用新的防跨站令牌。"""
        self.token = secrets.token_urlsafe(32)
        self.saved_keys = SavedKeyStore(key_directory)
        super().__init__(("127.0.0.1", port), IssuerHandler)

    @property
    def origin(self) -> str:
        """返回浏览器应访问的唯一 Host 和 Origin。"""
        return f"http://127.0.0.1:{self.server_port}"


class IssuerHandler(BaseHTTPRequestHandler):
    server: IssuerServer

    def setup(self) -> None:
        """限制请求读取等待时间，避免慢请求占用服务线程。"""
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format: str, *args: object) -> None:
        """不记录请求内容、文件、密码或下载响应。"""
        return

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        """禁用缓存和跨站嵌入，禁止外部脚本与外部请求。"""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", f"default-src 'none'; script-src 'nonce-{self.server.token}'; style-src 'nonce-{self.server.token}'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        """仅返回明确结果，不在错误信息中泄漏密码或文件内容。"""
        self._send(status, canonical_json(payload))

    def _local_request(self) -> bool:
        """校验 Host 与浏览器来源，拒绝 DNS 重绑定及跨站请求。"""
        return (self.headers.get("Host") == self.server.origin.removeprefix("http://")
                and self.headers.get("Origin") in {None, self.server.origin}
                and self.headers.get("Sec-Fetch-Site") not in {"cross-site", "same-site"})

    def do_GET(self) -> None:
        """显示管理员 HTML，不提供任意磁盘路径访问。"""
        if not self._local_request():
            self._json(403, {"error": "只允许本机管理员页面访问"})
            return
        if self.path != "/":
            self._json(404, {"error": "页面不存在"})
            return
        html = Path(__file__).with_suffix(".html").read_text(encoding="utf-8")
        self._send(200, html.replace("__ISSUER_TOKEN__", self.server.token).encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        """本机令牌验证后操作；仅显式保存加密私钥，不持久化密码。"""
        if not self._local_request() or not hmac.compare_digest(self.headers.get("X-Issuer-Token", "").encode("utf-8"), self.server.token.encode("ascii")):
            self._json(403, {"error": "请求已失效或来源不可信，请刷新管理员页面"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self._json(400, {"error": "仅支持 JSON 请求"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 196608:
                raise ValueError("请求大小无效，最多 192 KB")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("请求内容不完整")
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(data, dict):
                raise ValueError("请求须为 JSON 对象")
            if self.path == "/api/keys":
                private, public = generate_key_pair(data.get("password"))
                self._json(200, {"private_pem": private.decode("ascii"), "public_pem": public.decode("ascii")})
            elif self.path == "/api/issue":
                private_pem = self.server.saved_keys.read(data.get("key_id"))
                signed = issue_license_for_device(data.get("device_code"), private_pem, data.get("password"), data.get("expires"))
                self._json(200, {"license": signed, "authorization_code": encode_license_code(signed), **self.server.saved_keys.status()})
            elif self.path == "/api/inspect":
                # 查询只读取授权码和发行公钥，不使用私钥，也不要求授权尚未到期。
                try:
                    self._json(200, inspect_license_code(data.get("authorization_code")))
                except (LicenseError, ValueError) as error:
                    self._json(400, {"error": str(error)})
            elif self.path == "/api/key/upload":
                private = data.get("private_pem")
                if not isinstance(private, str) or len(private) > 65536:
                    raise ValueError("请选择有效私钥")
                key_id = self.server.saved_keys.save(private.encode("ascii"), data.get("name"))
                self._json(200, {"key_id": key_id, **self.server.saved_keys.status()})
            elif self.path == "/api/key/status":
                self._json(200, self.server.saved_keys.status())
            elif self.path == "/api/key/clear":
                self.server.saved_keys.clear(data.get("key_id"))
                self._json(200, self.server.saved_keys.status())
            else:
                self._json(404, {"error": "接口不存在"})
        except (ValueError, UnicodeError, OverflowError, TypeError, RecursionError, OSError):
            # JSON 解析错误可能含用户输入，因此统一提示而不回显异常原文。
            self._json(400, {"error": "操作失败：请检查加密私钥文件、文件选择、私钥密码、设备码格式及未过期的到期时间（北京时间）。"})


def main() -> None:
    """PyCharm 运行后自动打开管理员网页，无需客户端授权。"""
    parser = ArgumentParser(description="启动本机管理员授权网页")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    server = IssuerServer(args.port)
    print(f"管理员授权网页：{server.origin}/（仅本机访问）")
    if not args.no_open:
        webbrowser.open(server.origin + "/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
