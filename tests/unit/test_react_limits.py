"""ReAct 熔断：单工具失败达阈值后被拦截（接线 MAX_PER_TOOL，消除死代码）。"""

from __future__ import annotations  # 延迟值注解

from langchain_core.messages import ToolMessage  # 工具结果消息

from app.agent.react.graph import _circuit_blocked_names  # 熔断判据
from app.agent.react.policies.limits import MAX_PER_TOOL  # 阈值


def _tm(name: str, ok: bool) -> ToolMessage:  # 造工具结果消息
    return ToolMessage(  # 构造（须带 tool_call_id）
        content="r",  # 正文
        name=name,  # 工具名
        tool_call_id=f"call-{name}",  # 关联调用 id
        status="success" if ok else "error",  # 成功/失败
    )  # 结束


def test_blocks_after_threshold() -> None:  # 失败达阈值 → 熔断
    msgs = [_tm("weather", False) for _ in range(MAX_PER_TOOL)]  # 连续失败
    assert "weather" in _circuit_blocked_names({"messages": msgs})  # 熔断


def test_under_threshold_not_blocked() -> None:  # 未达阈值不熔断
    msgs = [_tm("weather", False) for _ in range(MAX_PER_TOOL - 1)]  # 差一次
    assert "weather" not in _circuit_blocked_names({"messages": msgs})  # 不熔断


def test_success_does_not_count_as_failure() -> None:  # 成功不计入失败
    msgs = [_tm("weather", False) for _ in range(MAX_PER_TOOL - 1)] + [_tm("weather", True)]  # 末次成功
    assert "weather" not in _circuit_blocked_names({"messages": msgs})  # 不熔断


def test_mixed_tools_only_blocked_one() -> None:  # 只熔断达标的那个
    msgs = [_tm("a", False) for _ in range(MAX_PER_TOOL)]  # a 达标
    msgs += [_tm("b", False) for _ in range(MAX_PER_TOOL - 1)]  # b 未达标
    blocked = _circuit_blocked_names({"messages": msgs})  # 结果
    assert "a" in blocked and "b" not in blocked  # 只 a 熔断