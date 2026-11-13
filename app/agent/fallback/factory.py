"""兜底子图工厂：真实模型接线（planner / replanner / run_expert）。

模型不可用（未配置/加载失败）时**优雅降级**：planner 直接标 unsupported，run_expert 空，
让 `run_fallback` 走"无专家 → 降级话术"，不抛异常、不静默。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any

from app.agent.fallback.run import ExpertRunner, Planner, Replanner  # 类型别名
from app.agent.fallback.schema import Plan, PlanStep, ReplanDecision  # 结构化输出

#: 单步执行器提示词（接法 A：一次只完成一小步，不规划后续）。
STEP_EXECUTOR_PROMPT = (  # 提示词
    "你是任务执行器。只完成下面这一小步任务并给出结果；"
    "需要查询就用可用工具（时间/天气/本地资料检索），资料不足则如实说明。"
    "不要规划后续步骤、不要替用户做主、不要编造。"
)  # 提示词结束

#: 单步执行器可调工具名（与单一 ReAct 兜底一致）。
_STEP_TOOLS: tuple[str, ...] = (  # 工具
    "current_time",  # 时间
    "query_weather",  # 天气
    "search_knowledge_base",  # 检索
    "query_order",  # 订单
    "query_logistics",  # 物流
)  # 工具结束

#: Planner 提示词（先分解、再标注，能力边界列清楚）。
PLANNER_PROMPT = (  # 提示词
    "你是任务规划器。先把这个任务分解为原子、可执行的步骤（1-7 步）；"
    "再为每步标注专家：query_agent=只读查询 / action_agent=可逆写 / "
    "sensitive_agent=不可逆操作。若某步没有任何现有专家能做，标注 unsupported 并说明。"
    "跨领域必须拆成多步。"
)  # 提示词结束


def make_planner(model: Any | None) -> Planner:  # 造规划器
    if model is None:  # 没模型
        async def degrade(query, candidates):  # 降级
            del candidates  # 不用
            return Plan(steps=[PlanStep(step=str(query), agent="unsupported")])  # 全标 unsupported

        return degrade  # 返回降级
    structured = model.with_structured_output(Plan)  # 结构化输出

    async def planner(query, candidates):  # 规划（异步，避免在运行中 loop 里同步 invoke 死锁）
        del candidates  # 由 prompt 承载
        return await structured.ainvoke(  # 异步调模型
            [("system", PLANNER_PROMPT), ("human", str(query))]  # 消息
        )

    return planner  # 返回


def make_replanner(model: Any | None) -> Replanner:  # 造重规划器
    if model is None:  # 没模型
        async def degrade(state):  # 降级：空计划即完成
            del state  # 不用
            return ReplanDecision(is_done=True)  # 直接结束

        return degrade  # 返回
    structured = model.with_structured_output(ReplanDecision)  # 结构化输出

    async def replanner(state):  # 重规划（异步）
        past = state.get("past_steps") or []  # 已执行
        prompt = "\n".join(f"- {s}\n  {r}" for s, r in past) if past else "（无已执行步骤）"  # 摘要
        return await structured.ainvoke(  # 异步调模型
            [("system", "你评估结果并决定完成/追加/终止。"), ("human", f"已执行:\n{prompt}")]  # 消息
        )

    return replanner  # 返回


def make_run_expert_single_react(model: Any | None, tools: list[Any] | None = None) -> ExpertRunner:  # 接法 A：单步 ReAct
    """每个计划步用一个小型 ReAct（build_react）只完成该步，返回 (ok/result)。

    工具缺省用 `_STEP_TOOLS`（与单一 ReAct 兜底一致）；图复用同一份（消息按步不同），
    避免每步重建。
    """
    from langchain_core.messages import HumanMessage  # 消息

    from app.agent.react.graph import build_react  # 自定义 ReAct 图

    resolved = list(tools) if tools else _resolve_tools()  # 工具
    graph = (  # 编译图（无模型时为 None → 降级）
        build_react(model, resolved, STEP_EXECUTOR_PROMPT) if model is not None else None  # 图
    )  # 结束

    async def run_expert(agent: str, step: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:  # 执行器
        del agent, state  # 接法 A 不区分专家、不读父状态
        if graph is None or model is None:  # 无模型/无图
            return {"ok": False, "escalate": None, "error": "no model"}  # 不可用
        try:  # 执行该步
            goal = str(step.get("step") or "")  # 步骤目标
            out = await graph.ainvoke({"messages": [HumanMessage(content=goal)]})  # 跑
            pending = list(out.get("approval_pending") or [])  # 需用户确认的请求（如有）
            if pending:  # 该步触发用户确认（告知后执行/记录）
                return {"ok": True, "result": "", "escalate": None, "error": "", "consent_pending": pending}  # 停止等确认
            text = _last_text(out.get("messages") or [])  # 最后回答
            return {"ok": bool(text), "result": text, "escalate": None, "error": ""}  # 成功
        except Exception as exc:  # 失败
            return {"ok": False, "escalate": None, "error": str(exc)[:200]}  # 失败

    return run_expert  # 返回


def _resolve_tools() -> list[Any]:  # 按目录解析默认工具
    from app.agent.tools.catalog import tool_catalog  # 工具目录

    catalog = tool_catalog()  # 名 → 工具
    return [catalog[name] for name in _STEP_TOOLS if name in catalog]  # 解析


def _last_text(messages: list[Any]) -> str:  # 取最后一条纯文本回答
    for msg in reversed(messages):  # 从后往前
        if getattr(msg, "tool_calls", None):  # 还在调工具 → 跳过
            continue  # 下一个
        content = getattr(msg, "content", "")  # 内容
        if isinstance(content, (list, tuple)):  # 多段
            text = " ".join(str(c.get("text") or "") for c in content if isinstance(c, dict))  # 拼
        else:  # 字符串
            text = str(content or "")  # 取
        if text.strip():  # 非空
            return text  # 返回
    return ""  # 无


async def make_run_expert(model: Any | None, experts: dict[str, Any] | None = None) -> ExpertRunner:  # 造专家执行器
    """用 `agents.experts` 已发现专家构建 runner；工具按名从工具目录解析。"""
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
