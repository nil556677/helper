"""知识库检索工具：按统一范式编写 —— 异步纯函数返回 data，检索失败让上游异常冒泡给 tool_spec。

由 `@tool_spec` 收口错误（转 execution.failed 可重试包络）与 10 分钟幂等缓存；作者只负责把
检索结果整理成固定 data（含给模型看的编号文本 + 结构化证据，供图执行器回填 state 做引用校验）。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意类型

from pydantic import BaseModel, Field  # 参数契约（会随 args_schema 发给模型）

from app.agent.nodes.rag import RagRunner  # 检索运行器
from app.agent.nodes.rag.factory import real_steps  # 真实检索步骤
from app.agent.tools.tooling import tool_spec  # 统一范式

_RUNNER: RagRunner | None = None  # 进程级复用的运行器


def _runner() -> RagRunner:  # 懒建运行器
    """缓存运行器，避免每次查询重编译子图。"""
    global _RUNNER  # 改模块级
    if _RUNNER is None:  # 还没建
        _RUNNER = RagRunner(real_steps())  # 建
    return _RUNNER  # 返回


def _format(result: Any, max_chars: int = 2000) -> str:  # 组装带编号的模型文本
    """把证据片段拼成「[序号] 来源「标题」正文」的段落，模型按号引用。"""
    lines: list[str] = []  # 段落
    for number, item in enumerate(result.evidence, start=1):  # 逐条证据
        body = str(item.get("text") or "")[:max_chars]  # 限长正文
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


#: 参数契约：字段说明只写 Field(description=...)，不写行尾注释（模型只能读到 description）。
class KnowledgeQuery(BaseModel):  # 参数契约
    query: str = Field(  # 查询参数
        description="要在知识库检索的问题或关键词",  # 给模型的说明
    )  # 字段结束
    top_k: int = Field(  # 返回条数参数
        default=6,  # 默认 6
        ge=1,  # 最少 1
        le=20,  # 最多 20
        description="返回的命中片段条数",  # 给模型的说明
    )  # 字段结束


@tool_spec(  # 统一范式 + 10 分钟幂等缓存
    description="在本地知识库检索相关问题资料，返回带 [编号] 的命中片段。",  # 能力说明
    args_schema=KnowledgeQuery,  # 参数契约
    ttl=600.0,  # 同参 10 分钟内幂等
)  # 装饰结束
async def search_knowledge_base(query: str, top_k: int = 6):  # 异步纯函数（返回 data）
    """成功返回 data（含编号文本/证据/门限/依据）；检索失败抛给 tool_spec 转带内可重试。"""
    result = await _runner().run(query, profile="tool", top_k=int(top_k))  # 跑检索链路（工具档：轻、零 LLM）
    return _data(result)  # 固定 data 结构


def _data(result: Any) -> dict:  # 检索结果 → 固定 data
    """编号文本 + 结构化证据 + 门限 + 依据（各检索工具共用同一形状）。"""
    return {  # 固定 data 结构
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


class HybridQuery(BaseModel):  # 混合检索参数
    query: str = Field(description="要在知识库检索的问题或关键词")  # 查询
    top_k: int = Field(default=10, ge=1, le=20, description="返回的命中片段条数")  # 条数


class DeepQuery(BaseModel):  # 深度检索参数
    query: str = Field(description="要深度检索的问题或关键词")  # 查询
    top_k: int = Field(default=20, ge=1, le=50, description="深度检索返回条数（更大召回）")  # 条数


@tool_spec(  # 混合检索（语义+关键词 + 重排）
    description="多路混合检索（语义 + 关键词，含重排），返回带 [编号] 的命中片段。",  # 能力
    args_schema=HybridQuery,  # 参数
    ttl=600.0,  # 幂等缓存
)  # 装饰结束
async def search_hybrid(query: str, top_k: int = 10):  # 混合检索
    """唯一底层仍是 nodes/rag；这里只是换召回规模/重排。"""
    result = await _runner().run(query, profile="tool", top_k=int(top_k), rerank=True)  # 同一检索源（工具档）
    return _data(result)  # 统一结构


@tool_spec(  # 深度检索（更大召回 + 重排）
    description="深度检索：更大召回并重排，适合复杂/多跳问题。",  # 能力
    args_schema=DeepQuery,  # 参数
    ttl=600.0,  # 幂等缓存
)  # 装饰结束
async def search_deep(query: str, top_k: int = 20):  # 深度检索
    """唯一底层仍是 nodes/rag；只放大 top_k。"""
    result = await _runner().run(query, profile="deep", top_k=int(top_k), rerank=True)  # 同一检索源（深档）
    return _data(result)  # 统一结构