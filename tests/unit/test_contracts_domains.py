"""六域契约的边界行为（P1 契约收紧后的回归：类型安全、静默失败、状态不变量）。"""

from __future__ import annotations  # 类型注解延迟求值

import ast  # 静态检查依赖方向
import functools  # partial 可调用对象
import typing  # 读取注解
from pathlib import Path  # 读取源码
from typing import ClassVar  # 类级可变默认值标注

import pytest  # 测试框架
from pydantic import BaseModel, ValidationError  # 校验异常

from app.agent.contracts import (  # 包级导出（含本次新增）
    Candidate,  # 澄清候选
    DecisionTarget,  # 决策目标枚举
    EventEnvelope,  # 事件信封
    EventType,  # 事件类型
    Namer,  # 命名器
    RouteDecision,  # 决策对象
    TaskEnvelope,  # 任务信封
    TaskStatus,  # 任务状态
    collab,  # 委派子模块
    is_final,  # 终态判断
    kinds,  # 事件类型子模块
    make,  # 事件构造器
    state,  # 状态子模块
)
from app.agent.contracts.events import DeltaPayload, ToolPayload  # 载荷模型
from app.agent.contracts.serialize import from_dict, to_dict, validate_sequence  # 序列化
from app.agent.contracts.state import Turn, TurnStatus, TurnToolCall, merge_turns  # 状态
from app.agent.contracts.tools import ErrorCode, ToolDescriptor  # 工具契约


class _Args(BaseModel):  # 合法参数模型
    text: str = ""  # 一个字段


class _FakeResult:  # 结构化结果（不 import app.core.result）
    ok = False  # 失败
    code = "execution.failed"  # 错误码
    data: ClassVar[dict[str, int]] = {"raw": 1}  # 审计数据


def _named() -> None:  # 有 __name__ 的可调用对象
    """占位函数。"""


def test_task_envelope_persists_output_and_error() -> None:  # 完成结果有处可放
    """COMPLETED 带 output、FAILED 带 error，且不再被 extra 静默丢弃。"""
    done = TaskEnvelope(task_id="t1", idempotency_key="k1", status=TaskStatus.COMPLETED, output={"answer": 42})  # 完成
    assert done.output == {"answer": 42}  # 结果保留
    failed = TaskEnvelope(task_id="t2", idempotency_key="k2", status=TaskStatus.FAILED, error="boom")  # 失败
    assert failed.error == "boom"  # 原因保留
    assert done.status is TaskStatus.COMPLETED  # 状态显式保留


def test_task_envelope_requires_idempotency_key() -> None:  # 幂等键必填
    """发起方必须显式给幂等键，不允许“空着也行”。"""
    with pytest.raises(ValidationError):  # 缺字段
        TaskEnvelope(task_id="t1")  # 不传 idempotency_key


def test_task_envelope_rejects_bad_deadline() -> None:  # deadline 跨字段校验
    """deadline 必须严格晚于 created_at。"""
    with pytest.raises(ValidationError):  # 相等也不行
        TaskEnvelope(task_id="t1", idempotency_key="k", created_at=100.0, deadline=100.0)  # 等于创建时间
    with pytest.raises(ValidationError):  # 早于创建时间
        TaskEnvelope(task_id="t1", idempotency_key="k", created_at=100.0, deadline=99.0)  # 已过期
    task = TaskEnvelope(task_id="t1", idempotency_key="k", created_at=100.0, deadline=101.0)  # 合法
    assert task.deadline == 101.0  # 保留


def test_is_final_is_exported_from_package() -> None:  # 终态判断单一事实来源
    """从包导入即可，不用绕过 __init__ 直接找子模块。"""
    assert is_final(TaskStatus.COMPLETED) is True  # 终态
    assert is_final(TaskStatus.WORKING) is False  # 非终态


def test_make_rejects_payload_and_fields_mix() -> None:  # 两种来源不能混用
    """payload 与 **fields 同时传时显式报错，不再静默丢字段。"""
    payload = ToolPayload(tool_name="real", status="ok")  # 完整载荷
    with pytest.raises(ValueError, match="不能同时传"):  # 明确拒绝
        make(EventType.TOOL, payload, tool_name="ignored")  # 混用
    with pytest.raises(TypeError):  # 载荷类型不对
        make(EventType.TOOL, payload=DeltaPayload(text="x"))  # 错配模型


def test_make_rejects_unknown_payload_field() -> None:  # 拼写错误构造期失败
    """载荷 extra=forbid：tool_nmae 这类拼写错误不再静默通过。"""
    with pytest.raises(ValidationError):  # 未知字段
        make(EventType.TOOL, tool_name="x", status="ok", tool_nmae="oops")  # 拼错字段


def test_event_payload_is_discriminated_union() -> None:  # 判别联合收窄
    """payload 由 type 判别字段选模型，静态/运行期都能收窄。"""
    event = EventEnvelope(event_id="e1", type=EventType.DELTA, payload={"text": "hi"})  # 只给 dict
    assert isinstance(event.payload, DeltaPayload)  # 已收窄成模型
    assert event.payload.text == "hi"  # 可直接取字段
    data = to_dict(event)  # 序列化
    assert data["payload"]["type"] == "delta"  # 判别字段进 JSON（反序列化需要）
    assert from_dict(data).payload.text == "hi"  # 往返一致


def test_event_envelope_rejects_payload_type_mismatch() -> None:  # type 两处必须一致
    """信封 type=delta 但载荷是 ToolPayload，构造期拒绝。"""
    with pytest.raises(ValidationError):  # 不一致
        EventEnvelope(event_id="e1", type=EventType.DELTA, payload=ToolPayload(tool_name="x", status="ok"))  # 错配
    with pytest.raises(ValidationError):  # dict 自带冲突的 type
        EventEnvelope(event_id="e1", type=EventType.DELTA, payload={"type": "tool", "tool_name": "x", "status": "ok"})  # 冲突


def test_event_forbids_unknown_fields() -> None:  # 信封与载荷都拒绝多余字段
    """未知字段直接报错，避免拼写错误静默通过。"""
    with pytest.raises(ValidationError):  # 载荷多字段
        ToolPayload(tool_name="x", status="ok", extra_typo=1)  # 未知字段
    with pytest.raises(ValidationError):  # 信封多字段
        EventEnvelope(event_id="e1", type=EventType.ACK, payload={}, runid="typo")  # 未知字段


def test_route_decision_clarify_requires_candidates() -> None:  # CLARIFY 语义一致
    """next=CLARIFY 必须带候选，避免下游拿到空澄清。"""
    with pytest.raises(ValidationError, match="candidates"):  # 无候选
        RouteDecision(next=DecisionTarget.CLARIFY)  # 空澄清
    decision = RouteDecision(  # 合法澄清
        next=DecisionTarget.CLARIFY,  # 追问
        candidates=[Candidate(id="a", text="甲")],  # 有候选
        needs_clarification=True,  # 标记
    )  # 结束
    assert decision.candidates[0].id == "a"  # 候选保留
    assert RouteDecision(next=DecisionTarget.ANSWER).candidates == []  # 非 CLARIFY 可空


def test_namer_rejects_callable_without_name() -> None:  # 空 id 守门
    """没有 __module__/__name__ 的可调用对象不能产出空 id。"""
    with pytest.raises(ValueError, match="派生 id"):  # object 实例
        Namer.from_callable(object())  # 无名
    with pytest.raises(ValueError, match="派生 id"):  # partial 只有 __module__
        Namer.from_callable(functools.partial(len, []))  # 无名
    assert Namer.from_callable(_named).endswith("._named")  # 正常函数仍可派生


def test_turn_tool_call_from_env_uses_protocol() -> None:  # 只依赖结果形状
    """from_env 接受任何含 ok/code/data 的对象，不需要 core.result。"""
    call = TurnToolCall.from_env(_FakeResult(), tool_call_id="c1", name="demo")  # 派生
    assert call.status == "error"  # 失败
    assert call.error_code == "execution.failed"  # 错误码
    assert call.artifact == {"raw": 1}  # 审计数据


def test_contract_state_does_not_import_core() -> None:  # 契约层零业务依赖
    """state.py 不得 import app.core（AST 检查，注释里的词不算）。"""
    tree = ast.parse(Path(state.__file__).read_text(encoding="utf-8"))  # 解析源码
    modules: list[str] = []  # 实际 import 的模块
    for node in ast.walk(tree):  # 逐节点
        if isinstance(node, ast.ImportFrom):  # from x import y
            modules.append(node.module or "")  # 记录模块
        elif isinstance(node, ast.Import):  # import x
            modules.extend(alias.name for alias in node.names)  # 记录模块
    assert not any(name.startswith("app.core") for name in modules)  # 不允许
    assert isinstance(TurnToolCall.from_env(_FakeResult()), TurnToolCall)  # 形状协议可用


def test_frozenset_annotations_are_parameterized() -> None:  # 泛型注解不丢元素类型
    """kinds/collab 的 frozenset 都带元素类型，类型检查器能校验枚举拼写。"""
    kinds_hints = typing.get_type_hints(kinds)  # kinds 注解
    assert typing.get_origin(kinds_hints["TERMINAL_TYPES"]) is frozenset  # 泛型
    assert typing.get_args(kinds_hints["TERMINAL_TYPES"])[0] is kinds.EventType  # 元素类型
    collab_hints = typing.get_type_hints(collab)  # collab 注解
    transitions = collab_hints["TASK_TRANSITIONS"]  # 迁移表注解
    assert typing.get_origin(transitions) is frozenset  # 泛型
    pair = typing.get_args(transitions)[0]  # 元素类型（元组）
    assert typing.get_origin(pair) is tuple  # 元组
    assert set(typing.get_args(pair)) == {collab.TaskStatus}  # 两个元素都是 TaskStatus


def test_merge_turns_append_and_overwrite() -> None:  # reducer 语义不变
    """同 id 覆盖、新 id 追加且保持顺序；纯追加不修改旧列表。"""
    first = Turn(turn_id="t1", status=TurnStatus.DONE, user="问 1", assistant="答 1")  # 第一轮
    second = Turn(turn_id="t2", status=TurnStatus.DONE, user="问 2", assistant="答 2")  # 第二轮
    base = [first]  # 旧值
    merged = merge_turns(base, [second])  # 纯追加
    assert [turn.turn_id for turn in merged] == ["t1", "t2"]  # 顺序
    assert base == [first]  # 旧列表未被就地修改
    over = Turn(turn_id="t1", status=TurnStatus.FAILED, user="问 1", assistant="答 1 改")  # 覆盖
    merged = merge_turns(base, [over])  # 覆盖
    assert merged[0].status is TurnStatus.FAILED  # 已覆盖
    assert len(merged) == 1  # 不重复
    dup = merge_turns([first], [over, first])  # update 内部同 id 重复
    assert len(dup) == 1  # 仍只一条


def test_validate_sequence_rejects_empty_by_default() -> None:  # 空 run 是上游 bug
    """默认拒绝空序列；需要逐步校验的流式调用显式 allow_empty=True。"""
    with pytest.raises(ValueError, match="不能为空"):  # 默认严格
        validate_sequence([])  # 空
    validate_sequence([], allow_empty=True)  # 显式放宽
    done = make(EventType.DONE, ttft_ms=0, total_ms=1)  # 终态
    validate_sequence([done])  # 合法
    with pytest.raises(ValueError, match="最后一帧"):  # 终态不在最后
        validate_sequence([done, make(EventType.ACK)])  # 非法顺序


def test_tool_descriptor_schema_types() -> None:  # schema 不再是 Any
    """参数/结果 schema 只接受模型类、JSON Schema 字典或 None。"""
    assert ToolDescriptor(name="t", args_schema=_Args).args_schema is _Args  # 模型类
    assert ToolDescriptor(name="t", result_schema={"type": "object"}).result_schema == {"type": "object"}  # 字典
    with pytest.raises(ValidationError):  # 字符串被拒绝
        ToolDescriptor(name="t", args_schema="not-a-model")  # 非法类型


def test_error_code_prefix_rule() -> None:  # 前缀规则可判定
    """protocol.* 是协议层；execution/internal 各自判断；扁平码保持扁平。"""
    assert ErrorCode.is_protocol(ErrorCode.PROTOCOL_ARG_INVALID) is True  # 协议层
    assert ErrorCode.is_protocol(ErrorCode.EXECUTION_FAILED) is False  # 执行层不算协议层
    assert ErrorCode.EXECUTION_FAILED.startswith("execution.")  # 执行层前缀
    assert ErrorCode.INTERNAL_ERROR.startswith("internal.")  # 内部层前缀
    assert not ErrorCode.SCHEMA_INVALID.startswith(("protocol.", "execution.", "internal."))  # 扁平码
