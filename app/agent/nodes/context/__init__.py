"""context 包：新链路的异步节点 + 旧图的同步兼容入口。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意类型

from langchain_core.runnables import RunnableConfig  # 图配置


def load_context(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:  # 旧图兼容
    """旧图只需要一个空操作；新图用 .node.load_context。"""
    del state, config  # 不读取
    return {}  # 无补充


__all__ = ["load_context"]  # 导出
