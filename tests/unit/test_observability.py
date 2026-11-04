"""可观测单测：run 级工具汇总正确（工具 span 由 TraceCallbacks 统一记录）。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.context import RunContext  # 运行上下文
from app.agent.driver import _tools_summary  # 工具汇总


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