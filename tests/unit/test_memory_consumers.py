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


def test_state_context_prefers_memory_facts(monkeypatch) -> None:  # 装配优先用 load_context 召回的 facts
    calls: list[str] = []  # 记录 recall 调用
    import app.agent.memory.manager as mm  # 记忆模块

    def _fake_recall(question: str = "", top_k: int = 6):  # 假召回
        calls.append(str(question))  # 记录
        return mm.RecallBundle(facts=[{"key": "lang", "value": "python", "confidence": 0.9, "source": "explicit"}], profile_lines=[])  # 返回

    monkeypatch.setattr(mm, "recall", _fake_recall)  # 顶掉真实召回
    state = {  # 状态
        "question": "q",  # 问题
        "conversation_id": "",  # 无会话
        "memory_facts": [{"key": "prefer", "value": "x", "confidence": 0.8, "source": "explicit"}],  # 入口已召回
    }  # 结束
    out = agent_mod._state_context(state)  # 装配
    assert out is not None  # 成功
    assert "prefer" in out.messages[0]["content"] or "prefer" in out.system  # 用入口 facts
    assert calls == []  # 未再查（单轮只查一次）
