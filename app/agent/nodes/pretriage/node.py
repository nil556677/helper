"""前置短路分诊（路由之前，**纯规则零 LLM**）。

有些请求根本不需要路由：结构化字段（pending_clarify / flags / 空输入 / 失败标记）
命中就直接给目标，跳过规则/语义/LLM 三层路由。判断依据是结构化字段，不是 query 语义。
"""

from langchain_core.runnables import RunnableConfig  # 节点 config 类型（LangGraph）

from typing import Any  # 任意值

from app.agent.contracts.output import DecisionTarget, RouteDecision  # 决策
from app.agent.contracts.triage import PreTriageResult  # 契约
from app.core.retrieval.tokenize import cut  # 关键词清洗

__all__ = ["pretriage_node"]  # 导出

#: 输入长度上限（超长视为异常输入）。
MAX_QUERY_CHARS = 4000  # 上限

#: 明显注入/越权模式（保守词表，命中即拦截；不追求全，只挡常见套路）。
_INJECTION = (
    "忽略以上",  # 中文
    "忽略之前",  # 中文
    "ignore previous",  # 英文
    "ignore all",  # 英文
    "system prompt",  # 提示词探测
    "打印你的提示",  # 泄提示
    "你现在是",  # 角色越权
    "开发者模式",  # 越权
)  # 注入词表

#: 参数值清洗时丢掉的噪声词。
_FILLER = {"查", "帮我", "请问", "一下", "天气", "气温", "看看", "的", "了", "吗", "呢", "？", "?"}  # 噪声


def _direct(route_name: str, reason: str) -> dict[str, Any]:  # 构造"直接路由"决策
    """短路到某个固定路径时的 RouteDecision。"""
    decision = RouteDecision(  # 直接路由
        next=DecisionTarget.ANSWER,  # 下一步作答
        intent="direct",  # 直接
        route=str(route_name),  # 路径
        confidence=1.0,  # 用户已定
        reason=reason,  # 依据
        decided_by="config",  # 结构化字段
    )  # 结束
    return decision.model_dump(mode="json")  # 序列化


def _result(**fields: Any) -> dict[str, Any]:  # 包 PreTriageResult
    """把字段序列化进 state。"""
    return PreTriageResult(**fields).model_dump(mode="json")  # 序列化


def pretriage_node(state: dict[str, Any], config: RunnableConfig | None = None) -> dict[str, Any]:  # 图节点
    """按结构化字段短路；命中写 `pretriage` 与目标所需字段。"""
    del config  # 不用图配置
    pre = state.get("preprocessed") if isinstance(state.get("preprocessed"), dict) else {}  # 预处理
    query = str(pre.get("query") or state.get("question") or "").strip()  # 问题
    flags = pre.get("flags") if isinstance(pre.get("flags"), dict) else {}  # flags
    pending = pre.get("pending_clarify") if isinstance(pre.get("pending_clarify"), dict) else state.get("pending_clarify")  # 待澄清
    # ① 空输入
    if not query:  # 空/全空白
        return {"pretriage": _result(short_circuit=True, target="finalize", reason="empty_input"), "note": "请描述你遇到的问题或想查询的内容。"}  # 拦截
    # ② 超长输入
    if len(query) > MAX_QUERY_CHARS:  # 太长
        return {"pretriage": _result(short_circuit=True, target="finalize", reason="too_long"), "note": f"输入过长（>{MAX_QUERY_CHARS} 字），请精简后再问。"}  # 拦截
    # ③ 明显注入
    lowered = query.lower()  # 小写
    if any(word in query or word in lowered for word in _INJECTION):  # 命中注入模式
        return {"pretriage": _result(short_circuit=True, target="finalize", reason="injection"), "note": "抱歉，这个请求我无法处理。"}  # 拦截
    # ④ 上一轮失败 + 相似（不短路，只标记；相似用同 query 前缀判定）
    retry = False  # 默认不重试
    if str(state.get("last_turn_status") or "") == "failed":  # 上轮失败
        previous = str(state.get("last_failed_question") or "")  # 上轮问题
        retry = bool(previous) and (previous[:20] == query[:20] or query == previous)  # 相似（前缀）
    # ⑤ pending_clarify 接住（结构化匹配，跳过路由）
    if isinstance(pending, dict) and str(pre.get("phase") or "") == "followup":  # 跟进轮
        expect = str(pending.get("expect") or "")  # 期望类型
        route_name = str(pending.get("route") or "")  # 目标路径
        if expect == "param" and route_name:  # 补参数
            tokens = [str(t) for t in cut(query) if str(t) not in _FILLER and len(str(t)) >= 2]  # 去噪声
            value = (tokens[-1] if tokens else query).strip()  # 取参数值
            return {  # 接住
                "pretriage": _result(short_circuit=True, target="dispatch_fixed", reason="pending_param"),  # 结果
                "route": _direct(route_name, "param_filled"),  # 路由
                "pending_param": value,  # 参数值
                "pending_param_name": str(pending.get("param") or ""),  # 参数名
            }  # 结束
        choices = pending.get("choices") if isinstance(pending.get("choices"), dict) else {}  # 选项映射
        for label, target_route in choices.items():  # 逐候选
            if label and label in query:  # 命中
                return {  # 接住
                    "pretriage": _result(short_circuit=True, target="dispatch_fixed", reason="pending_choice"),  # 结果
                    "route": _direct(str(target_route), "clarify_resolved"),  # 路由
                }  # 结束
    # ⑥ flags 硬约束（显式字段，强制出口）
    mode = str(flags.get("mode") or "")  # 模式
    if flags.get("no_tools"):  # 禁工具
        return {"pretriage": _result(short_circuit=True, target="general_agent", reason="flag_no_tools")}  # 通用兜底
    if mode == "deep_research":  # 深研
        return {"pretriage": _result(short_circuit=True, target="llm_fallback", reason="flag_deep")}  # 强 ReAct
    if mode == "answer_only":  # 只要答案
        return {"pretriage": _result(short_circuit=True, target="dispatch_fixed", reason="flag_answer_only")}  # 快捷
    # 未短路：继续走三层路由
    return {"pretriage": _result(short_circuit=False, target="", reason="", retry_after_failure=retry)}  # 放行