"""知识库检索工具（合并版）：一个工具、一个描述，用 `mode` 区分检索强度。

由 `@tool_spec` 收口错误、缓存与视角分离；作者只负责把检索结果整理成固定 data
（给模型看的编号文本 + 结构化证据 + 门限 + 依据 + 本次 query）。
"""

from __future__ import annotations  # 延迟求值注解

import threading  # 运行器懒建加锁
from typing import Any, Literal  # 任意类型 / 枚举

from pydantic import BaseModel, Field  # 参数/结果契约（args_schema 会发给模型）

from app.agent.nodes.rag import RagRunner  # 检索运行器
from app.agent.nodes.rag.factory import real_steps  # 真实检索步骤
from app.agent.tools.tooling import tool_spec  # 统一范式

_RUNNER: RagRunner | None = None  # 进程级复用的运行器
_RUNNER_LOCK = threading.Lock()  # 懒建锁（防并发重复构建）

#: 各模式的推荐返回条数（未显式给 top_k 时用）。
_MODE_TOP_K: dict[str, int] = {"fast": 6, "hybrid": 10, "deep": 20}  # 模式 → 条数


def _runner() -> RagRunner:  # 懒建运行器
    """缓存运行器，避免每次查询重编译子图；双检加锁。"""
    global _RUNNER  # 改模块级
    if _RUNNER is None:  # 还没建（快路径）
        with _RUNNER_LOCK:  # 加锁
            if _RUNNER is None:  # 双检
                _RUNNER = RagRunner(real_steps())  # 建
    return _RUNNER  # 返回


def _format(result: Any, max_chars: int | None = None) -> str:  # 组装带编号的模型文本
    """把证据片段拼成「[序号] 来源「标题」正文」；总长度按条数做预算，防止撑爆上下文。"""
    count = max(1, len(getattr(result, "evidence", []) or []))  # 证据条数
    per_item = int(max_chars) if max_chars is not None else max(300, 3000 // count)  # 单条预算（总约 3000 字）
    lines: list[str] = []  # 段落
    for number, item in enumerate(result.evidence, start=1):  # 逐条证据
        body = str(item.get("text") or "")[:per_item]  # 限长正文
        source = str(item.get("source_name") or "资料")  # 来源名
        heading = str(item.get("heading") or "")  # 标题
        head = f"{source}" + (f"「{heading}」" if heading else "")  # 头
        lines.append(f"[{number}] {head}\n{body}")  # 一段
    if not lines:  # 没证据
        return "未在本地知识库检索到相关资料。"  # 说明
    return "\n\n".join(lines)  # 拼接


def collect_evidence(data: dict[str, Any]) -> list[dict[str, Any]]:  # 取证据并返回
    """从成功包络的 data 里抽出 evidence；工具壳返回后由图执行器调用。"""
    return list(data.get("evidence") or [])  # 原始证据（编号由调用方累加）


class KnowledgeQuery(BaseModel):  # 参数契约（会作为 args_schema 发给模型）
    query: str = Field(  # 查询参数
        description="要在本地知识库检索的问题或关键词",  # 给模型的说明
    )  # 字段结束
    mode: Literal["fast", "hybrid", "deep"] = Field(  # 检索强度
        default="fast",  # 默认快速
        description="检索强度：fast=快速（一般问题）；hybrid=混合召回更大（多来源/术语多）；deep=深度（复杂/多跳，召回最大）",  # 给模型的说明
    )  # 字段结束
    top_k: int = Field(  # 返回条数参数
        default=0,  # 0 = 按 mode 自动
        ge=0,  # 允许 0（自动）
        le=50,  # 最多 50
        description="返回的命中片段条数；0 表示按 mode 自动（fast 6 / hybrid 10 / deep 20）",  # 给模型的说明
    )  # 字段结束


class SearchResultData(BaseModel):  # 结果契约（describe/审计/下游校验用）
    """`search_knowledge_base` 成功时 data 的固定形状。"""

    query: str = ""  # 本次检索用的 query
    mode: str = "fast"  # 检索强度
    model_view: str = ""  # 给模型的编号文本
    evidence: list[dict[str, Any]] = Field(default_factory=list)  # 结构化证据
    gate: dict[str, Any] = Field(default_factory=dict)  # 门限判决
    basis: dict[str, Any] = Field(default_factory=dict)  # 检索依据（计数/档位/路径）


@tool_spec(  # 统一范式 + 10 分钟幂等缓存
    description=(  # 能力说明（一个工具、一个描述）
        "在本地知识库检索资料并返回带 [编号] 的命中片段。"
        "mode 控制检索强度：fast 适合一般问题；hybrid 召回更大，适合术语多/要多来源；"
        "deep 适合复杂、多跳、需要更多证据的问题。"
    ),  # 说明结束
    args_schema=KnowledgeQuery,  # 参数契约
    result_schema=SearchResultData,  # 结果契约
    ttl=600.0,  # 同参 10 分钟内幂等
    timeout_ms=8000,  # 检索可能较慢，给到 8 秒
)  # 装饰结束
async def search_knowledge_base(query: str, mode: str = "fast", top_k: int = 0):  # 异步纯函数（返回 data）
    """成功返回固定 data；检索失败抛给 tool_spec 转带内可重试。"""
    chosen = str(mode or "fast")  # 模式
    profile = "deep" if chosen == "deep" else "tool"  # 深度档 vs 工具档
    limit = int(top_k) or _MODE_TOP_K.get(chosen, 6)  # 0 → 按模式自动
    result = await _runner().run(query, profile=profile, top_k=int(limit), rerank=True)  # 唯一检索内核
    return _data(result, query=query, mode=chosen)  # 固定 data 结构


def _data(result: Any, *, query: str, mode: str) -> dict:  # 检索结果 → 固定 data
    """编号文本 + 结构化证据 + 门限 + 依据 + 本次 query（归因用）。"""
    return {  # 固定 data 结构
        "query": str(query),  # 本次检索 query（模型/审计确认对应哪次调用）
        "mode": str(mode),  # 检索强度
        "model_view": _format(result),  # 给模型的编号文本
        "evidence": list(result.evidence),  # 结构化证据（供回填/审计）
        "gate": dict(result.gate),  # 门限判决
        "basis": {  # 检索依据
            "dense_count": result.dense_count,  # 语义命中数
            "sparse_count": result.sparse_count,  # 关键词命中数
            "fused_count": result.fused_count,  # 融合数
            "selected_count": result.selected_count,  # 证据数
            "profile": getattr(result, "profile", "tool"),  # 检索档位（观测）
            "paths": list(getattr(result, "paths", ["baseline"])),  # 实际路径（观测）
        },  # basis 结束
    }  # 返回