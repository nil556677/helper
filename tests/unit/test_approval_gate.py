"""审批判定：只读工具不批、写工具需批。"""

from __future__ import annotations  # 延迟求值注解

from types import SimpleNamespace  # 假工具

from app.agent.contracts import Mutation  # 变更画像
from app.agent.routing.approval import requires_approval  # 被测


def _tool(mutation) -> SimpleNamespace:  # 造工具
    return SimpleNamespace(metadata={"dsh": {"mutation": mutation}})  # 带元数据


def test_read_only_needs_no_approval() -> None:  # 只读
    catalog = {"a": _tool(Mutation.READ_ONLY)}  # 目录
    assert requires_approval(("a",), catalog) is False  # 不批


def test_write_needs_approval() -> None:  # 写
    catalog = {"a": _tool(Mutation.NON_IDEMPOTENT_WRITE)}  # 目录
    assert requires_approval(("a",), catalog) is True  # 需批


def test_unknown_tool_is_safe() -> None:  # 未知工具
    assert requires_approval(("nope",), {}) is False  # 不误判


def test_current_knowledge_agent_is_read_only() -> None:  # 现网知识专家
    from app.agent.agents.experts import EXPERTS  # 注册表

    tools = EXPERTS["knowledge_agent"][0].tools  # 工具
    assert requires_approval(tools) is False  # 全部只读