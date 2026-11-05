"""兜底子图工厂：真实模型接线（planner / replanner / run_expert）。

模型不可用（未配置/加载失败）时**优雅降级**：planner 直接标 unsupported，run_expert 空，
让 `run_fallback` 走"无专家 → 降级话术"，不抛异常、不静默。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any, Awaitable, Callable  # 类型

from app.agent.fallback.run import ExpertRunner, Planner, Replanner  # 类型别名
from app.agent.fallback.schema import Plan, PlanStep, ReplanDecision  # 结构化输出

#: Planner 提示词（先分解、再标注，能力边界列清楚）。
PLANNER_PROMPT = (  # 提示词
    "你是任务规划器。先把这个任务分解为原子、可执行的步骤（1-7 步）；"
    "再为每步标注专家：query_agent=只读查询 / action_agent=可逆写 / "
    "sensitive_agent=不可逆操作。若某步没有任何现有专家能做，标注 unsupported 并说明。"
    "跨领域必须拆成多步。"
)  # 提示词结束


def make_planner(model: Any | None) -> Planner:  # 造规划器
    if model is None:  # 没模型
        def degrade(query, candidates):  # 降级
            del candidates  # 不用
            return Plan(steps=[PlanStep(step=str(query), agent="unsupported")])  # 全标 unsupported

        return degrade  # 返回降级
    structured = model.with_structured_output(Plan)  # 结构化输出

    def planner(query, candidates):  # 规划
        del candidates  # 由 prompt 承载
        return structured.invoke(  # 调模型
            [("system", PLANNER_PROMPT), ("human", str(query))]  # 消息
        )

    return planner  # 返回


def make_replanner(model: Any | None) -> Replanner:  # 造重规划器
    if model is None:  # 没模型
        def degrade(state):  # 降级：空计划即完成
            del state  # 不用
            return ReplanDecision(is_done=True)  # 直接结束

        return degrade  # 返回
    structured = model.with_structured_output(ReplanDecision)  # 结构化输出

    def replanner(state):  # 重规划
        past = state.get("past_steps") or []  # 已执行
        prompt = "\n".join(f"- {s}\n  {r}" for s, r in past) if past else "（无已执行步骤）"  # 摘要
        return structured.invoke(  # 调模型
            [("system", "你评估结果并决定完成/追加/终止。"), ("human", f"已执行:\n{prompt}")]  # 消息
        )

    return replanner  # 返回


async def make_run_expert(model: Any | None, experts: dict[str, Any] | None = None) -> ExpertRunner:  # 造专家执行器
    """用 `agents.experts` 已发现专家构建 runner；工具按名从工具目录解析。"""
    from app.agent.agents.base import build_expert_agent  # 构建（保留引用）
    from app.agent.tools.catalog import tool_catalog  # 工具目录

    available: dict[str, Any] = {}  # 可用专家（agent 名 → (描述符, build, 工具对象)）
    if model is not None and experts:  # 有模型才构建真实专家
        catalog = tool_catalog()  # 名 → 工具
        for name, (descriptor, build) in experts.items():  # 逐专家
            resolved = [catalog[item] for item in descriptor.tools if item in catalog]  # 解析工具
            available[name] = (descriptor, build, resolved)  # 记录

    async def run_expert(agent, step, state):  # 执行器
        del state  # 不用
        if model is None or agent not in available:  # 模型缺失/专家不存在
            return {"ok": False, "escalate": None, "error": f"expert {agent} unavailable"}  # 不可用
        descriptor, build, tools = available[agent]  # 拆包
        del descriptor  # 未直接用
        graph = build(model, tools)  # 构建专家图
        try:  # 执行该步
            out = await graph.ainvoke({"messages": [("human", str(step.get("step") or ""))]})  # 执行
            msgs = out.get("messages") or []  # 消息
            text = str(getattr(msgs[-1], "content", "")) if msgs else ""  # 回答
            return {"ok": True, "result": text, "escalate": None, "error": ""}  # 成功
        except Exception as exc:  # 失败
            return {"ok": False, "escalate": None, "error": str(exc)[:200]}  # 失败

    return run_expert  # 返回