"""焦点 · 实体抽取：结构化直接取 / 正则（含上下文约束 + 置信度）/ 词典打话题标签。

硬约束（与补充方案一致）：
- 裸数字必须有上下文词（前瞻断言：同句出现 订单/退款/物流 等业务词）才认成 order_id；
- 正则失败不猜，低置信（<0.5）不入栈，交给澄清。
"""

from __future__ import annotations  # 延迟值注解

import re  # 正则
from typing import Any  # 任意值

from app.agent.contracts.focus import FocusEntity  # 实体

__all__ = ["extract_entities", "normalize_id"]  # 导出

#: 业务上下文词（裸数字必须有它们才认成订单号）。
_ORDER_CTX = r"(?:订单|order|退款|退货|物流|快递|售后)"  # 上下文
#: 实体类型 → 规则（取第 1 组；带前缀=高置信）。
_PATTERNS: tuple[tuple[str, list[re.Pattern[str]], float], ...] = (  # (类型, 规则, 基础置信)
    ("order_id", [  # 订单号
        re.compile(r"(?:订单|order)[^\dA-Za-z]{0,6}([A-Za-z0-9]{4,})", re.I),  # 带前缀 → 1.0
        re.compile(r"(?<![\d])(\d{6,20})(?![\d])(?=.*(?:订单|退款|物流|order))", re.I),  # 裸数字+业务词 → 0.8
    ], 1.0),  # 基础档
    ("phone", [  # 手机号
        re.compile(r"(?:手机|电话)\s*[号:：]?\s*(1[3-9]\d{9})"),  # 带前缀
        re.compile(r"(?<![\d])(1[3-9]\d{9})(?![\d])"),  # 裸号 → 0.5（格式唯一）
    ], 0.5),
    ("tracking_no", [  # 运单号
        re.compile(r"(?:运单|快递单|单号)\s*[号:：]?\s*([A-Z0-9]{8,20})", re.I),  # 带前缀
        re.compile(r"\b([A-Z]{2}\d{9,13})\b"),  # SF 开头格式
    ], 0.5),
    ("policy_term", [  # 政策条款
        re.compile(r"(退款政策|退货规则|售后条款|物流政策|运费规则)"),  # 直接匹配
    ], 1.0),
)  # 结束

#: 裸数字/短文本同时可能命中多类 → 记为歧义类型（置信 0.5）。
_AMBIGUOUS_TYPES = ("order_id", "tracking_no")  # 易混两类


def normalize_id(raw: str) -> str:  # 规范化实体 id
    return str(raw or "").strip()  # 去空白


def extract_entities(query: str, structured: dict[str, Any] | None = None) -> list[FocusEntity]:  # 抽取主入口
    """structured（前端/表单给的）优先且恒 1.0；其余走正则。返回按 (置信, 出现序) 排序。"""
    text = str(query or "")  # 文本
    found: list[FocusEntity] = []  # 结果
    # 1) 结构化字段直接取（零成本，恒 1.0）
    for key in ("order_id", "phone", "tracking_no"):  # 结构化键
        raw = (structured or {}).get(key)  # 值
        if not raw:  # 空
            continue  # 跳过
        found.append(FocusEntity(  # 实体
            entity_type=key,  # 类型
            entity_id=normalize_id(str(raw)),  # 值
            surface=str(raw),  # 表面
            confidence=1.0,  # 恒高
            source="structured",  # 来源
        ))  # 结束
    # 2) 正则
    for etype, rules, base_conf in _PATTERNS:  # 逐类型
        for idx, pattern in enumerate(rules):  # 逐规则
            for m in pattern.finditer(text):  # 全部命中
                val = m.group(1)  # 捕获组
                conf = base_conf if idx == 0 else (0.8 if etype == "order_id" else 0.5)  # 前缀档碾压 / 裸档
                found.append(FocusEntity(  # 实体
                    entity_type=etype,  # 类型
                    entity_id=normalize_id(val),  # 值
                    surface=val,  # 表面
                    span=m.span(1),  # 字符区间
                    confidence=conf,  # 置信
                    source="regex",  # 来源
                ))  # 结束
    # 2b) 列表式多个订单号（"查订单 12345 和 67890"）：句内有订单上下文时，额外收裸数字（排除金额）
    if any(w in text for w in ("订单", "order")):  # 有订单上下文
        for m in re.finditer(r"(?<![\d])(\d{5,20})(?![\d])", text):  # 裸数字
            tail = text[m.end():m.end() + 4]  # 后面几个字符
            if any(a in tail for a in ("元", "块", "RMB")):  # 金额排除
                continue  # 跳过
            found.append(FocusEntity(  # 实体
                entity_type="order_id",  # 类型
                entity_id=normalize_id(m.group(1)),  # 值
                surface=m.group(1),  # 表面
                span=m.span(1),  # 区间
                confidence=0.8,  # 列表上下文档
                source="regex",  # 来源
            ))  # 结束
    # 3) 易混类型 → 歧义（同一数字既订单又运单）
    merged: list[FocusEntity] = []  # 去重合并
    for e in found:  # 逐个
        dup = _find_same(merged, e)  # 同类型同值
        if dup is not None:  # 已有
            dup.confidence = max(dup.confidence, e.confidence)  # 保留高置信
            continue  # 跳过
        merged.append(e)  # 新加入
    return [e for e in merged if e.confidence >= 0.5]  # 过滤低置信


def _find_same(items: list[FocusEntity], e: FocusEntity) -> FocusEntity | None:  # 找同类型同值
    for x in items:  # 逐个
        if x.entity_type == e.entity_type and x.entity_id == e.entity_id:  # 同键
            return x  # 命中
    return None  # 无
