"""finalize 包：唯一入口是异步节点 `node.finalize_node`（旧的同步桥已删除）。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.nodes.finalize.node import finalize_node  # 收尾节点

__all__ = ["finalize_node"]  # 对外导出
