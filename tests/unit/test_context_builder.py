"""C1 ContextBuilder：三层顺序 / 记忆块拼接 / 近轮剪枝 / 预算反向截断。"""

from __future__ import annotations  # 延迟值注解

from app.agent.contexts.builder import build_context, set_embed  # 被测
from app.agent.contexts.budget import Budget  # 预算


def _working(**kw) -> dict:  # 白板
    base = {"task_goal": "查年假", "key_facts": {"id": "A1"}}  # 默认
    base.update(kw)  # 覆盖
    return base  # 返回


def test_compose_three_tiers_and_question_last() -> None:  # 三层 + 问题最末
    out = build_context(  # 装配
        system="SYS",  # S
        working=_working(),  # F 锚点
        facts=[{"key": "lang", "value": "python", "confidence": 0.9, "source": "explicit"}],  # 事实
        recent=[{"user": "上一轮", "assistant": "已答"}],  # 近轮
        question="问题",  # 动态
    )  # 结束
    assert out.system.startswith("SYS")  # S 开头
    assert "<working>" in out.system  # working 进 system（锚点）
    assert "<facts>" in out.messages[0]["content"]  # 事实进消息
    assert "<dialog>" in out.messages[0]["content"]  # 近轮进消息
    assert out.messages[0]["content"].rstrip().endswith("问题")  # 问题最末


def test_working_is_anchor_kept_under_minimum_budget() -> None:  # 预算极紧，working/question 仍在
    tiny = Budget(window=512, output_reserve=32, history_ratio=0.1)  # 极小窗口
    out = build_context(  # 装配
        system="SYS",  # S
        working=_working(),  # 锚点
        facts=[{"key": "a", "value": "x", "confidence": 0.9}],  # 事实
        recent=[{"user": "u"}] * 40,  # 大量近轮
        question="Q",  # 问题
        budget=tiny,  # 极小预算
    )  # 结束
    assert out.system and "Q" in out.messages[0]["content"]  # system 与问题都在
    assert "A1" in out.system  # key_facts 保留


def test_prune_recent_by_relevance() -> None:  # 超过 keep 的旧轮按相关度剪
    set_embed(lambda s: [1.0, 0.0] if "keep" in str(s) else [0.0, 1.0])  # keep→近，其它→远
    try:  # 跑后复位
        recent = [{"user": f"keep{i}", "assistant": ""} for i in range(2)] + [{"user": f"old{i}", "assistant": ""} for i in range(6)]  # 2 近 + 6 远
        out = build_context(system="S", working={}, recent=recent, question="keepnow", keep_turns=2)  # 保底 2
        kept = [t["user"] for t in recent]  # 原样
        assert out.pruned["recent"] >= 4  # 远的相关度低于阈值被剪（6 远里只留≥阈值；0.5 阈值下 none 等）
    finally:  # 复位
        set_embed(None)  # 清


def test_prune_keeps_all_without_embed() -> None:  # 无嵌入保守全留
    set_embed(None)  # 无嵌入
    recent = [{"user": f"u{i}", "assistant": ""} for i in range(8)]  # 8 轮
    out = build_context(system="S", working={}, recent=recent, question="q", keep_turns=4)  # 装配
    assert out.pruned.get("recent", 0) == 0  # 无法判相关 → 全留（保守）


def test_no_input_shape() -> None:  # 空输入 shape 正确
    out = build_context(system="SYS", working={}, question="q")  # 最小
    assert out.system == "SYS"  # system
    assert out.messages and out.messages[0]["content"] == "q"  # 仅问题
    assert out.blocks == []  # 无块
    assert "q" in out.system or "q" in out.messages[0]["content"]  # 问题可达


def test_state_context_via_builder(monkeypatch) -> None:  # C4：run_react 用 builder 装配
    import app.agent.contexts.builder as b  # builder 模块
    import app.agent.fallback.agent as a  # ReAct 模块

    monkeypatch.setattr(b, "set_embed", lambda f: None)  # 禁嵌入（退保守剪枝，别拉模型）
    out = a._state_context({  # 装配
        "question": "q",  # 问题
        "working_memory": {"task_goal": "g", "key_facts": {"id": "1"}},  # 锚点
        "turns": [{"user": "上轮", "assistant": "已答"}],  # 近轮
    })  # 结束
    assert out is not None  # 装配成功
    assert "g" in out.system  # working 目标进 system
    assert "1" in out.system  # key_facts 进 system（锚点）
    assert "上轮" in out.messages[0]["content"]  # 近轮进消息
    assert "q" in out.messages[0]["content"]  # 问题最末