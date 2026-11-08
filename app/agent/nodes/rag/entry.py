"""rag 节点：把 RAG 子图接到父图（薄入口，检索编排全在内核 RagRunner）。

不再自带"改写 + 串行补查"编排——那是旧实现，会绕开内核。这里只按档位调一次内核。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from langchain_core.runnables import RunnableConfig  # 图配置

from app.agent.context import ctx_from_config  # 上下文
from app.agent.contracts import (  # 契约（决策/分组）
    DecisionTarget,
    RouteDecision,
    StageGroup,
)
from app.agent.events import basis as basis_event  # 检索依据
from app.agent.nodes.rag.node import RagRunner  # 内核运行器
from app.agent.stages import stage  # 步骤事件
from app.core.retrieval.tokenize import cut  # 关键词
from app.services import attachments  # 附件读取


def _keywords(question: str) -> list[str]:  # 提取检索词
    """去重、丢掉单字，最多 8 个。"""
    result: list[str] = []  # 结果
    for token in cut(question):  # 分词
        text = str(token).strip()  # 去空白
        if len(text) >= 2 and text not in result:  # 有效且不重复
            result.append(text)  # 追加
        if len(result) >= 8:  # 够了
            break  # 停止
    return result  # 返回


def _sources(evidence: list[dict]) -> list[dict]:  # 来源列表
    """给"检索依据"用的轻量来源。"""
    return [  # 来源
        {
            "index": item.get("index"),  # 序号
            "source_name": item.get("source_name", "资料"),  # 来源
            "origin": item.get("origin", "资料"),  # 位置
            "snippet": item.get("snippet", ""),  # 摘要
        }
        for item in evidence
    ]  # 返回


def make_rag_node(runner: RagRunner):  # 工厂
    """返回绑定 runner 的节点函数（内核档位由这里统一选 full）。"""

    async def rag_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:  # 检索节点
        """调内核 full 档 → 写 evidence/hits/gate，并发检索依据。"""
        ctx = ctx_from_config(config)  # 上下文
        question = str(state.get("question") or "")  # 问题
        with stage("查找资料", group=StageGroup.SEARCH):  # 父图步骤
            result = await runner.run(question, profile="full", top_k=6, rerank=True)  # 唯一内核（full 档）
        gate = dict(result.gate or {})  # 门限判决
        decision = str(gate.get("decision") or "")  # pass/weak/reject
        has_files = any(  # 附件可读（按 disk 现读）
            attachments.load_text(item).strip() for item in (state.get("attachments") or [])  # 逐个判可读
        )  # 附件判断结束
        answerable = has_files or (decision != "reject" and bool(result.evidence))  # 可答判断
        route = RouteDecision(  # 唯一决策对象
            next=DecisionTarget.ANSWER if answerable else DecisionTarget.FINALIZE,  # 下一步
            confidence=None,  # 规则判断不表信心
            needs_clarification=False,  # 本轮不需要追问
            reason=decision or "no_evidence",  # 人话原因
            version=1,  # 输出契约版本
        )  # 决策结束
        basis: dict[str, Any] = {}  # 检索依据
        note = ""  # 说明
        if ctx is not None:  # 新链路
            basis = {  # 检索依据
                "keywords": _keywords(question),  # 关键词
                "hit_counts": {  # 计数
                    "semantic": result.dense_count,  # 语义
                    "keyword": result.sparse_count,  # 关键词
                    "merged": result.fused_count,  # 合并
                    "selected": result.selected_count,  # 证据
                },  # 计数结束
                "sources": _sources(result.evidence),  # 来源
                "gate": decision,  # 门限结论
                "profile": "full",  # 档位（观测）
                "queries": [question],  # 实际查询（单次，内核内可再扩展）
            }  # 依据结束
            ctx.emit(basis_event(basis["keywords"], basis["hit_counts"], basis["sources"]))  # 事件
            if not answerable:  # 不可答
                note = "没有在本地资料里找到相关内容，换个说法试试。"  # 说明
            elif decision == "weak" and not has_files:  # 证据偏弱
                note = "证据较弱：以下回答请对照引用原文核对。"  # 提示
        return {  # 写回
            "evidence": result.evidence if answerable else [],  # 不可答清空
            "hits": result.hits,  # 命中
            "note": note,  # 说明
            "basis": basis,  # 检索依据
            "gate": gate,  # 门限判决
            "route": route.model_dump(mode="json"),  # 唯一决策对象
        }  # 返回结束

    return rag_node  # 返回节点