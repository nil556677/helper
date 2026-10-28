"""答复生成（UI-free）：原文拼装 + 引用角标校验。

真正的生成走 `agent.nodes.answer.node`（类 B 网关 + 类 C 装配器）；
这里只保留两块**无模型**的纯函数，供回退与测试复用。
"""

from __future__ import annotations  # 类型注解延迟求值

import re  # 正则

__all__ = ["extractive", "validate_citations"]  # 对外导出

_CITE = re.compile(r"\[(\d+)\]")  # 引用角标


def validate_citations(text: str, evidence: list[dict]) -> tuple[str, list[int]]:  # 校验角标
    """去掉越界角标；返回 (clean_text, 合法引用索引列表)。"""
    max_n = len(evidence)  # 证据条数
    used: list[int] = []  # 合法且去重的序号
    for raw in _CITE.findall(text):  # 扫全部角标
        n = int(raw)  # 转数字
        if 1 <= n <= max_n and n not in used:  # 在范围内且没记过
            used.append(n)  # 记

    def _keep(match: re.Match[str]) -> str:  # 越界就删
        n = int(match.group(1))  # 序号
        return match.group(0) if 1 <= n <= max_n else ""  # 保留或删掉

    return _CITE.sub(_keep, text), used  # 清理后文本 + 合法序号


def extractive(evidence: list[dict]) -> str:  # 原文回退
    """把证据按来源/出处拼成 Markdown。"""
    if not evidence:  # 没证据
        return ""  # 空
    blocks = [  # 每条一块
        f"#### {item.get('source_name') or '资料'} · {item.get('origin') or '资料'}\n\n{item.get('text')}"  # 块
        for item in evidence  # 遍历
    ]  # 列表结束
    return "\n\n---\n\n".join(blocks) + "\n\n> 以上为本地资料原文（未改写）。"  # 拼接 + 说明
