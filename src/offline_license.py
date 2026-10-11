"""授权文件验签与 NTP 期限管理；客户端只持有公钥，不能签发授权。"""
from __future__ import annotations

import base64
import json
import os
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Any, Callable
from network_time import NetworkClock

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PRODUCT = "douyin-comment-crawler"
DEVICE_PATTERN = r"DY1-(?:[A-F0-9]{8}-){7}[A-F0-9]{8}"


class LicenseError(ValueError):
    """可直接显示给用户的授权校验错误。"""


def canonical_json(payload: dict[str, Any]) -> bytes:
    """固定 JSON 编码规则，使签发端和用户端验证完全相同的字节。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复字段，避免同一个文件被不同解析器解释成不同授权。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise LicenseError("文件包含重复字段")
        result[key] = value
    return result


def read_json(path: Path) -> dict[str, Any]:
    """有界读取授权/申请/时间记录，拒绝损坏文件和非对象 JSON。"""
    try:
        with path.open("rb") as source:
            content = source.read(65537)
        if len(content) > 65536:
            raise LicenseError("文件超过 64 KB")
        data = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object)
        if not isinstance(data, dict):
            raise LicenseError("文件内容必须是 JSON 对象")
        return data
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise LicenseError(f"无法读取授权相关文件：{error}") from error


def utc_time(value: Any) -> datetime:
    """只接受带时区的 ISO 时间，防止时区不同导致到期边界不一致。"""
    try:
        if not isinstance(value, str):
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError) as error:
        raise LicenseError("授权时间格式错误，必须包含时区") from error


def verify_license(data: dict[str, Any], public_pem: bytes, code: str, now: datetime) -> dict[str, Any]:
    """先验 Ed25519 签名再验证产品、设备、生效时间及到期时间。"""
    payload = data.get("payload")
    try:
        key = serialization.load_pem_public_key(public_pem)
        if not isinstance(key, Ed25519PublicKey) or not isinstance(payload, dict):
            raise ValueError()
        signature = base64.b64decode(data["signature"], validate=True)
        key.verify(signature, canonical_json(payload))
    except (InvalidSignature, ValueError, TypeError, KeyError) as error:
        raise LicenseError("授权签名无效或文件被修改，请联系管理员") from error
    if payload.get("product") != PRODUCT or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise LicenseError("授权产品或版本不匹配")
    if not re.fullmatch(DEVICE_PATTERN, code) or payload.get("device_code") != code:
        raise LicenseError("授权文件不属于当前设备")
    if not all(isinstance(payload.get(field), str) and payload[field].strip() for field in ("license_id", "customer")):
        raise LicenseError("授权编号或客户信息缺失")
    start, end = utc_time(payload.get("issued_at")), utc_time(payload.get("expires_at"))
    if end <= start:
        raise LicenseError("授权期限无效")
    if now < start:
        raise LicenseError("授权尚未生效或网络时间异常")
    if now >= end:
        raise LicenseError("授权已过期，请导入新的授权文件")
    return dict(payload)


def user_data_dir() -> Path:
    """数据写入用户可写目录，不写入 EXE 解压目录或 Program Files。"""
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path.home() / "Library" / "Application Support"
    return base / "DouyinCommentWorkbench" / "licensing"


def public_key_path() -> Path:
    """只读取随程序发布的公钥，不信任授权文件自带的公钥。"""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / "licensing" / "license_public_key.pem"


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    """同目录临时文件加原子替换，避免断电留下半份授权记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(canonical_json(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp and temp.exists():
            temp.unlink()


class LicenseManager:
    """线程安全的签名授权；以网络 UTC 和单调时钟验证期限。"""

    def __init__(self, data_dir: Path | None = None, key_path: Path | None = None, code: str | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        """初始化路径和单调时钟基准，不生成私钥或随机设备身份。"""
        self.data_dir = data_dir or user_data_dir()
        self.key_path = key_path or public_key_path()
        self.code = code
        self.lock = RLock()
        self.network_clock = NetworkClock()
        self.clock = clock or self.network_clock.now

    def device(self) -> str:
        """延迟读取设备标识，避免在模块导入时执行系统查询。"""
        with self.lock:
            if self.code is None:
                from activation_window import current_device_code
                self.code = current_device_code()
            return self.code

    def _now(self) -> datetime:
        """只读取网络时钟，不以本机时间或历史 JSON 代替联网结果。"""
        return self.clock()

    def _verify(self, data: dict[str, Any]) -> dict[str, Any]:
        """使用固定发行公钥验签，并按网络时间核对有效期与设备。"""
        try:
            pem = self.key_path.read_bytes()
        except OSError as error:
            raise LicenseError("程序缺少授权公钥，请联系管理员重新提供程序") from error
        now = self._now()
        payload = verify_license(data, pem, self.device(), now)
        return payload

    def require(self) -> dict[str, Any]:
        """启动和操作前校验保存的授权，没有授权或损坏时关闭访问。"""
        with self.lock:
            path = self.data_dir / "license.lic"
            if not path.exists():
                raise LicenseError("尚未激活，请导入管理员签发的授权文件")
            return self._verify(read_json(path))

    def import_license(self, path: Path) -> dict[str, Any]:
        """完整验证成功后才替换本机授权，导入失败不覆盖已有有效文件。"""
        with self.lock:
            data = read_json(path)
            payload = self._verify(data)
            atomic_json(self.data_dir / "license.lic", data)
            return payload
