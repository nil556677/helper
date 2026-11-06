"""L2 LLM 兜底：仅 ambiguous 时做轻量意图分类（不规划）。

模型入口单一：由调用方注入一个 chat_model（来自 `llm/gateway` 产出的实例）。
模型缺失/失败 → 降级为 `intent=clarify`（符合 spec：LLM 兜底失败→澄清）。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any, Sequence  # 类型

from app.agent.contracts.triage import IntentDecision  # 意图契约

__all__ = ["classify", "degrade"]  # 导出

#: 分类提示：只分类、不给执行计划。
PROMPT = (  # 提示
    "你是意图分类器。只输出分类结果，不要给执行计划。"
    "intent: query=资料检索/问答；action=执行操作；clarify=信息不足需澄清。"
    "domain: 业务域（knowledge/order/payment/...），未知留空。"
    "confidence: 0..1。"
)  # 提示结束


def degrade(reason: str = "no_model") -> IntentDecision:  # 降级决策
    """模型不可用/失败时的保守出口：让下游走澄清。"""
    return IntentDecision(intent="clarify", confidence=0.0, source="llm", reason=reason)  # 澄清


async def classify(  # 轻量分类
    question: str,  # 问题
    candidates: Sequence[dict[str, Any]],  # 语义候选（帮助模型分域）
    model: Any | None = None,  # chat_model（gateway 产出）
) -> IntentDecision:  # 意图决策
    """有模型才分类；无模型/异常一律降级 clarify。"""
    if model is None:  # 无模型
        return degrade("no_model")  # 降级
    try:  # 调模型
        runner = model.with_structured_output(IntentDecision)  # 结构化
        hints = "、".join(str(item.get("route") or "") for item in candidates if isinstance(item, dict))  # 候选
        out = await runner.ainvoke(  # 请求
            [("system", PROMPT), ("human", f"候选:{hints}\n问题:{question}")]  # 消息
        )  # 结束
        if isinstance(out, IntentDecision):  # 直接对象
            return out  # 返回
        return IntentDecision.model_validate(out)  # 兼容 dict
    except Exception:  # 任何失败
        return degrade("llm_error")  # 降级