"""`dispatch_fixed` 节点：按 route 白名单直调工具并模板化成回复（不调 LLM）。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any, Callable  # 类型

from app.agent.context import current_run  # 当前 run（发事件）
from app.agent.events import basis as basis_event  # 检索依据事件
from app.agent.events import tool as tool_event  # 工具事件
from app.agent.stages import stage  # 过程步骤
from app.agent.tools.library import current_time, query_weather, search_knowledge_base  # 内置工具
from app.rag.answer import extractive  # 原文回退（无模型时也能给证据原文）

ArgsBuilder = Callable[[str], dict[str, Any]]  # question → 工具参数


def _weather_args(question: str) -> dict[str, Any]:  # 从问题里抠城市
    text = str(question or "")  # 原文
    for word in ("的天气", "天气", "怎么样", "气温", "多少", "会不会下雨", "会下雨吗", "吧", "？", "?"):  # 去噪声
        text = text.replace(word, "")  # 去
    city = text.strip() or "北京"  # 剩余当城市，兜底北京
    return {"city": city}  # 参数


#: route → (工具, 参数生成器)。
_ROUTE_TOOLS: dict[str, tuple[Any, ArgsBuilder]] = {  # 路由表
    "time": (current_time, lambda _q: {}),  # 时间（走默认格式）
    "weather": (query_weather, _weather_args),  # 天气（抠城市）
    "knowledge": (search_knowledge_base, lambda q: {"query": q}),  # 知识库（检索→原文）
}


def _format(name: str, data: dict[str, Any]) -> str:  # 模板化回复
    if name == "time":  # 时间
        value = str(data.get("value") or "")  # 文本
        return f"现在是 {value}。"  # 模板
    if name == "weather":  # 天气
        city = str(data.get("city") or "")  # 城市
        weather = str(data.get("weather") or data.get("type") or "")  # 天气
        temp = data.get("temperature_c")  # 温度
        temp_part = f"，{temp}℃" if temp is not None else ""  # 温度片段
        return f"{city}的天气：{weather}{temp_part}。"  # 模板
    if name == "knowledge":  # 知识库
        model_view = str(data.get("model_view") or "")  # 编号文本
        return model_view or extractive(data.get("evidence") or [])  # 编号文本或原文回退
    return str(data.get("text") or "")  # 兜底


async def dispatch_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:  # 图节点
    """按 `route` 取白名单工具，直调并模板化；越界/失败给带内说明。"""
    del config  # 不用图配置
    route = state.get("route") or {}  # 决策 JSON
    name = route.get("route") if isinstance(route, dict) else None  # 路径
    entry = _ROUTE_TOOLS.get(name)  # 取工具
    if entry is None:  # 未在白名单
        note = f"固定路径 {name!r} 未配置。" if name else "缺少固定路径。"  # 说明
        return {"answer": note, "note": note, "fragments": []}  # 不带内错误
    tool, args_builder = entry  # 拆包
    question = str(state.get("question") or "")  # 问题
    plan = None  # 检索预算（仅知识走）
    args = args_builder(question)  # 基础参数
    run = current_run()  # 当前 run（过程/工具事件）
    if name == "knowledge":  # 知识库：按复杂度动态设定证据数
        from app.agent.routing.retrieval_budget import plan_for  # 预算

        plan = plan_for(question)  # 动态档位
        args["top_k"] = int(plan.final_max)  # 动态 top_k（简单少/复杂多）
    tool_event_sent = False  # 工具事件是否已发
    if run is not None:  # 有 run 才发事件
        run.emit(tool_event(name=str(getattr(tool, "name", "") or name), status="running", detail="开始查询"))  # 工具开始
        tool_event_sent = True  # 标记
    try:  # 直调工具
        if name == "knowledge":  # 检索类走"查找资料"过程步（含计步）
            with stage("查找资料"):  # 不归组：只进 trace 与工具区
                envelope = await tool.ainvoke(args)  # 执行
        else:  # 时间/天气直调
            envelope = await tool.ainvoke(args)  # 执行
    except Exception as exc:  # 工具失败 → 带内 + 升级（让 agentic 再试/降级）
        note = f"{name} 查询失败：{exc}"  # 说明
        if run is not None and tool_event_sent:  # 发失败
            run.emit(tool_event(name=str(getattr(tool, "name", "") or name), status="failed", detail=note))  # 工具失败
        return {"answer": note, "note": note, "fragments": [], "escalate_reason": "search_failed", "escalated_this_session": True}  # 失败也升级
    if run is not None and tool_event_sent:  # 发成功
        run.emit(tool_event(name=str(getattr(tool, "name", "") or name), status="done", detail=f"{name} 完成"))  # 工具完成
    data = getattr(envelope, "data", None) or {}  # 结果数据
    text = _format(name, data)  # 模板化
    fragments = list(data.get("evidence") or []) if name == "knowledge" else []  # 知识库带引用
    basis_payload: dict[str, Any] = {}  # 检索依据（知识路径才有）
    if name == "knowledge" and run is not None:  # 发检索依据（UI"检索依据"区 + 落库）
        raw_basis = data.get("basis") or {}  # 工具返回的计数
        gate = data.get("gate") or {}  # 门限
        sources = [  # 轻量来源
            {"index": idx + 1, "source_name": item.get("source_name", "资料"), "origin": item.get("origin", "资料"), "snippet": str(item.get("text") or "")[:80]}
            for idx, item in enumerate(fragments)
        ]  # 来源结束
        basis_payload = {  # 依据
            "keywords": [str(word) for word in args.get("query", question).split() if word][:8],  # 粗关键词
            "hit_counts": dict(raw_basis) if isinstance(raw_basis, dict) else {},  # 计数
            "sources": sources,  # 来源
            "gate": str(gate.get("decision") or ""),  # 门限
        }  # 结束
        run.emit(basis_event(basis_payload["keywords"], basis_payload["hit_counts"], basis_payload["sources"]))  # 事件
    escalate_reason = ""  # 默认不升级
    if name == "knowledge":  # 知识库走质量信号（阈值随档位）
        from app.agent.routing.quality import assess  # 判定

        min_results = int(plan.min_results) if plan is not None else 3  # 最少命中（随预算）
        escalate, reason = assess(list(data.get("evidence") or []), question, min_results=min_results)  # 评估
        escalate_reason = reason if escalate else ""  # 不够好 → 带原因升级
    update: dict[str, Any] = {"answer": text, "route": route, "fragments": fragments, "escalate_reason": escalate_reason, "escalated_this_session": bool(escalate_reason)}  # 更新
    if basis_payload:  # 有依据
        update["basis"] = basis_payload  # 进 state（落库）
    return update  # 返回（升级则记防乒乓标记）