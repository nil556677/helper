"""rag 节点：把 RAG 子图接到父图。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

from langchain_core.runnables import RunnableConfig  # 图配置

from app.agent.context import ctx_from_config  # 上下文
from app.agent.contexts.history import messages_from  # 轮次 -> 消息
from app.agent.contracts import (  # 契约（决策/分组/轮次）
    DecisionTarget,
    RouteDecision,
    StageGroup,
    Turn,
)
from app.agent.events import basis as basis_event  # 检索依据
from app.agent.nodes.rag.node import RagRunner  # 子图运行器
from app.agent.stages import stage  # 步骤事件
from app.core.retrieval.tokenize import cut  # 关键词
from app.rag import rewrite as rewrite_mod  # 查询改写（LLM + 规则归一）
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
    """给“检索依据”用的轻量来源。"""
    result: list[dict] = []  # 结果
    for item in evidence:  # 逐条
        result.append(  # 追加
            {  # 来源
                "index": item.get("index"),  # 序号
                "source_name": item.get("source_name", "资料"),  # 来源
                "origin": item.get("origin", "资料"),  # 位置
                "snippet": item.get("snippet", ""),  # 摘要
            }  # 来源结束
        )  # 追加结束
    return result  # 返回


def _better(first: Any, second: Any) -> Any:  # 挑更好的结果
    """先比判决（pass>weak>reject），同级比 top1。"""
    def key(result: Any) -> tuple[int, float]:  # 排序键
        gate = dict(getattr(result, "gate", {}) or {})  # 判决
        return (  # 元组
            rewrite_mod.better_rank(str(gate.get("decision") or "")),  # 判决等级
            float(gate.get("top1") or -99.0),  # 最高分
        )  # 结束
    return second if key(second) > key(first) else first  # 取大的


def _reject_note(gate: dict[str, Any]) -> str:  # 拒答文案
    """按门限原因给一句"能照着改"的话，别只说没找到。"""
    reasons = set(gate.get("reasons") or [])  # 原因集合
    missing = [str(value) for value in (gate.get("hard_missing") or []) if value]  # 缺的硬要素
    if "hard_missing" in reasons and missing:  # 编号/日期没命中
        return f"资料里没有找到「{'、'.join(missing)}」相关的内容，确认编号或日期是否写对。"  # 提示改问法
    if "qa_low" in reasons:  # 答非所问
        return "找到的资料只是在讲同一话题，没有正面回答这个问题，换个更具体的问法试试。"  # 提示
    if "too_few" in reasons:  # 证据太少
        return "只找到一处相关片段，不足以支撑回答；补充一点上下文再问一次。"  # 提示
    return "没有在本地资料里找到相关内容，换个说法试试。"  # 兜底


def make_rag_node(runner: RagRunner):  # 工厂
    """返回绑定 runner 的节点函数。"""

    async def rag_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:  # 检索节点
        """跑 RAG 子图，写 evidence/hits，并发检索依据。"""
        ctx = ctx_from_config(config)  # 上下文
        question = str(state.get("question") or "")  # 问题
        history = messages_from([Turn.model_validate(item) for item in (state.get("turns") or [])])  # 历史（只喂 done）
        with stage("查找资料", group=StageGroup.SEARCH):  # 父图步骤
            with stage("改写问题", group=StageGroup.PREPARE, name="rewrite"):  # 改写单独一段（慢的话能一眼看出来）
                planned = await rewrite_mod.rewrite(question, history=history)  # 异步走统一网关
            result = await runner.run(question, top_k=6, rerank=True)  # 首轮检索
            for extra in planned.queries[1:]:  # 懒改写：只有首轮证据不足才补查
                if rewrite_mod.better_rank(str((result.gate or {}).get("decision") or "")) >= 2:  # 已经 PASS
                    break  # 不再花时间
                other = await runner.run(extra, top_k=6, rerank=True)  # 补一轮
                result = _better(result, other)  # 取更好的一路
        gate = dict(result.gate or {})  # 门限判决（新老路径都要算）
        decision = str(gate.get("decision") or "")  # pass / weak / reject
        has_files = any(  # 附件可读（按 disk 现读）
            attachments.load_text(item).strip() for item in (state.get("attachments") or [])  # 逐个判可读
        )  # 附件判断结束
        # 可答只看门限判决：无证据/低分（reject）才拒；证据少已由 gate 降级为 weak，不在此拦。
        answerable = has_files or (decision != "reject" and bool(result.evidence))  # 可答判断（FSM 与边路由共用）
        route = RouteDecision(  # 唯一决策对象：迁移与边路由都读它
            next=DecisionTarget.ANSWER if answerable else DecisionTarget.FINALIZE,  # 下一步
            confidence=None,  # 规则判断不表信心
            needs_clarification=False,  # 本轮不需要追问
            reason=decision or "no_evidence",  # 人话原因（进 trace）
            version=1,  # 输出契约版本
        )  # 决策结束
        note = ""  # 说明（只进 state）
        basis: dict[str, Any] = {}  # 检索依据（只进 state）
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
                "gate": decision,  # 门限结论（界面/轨迹可见）
                "rewrite": planned.reason,  # 改写结论（used/skipped/unconfigured/…）
                "queries": list(planned.queries),  # 实际查过的查询
            }  # 依据结束
            ctx.emit(basis_event(basis["keywords"], basis["hit_counts"], basis["sources"]))  # 事件
            if answerable:  # 有可用证据或可读附件
                if decision == "weak" and not has_files:  # 证据偏弱/偏少
                    note = "证据较弱：以下回答请对照引用原文核对。"  # 提示
            else:  # 不可答
                note = _reject_note(gate) if decision == "reject" else "没有在本地资料里找到相关内容，换个说法试试。"  # 说明
        return {  # 写回
            "evidence": result.evidence if answerable else [],  # 不可答时清空（只留决策）
            "hits": result.hits,  # 命中
            "note": note,  # 说明（只进 state）
            "basis": basis,  # 检索依据（只进 state）
            "gate": gate,  # 门限判决（只进 state）
            "route": route.model_dump(mode="json"),  # 唯一决策对象（JSON 形态，可直接进 checkpoint）
        }  # 返回结束

    return rag_node  # 返回节点
