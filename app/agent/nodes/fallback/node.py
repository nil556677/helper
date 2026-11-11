"""`llm_fallback` 节点：把不确定请求交给兜底单一 ReAct（可调 3 工具），结果写回父图。"""

from langchain_core.runnables import RunnableConfig  # 节点 config 类型（LangGraph）

from typing import Any  # 任意值

from app.agent.fallback.agent import run_react  # 单一 ReAct 兜底
from app.agent.llm.provider import get_chat_model  # 统一模型入口


async def llm_fallback_node(state: dict[str, Any], config: RunnableConfig | None = None) -> dict[str, Any]:  # 图节点
    """无模型 → 模板降级；有模型 → 跑单一 ReAct。"""
    del config  # 不用图配置
    model = get_chat_model()  # 统一提供者
    if model is None:  # 无模型
        return {  # 降级
            "answer": "已尝试，但未能完成，请换一种方式描述。",  # 兜底话术
            "fallback_used": True,  # 标记
            "fragments": [],  # 无引用
            "note": "未完全匹配到合适路径，已尽力完成。",  # 说明
        }  # 返回
    return await run_react(state, model=model)  # 单一 ReAct


__all__ = ["llm_fallback_node"]  # 导出