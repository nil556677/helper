"""工具参数的提取 / 清洗（纯函数；规则、分发、补参共用）。

统一解决“查询天气 → 城市=查询”这类问题：动作词、疑问词、时间词都不算参数值。
"""

from __future__ import annotations

import re

#: 城市前常见的动作/语气前缀（长词优先，避免“查”先吃掉“查询”）。
CITY_PREFIXES: tuple[str, ...] = (
    "查询一下",
    "查一下",
    "看一下",
    "搜索一下",
    "搜一下",
    "查询",
    "查看",
    "获取",
    "想知道",
    "告诉我",
    "搜索",
    "帮我",
    "帮忙",
    "请问",
    "麻烦",
    "看看",
    "今天",
    "明天",
    "后天",
    "现在",
    "当前",
    "查",
    "看",
    "搜",
    "问",
)

#: 城市里不能出现的词（出现即判定“不是城市”）。
CITY_STOP_WORDS: tuple[str, ...] = (
    "什么",
    "怎么",
    "如何",
    "是否",
    "多少",
    "哪",
    "这",
    "那",
    "我",
    "你",
    "他",
    "她",
    "它",
    "样",
    "天气",
    "温度",
    "预报",
    "情况",
    "查询",
    "查看",
    "获取",
    "搜索",
    "知道",
    "告诉",
    "帮我",
    "帮忙",
    "一下",
    "今天",
    "明天",
    "后天",
    "现在",
    "当前",
)

#: 参数名 → 中文（报错/追问用）。
_PARAM_LABELS: dict[str, str] = {
    "order_id": "订单号",
    "tracking_no": "运单号",
    "city": "城市",
}

_WEATHER_TAIL = re.compile(
    r"([\u4e00-\u9fa5]{2,10})(?:市)?(?:今天|明天|后天)?(?:天气|冷不冷|热不热|下不下雨)"
)


def label(name: str) -> str:
    return _PARAM_LABELS.get(name, name)


def clean_city(value: str) -> str | None:
    """清洗城市候选；不像城市返回 ``None``。"""
    city = str(value or "").strip().strip("，。！？,.!? ")
    changed = True
    while changed:
        changed = False
        for prefix in CITY_PREFIXES:
            if city.startswith(prefix) and len(city) > len(prefix):
                city = city[len(prefix) :]
                changed = True
    if not 2 <= len(city) <= 10:
        return None
    if any(word in city for word in CITY_STOP_WORDS):
        return None
    return city or None


def extract_city(text: str) -> str:
    """从“查询北京天气 / 北京 / 今天北京”里取城市；取不到返回空串。"""
    normalized = str(text or "").strip()
    match = _WEATHER_TAIL.search(normalized)
    if match:
        city = clean_city(match.group(1))
        if city:
            return city
    if len(normalized) <= 10:
        return clean_city(normalized) or ""
    return ""


def extract_param(name: str, text: str) -> str | None:
    """按参数名从一段文本里取值（用于规则槽位 / 用户补参）。"""
    raw = str(text or "").strip()
    if name == "city":
        return extract_city(raw) or None
    if name == "order_id":
        match = re.search(r"\d{6,}", raw)
        return match.group(0) if match else None
    if name == "tracking_no":
        match = re.search(r"[A-Za-z0-9]{8,32}", raw)
        return match.group(0) if match else None
    return raw or None


def normalize_param(name: str, value: str) -> str | None:
    """校验并规范化一个参数值；不合法返回 ``None``。"""
    raw = str(value or "").strip()
    if not raw:
        return None
    if name == "city":
        return clean_city(raw)
    if name == "order_id":
        return raw if re.fullmatch(r"\d{6,}", raw) else None
    if name == "tracking_no":
        return raw if re.fullmatch(r"[A-Za-z0-9]{8,32}", raw) else None
    return raw
