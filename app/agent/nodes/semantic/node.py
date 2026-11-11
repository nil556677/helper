"""语义层节点：候选召回 + 三态判定（不拍板，只给候选与置信）。

运行时：query 只 embed 一次 → 与启动期预计算的 route 矩阵算余弦 → 三态。
"""

from langchain_core.runnables import RunnableConfig  # 节点 config 类型（LangGraph）

from typing import Any, Sequence  # 类型

from app.agent.contracts.output import DecisionTarget, RouteDecision  # 复用单一决策契约
from app.agent.contracts.routing import RouteCandidate, RouteSpec, SemanticRecall  # 契约
from app.agent.context import bind_node_run, current_run  # 当前 run（指标）
from app.agent.metrics import bump  # 指标累加
from app.agent.routing.embeddings import RouteIndex, default_embed, default_route_index  # 索引
from app.agent.routing.policy import resolve_policy  # 灰度分桶
from app.agent.routing.rules.loader import load_routes  # 路径定义

#: 直接路由阈值（绝对分 + 与第二名的差距）。
DIRECT_TOP_THRESHOLD = 0.95  # 绝对分门槛
DIRECT_MARGIN_THRESHOLD = 0.10  # top1 − top2 最小分差

#: 低于它就当"连候选都没有"。
MISS_TOP_THRESHOLD = 0.30  # miss 门槛

#: 召回候选数。
TOP_K = 5  # 取前几

_specs: dict[str, RouteSpec] | None = None  # 进程级路由定义表


def route_specs() -> dict[str, RouteSpec]:  # 懒加载 route 定义
    global _specs  # 改模块级
    if _specs is None:  # 未建
        _specs = {spec.route_id: spec for spec in load_routes()}  # 建表
    return _specs  # 返回


def decide(  # 纯函数：向量 → 三态
    index: RouteIndex,  # 预计算矩阵
    query_vector: Sequence[float],  # query 向量
    *,
    policy: Any = None,  # 策略（提供阈值；缺省用模块常量）
    direct_top: float = DIRECT_TOP_THRESHOLD,  # 门槛
    direct_margin: float = DIRECT_MARGIN_THRESHOLD,  # 分差
    miss_top: float = MISS_TOP_THRESHOLD,  # miss 门槛
    top_k: int = TOP_K,  # 候选数
) -> SemanticRecall:  # 三态结果
    """两条件缺一不可：绝对分高 + 与第二名差距大，才允许直接路由。"""
    if policy is not None:  # 灰度策略覆盖默认阈值
        direct_top = float(policy.direct_top)  # 门槛
        direct_margin = float(policy.direct_margin)  # 分差
        miss_top = float(policy.miss_top)  # miss 门槛
    ranked = index.scores(query_vector)  # 排序
    if not ranked:  # 无路由
        return SemanticRecall(decision="miss")  # 未命中
    candidates = [RouteCandidate(route=route, score=score) for route, score in ranked[:top_k]]  # 候选
    top1 = candidates[0].score  # 最高分
    margin = (candidates[0].score - candidates[1].score) if len(candidates) > 1 else 1.0  # 差距
    if top1 < miss_top:  # 连候选都算不上
        decision = "miss"  # 无关
    elif top1 >= direct_top and margin >= direct_margin:  # 高置信 + 高分差
        decision = "direct"  # 允许直接路由
    else:  # 有候选但分不出胜负
        decision = "ambiguous"  # 交兜底子图裁决
    return SemanticRecall(candidates=candidates, top1=top1, margin=margin, decision=decision)  # 返回


@bind_node_run
async def semantic_node(state: dict[str, Any], config: RunnableConfig | None = None) -> dict[str, Any]:  # 图节点
    """写 `semantic_recall`；若判定 direct，同时写 `route` 供 dispatch_fixed 使用。"""
    del config  # 不用图配置
    from app.agent.contracts import StageGroup  # 分组
    from app.agent.stages import stage  # 过程步

    question = str(state.get("question") or "")  # 问题
    key = str(state.get("conversation_id") or state.get("turn_id") or "default")  # 灰度键
    policy = resolve_policy(key)  # 按会话稳定分桶（可复现）
    with stage("判断意图", group=StageGroup.PREPARE):  # 过程区可见
        try:  # 本地 embedding 可能不可用
            query_vector = default_embed()(question)  # 唯一一次 embed
            recall = decide(default_route_index(), query_vector, policy=policy)  # 三态（用策略阈值）
        except Exception:  # 模型缺失/异常 → 保守 miss
            recall = SemanticRecall(decision="miss")  # 未命中
    payload: dict[str, Any] = {"semantic_recall": recall.model_dump(mode="json")}  # 写召回
    run = current_run()  # 当前 run（指标打点）
    if run is not None:  # 有 run 才记
        bump(run.metrics, f"route_{recall.decision}")  # 三态分布
    if recall.decision == "direct":  # 直接路由
        spec = route_specs().get(recall.candidates[0].route)  # 取路径定义
        if run is not None:  # 指标
            bump(run.metrics, "route_decided_embedding")  # 来源
            bump(run.metrics, "tools_exposed", len(spec.tools) if spec is not None else 0)  # 工具暴露
        payload["route"] = RouteDecision(  # 写统一决策
            next=DecisionTarget.ANSWER,  # 兼容字段
            intent="direct",  # 直接固定路径
            route=recall.candidates[0].route,  # 路径名
            tools=tuple(spec.tools) if spec is not None else (),  # 工具
            confidence=recall.top1,  # 置信
            reason=f"semantic:top={recall.top1:.3f},margin={recall.margin:.3f}",  # 依据
            policy_version=policy.version,  # 灰度版本（可复现/审计）
            decided_by="embedding",  # 来源
        ).model_dump(mode="json")  # 序列化
    return payload  # 返回