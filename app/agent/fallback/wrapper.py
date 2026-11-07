"""父图 wrapper：把不确定请求交给兜底单一 ReAct（可调 3 工具），再把结果写回父图 State。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.fallback import run_react  # 单一 ReAct 兜底
from app.agent.llm.provider import get_chat_model  # 统一模型入口


async def call_fallback_subgraph(  # 父图 wrapper 节点
    state: dict[str, Any],  # 父图 State
    *,
    model: Any | None = None,  # 模型实例（可注入；缺省取统一提供者）
    **_: Any,  # 兼容旧签名（experts/max_replans 不再需要）
) -> dict[str, Any]:  # 写回父图的更新
    """单一 ReAct：父图 State → run_react → 输出转回父图。"""
    model = model if model is not None else get_chat_model()  # 缺省取统一入口
    if model is None:  # 无模型
        return {  # 降级
            "answer": "已尝试，但未能完成，请换一种方式描述。",  # 兜底话术
            "fallback_used": True,  # 标记
            "fragments": [],  # 无引用
            "note": "未完全匹配到合适路径，已尽力完成。",  # 说明
        }  # 返回
    return await run_react(state, model=model)  # 单一 ReAct