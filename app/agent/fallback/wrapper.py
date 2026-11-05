"""父图 wrapper：把 `ambiguous` 请求交给兜底子图（黑盒），再把结果写回父图 State。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.fallback import run_fallback  # 子图主循环


async def call_fallback_subgraph(  # 父图 wrapper 节点
    state: dict[str, Any],  # 父图 State
    *,
    model: Any | None = None,  # 模型实例（可注入；缺省走降级）
    experts: dict[str, Any] | None = None,  # 已发现专家
    max_replans: int = 2,  # 止损
) -> dict[str, Any]:  # 写回父图的更新
    """三步：父图 State → 子图输入 → run_fallback → 输出转回父图。"""
    from app.agent.fallback.factory import make_planner, make_replanner, make_run_expert  # 工厂

    planner = make_planner(model)  # 规划器
    replanner = make_replanner(model)  # 重规划器
    run_expert = await make_run_expert(model, experts)  # 专家执行器
    recall = state.get("semantic_recall") or {}  # 语义召回
    candidates = recall.get("candidates") if isinstance(recall, dict) else []  # 候选
    sub_state = {  # 子图输入
        "query": str(state.get("question") or ""),  # 问题
        "semantic_candidates": list(candidates),  # 候选
        "thread_id": str(state.get("conversation_id") or "default"),  # 共享 thread_id
    }  # 输入结束
    result = await run_fallback(  # 跑子图
        sub_state,  # 输入
        planner=planner,  # 规划
        replanner=replanner,  # 重规划
        run_expert=run_expert,  # 专家
        max_replans=max_replans,  # 止损
    )  # 结束
    return {  # 转回父图
        "answer": str(result.get("final_response") or ""),  # 最终回答
        "fallback_used": True,  # 只进一次
        "fragments": [],  # 兜底不产引用
        "note": "未完全匹配到合适路径，已尽力完成。" if result.get("unsupported_steps") else "",  # 说明
    }  # 返回