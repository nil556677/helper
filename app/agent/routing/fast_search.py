"""L4a 快捷搜索：一次检索 → 质量信号 →（不够则升级 agentic）。

检索统一走 `nodes/rag`（唯一底层源）；本模块只做编排与判定，不重写检索。
"""

from __future__ import annotations  # 延迟求值注解

from dataclasses import dataclass  # 结果
from typing import Any  # 任意值

from app.agent.routing.quality import assess  # 质量

__all__ = ["FastSearchResult", "fast_search"]  # 导出


@dataclass(frozen=True)
class FastSearchResult:  # 快捷检索结果
    evidence: list[dict[str, Any]]  # 证据
    escalate: bool  # 是否升级
    escalate_reason: str  # 升级原因


async def fast_search(  # 单次快捷检索
    runner: Any,  # 检索运行器（RagRunner，唯一底层）
    query: str,  # 问题
    *,
    top_k: int = 10,  # 条数
    rerank: bool = True,  # 精排
) -> FastSearchResult:  # 结果
    """跑一次检索并判定质量；不做多轮、不调模型。"""
    result = await runner.run(query, top_k=int(top_k), rerank=bool(rerank))  # 唯一检索源
    evidence = list(getattr(result, "evidence", []) or [])  # 证据
    escalate, reason = assess(evidence, query)  # 判定
    return FastSearchResult(evidence=evidence, escalate=escalate, escalate_reason=reason)  # 返回