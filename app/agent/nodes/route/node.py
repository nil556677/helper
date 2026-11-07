"""规则层节点 `rule_route`：纯函数匹配，命中即写 route，未定则交给语义层。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.contracts.output import DecisionTarget, RouteDecision  # 复用单一决策契约
from app.agent.context import bind_node_run, current_run  # 当前 run（指标）
from app.agent.metrics import bump  # 指标累加
from app.agent.routing.rules.loader import match  # 匹配

_rules: Any = None  # 进程级懒加载规则


def _default_rules():  # 懒加载内置规则
    global _rules  # 改模块级
    if _rules is None:  # 未加载
        from app.agent.routing.rules.loader import default_rules  # 内置规则

        _rules = default_rules()  # 加载
    return _rules  # 返回


@bind_node_run
def rule_route_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:  # 图节点
    """命中唯一且 exact → 写 `route`（direct）+ rule_hits；否则 route=None（走语义层）。"""
    del config  # 不用图配置
    question = str(state.get("question") or "")  # 问题
    hit = match(question, _default_rules())  # 匹配（宁漏勿错）
    run = current_run()  # 当前 run（指标打点）
    if run is not None and hit is not None:  # 规则命中才记
        bump(run.metrics, "route_decided_rule")  # 规则来源
        bump(run.metrics, "tools_exposed", len(hit.tools))  # 工具暴露
    if hit is None:  # 未定
        return {"route": None, "rule_hits": []}  # 交语义层
    decision = RouteDecision(  # 唯一决策
        next=DecisionTarget.ANSWER,  # 兼容字段
        intent="direct",  # 直接固定路径
        route=hit.route,  # 路径
        tools=hit.tools,  # 工具
        confidence=1.0,  # 规则命中即最大
        reasons=hit.reasons,  # 依据
        decided_by="rule",  # 来源
    )  # 决策结束
    return {  # 写回
        "route": decision.model_dump(mode="json"),  # 决策 JSON
        "rule_hits": [{"rule_id": hit.rule_id, "route": hit.route, "exact": hit.exact}],  # 命中明细
    }  # 返回