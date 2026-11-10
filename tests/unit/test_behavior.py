"""P5 行为统计通道：回答长度偏好（样本门槛 / 分档）。"""

from __future__ import annotations  # 延迟值注解

from app.memory.behavior import signal_from_messages  # 被测


def _progress_msgs(n: int, role: str = "assistant", length: int = 50) -> list[dict]:  # 造 n 条消息
    return [{"role": role, "content": "字" * length} for _ in range(n)]  # 构造


def test_sample_below_threshold() -> None:  # 样本 <5 不落库
    signals = signal_from_messages(_progress_msgs(3))  # 3 条
    assert signals.verbosity is None  # 样本不足
    assert signals.answer_sample == 3  # 样本数记录


def test_short_verbosity() -> None:  # 短回答 → short
    signals = signal_from_messages(_progress_msgs(6, length=60))  # 6 条短答
    assert signals.verbosity == "short"  # 短偏好


def test_detailed_verbosity() -> None:  # 长回答 → detailed
    signals = signal_from_messages(_progress_msgs(6, length=500))  # 6 条长答
    assert signals.verbosity == "detailed"  # 长偏好


def test_normal_verbosity() -> None:  # 中长 → normal
    signals = signal_from_messages(_progress_msgs(6, length=200))  # 6 条中长
    assert signals.verbosity == "normal"  # 中偏好


def test_ignores_non_assistant() -> None:  # 只统计 assistant
    mixed = [{"role": "user", "content": "x" * 500} for _ in range(6)]  # 全是 user（超长）
    signals = signal_from_messages(mixed)  # 不应计入
    assert signals.verbosity is None  # 无 assistant → 样本不足
