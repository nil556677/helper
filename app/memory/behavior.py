"""画像行为统计通道（附 C 第②通道）：从会话消息算稳定行为信号，零 LLM、确定性。

与 LLM 通道互补：行为统计反映"用户实际接受/偏好的形态"（回答长度等），不依赖模型判断。
**样本量门槛**（C.6 第 1 条）：行为统计类字段样本 <5 只累计不落库，避免单次偶然当成稳定偏好。

本模块只被 `app/agent/memory/manager.distill_conversation` 调用（域纪律）。
"""

from __future__ import annotations  # 延迟值注解

from dataclasses import dataclass  # 结果容器
from typing import Any  # 任意值

__all__ = ["BehaviorSignals", "signal_from_messages"]  # 导出

#: 行为统计样本量下限（少于则不算稳定偏好，防单次偶然）。
_SAMPLE_MIN = 5

#: 长短回答的 token 阈值（按中文字数计，粗略分档）。
_VERBOSITY_SHORT = 120  # 回答 ≤ 120 字视为"短"
_VERBOSITY_LONG = 300  # 回答 ≥ 300 字视为"长"


@dataclass(frozen=True)
class BehaviorSignals:  # 行为统计结果
    """一组稳定偏好候选；每个字段都有样本量门槛，没达标则为 None（不写）。"""

    verbosity: str | None = None  # 回答长度偏好（short/normal/detailed；None=样本不足）
    answer_sample: int = 0  # 参与统计的回答条数
    verbosity_avg: float = 0.0  # 平均回答长（字）


def _answer_lengths(messages: list[dict[str, Any]]) -> list[tuple[str, int]]:  # 提取问答对
    """从消息里拿出 role=assistant 的回答及其长度（近似字数）。

    只取 assistant 消息；工具轨迹（channel=tool）不算用户回答，跳过。
    """
    pairs: list[tuple[str, int]] = []  # (role, len)
    for msg in messages or []:  # 逐条
        if str(msg.get("role") or "") != "assistant":  # 只要助手回答
            continue  # 跳过
        text = str(msg.get("content") or "")  # 正文
        if not text.strip():  # 空回答（拒绝等）
            continue  # 跳过
        pairs.append(("assistant", len(text)))  # 记录
    return pairs  # 返回


def signal_from_messages(messages: list[dict[str, Any]]) -> BehaviorSignals:  # 算行为信号
    """从消息列表算回答长度偏好；样本不足返回 None 字段。

    判定：平均长度 < short 阈值 → short；> long 阈值 → detailed；否则 normal。
    """
    pairs = _answer_lengths(messages)  # 问答对
    sample = len(pairs)  # 样本数
    if sample < _SAMPLE_MIN:  # 样本不足
        return BehaviorSignals(verbosity=None, answer_sample=sample)  # 不落库
    avg = sum(length for _role, length in pairs) / sample  # 平均长度
    if avg < _VERBOSITY_SHORT:  # 短
        verbosity = "short"  # 短偏好
    elif avg > _VERBOSITY_LONG:  # 长
        verbosity = "detailed"  # 长偏好
    else:  # 中等
        verbosity = "normal"  # 中
    return BehaviorSignals(verbosity=verbosity, answer_sample=sample, verbosity_avg=round(avg, 1))  # 返回
