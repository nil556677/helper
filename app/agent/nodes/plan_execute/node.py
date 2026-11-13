"""`plan_execute` 节点：把复杂/多步请求交给 Plan+Execute，结果写回父图。"""

from typing import Any  # 任意值

from langchain_core.runnables import RunnableConfig  # 节点 config 类型（LangGraph）

from app.agent.llm.provider import get_chat_model  # 统一模型入口

__all__ = ["plan_execute_node"]  # 导出


async def plan_execute_node(state: dict[str, Any], config: RunnableConfig | None = None) -> dict[str, Any]:  # 图节点
    """无模型 → 降级；有模型 → 跑 plan+execute。"""
    del config  # 不用图配置
    model = get_chat_model()  # 统一提供者
    if model is None:  # 无模型
        return {  # 降级
            "answer": "已尝试，但未能完成，请换一种方式描述。",  # 兜底话术
            "fallback_used": True,  # 标记
            "fragments": [],  # 无引用
            "note": "未完全匹配到合适路径，已尽力完成。",  # 说明
            "plan_type": "plan_execute",  # 执行模式
        }  # 返回
    from app.agent.fallback.orchestrator import run_plan_execute  # 编排（延迟导入避免启动环）

    return await run_plan_execute(state, model=model)  # 计划+执行
