"""`dispatch_fixed` 节点：按 route 白名单直调工具并模板化成回复（不调 LLM）。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any, Callable  # 类型

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
    try:  # 直调工具
        envelope = await tool.ainvoke(args_builder(str(state.get("question") or "")))  # 执行
    except Exception as exc:  # 工具失败转带内
        note = f"{name} 查询失败：{exc}"  # 说明
        return {"answer": note, "note": note, "fragments": []}  # 带内
    data = getattr(envelope, "data", None) or {}  # 结果数据
    text = _format(name, data)  # 模板化
    fragments = list(data.get("evidence") or []) if name == "knowledge" else []  # 知识库带引用
    return {"answer": text, "route": route, "fragments": fragments}  # 返回