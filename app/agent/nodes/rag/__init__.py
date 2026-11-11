"""RAG 子图包：state / graph / node / factory。

**检索唯一入口**：对外只经 `search_knowledge_base` 工具（ReAct 图谱里模型按需调用）
→ `RagRunner(real_steps())`。旧的父图级固定节点 ``make_rag_node`` 已被移除
（它未接进生产路由图，且带独立的 gate/档位语义，容易造成两套检索分叉）。
"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.nodes.rag.factory import real_steps  # 真实步骤
from app.agent.nodes.rag.graph import RagSteps, build_rag_graph  # 图
from app.agent.nodes.rag.node import RagResult, RagRunner  # 运行器
from app.agent.nodes.rag.state import RagState  # 状态

__all__ = [  # 导出
    "RagResult",  # 结果
    "RagRunner",  # 运行器
    "RagState",  # 状态
    "RagSteps",  # 步骤
    "build_rag_graph",  # 建图
    "real_steps",  # 真实步骤
]  # 导出结束
