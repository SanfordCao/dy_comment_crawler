from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse
import re

_TOP_LEVEL_COMMENT_PATH = re.compile(r"/comment/list(?:/|$)", re.IGNORECASE)
_REPLY_COMMENT_PATH = re.compile(
    r"/comment/list/(?:reply|sub)(?:/|$)|/reply/comment/list",
    re.IGNORECASE,
)


def _first_defined(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _string(value: Any) -> str:
    return "" if value is None else str(value)


def _number(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _normalize_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if normalized.startswith("http://"):
        normalized = f"https://{normalized.removeprefix('http://')}"
    return normalized if normalized.startswith("https://") else ""


def _find_best_url(value: Any, depth: int = 0) -> str:
    if depth > 5 or value is None:
        return ""
    if isinstance(value, str):
        return _normalize_url(value)
    if isinstance(value, list):
        for item in value:
            if url := _find_best_url(item, depth + 1):
                return url
        return ""
    if not isinstance(value, dict):
        return ""

    preferred_fields = (
        "origin_url",
        "large_url",
        "download_url",
        "display_url",
        "medium_url",
        "thumb_url",
        "static_url",
        "url_list",
        "urls",
        "url",
    )
    for field in preferred_fields:
        if field in value and (url := _find_best_url(value[field], depth + 1)):
            return url

    for key, nested in value.items():
        if re.search(r"url|image|uri", str(key), re.IGNORECASE):
            if url := _find_best_url(nested, depth + 1):
                return url
    return ""


def extract_comment_image_urls(raw_comment: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    content = raw_comment.get("content")
    content_dict = content if isinstance(content, dict) else {}
    image_fields = (
        raw_comment.get("image_list"),
        raw_comment.get("images"),
        raw_comment.get("pic_list"),
        raw_comment.get("media_list"),
        content_dict.get("image_list"),
        content_dict.get("images"),
    )

    for field in image_fields:
        image_items = field if isinstance(field, list) else [field]
        for item in image_items:
            url = _find_best_url(item)
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def is_top_level_comment_response_url(raw_url: str) -> bool:
    try:
        parsed = urlparse(raw_url)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    return bool(
        (hostname == "douyin.com" or hostname.endswith(".douyin.com"))
        and _TOP_LEVEL_COMMENT_PATH.search(parsed.path)
        and not _REPLY_COMMENT_PATH.search(parsed.path)
    )


def get_aweme_id_from_url(raw_url: str) -> str:
    try:
        parsed = urlparse(raw_url)
        query = parse_qs(parsed.query)
    except ValueError:
        return ""

    for name in ("aweme_id", "item_id", "modal_id"):
        value = query.get(name, [""])[0]
        if value.isdigit():
            return value

    match = re.search(r"/video/(\d+)", parsed.path)
    return match.group(1) if match else ""


def extract_top_level_comment_array(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    data_dict = data if isinstance(data, dict) else {}
    candidates = (
        payload.get("comments"),
        data_dict.get("comments"),
        payload.get("comment_list"),
        data_dict.get("comment_list"),
    )
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def extract_pagination(payload: Any) -> dict[str, bool | str | None]:
    if not isinstance(payload, dict):
        return {"has_more": None, "cursor": None}
    data = payload.get("data")
    source = data if isinstance(data, dict) else payload
    raw_has_more = _first_defined(source.get("has_more"), source.get("hasMore"))
    cursor = _first_defined(
        source.get("cursor"),
        source.get("next_cursor"),
        source.get("nextCursor"),
    )
    return {
        "has_more": (
            None
            if raw_has_more is None
            else raw_has_more is True or raw_has_more in (1, "1")
        ),
        "cursor": None if cursor is None else str(cursor),
    }


def parse_top_level_comment(
    raw_comment: Any,
    fallback_aweme_id: str = "",
) -> dict[str, Any] | None:
    if not isinstance(raw_comment, dict):
        return None

    comment_id = _string(
        _first_defined(
            raw_comment.get("cid"),
            raw_comment.get("comment_id"),
            raw_comment.get("commentId"),
            raw_comment.get("id"),
        )
    )
    if not comment_id:
        return None

    user_value = raw_comment.get("user", raw_comment.get("author", {}))
    user = user_value if isinstance(user_value, dict) else {}
    content = raw_comment.get("content")
    content_dict = content if isinstance(content, dict) else {}
    text = _first_defined(
        raw_comment.get("text"),
        content_dict.get("text"),
        content if isinstance(content, str) else "",
    )
    create_time = _number(
        _first_defined(
            raw_comment.get("create_time"),
            raw_comment.get("createTime"),
        )
    )
    create_time_iso = (
        datetime.fromtimestamp(create_time, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
        if create_time
        else None
    )

    return {
        "id": comment_id,
        "aweme_id": _string(
            _first_defined(
                raw_comment.get("aweme_id"),
                raw_comment.get("awemeId"),
                fallback_aweme_id,
            )
        ),
        "text": _string(text),
        "author": {
            "uid": _string(_first_defined(user.get("uid"), user.get("id"))),
            "sec_uid": _string(
                _first_defined(user.get("sec_uid"), user.get("secUid"))
            ),
            "nickname": _string(
                _first_defined(user.get("nickname"), user.get("name"))
            ),
            "unique_id": _string(
                _first_defined(
                    user.get("unique_id"),
                    user.get("uniqueId"),
                    user.get("short_id"),
                )
            ),
        },
        "create_time": create_time or None,
        "create_time_iso": create_time_iso,
        "ip_label": _string(
            _first_defined(raw_comment.get("ip_label"), raw_comment.get("ipLabel"))
        ),
        "likes": _number(
            _first_defined(raw_comment.get("digg_count"), raw_comment.get("like_count"))
        ),
        "reply_count": _number(
            _first_defined(
                raw_comment.get("reply_comment_total"),
                raw_comment.get("reply_count"),
            )
        ),
        "source_image_urls": extract_comment_image_urls(raw_comment),
        "images": [],
    }


class CommentStore:
    def __init__(self) -> None:
        self._comments: dict[str, dict[str, Any]] = {}

    def add(self, raw_comment: Any, fallback_aweme_id: str = "") -> bool:
        parsed = parse_top_level_comment(raw_comment, fallback_aweme_id)
        if parsed is None:
            return False

        existing = self._comments.get(parsed["id"])
        if existing is not None:
            if not existing["text"]:
                existing["text"] = parsed["text"]
            existing["likes"] = max(existing["likes"], parsed["likes"])
            existing["reply_count"] = max(
                existing["reply_count"], parsed["reply_count"]
            )
            existing_urls = existing["source_image_urls"]
            for url in parsed["source_image_urls"]:
                if url not in existing_urls:
                    existing_urls.append(url)
            return False

        self._comments[parsed["id"]] = parsed
        return True

    def __len__(self) -> int:
        return len(self._comments)

    def values(self) -> list[dict[str, Any]]:
        return list(self._comments.values())

