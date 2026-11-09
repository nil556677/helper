"""`dispatch_fixed` 节点：按 route 白名单直调工具并模板化成回复（不调 LLM）。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any, Callable  # 类型

from app.agent.contracts import StageGroup  # 分组（时间线步骤归类）
from app.agent.context import bind_node_run, current_run  # 当前 run（发事件）
from app.agent.events import basis as basis_event  # 检索依据事件
from app.agent.events import tool as tool_event  # 工具事件
from app.agent.stages import stage  # 过程步骤
from app.agent.tools.library import current_time, query_weather, search_knowledge_base  # 内置工具
from app.agent.tools.wiring import unwrap_tool_output  # 工具出参还原（content+artifact → Result）
from app.agent.nodes.semantic.node import route_specs  # 路径定义（含必需参数）
from app.rag.answer import extractive  # 原文回退（无模型时也能给证据原文）

ArgsBuilder = Callable[[str], dict[str, Any]]  # question → 工具参数

#: 是否启用"上文接入"（近轮并进知识查询，便于连续追问；默认关保持测试兼容，接线时开）。
_DISPATCH_CONTEXT = False  # 开关


def _weather_args(question: str) -> dict[str, Any]:  # 从问题里抠城市/日期
    """``city`` 可能为空（由必需参数检查兜住）；``date`` 相对词→ISO 日期。"""
    import re  # 正则
    from datetime import date, timedelta  # 日期

    text = str(question or "")  # 原文
    city_match = re.search(r"([\u4e00-\u9fa5]{2,6}?)(?:的)?(?:天气|气温|温度|下不下雨|下雨)", text)  # 城市
    city = city_match.group(1).strip() if city_match else ""  # 城市（可能空）
    base = date.today()  # 今天
    offset = 0  # 偏移
    for token, off in (("前天", -2), ("明天", 1), ("后天", 2)):  # 相对词
        if token in text:  # 命中
            offset = off  # 设偏移
            break  # 取第一个
    day = base + timedelta(days=offset)  # 目标日
    return {"city": city, "mode": "forecast", "date": day.isoformat()}  # 显式预报分支 + ISO 日期


def _fill_param(args: dict[str, Any], param_name: str, value: str) -> dict[str, Any]:  # 按参数名填值
    """用户补的参数值写入工具参数(参数名 city/date 与工具字段同名)。"""
    if not value:  # 空
        return args  # 不改
    args[str(param_name)] = value  # 写入
    return args  # 返回


def _args_complete(route_name: str, args: dict[str, Any]) -> bool:  # 参数是否已齐
    """weather 需 city 非空；other 需其它必需参数。"""
    if route_name == "weather":  # 天气
        city = str(args.get("city") or "").strip()  # 城市
        date = str(args.get("date") or "").strip()  # 日期（可空：默认当天）
        return bool(city) and (not date or True)  # 城市必填，日期可空
    return True  # 其余齐


def _extract_param(pattern: str, question: str) -> str:  # 按正则抽参数
    import re  # 正则

    match = re.search(pattern, str(question or ""))  # 搜索
    return str(match.group(1)).strip() if match else ""  # 命中取第 1 组


def _missing_param(spec: Any, question: str) -> tuple[str, str, tuple[str, ...]]:  # 检查必需参数
    """返回 (参数名, 追问话术, 快捷选项)；都抽到返回 ("", "", ())。"""
    import re  # 正则

    for item in (spec.required_params or ()):  # 逐必需参数
        name = str(item.get("name") or "")  # 名
        pattern = str(item.get("extract_regex") or "")  # 抽取式
        question_text = str(item.get("question") or f"请提供{name}？")  # 追问
        options = tuple(str(x) for x in (item.get("options") or ()))  # 快捷选项
        if pattern:  # 有抽取式
            try:  # 坏正则容错
                if re.search(pattern, question):  # 抽到
                    continue  # 下一个
            except re.error:  # 正则坏
                pass  # 当缺失
        return name, question_text, options  # 缺
    return "", "", ()  # 全齐


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


@bind_node_run
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
    run = current_run()  # 当前 run（过程/工具事件）
    # —— 必需参数检查：缺 → HITL 中断追问 或 模板追问（绝不静默兜底）——
    spec = route_specs().get(name)  # 路径定义
    pending_param = str(state.get("pending_param") or "")  # 上轮接住/恢复的参数值
    pending_name = str(state.get("pending_param_name") or "")  # 该值对应哪个必需参数
    args = (_fill_param({**args_builder(question)}, pending_name, pending_param) if pending_name else args_builder(question))  # 基础参数
    if spec is not None and spec.required_params and not pending_param:  # 声明了必需参数且本轮没带
        param_name, ask_text, ask_options = _missing_param(spec, question)  # 检查
        if param_name and not _args_complete(name, args):  # 仍缺（args 里没有兜底值才算缺）
            # 参数交互按能否枚举决定：可枚举(options 非空)→让选 choose；开放(空)→让填 input
            enum = bool(ask_options)  # 能否枚举完
            kind = "choose" if enum else "input"  # 选 or 填
            payload_options = [{"value": item, "label": item} for item in ask_options] if enum else []  # 枚举才有选项
            if state.get("hitl_enabled"):  # 生产：暂停等用户（重放后 resume 值进 pending_param）
                from langgraph.types import interrupt  # 原生暂停
                from app.agent.contracts.routing import InterruptPayload  # 负载

                payload = InterruptPayload(  # 追问负载
                    run_id=str(state.get("run_id") or ""),  # run
                    thread_id=str(state.get("thread_id") or ""),  # 线程
                    node="dispatch_fixed",  # 节点
                    kind=kind,  # choose=可枚举选 / input=开放填
                    title="需要补充信息",  # 标题
                    description=ask_text,  # 追问话术
                    options=payload_options,  # 可枚举才带选项
                    risk="low",  # 低风险
                ).model_dump(mode="json")  # 序列化
                answer_value = str(interrupt(payload) or "")  # 恢复值 = 用户选择/输入
                args = _fill_param({**args_builder(question)}, param_name, answer_value)  # 用恢复值精确填入
            else:  # 测试/未启用：模板追问 + pending_clarify（下轮打字接住）
                return {  # 追问
                    "answer": ask_text,  # 话术
                    "note": ask_text,  # 说明
                    "fragments": [],  # 无引用
                    "pending_clarify": {"expect": "param", "param": param_name, "route": name, "choices": {}},  # 下轮接住（expect=param 模式）
                }  # 结束
    if name == "knowledge":  # 知识库：按复杂度动态设定证据数
        from app.agent.routing.retrieval_budget import plan_for  # 预算

        plan = plan_for(question)  # 动态档位
        args["top_k"] = int(plan.final_max)  # 动态 top_k（简单少/复杂多）
        if _DISPATCH_CONTEXT:  # 启用上文接入：把上一轮用户问题并进检索，便于连续追问
            turns = list(state.get("turns") or [])  # 近轮
            if turns:  # 有历史
                prev = str((turns[-1] if isinstance(turns[-1], dict) else {}).get("user") or "")  # 上一轮问
                if prev:  # 非空
                    args["query"] = f"{prev}\n{args.get('query') or question}"  # 上文 + 当前问
    tool_event_sent = False  # 工具事件是否已发
    if run is not None:  # 有 run 才发事件
        run.emit(tool_event(name=str(getattr(tool, "name", "") or name), status="running", detail="开始查询"))  # 工具开始
        tool_event_sent = True  # 标记
    try:  # 直调工具
        if name == "knowledge":  # 检索类走"查找资料"过程步（含计步）
            with stage("查找资料", name="search", group=StageGroup.SEARCH) as info:  # 过程可点开看详情
                envelope = unwrap_tool_output(await tool.ainvoke(args))  # 执行（还原完整包络）
                _data = getattr(envelope, "data", None) or {}  # 结果
                _frags = list(_data.get("evidence") or [])  # 命中片段
                info.detail = f"找到 {len(_frags)} 段资料"  # 一行摘要
                _gate = _data.get("gate") or {}  # 门限判定
                _lines: list[str] = []  # 正文行
                _lines.append(f"门限判定：{_gate.get('decision') or '未判定'}")  # gate
                for _i, _f in enumerate(_frags[:5], 1):  # 前 5 条
                    _snip = str(_f.get("text") or "").strip().replace("\n", " ")[:120]  # 摘要
                    _lines.append(f"{_i}. {_f.get('source_name', '资料')}：{_snip}")  # 一条
                info.body = "\n".join(_lines)  # 点开看到资料+判断
        else:  # 时间/天气直调
            with stage("调用工具", name="run", group=StageGroup.TOOL):  # 工具过程步（name 区分同函数两支）
                envelope = unwrap_tool_output(await tool.ainvoke(args))  # 执行（还原完整包络）
    except Exception as exc:  # 工具失败 → 带内 + 升级（让 agentic 再试/降级）
        note = f"{name} 查询失败：{exc}"  # 说明
        if run is not None and tool_event_sent:  # 发失败
            run.emit(tool_event(name=str(getattr(tool, "name", "") or name), status="failed", detail=note))  # 工具失败
        return {"answer": note, "note": note, "fragments": [], "escalate_reason": "search_failed", "escalated_this_session": True}  # 失败也升级
    if run is not None and tool_event_sent:  # 发成功
        run.emit(tool_event(name=str(getattr(tool, "name", "") or name), status="done", detail=f"{name} 完成"))  # 工具完成
    data = getattr(envelope, "data", None) or {}  # 结果数据
    text = _format(name, data)  # 模板化
    from app.rag.evidence import CITATION_MAX  # 引用上限（单一来源）

    fragments = list(data.get("evidence") or [])[:CITATION_MAX] if name == "knowledge" else []  # 知识库带引用（限条数）
    for position, item in enumerate(fragments, start=1):  # 生成层统一编号
        item["index"] = position  # 单次调用即 1..N
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
            "profile": str(((data.get("basis") or {}) if isinstance(data, dict) else {}).get("profile") or "tool"),  # 检索档位（观测）
            "paths": list(((data.get("basis") or {}) if isinstance(data, dict) else {}).get("paths") or ["baseline"]),  # 实际路径（观测）
            "elapsed_ms": int(((data.get("basis") or {}) if isinstance(data, dict) else {}).get("elapsed_ms") or 0),  # 检索耗时（观测）
            "llm_calls": int(((data.get("basis") or {}) if isinstance(data, dict) else {}).get("llm_calls") or 0),  # LLM 次数（观测）
        }  # 结束
        run.emit(basis_event(basis_payload["keywords"], basis_payload["hit_counts"], basis_payload["sources"]))  # 事件
    escalate_reason = ""  # 默认不升级
    if name == "knowledge":  # 知识库走质量信号（阈值随档位）
        from app.agent.routing.quality import assess  # 判定

        min_results = int(plan.min_results) if plan is not None else 3  # 最少命中（随预算）
        escalate, reason = assess(list(data.get("evidence") or []), question, min_results=min_results)  # 评估
        escalate_reason = reason if escalate else ""  # 不够好 → 带原因升级
    update: dict[str, Any] = {"answer": text, "route": route, "fragments": fragments, "escalate_reason": escalate_reason, "escalated_this_session": bool(escalate_reason)}  # 更新
    # —— 依据资料作答门禁：证据弱/不足时如实提示（不假装有据）——
    if name == "knowledge":  # 只对资料检索
        gate_decision = str(((data.get("gate") or {}) if isinstance(data, dict) else {}).get("decision") or "")  # 门限
        if gate_decision == "weak" and fragments:  # 证据弱
            update["note"] = "证据较弱：以上回答请对照引用原文核对。"  # 提示
        elif gate_decision == "reject" and not fragments:  # 无可用证据
            update["note"] = "没有在本地资料里找到能支撑回答的内容，换个说法试试。"  # 诚实说明
    if basis_payload:  # 有依据
        update["basis"] = basis_payload  # 进 state（落库）
    return update  # 返回（升级则记防乒乓标记）