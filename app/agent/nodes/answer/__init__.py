"""answer 包：唯一入口是异步节点 `node.answer_node`（旧的同步桥已删除）。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.nodes.answer.node import answer_node  # 生成节点

__all__ = ["answer_node"]  # 对外导出
