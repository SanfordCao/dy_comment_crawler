"""Filter comments discovered during a crawl before queuing a reply."""

from __future__ import annotations

from typing import Any


# 抖音的 IP 属地通常只到省级；这里不从昵称或正文推断更细的城市。
REGIONS = (
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
    "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
    "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州",
    "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆", "香港",
    "澳门", "台湾",
)

_SUFFIXES = ("特别行政区", "维吾尔自治区", "壮族自治区", "回族自治区", "自治区", "省", "市")


def normalize_region(value: str) -> str:
    """Remove only administrative suffixes so an IP label can match a province choice."""
    label = "".join(str(value or "").split())
    for suffix in _SUFFIXES:
        if label.endswith(suffix):
            return label[:-len(suffix)]
    return label


def validate_reply_filter(region: str, keywords: str | list[str]) -> tuple[str, list[str]]:
    """Normalize province and ordered keywords; require at least one active condition."""
    normalized = normalize_region(region)
    if normalized and normalized not in REGIONS:
        raise ValueError("请选择列表中的国内省级地区")
    values = [keywords] if isinstance(keywords, str) else keywords
    if not isinstance(values, list) or len(values) > 50 or any(not isinstance(item, str) for item in values):
        raise ValueError("关键词须为不超过 50 项的文字列表")
    terms, seen = [], set()
    for item in values:
        term = item.strip()
        if not term:
            continue
        if len(term) > 100:
            raise ValueError("每个关键词不能超过 100 个字符")
        folded = term.casefold()
        if folded not in seen:
            terms.append(term)
            seen.add(folded)
    if not normalized and not terms:
        raise ValueError("请至少设置地区或关键词，避免匹配全部评论")
    return normalized, terms


def matches_reply_filter(comment: dict[str, Any], region: str, keywords: str | list[str]) -> bool:
    """Require region and any one keyword when both conditions are configured."""
    if region and normalize_region(comment.get("ip_label", "")) != region:
        return False
    terms = [keywords] if isinstance(keywords, str) else keywords
    if terms and not any(term.casefold() in str(comment.get("text", "")).casefold() for term in terms):
        return False
    return bool(str(comment.get("id", "")).strip())
