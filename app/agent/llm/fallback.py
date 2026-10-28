"""确定性兜底：模型不可用 / 修不好时给资料原文，绝不留空白（类 B #11 兜底）。

兜底必须是**确定性**的：不调模型、不看时间、不随机，同样的证据永远给同样的文本。
"""

from __future__ import annotations

from typing import Any  # 任意值

from app.rag.answer import extractive  # 已有的原文拼装

#: 兜底原因 → 给用户的一句说明。
NOTES: dict[str, str] = {  # 说明表
    "unavailable": "模型暂时不可用，以下是资料原文。",  # 槽位没配全
    "invalid": "没能给出可溯源的引用，以下是资料原文。",  # 校验没过且修不好
    "empty": "模型没有返回内容，以下是资料原文。",  # 空答案
    "cancelled": "已停止生成，以上是已完成的部分。",  # 取消
}  # 说明表结束


def extractive_answer(evidence: list[dict[str, Any]]) -> str:  # 原文兜底
    """没证据时返回空串（调用方据此给"没找到"的 note）。"""
    return extractive(evidence)  # 复用已有实现


def note_for(reason: str) -> str:  # 兜底说明
    """未知原因给通用说法，不抛。"""
    return NOTES.get(str(reason or ""), NOTES["invalid"])  # 查表
