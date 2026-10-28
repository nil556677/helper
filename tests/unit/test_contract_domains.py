"""六域契约的基础行为测试（P1：只验定义，不验接线）。"""

from __future__ import annotations  # 类型注解延迟求值

import pytest  # 测试框架
from pydantic import ValidationError  # 校验异常

from app.agent.contracts.collab import (  # 任务委派契约
    AgentCard,
    TaskEnvelope,
    TaskStatus,
    is_final,
    task_can_transition,
)
from app.agent.contracts.events import StagePayload  # 事件载荷
from app.agent.contracts.messages import ToolCall, ToolResult  # 消息契约
from app.agent.contracts.output import (  # 模型输出契约
    Candidate,
    DecisionTarget,
    RouteDecision,
    candidate_id,
)
from app.agent.contracts.state import (  # 共享状态契约
    WRITE_TABLE,
    Turn,
    TurnStatus,
    TurnToolCall,
    merge_turns,
    only_done,
)
from app.agent.contracts.tools import (  # 工具契约
    ErrorCode,
    Mutation,
    ResultEnvelope,
    ToolDescriptor,
)


def test_route_decision_defaults_and_enum() -> None:  # 决策默认值
    """最小构造即可用；next 收敛成枚举。"""
    decision = RouteDecision(next="answer")  # 构造
    assert decision.next is DecisionTarget.ANSWER  # 枚举收敛
    assert decision.confidence is None  # 默认不表信心
    assert decision.candidates == []  # 默认无候选
    assert decision.version == 1  # 版本号


def test_route_decision_rejects_bad_confidence() -> None:  # 信心越界
    """confidence 必须在 0–1。"""
    with pytest.raises(ValidationError):  # 期望校验失败
        RouteDecision(next="answer", confidence=1.5)  # 越界


def test_route_decision_rejects_duplicate_candidate_ids() -> None:  # 候选重复
    """候选 id 重复会让去重/resume 失效。"""
    with pytest.raises(ValidationError):  # 期望校验失败
        RouteDecision(  # 构造
            next="clarify",  # 追问
            needs_clarification=True,  # 需要澄清
            candidates=[Candidate(id="a", text="甲"), Candidate(id="a", text="乙")],  # 重复 id
        )  # 结束


def test_candidate_id_is_stable() -> None:  # 候选 id 稳定
    """同一文本两次生成同一个 id（程序生成，不采信模型随机 token）。"""
    assert candidate_id("按 项目 名称") == candidate_id("按 项目 名称")  # 同输入同输出
    assert candidate_id("按 项目 名称")  # 非空


def test_turn_done_requires_assistant() -> None:  # done 必有文本
    """done 却没有 assistant 文本视为构造错误。"""
    with pytest.raises(ValidationError):  # 期望失败
        Turn(turn_id="t1", status="done", user="问题", assistant="")  # 空回答


def test_turn_tool_call_ids_unique() -> None:  # 调用 id 唯一
    """同一轮里 tool_call_id 必须唯一。"""
    with pytest.raises(ValidationError):  # 期望失败
        Turn(  # 构造
            turn_id="t1",  # 轮次
            status="done",  # 完成
            user="问题",  # 问题
            assistant="回答",  # 回答
            tool_calls=[  # 重复调用
                TurnToolCall(tool_call_id="c1", name="search_kb"),  # 第一条
                TurnToolCall(tool_call_id="c1", name="read_file"),  # 重复 id
            ],  # 列表结束
        )  # 结束


def test_merge_turns_replaces_by_id() -> None:  # 轮次合并
    """同 turn_id 覆盖、不同追加（重放幂等）。"""
    a = Turn(turn_id="t1", status="done", user="问 1", assistant="答 1")  # 第一轮
    b = Turn(turn_id="t1", status="failed", user="问 1", assistant="答 1 改")  # 覆盖同 id
    c = Turn(turn_id="t2", status="done", user="问 2", assistant="答 2")  # 新一轮
    merged = merge_turns([a], [b, c])  # 合并
    assert [t.turn_id for t in merged] == ["t1", "t2"]  # 顺序保持
    assert merged[0].status is TurnStatus.FAILED  # 已覆盖


def test_only_done_filters_unfinished() -> None:  # 只喂 done
    """未完成轮不进历史（不给模型噪音）。"""
    turns = [  # 混合轮次
        Turn(turn_id="t1", status="done", user="问 1", assistant="答 1"),  # 完成
        Turn(turn_id="t2", status="unanswered", user="问 2"),  # 未答
        Turn(turn_id="t3", status="cancelled", user="问 3", assistant="半截"),  # 取消
    ]  # 列表结束
    assert [t.turn_id for t in only_done(turns)] == ["t1"]  # 只留 done


def test_write_table_has_core_channels() -> None:  # 写契约表
    """核心 channel 都必须有 owner（单一事实来源）。"""
    channels = {row.channel for row in WRITE_TABLE}  # 收集
    assert {"turns", "evidence", "route", "answer", "status"} <= channels  # 子集
    assert all(row.owner for row in WRITE_TABLE)  # 每行都有 owner


def test_tool_descriptor_defaults() -> None:  # 工具描述符
    """默认只读、版本 1。"""
    desc = ToolDescriptor(name="search_kb")  # 构造
    assert desc.mutation is Mutation.READ_ONLY  # 默认只读
    assert desc.version == 1  # 默认版本


def test_result_envelope_error_in_band() -> None:  # 带内错误
    """错误走包络而不是异常。"""
    env = ResultEnvelope(ok=False, code=ErrorCode.EXECUTION_RATE_LIMIT, retryable=True, retry_after_s=2.0)  # 限流
    assert env.ok is False  # 失败
    assert env.retryable is True  # 可重试
    assert env.retry_after_s == 2.0  # 建议等待


def test_error_code_protocol_prefix() -> None:  # 错误码分级
    """protocol.* 与 execution.* 可区分。"""
    assert ErrorCode.is_protocol(ErrorCode.PROTOCOL_ARG_INVALID) is True  # 协议层
    assert ErrorCode.is_protocol(ErrorCode.EXECUTION_TIMEOUT) is False  # 执行层


def test_tool_result_carries_call_id() -> None:  # 调用关联
    """结果必须能对应回请求。"""
    call = ToolCall(tool_call_id="c1", name="search_kb", args={"q": "x"})  # 请求
    result = ToolResult(tool_call_id=call.tool_call_id, status="ok", content="命中")  # 结果
    assert result.tool_call_id == call.tool_call_id  # 关联一致


def test_task_transitions_and_terminal_absorb() -> None:  # 任务状态机
    """合法迁移放行；终态吸收。"""
    assert task_can_transition(TaskStatus.SUBMITTED, TaskStatus.WORKING) is True  # 开始
    assert task_can_transition(TaskStatus.WORKING, TaskStatus.COMPLETED) is True  # 完成
    assert task_can_transition(TaskStatus.COMPLETED, TaskStatus.WORKING) is False  # 终态不可退
    assert task_can_transition(TaskStatus.COMPLETED, TaskStatus.COMPLETED) is True  # 停在原状
    assert is_final(TaskStatus.FAILED) is True  # 终态判定


def test_agent_card_and_task_envelope_defaults() -> None:  # 委派对象
    """AgentCard/TaskEnvelope 默认值可用。"""
    card = AgentCard(agent_id="kb_agent")  # 能力卡
    assert card.tools == []  # 默认无工具
    task = TaskEnvelope(task_id="task-1", target="kb_agent")  # 任务
    assert task.status is TaskStatus.SUBMITTED  # 默认已提交


def test_stage_payload_agent_fields_default_empty() -> None:  # 事件扩展
    """新增 agent 维度默认空，旧调用不受影响。"""
    payload = StagePayload(stage_id="s1", status="running")  # 旧式构造
    assert payload.agent_id == ""  # 默认主链
    assert payload.parent_run_id == ""  # 无父 run
    assert payload.branch == ""  # 无分支
