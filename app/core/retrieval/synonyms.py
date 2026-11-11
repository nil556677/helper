"""查询同义词扩展（落点三）：运行时双向展开，桥接词汇不匹配（退款↔退钱↔退费）。

- **查询时**展开（不在索引时塞同义词，避免索引膨胀/改同义词要重建索引）；
- 组内任一词命中 → 组内全部词加入查询向量（BM25 侧提高召回）；
- 同义词组运行时热更：改这里/配置即可，查询立即生效。
"""

from __future__ import annotations  # 延迟值注解

#: 同义词组（可维护；词内不去重，扩展时才并集）。
SYNONYM_GROUPS: list[list[str]] = [  # 组表
    ["退款", "退钱", "退费", "refund"],  # 退款
    ["物流", "快递", "配送", "运单", "shipment"],  # 物流
    ["发票", "票", "invoice"],  # 发票
]  # 组表结束


def _group_of(token: str) -> list[str]:  # 词所在组（无则空）
    for group in SYNONYM_GROUPS:  # 逐组
        if token in group:  # 命中
            return group  # 返回整组
    return []  # 不在任何组


def expand_tokens(tokens: list[str]) -> list[str]:  # 双向扩展
    """把命中同义词组的词，扩展成组内全部词（保留原词；去重保序）。"""
    out: list[str] = []  # 结果
    seen: set[str] = set()  # 去重
    for token in tokens:  # 逐个
        group = _group_of(token)  # 所在组（无则空）
        members = group if group else [token]  # 组内全部或原词
        for member in members:  # 逐个成员
            if member not in seen:  # 未加入
                seen.add(member)  # 记
                out.append(member)  # 加
    return out  # 返回