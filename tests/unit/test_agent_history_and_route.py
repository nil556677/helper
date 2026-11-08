"""回归：历史成对读取 + 新路由图边函数（single source）与状态机一致。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.contexts.history import messages_from, turns_from_rows  # 被测：轮次成对
from app.agent.routing.edges import after_rule, after_semantic  # 被测：条件边


def test_history_drops_orphan_user_turns() -> None:  # 历史成对
    """上一轮回答为空时，不能把它的 user 问题单独喂给模型。"""
    rows = [  # 真实故障的数据形状
        {"id": 1, "role": "user", "content": "成都市是哪个省的"},  # 上一轮问题
        {"id": 2, "role": "assistant", "content": ""},  # 回答为空（被过滤）
        {"id": 3, "role": "user", "content": "广元市下面有哪些县区"},  # 又一悬空问题
        {"id": 4, "role": "assistant", "content": ""},  # 回答为空
        {"id": 5, "role": "user", "content": "成都下面有哪些区县"},  # 有配对
        {"id": 6, "role": "assistant", "content": "答案 [1]"},  # 回答
        {"id": 7, "role": "user", "content": "还没回答的新问题"},  # 结尾未配对
    ]  # 数据结束
    turns = turns_from_rows(0, rows)  # 读轮次
    assert [t.user for t in turns] == ["成都市是哪个省的", "广元市下面有哪些县区", "成都下面有哪些区县"]  # 成对
    assert [t.status.value for t in turns] == ["unanswered", "unanswered", "done"]  # 空回答标未答
    assert [m["content"] for m in messages_from(turns)] == ["成都下面有哪些区县", "答案 [1]"]  # 只喂 done


def test_after_rule_reads_route_only() -> None:  # 规则后分支（单一事实来源）
    """只按 route 决策，不看其余 state。"""
    assert after_rule({"route": {"intent": "direct"}}) == "dispatch_fixed"  # 已定
    assert after_rule({"route": None}) == "semantic_router"  # 未定
    assert after_rule({}) == "semantic_router"  # 缺省未定


def test_after_semantic_three_states() -> None:  # 语义后三态分支
    assert after_semantic({"semantic_recall": {"decision": "direct"}}) == "dispatch_fixed"  # direct
    assert after_semantic({"semantic_recall": {"decision": "ambiguous"}}) == "route_llm"  # LLM 兜底
    assert after_semantic({"semantic_recall": {"decision": "miss"}}) == "triage"  # miss → 后置分诊
    assert after_semantic({}) == "triage"  # 缺省 miss → 后置分诊