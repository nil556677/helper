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
    result = await _runner().run(query, top_k=int(top_k))  # 跑检索链路（异常冒泡 -> execution.failed）
    return {  # 固定 data 结构
        "model_view": _format(result),  # 给模型的编号文本
        "evidence": list(result.evidence),  # 结构化证据（供回填/审计）
        "gate": dict(result.gate),  # 门限判决
        "basis": {  # 检索依据
            "dense_count": result.dense_count,  # 语义命中数
            "sparse_count": result.sparse_count,  # 关键词命中数
            "fused_count": result.fused_count,  # 融合数
            "selected_count": result.selected_count,  # 证据数
        },  # basis 结束
    }  # 返回