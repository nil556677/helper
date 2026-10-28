"""L1 规则分类：只收**高精度**规则；未命中返回 ``None``（交给 L2 / 默认 search_kb）。

优先级：转人工 > 订单 > 快递 > 天气 > 时间 > 查数据 > 寒暄。规则是纯函数，零模型、零 I/O。
"""

from __future__ import annotations

import re

from app.core.intent import Intent
from app.core.intent import slots as slot_utils
from app.core.intent.schema import RouteDecision

#: 寒暄子类（问候 / 感谢 / 告别）→ 槽位 subtype。
_SUBTYPES: tuple[tuple[str, re.Pattern], ...] = (
    ("greet", re.compile(r"^(?:你好|您好|hi|hello)")),
    ("thanks", re.compile(r"谢谢|辛苦了")),
    ("bye", re.compile(r"再见|拜拜")),
)

#: 规则（label, intent, tool, pattern）；顺序即优先级。
_PATTERNS: tuple[tuple[str, Intent, str, re.Pattern], ...] = (
    (
        "转人工",
        Intent.HUMAN_HANDOFF,
        "human_handoff",
        re.compile(r"转人工|人工客服|找人工|真人|投诉"),
    ),
    (
        "订单",
        Intent.TOOL_CALL,
        "query_order",
        re.compile(
            r"订单[号]?\s*(\d{6,})"
            r"|^(?:我的|帮我)?(?:查(?:询)?(?:一下)?|看(?:一下)?)?"
            r"订单(?:号|状态|信息)?(?:是多少|呢|啊)?$"
        ),
    ),
    (
        "快递",
        Intent.TOOL_CALL,
        "query_express",
        re.compile(
            r"(?:(?:查|查询|追踪)(?:一下)?)?(?:快递|物流|运单)(?:单号|号)?\s*([A-Za-z0-9]{8,32})"
            r"|(?:查|查询|追踪)(?:一下)?(?:快递|物流|运单)"
        ),
    ),
    (
        "天气",
        Intent.TOOL_CALL,
        "weather",
        re.compile(r"天气|冷不冷|热不热|下不下雨"),
    ),
    (
        "时间",
        Intent.TOOL_CALL,
        "time",
        re.compile(r"(?:现在|当前)?几点了?|(?:现在|当前)时间|今天(?:几号|日期)"),
    ),
    (
        "查数据",
        Intent.TOOL_CALL,
        "search_kb",
        re.compile(r"查(?:一下)?(?:数据|资料)|搜索(?:一下)?(?:资料|文档)|搜一下资料"),
    ),
)

#: 快递公司常见写法（只用于槽位，具体编码由工具映射）。
_CARRIER_WORDS = ("顺丰", "中通", "圆通", "韵达", "申通", "京东", "邮政", "EMS", "极兔")


def classify(text: str) -> RouteDecision | None:
    """纯函数：命中返回 :class:`RouteDecision`；未命中返回 ``None``。"""
    normalized = re.sub(r"\s+", "", str(text or ""))
    if not normalized:
        return None
    for label, intent, tool, pattern in _PATTERNS:
        if not pattern.search(normalized):
            continue
        found: dict[str, str] = {}
        if tool == "query_order":
            order_id = slot_utils.extract_param("order_id", normalized)
            if order_id:
                found["order_id"] = order_id
        if tool == "query_express":
            tracking_no = slot_utils.extract_param("tracking_no", normalized)
            if tracking_no:
                found["tracking_no"] = tracking_no
            lowered = normalized.lower()
            for name in _CARRIER_WORDS:
                if name.lower() in lowered:
                    found["carrier"] = name
                    break
        if tool == "weather":
            city = slot_utils.extract_city(normalized)
            if city:
                found["city"] = city
        return RouteDecision(
            intent=intent,
            tool=tool,
            confidence=1.0,
            evidence=f"命中规则：{label}",
            stage="rules",
            slots=found,
        )
    # 寒暄：再按子类细分（问候/感谢/告别），其余归闲聊兜底。
    for subtype, pattern in _SUBTYPES:
        if pattern.search(normalized):
            return RouteDecision(
                intent=Intent.CHITCHAT,
                confidence=1.0,
                evidence=f"命中规则：寒暄（{subtype}）",
                stage="rules",
                slots={"subtype": subtype},
            )
    return None
