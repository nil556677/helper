"""可观测单测：trace_tool 无会话安全空操作；run 级工具汇总正确。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.context import RunContext  # 运行上下文
from app.agent.driver import _tools_summary  # 工具汇总
from app.core.trace.recorder import trace_tool  # 工具 trace


def test_trace_tool_no_session_is_noop() -> None:  # 无会话不崩
    trace_tool(  # 直接调用
        "current_time",  # 工具名
        ok=True,  # 成功
        code="ok",  # 错误码
        retryable=False,  # 不重试
        cached=False,  # 未命中
        args_chars=2,  # 入参体积
        output_chars=10,  # 输出体积
    )  # 应安全空操作


def test_tools_summary_aggregates() -> None:  # 汇总聚合
    ctx = RunContext(run_id="r", question="q")  # 上下文
    ctx.tool_calls.extend(  # 两条工具调用
        [
            {"name": "a", "ok": True, "cached": True, "ms": 10, "code": "ok"},  # 命中
            {"name": "b", "ok": False, "cached": False, "ms": 20, "code": "execution.failed"},  # 失败
        ]
    )
    summary = _tools_summary(ctx)  # 汇总
    assert summary == {"count": 2, "total_ms": 30, "hits_cached": 1, "errors": 1}  # 断言