"""装配器：进程级 RAG 子图与父图（stream 与 runtime 共用同一实例）。

E1：把「建图」从 `stream.py` / `runtime.py` 各自抽走，集中到一处。
E2：本模块保持同步 ``_graph()`` 接缝（测试可接桩）；AsyncSqliteSaver 的挂载因绑定
单个事件循环、与多循环单测冲突，列为 E2 收尾项（见 `checkpoint` 模块说明）。
"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

from app.agent.agent import build_route_graph  # 新路由父图（生产）
from app.agent.llm.provider import set_chat_model_provider  # 统一模型提供者
from app.agent.nodes.rag import RagRunner, real_steps  # RAG 子图（旧链路仍保留）

_RUNNER: RagRunner | None = None  # 进程级子图运行器
_GRAPH: Any = None  # 进程级编译父图
_GRAPH_OVERRIDE: Any = None  # worker 注入的 checkpointed 图（运行时接线 E2）
_MODEL: dict[str, Any] = {"tried": False, "model": None}  # 模型懒加载缓存


def _provider() -> Any:  # 现有模型提供者（懒解析，失败返回 None）
    """首次调用才构造；没配置/构造失败一律返回 None，让节点走降级。"""
    if not _MODEL["tried"]:  # 只解析一次
        _MODEL["tried"] = True  # 标记
        try:  # 容错
            from app.integrations.llm import chat_model  # 现有模型工厂

            _MODEL["model"] = chat_model()  # 取实例
        except Exception:  # 未配置
            _MODEL["model"] = None  # 降级
    return _MODEL["model"]  # 返回


def set_graph(graph: Any) -> None:  # 注入预编译图
    """worker 在其单循环里编译好 checkpointed 图后注入；``build`` 优先返回它（E2）。"""
    global _GRAPH_OVERRIDE  # 改模块级
    _GRAPH_OVERRIDE = graph  # 存


def get_runner() -> RagRunner:  # 取子图运行器
    """懒建，进程内复用。"""
    global _RUNNER  # 改模块级
    if _RUNNER is None:  # 还没有
        _RUNNER = RagRunner(real_steps())  # 建子图
    return _RUNNER  # 返回


def build(*, checkpointer: Any = None) -> Any:  # 取（必要时建）编译父图
    """首次编译后复用；显式传 checkpointer 时重建（测试/收尾用）。"""
    global _GRAPH  # 改模块级
    if _GRAPH_OVERRIDE is not None:  # worker 已注入
        return _GRAPH_OVERRIDE  # 用它
    if _GRAPH is None:  # 还没建
        _GRAPH = build_route_graph(checkpointer=checkpointer)  # 生产用新路由图
        set_chat_model_provider(_provider)  # 注入模型提供者（route_llm/general_agent 用）
    return _GRAPH  # 返回


def reset() -> None:  # 清空（测试隔离）
    """置空进程级缓存，让下一次 build 重建。"""
    global _RUNNER, _GRAPH, _GRAPH_OVERRIDE  # 改模块级
    _RUNNER = None  # 子图
    _GRAPH = None  # 父图
    _GRAPH_OVERRIDE = None  # 注入图
    _MODEL["tried"] = False  # 复位模型缓存
    _MODEL["model"] = None  # 清模型
