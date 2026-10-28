"""工具契约：ToolSpec + 处理器签名（handler 统一返回 Turn）。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.intent import RouteDecision
from app.ui.state.chat_model import Turn


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的元信息 + 执行入口。封闭集合：只能执行注册表里的工具。"""

    name: str
    description: str
    param_schema: dict[str, dict] = field(default_factory=dict)
    enabled: bool = True
    handler: Callable[[str, RouteDecision], Turn] | None = None
