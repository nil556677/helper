"""契约层一致性：错误码单一来源、事件类型↔载荷一一对应、ToolResult 从 ResultEnvelope 派生。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.contracts import EventType, ResultEnvelope  # 契约
from app.agent.contracts.events import PAYLOAD_BY_TYPE  # 事件载荷映射
from app.agent.contracts.state import TurnToolCall  # 工具审计（唯一形状）
from app.agent.contracts.tools import ErrorCode  # 唯一错误码来源
from app.core.retry import RETRYABLE_KINDS  # 可重试类别（成对核对）


def test_payload_by_type_exactly_covers_eventtype() -> None:  # 载荷映射不重不漏
    assert set(PAYLOAD_BY_TYPE) == set(EventType)  # 每个事件类型都有对应载荷


def test_error_code_covers_retry_kinds() -> None:  # retry 分类已是 ErrorCode 常量
    kinds = {ErrorCode.RATE_LIMIT, ErrorCode.SERVER, ErrorCode.TIMEOUT, ErrorCode.CONNECTION}  # 可重试类
    assert kinds >= RETRYABLE_KINDS  # ErrorCode 覆盖 core.retry 的可重试集合


def test_error_code_has_all_layers() -> None:  # 三个错误层齐全
    assert ErrorCode.PROTOCOL_ARG_INVALID.startswith("protocol.")  # 协议层
    assert ErrorCode.EXECUTION_FAILED.startswith("execution.")  # 执行层
    assert ErrorCode.INTERNAL_ERROR.startswith("internal.")  # 内部层


def test_group_icons_cover_all_groups() -> None:  # 分组图标齐全
    from app.agent.contracts import GROUP_ICONS, GROUP_ORDER  # 分组图标/顺序

    assert set(GROUP_ICONS) == set(GROUP_ORDER)  # 每个分组都有图标
    assert all(icon for icon in GROUP_ICONS.values())  # 图标非空


def test_turn_tool_call_from_ok_envelope() -> None:  # 成功映射
    env = ResultEnvelope(ok=True, code="ok", data={"v": 1})  # 成功包络
    tr = TurnToolCall.from_env(env, tool_call_id="c1", name="demo", content="x")  # 派生
    assert tr.status == "ok"  # 状态
    assert tr.error_code == ""  # 无错误码
    assert tr.tool_call_id == "c1"  # 关联
    assert tr.artifact == {"v": 1}  # 完整结果同源


def test_turn_tool_call_from_error_envelope() -> None:  # 失败映射
    env = ResultEnvelope(ok=False, code="execution.failed", message="boom")  # 失败包络
    tr = TurnToolCall.from_env(env, tool_call_id="c1", name="demo")  # 派生
    assert tr.status == "error"  # 状态
    assert tr.error_code == "execution.failed"  # 错误码同源