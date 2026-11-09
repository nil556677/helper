"""M0 记忆正确性：消费端读对 Turn 字段（user/assistant），跨轮护栏生效。"""

from __future__ import annotations  # 延迟值注解

from app.agent.fallback import agent as agent_mod  # 被测模块


def _turn(user: str, assistant: str) -> dict:  # 造一轮 Turn JSON
    return {"user": user, "assistant": assistant}  # Turn 真实字段


def test_format_memory_reads_turn_fields() -> None:  # 读 user/assistant 而非 question/answer
    state = {"turns": [_turn("年假怎么算", "年假按累计工龄折算。"), _turn("那病假呢", "病假按……")]}  # 状态
    text = agent_mod._format_memory(state)  # 格式化
    assert "用户：年假怎么算" in text  # 用户行
    assert "助手：年假按累计工龄折算。" in text  # 助手行
    assert "病假" in text  # 第二轮也在


def test_format_memory_empty_without_turns() -> None:  # 无历史给占位
    assert "无历史对话" in agent_mod._format_memory({})  # 占位


def test_guard_from_memory_reads_turn_fields() -> None:  # 护栏读 assistant
    turns = [_turn("什么", "您想查哪方面？"), _turn("查天气", "好，成都多云。")]  # 追问后用户答复
    count, confirmed = agent_mod.guard_from_memory(turns)  # 反推（dict 形态）
    assert count == 1  # 一次追问
    assert confirmed is True  # 已确认
    assert agent_mod.guard_from_memory([]) == (0, False)  # 空历史