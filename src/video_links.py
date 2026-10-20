"""分享文本提取与短链接解析；只有确认视频 ID 后才进入评论采集。"""

from __future__ import annotations

import asyncio
import json
import re
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from playwright.async_api import Error as PlaywrightError, async_playwright


class ShareValidationError(ValueError):
    """携带全部失败行的校验异常，供页面按行号弹窗提示。"""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        """保存结构化错误，不把分享文本作为 HTML 输出。"""
        self.issues = issues
        super().__init__("分享文本校验未通过，未启动任何采集任务")


def parse_share_texts(raw: Any) -> list[dict[str, Any]]:
    """按链接解析结果校验输入，不限制分享文案用词；空行保留原行号。"""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        raise ShareValidationError([{"row": 1, "text": "", "reason": "请填写 1 至 100 条视频链接或分享文本"}])
    rows, issues = [], []
    for index, value in enumerate(raw, 1):
        if isinstance(value, str) and not value.strip():
            continue
        try:
            if not isinstance(value, str) or len(value) > 16384:
                raise ValueError("分享文本须为 16384 字符以内的文本")
            # modal_id 只能定位视频，不能证明输入是分享文本；纯网址统一拒绝。
            if re.fullmatch(r"https?://\S+", value.strip()):
                raise ValueError("不能只粘贴网址（包括带 modal_id 的链接），请粘贴抖音完整分享文本")
            # 文案可能使用 Dou音 等不同措辞，只让链接解析器确定唯一目标视频。
            rows.append({"row": index, "share_text": value.strip(), "url": normalize_video_input(value)})
        except ValueError as error:
            issues.append({"row": index, "text": str(value or "")[:160], "reason": str(error)})
    if issues:
        raise ShareValidationError(issues)
    if not rows:
        raise ShareValidationError([{"row": 1, "text": "", "reason": "请先粘贴视频链接或分享文本"}])
    return _deduplicate_share_rows(rows, key="url")


async def resolve_share_rows(rows: list[dict[str, Any]], context: Any = None) -> list[dict[str, Any]]:
    """全部分享链接解析成功才返回；失败汇总所有行，不产生任何采集任务。"""
    if context is None:
        # 静态视频 ID 无需请求；短链接验证使用独立请求上下文，不启动浏览器视频页。
        if all(video_id_from_link(row["url"]) for row in rows):
            return _deduplicate_share_rows([{**row, "resolved_url": row["url"]} for row in rows])
        try:
            async with async_playwright() as playwright:
                request = await playwright.request.new_context()
                try:
                    return await resolve_share_rows(rows, SimpleNamespace(request=request))
                finally:
                    await request.dispose()
        except PlaywrightError as error:
            # 请求服务不可用时同样阻止整批启动，避免页面只得到无行号的通用异常。
            raise ShareValidationError([
                {"row": row["row"], "text": row["share_text"][:160],
                 "reason": f"链接校验服务不可用，请检查安装或网络后重新提交：{error}"}
                for row in rows
            ]) from error
    semaphore = asyncio.Semaphore(3)

    async def resolve_one(url: str) -> tuple[str, str]:
        """限制校验请求并发，同一短链接只请求一次，不自动重试。"""
        async with semaphore:
            try:
                return await resolve_video_link(context, url), ""
            except (ValueError, RuntimeError, PlaywrightError) as error:
                return "", str(error)

    urls = list(dict.fromkeys(row["url"] for row in rows))
    results = dict(zip(urls, await asyncio.gather(*(resolve_one(url) for url in urls))))
    valid, issues = [], []
    for row in rows:
        resolved, error = results[row["url"]]
        if error:
            issues.append({"row": row["row"], "text": row["share_text"][:160], "reason": error})
        else:
            valid.append({**row, "resolved_url": resolved})
    if issues:
        raise ShareValidationError(issues)
    return _deduplicate_share_rows(valid)


def _deduplicate_share_rows(rows: list[dict[str, Any]], key: str = "resolved_url") -> list[dict[str, Any]]:
    """发现重复则拒绝整批启动，提供重复行号让页面只清空后出现的输入。"""
    unique, issues = {}, []
    for row in rows:
        previous = unique.get(row[key])
        if previous is not None:
            issues.append({"row": row["row"], "text": row["share_text"][:160],
                           "reason": f"与第 {previous['row']} 条分享文本指向同一视频，已清空重复输入框，请检查后重新启动",
                           "clear_input": True})
        else:
            unique[row[key]] = row
    if issues:
        raise ShareValidationError(issues)
    return list(unique.values())


def validate_douyin_url(url: str) -> None:
    """只允许抖音官方域名及普通 HTTP 端口，不接受凭据或外部跳转。"""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    allowed = any(hostname == domain or hostname.endswith("." + domain)
                  for domain in ("douyin.com", "iesdouyin.com"))
    if (len(url) > 4096 or parsed.scheme not in {"http", "https"} or not allowed
            or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
        raise ValueError("请输入有效的抖音视频链接或分享文本")


def video_id_from_link(url: str) -> str:
    """从独立视频、分享页或 modal_id 提取 ID，冲突时拒绝猜测。"""
    validate_douyin_url(url)
    parsed = urlparse(url)
    match = re.fullmatch(r"/(?:share/)?(?:video|note)/([0-9]+)/?", parsed.path)
    ids = {match.group(1)} if match else set()
    query = parse_qs(parsed.query)
    for key in ("modal_id", "aweme_id", "item_id"):
        for value in query.get(key, []):
            if not re.fullmatch(r"[0-9]+", value):
                raise ValueError("视频 ID 格式无效")
            ids.add(value)
    if len(ids) > 1:
        raise ValueError("链接包含相互冲突的视频 ID，请重新复制分享链接")
    return next(iter(ids), "")


def normalize_video_input(value: Any) -> str:
    """提取一个输入框中的唯一抖音链接，静态可解析时立即转为独立视频地址。"""
    if not isinstance(value, str) or len(value) > 16384:
        raise ValueError("视频链接或分享文本须为 16384 字符以内的文本")
    candidates = []
    for match in re.finditer(r"https?://[^\s<>\"'，。！？；、（）【】]+", value.strip()):
        url = match.group().rstrip(".,!?;:)]}")
        try:
            validate_douyin_url(url)
        except ValueError:
            continue
        video_id = video_id_from_link(url)
        if video_id:
            candidate = f"https://www.douyin.com/video/{video_id}"
        elif urlparse(url).hostname == "v.douyin.com" and re.fullmatch(r"/[A-Za-z0-9_-]+/?", urlparse(url).path):
            candidate = url
        else:
            raise ValueError("该地址未包含视频 ID，请复制具体视频的分享链接，不要使用推荐页或纯搜索页地址")
        if candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        raise ValueError("未找到有效抖音视频链接，请粘贴视频地址或完整分享文本")
    if len(candidates) != 1:
        raise ValueError("一个输入框只能填写一个视频，请将多个分享链接分行添加")
    return candidates[0]


class _SharePage(HTMLParser):
    """仅读取明确的页面主链接及 videoDetail，不从推荐列表选取视频。"""

    def __init__(self) -> None:
        """初始化有界响应解析状态，脚本作为数据处理，绝不执行。"""
        super().__init__()
        self.links: list[str] = []
        self.render_parts: list[str] = []
        self.in_render = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """获取 canonical/og:url 和 RENDER_DATA 脚本。"""
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("property") == "og:url":
            self.links.append(attributes.get("content") or "")
        if tag == "link" and attributes.get("rel") == "canonical":
            self.links.append(attributes.get("href") or "")
        if tag == "script":
            self.in_render = attributes.get("id") == "RENDER_DATA"

    def handle_data(self, data: str) -> None:
        """收集指定 JSON 脚本，不读取其他脚本中的任意 ID。"""
        if self.in_render:
            self.render_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        """结束脚本数据收集。"""
        if tag == "script":
            self.in_render = False

    def video_id(self, base_url: str) -> str:
        """仅接受一致的主视频标识，数据不足或冲突时拒绝猜测。"""
        ids = set()
        for link in self.links:
            value = video_id_from_link(urljoin(base_url, link))
            if value:
                ids.add(value)
        if self.render_parts:
            data = json.loads(unquote("".join(self.render_parts).strip()))
            app = data.get("app") if isinstance(data, dict) else None
            detail = app.get("videoDetail") if isinstance(app, dict) else None
            if not isinstance(detail, dict):
                detail = {}
            value = str(detail.get("awemeId", detail.get("aweme_id", "")))
            if re.fullmatch(r"[0-9]+", value):
                ids.add(value)
        if len(ids) > 1:
            raise ValueError("页面中的主视频 ID 不一致，请重新复制分享链接")
        return next(iter(ids), "")


async def resolve_video_link(context: Any, value: str,
                             stop_requested: Callable[[], bool] | None = None) -> str:
    """限时解析短链接且逐跳校验域名，不打开额外浏览器页或触发评论加载。"""
    url = normalize_video_input(value)
    if video_id_from_link(url):
        return url

    async def follow_redirects() -> str:
        """最多五跳；不自动重试、不跟随外域、不复用过期分享签名。"""
        current = url
        for _ in range(5):
            if stop_requested and stop_requested():
                raise RuntimeError("任务已停止，取消分享链接解析")
            validate_douyin_url(current)
            video_id = video_id_from_link(current)
            if video_id:
                return f"https://www.douyin.com/video/{video_id}"
            # 手动跟随跳转，避免先访问外域再发现跳转不安全。
            response = await context.request.get(current, max_redirects=0, timeout=10000)
            try:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("短链接跳转缺少目标地址")
                    current = urljoin(current, location)
                    validate_douyin_url(current)
                    continue
                if response.status != 200:
                    raise ValueError(f"短链接返回 HTTP {response.status}，请检查链接是否失效")
                size = response.headers.get("content-length")
                if size and int(size) > 2_000_000:
                    raise ValueError("分享页面内容过大，无法安全解析")
                body = await response.text()
                if len(body) > 2_000_000:
                    raise ValueError("分享页面内容过大，无法安全解析")
                parser = _SharePage()
                parser.feed(body)
                video_id = parser.video_id(current)
                if video_id:
                    return f"https://www.douyin.com/video/{video_id}"
                raise ValueError("分享页面未提供明确的视频 ID，请通过抖音分享按钮重新复制链接")
            finally:
                await response.dispose()
        # 第五次返回的跳转仍可以仅从地址解析，不额外发送请求。
        video_id = video_id_from_link(current)
        if video_id:
            return f"https://www.douyin.com/video/{video_id}"
        raise ValueError("分享链接跳转次数过多，请重新复制链接")

    try:
        return await asyncio.wait_for(follow_redirects(), timeout=30)
    except TimeoutError as error:
        raise RuntimeError("分享链接解析超时（30 秒），请检查网络后重新提交；未开始采集该视频") from error
    except (ValueError, PlaywrightError) as error:
        raise RuntimeError(f"分享链接解析失败：{error}；未开始采集该视频") from error
