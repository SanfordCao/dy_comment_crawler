"""通过阿里云与腾讯云 NTP 取时，不使用本机墙上时钟作为授权依据。"""

from datetime import datetime, timedelta, timezone
from threading import RLock, Event, Thread
import secrets
import socket
import struct
import time

ALIYUN_SOURCES = ("ntp.aliyun.com", *(f"ntp{i}.aliyun.com" for i in range(1, 8)))
TENCENT_SOURCES = ("ntp.tencent.com", *(f"ntp{i}.tencent.com" for i in range(1, 6)))
# 交替尝试不同厂商，避免单一厂商网络异常导致等待完所有同厂备用地址。
TIME_SOURCES = tuple(source for index in range(len(ALIYUN_SOURCES))
                     for source in (ALIYUN_SOURCES[index], TENCENT_SOURCES[index] if index < len(TENCENT_SOURCES) else None)
                     if source is not None)
QUERY_TIMEOUT = 2
NTP_EPOCH = datetime(1900, 1, 1, tzinfo=timezone.utc)


class NetworkTimeError(ValueError):
    """所有时间源失败时拒绝授权，错误可直接展示给用户。"""


def _decode_timestamp(value: bytes) -> datetime:
    """解析 NTP 64 位时间戳，兼容 2036 年后的 era 回绕。"""
    seconds, fraction = struct.unpack("!II", value)
    if seconds == 0 and fraction == 0:
        raise ValueError("NTP 时间戳为空")
    if seconds < 3913056000:  # 2024 年之前的 era 0 不作当前时间使用。
        seconds += 2 ** 32
    result = NTP_EPOCH + timedelta(seconds=seconds + fraction / 2 ** 32)
    if not 2024 <= result.year <= 2100:
        raise ValueError("NTP 时间超出合理范围")
    return result


def query_ntp(source: str) -> datetime:
    """向一个公网 NTP 源发送单次查询，校验响应身份、同步状态和时间戳。"""
    request = bytearray(48)
    request[0] = 0x23  # NTP v4，客户端模式。
    # 用不可预测请求标记匹配 originate 字段，不依赖本机时间，也不修改系统时钟。
    request[40:48] = secrets.token_bytes(8)
    started = time.monotonic()
    ready = Event()
    resolved = []

    def resolve() -> None:
        """隔离 DNS 阻塞，解析超时不阻塞启动线程或等待备用源。"""
        try:
            resolved.append(socket.getaddrinfo(source, 123, socket.AF_INET, socket.SOCK_DGRAM))
        except OSError as error:
            resolved.append(error)
        finally:
            ready.set()

    Thread(target=resolve, name="ntp-dns", daemon=True).start()
    if not ready.wait(QUERY_TIMEOUT):
        raise TimeoutError("NTP DNS 解析超时")
    if isinstance(resolved[0], OSError):
        raise resolved[0]
    addresses = resolved[0]
    if not addresses:
        raise OSError("NTP 域名没有可用地址")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        remaining = QUERY_TIMEOUT - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("NTP 查询超时")
        client.settimeout(remaining)
        # UDP connect 让内核只接收指定服务器地址/端口的数据，过滤其他来源。
        client.connect(addresses[0][4])
        client.send(request)
        packet = client.recv(1024)
    elapsed = time.monotonic() - started
    if len(packet) < 48 or elapsed > QUERY_TIMEOUT:
        raise ValueError("NTP 响应过短或延迟过大")
    leap, version, mode = packet[0] >> 6, (packet[0] >> 3) & 7, packet[0] & 7
    if leap == 3 or version not in {3, 4} or mode != 4 or not 1 <= packet[1] <= 15:
        raise ValueError("NTP 服务器未同步或响应模式/层级无效")
    if packet[24:32] != request[40:48]:
        raise ValueError("NTP 响应与当前请求不匹配")
    received = _decode_timestamp(packet[32:40])
    transmitted = _decode_timestamp(packet[40:48])
    if not 0 <= (transmitted - received).total_seconds() <= elapsed + 1:
        raise ValueError("NTP 接收/发送时间戳关系无效")
    # 授权判断采用服务端发送时间，不计算依赖本机墙上时钟的 offset。
    return transmitted


def fetch_network_time() -> tuple[datetime, str]:
    """交替尝试两家公网 NTP 地址，全部失败拒绝启动，不回退本机时间。"""
    failures = []
    for source in TIME_SOURCES:
        try:
            return query_ntp(source), source
        except (OSError, ValueError, TypeError, OverflowError) as error:
            failures.append(f"{source}: {error}")
    raise NetworkTimeError("无法获取网络时间，阿里云与腾讯云 NTP 均不可用；请检查网络及防火墙是否允许 UDP 123 后重试。" + " / ".join(failures))


class NetworkClock:
    def __init__(self) -> None:
        """每次进程启动重新取时，运行期仅以内存单调时钟推进。"""
        self.lock = RLock()
        self.base: datetime | None = None
        self.tick = 0.0
        self.source = ""

    def now(self) -> datetime:
        """首次及每五分钟联网校时；双源失败时不继续使用旧样本放行。"""
        with self.lock:
            elapsed = time.monotonic() - self.tick
            if self.base is None or elapsed >= 300:
                now, source = fetch_network_time()
                if self.base is not None and now < self.base + timedelta(seconds=elapsed) - timedelta(seconds=10):
                    raise NetworkTimeError("网络时间明显回退，请稍后重试或联系管理员")
                self.base = max(now, self.base + timedelta(seconds=elapsed)) if self.base else now
                self.tick = time.monotonic()
                self.source = source
            return self.base + timedelta(seconds=time.monotonic() - self.tick)
