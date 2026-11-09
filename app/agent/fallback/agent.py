"""兜底单一 ReAct agent：可调 天气/时间/本地资料检索 3 个工具。

替代原「计划→执行」子图：不再 planner/replanner 多步规划，直接用 `create_agent`（React）
带完整记忆 + 候选线索 决策。带去人所有不确定（代指/含糊/未命中/多轮）。

护栏（防追问循环）：
- `intent_confirmed`：意图已确认 → 不再进"信息不足→追问"，直接执行；
- `clarify_count`：最多追问 1 次；超过 → 强制按已有信息作答或诚实说明；
- 全程消息列表保留，工具结果如实回填，失败如实降级（不编造）。
"""

from __future__ import annotations  # 延迟求值注解

from functools import lru_cache  # 进程级缓存（graph 构建只做一次）
from typing import Any  # 任意值

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # 消息类型

from app.agent.agent_builder import build_react_agent  # 官方 ReAct 构造
from app.agent.context import current_run  # 当前 run（发 TOOL 事件）
from app.agent.events import tool as tool_event  # 工具事件
from app.agent.tools.catalog import tool_catalog  # 工具目录（单一来源）

#: 兜底 agent 可调的工具名（天气/时间/本地资料检索）。
TOOL_NAMES: tuple[str, ...] = ("current_time", "query_weather", "search_knowledge_base")  # 3 工具

#: 工具名 → UI variant（时间线图标归类）。
_VARIANT: dict[str, str] = {"current_time": "time", "query_weather": "weather", "search_knowledge_base": "search"}  # 变体

#: 说话者标签（多轮记忆格式化用）。
_MAX_TURNS = 6  # 最多带最近 6 轮记忆

#: 是否启用 ContextBuilder 装配（生产接线开关；默认关保持测试兼容，开则走 builder system+messages）。
_CONTEXT_BUILDER = False  # 开关


def _state_context(state: dict[str, Any]) -> Any | None:  # 用 ContextBuilder 装配上下文
    """从 state 取工作记忆/近轮/段/事实/问题 → build_context；失败返回 None（退回手拼）。"""
    try:  # 任何失败退回旧路径
        from app.agent.contexts.builder import build_context, set_embed  # builder
        from app.agent.memory import MemoryStore, embed_text  # 长期库/嵌入

        set_embed(embed_text)  # 注入 bge-m3 嵌入（不可用则退保底）
        conversation_id = str(state.get("conversation_id") or "")  # 会话
        segments: list[dict[str, Any]] = []  # 段
        facts: list[dict[str, Any]] = []  # 事实
        try:  # 段取数失败不影响装配
            if conversation_id:  # 有会话
                from app.services import conversations  # 会话服务

                segments = conversations.load_segments(conversation_id, limit=2)  # 最近段
        except Exception:  # 无表/坏库
            segments = []  # 空
        try:  # 事实取数失败不影响装配
            facts = MemoryStore().search(top_k=6)  # 长期事实
        except Exception:  # 无表/坏库
            facts = []  # 空
        return build_context(  # 装配（system 用现有工具规则）
            system=_system_prompt(state),  # S 规则
            working=state.get("working_memory") or {},  # 锚点
            facts=facts,  # 事实
            segments=segments,  # 段
            recent=list(state.get("turns") or []),  # 近轮
            question=str(state.get("question") or ""),  # 问题
        )  # 返回
    except Exception:  # 失败
        return None  # 退回旧路径


def _build_graph(model: Any, prompt: str) -> Any:  # 构造 ReAct 图
    """模型实例 + 系统提示 → 编译好的 ReAct 图。模型可能不可哈希，不缓存。"""
    catalog = tool_catalog()  # 工具目录
    tools = [catalog[name] for name in TOOL_NAMES if name in catalog]  # 取 3 工具
    return build_react_agent(  # 官方构造（含取消/PII/限额中间件）
        model,  # 模型
        tools,  # 工具
        prompt,  # 系统提示
        run_limit=6,  # 步数上限（防死循环）
        name="fallback_react",  # 名称
    )  # 返回图


def _format_memory(state: dict[str, Any]) -> str:  # 把对话历史格式成文本
    """只带最近几轮的用户/助手问答（简要）——agent 的"完整记忆"。

    注意：state['turns'] 是 `Turn` 的 JSON，字段是 `user/assistant`，不是 question/answer。
    """
    turns = list(state.get("turns") or [])[-_MAX_TURNS:]  # 最近几轮
    lines: list[str] = []  # 行
    for turn in turns:  # 逐轮
        if not isinstance(turn, dict):  # 坏数据
            continue  # 跳过
        q = str(turn.get("user") or "")  # 用户问题（Turn.user）
        a = str(turn.get("assistant") or "")  # 助手回答（Turn.assistant）
        if q:  # 有问题
            lines.append(f"用户：{q}")  # 用户行
        if a:  # 有答
            lines.append(f"助手：{a[:120]}")  # 助手行（截断）
    return "\n".join(lines) if lines else "（无历史对话）"  # 返回


def _candidate_hint(state: dict[str, Any]) -> str:  # 语义候选线索
    """语义层找出的候选方向（route+score），给 agent 参考，不盲从。"""
    recall = state.get("semantic_recall") or {}  # 语义召回
    candidates = recall.get("candidates") if isinstance(recall, dict) else []  # 候选
    if not candidates:  # 无候选
        return "（语义层未匹配到明确功能）"  # miss 标记
    parts = [f"{c.get('route')}({c.get('score', 0):.2f})" for c in candidates if isinstance(c, dict)]  # 候选串
    return "、".join(parts) or "（无候选）"  # 返回候选串


def _system_prompt(state: dict[str, Any]) -> str:  # 系统提示（含预算/防循环）
    """可控的兜底 agent 规则。"""
    return (  # 提示
        "你是通用助手。可用三个工具："
        "current_time（查当前时间）、query_weather（查某城市天气）、"
        "search_knowledge_base（本地资料检索，多用途）。\n"
        "会根据系统给的『候选方向』和对话记忆判断该用哪个工具，或直接作答。\n"
        "规则：\n"
        "1. 信息不足确实无法判断时，**最多问用户一次**补充（工具之外直接提问）。\n"
        "2. 问过一次并得到答复后，**必须据此作答或调工具，不得重复追问同一件事**。\n"
        "3. 若已确认意图（系统标注『意图已确认』），直接执行，不要再确认。\n"
        "4. 找不到/库里没有时，**诚实说明**，可提示可用方向（时间/天气/本地资料）；绝不编造。\n"
        "5. 工具失败就如实说明，不假装成功。"
    )  # 提示结束


def _user_message(state: dict[str, Any]) -> str:  # 组装单条 human（记忆+候选+预算）
    """把候选线索、对话记忆、意图确认状态、追问预算一起给 agent。"""
    question = str(state.get("question") or "")  # 当前问题
    confirmed = bool(state.get("intent_confirmed"))  # 意图是否已确认
    clarify_count = int(state.get("clarify_count") or 0)  # 已追问次数
    seg: list[str] = []  # 段
    seg.append(f"候选方向（仅供参考，不盲从）：{_candidate_hint(state)}")  # 候选
    seg.append(f"对话记忆：\n{_format_memory(state)}")  # 记忆
    seg.append(f"意图已确认：{confirmed}；已追问次数：{clarify_count}（>1 严禁再追问）")  # 预算
    if clarify_count >= 1:  # 已问过一次 → 强约束禁止再问
        seg.append("⚠ 你已追问过用户；这次必须直接作答或诚实说明，严禁再次提问。")  # 强约束
    if confirmed:  # 意图已确认 → 直接执行
        seg.append("意图已确认：直接执行请求，不要进行任何二次确认。")  # 强约束
    seg.append(f"当前问题：{question}")  # 问题
    return "\n\n".join(seg)  # 返回


def _message_text(msg: Any) -> str:  # 取消息文本
    """AIMessage/ToolMessage 的 content 转字符串。"""
    try:  # 兼容不同 content 形态
        content = getattr(msg, "content", "")  # 内容
        if isinstance(content, (list, tuple)):  # 多段
            return " ".join(str(c.get("text") or "") for c in content if isinstance(c, dict))  # 拼
        return str(content or "")  # 字符串
    except Exception:  # 坏内容
        return ""  # 空


def _is_question(text: str) -> bool:  # 判定终答是否像"提问"（用于防循环计数）
    """短句子以 ？/? 结尾 → 视为追问，递增 clarify_count。"""
    t = str(text or "").strip()  # 去空白
    if not t:  # 空
        return False  # 非提问
    if not t.endswith(("？", "?")):  # 无问号
        return False  # 非提问
    return len(t) <= 60  # 短句才算（长句带问号是回答）


def guard_from_memory(turns: list[Any]) -> tuple[int, bool]:  # 从对话记忆反推护栏
    """跨轮次防循环：从历史（Turn 列表）推出 clarify_count / intent_confirmed。

    记忆里每一条"短句提问"式助手回答 = 一次追问；一旦某轮被追问、后面又有新的用户轮，
    说明用户已答复 → 意图已确认 → 后续不得再重复询问。
    """
    count = 0  # 追问次数
    confirmed = False  # 是否已确认
    asked_before = False  # 上一轮是否追问
    for turn in turns or []:  # 逐轮
        answer = str(getattr(turn, "assistant", "") or "") if not isinstance(turn, dict) else str(turn.get("assistant") or "")  # 助手回答（Turn.assistant）
        if _is_question(answer):  # 本轮是追问
            count += 1  # 计数
            asked_before = True  # 记录
        elif asked_before:  # 被问过一次、这轮是用户在补信息
            confirmed = True  # 意图确认
            asked_before = False  # 复位
    return count, confirmed  # 返回


async def _collect_evidence(messages: list[Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:  # 收集引用+门禁
    """从 ReAct 的工具调用里回收知识库证据与门限（工具 10 分钟幂等缓存，重调很便宜）。

    agent 只拿到工具的编号文本，结构化证据/门限要在这里取回，才能给出引用、查看全文、
    并让"依据资料作答"的门禁生效（证据弱→提示，证据不足→诚实说明）。
    """
    from app.agent.tools.catalog import tool_catalog  # 工具目录
    from app.rag.evidence import CITATION_MAX  # 引用上限

    catalog = tool_catalog()  # 名 → 工具
    seen: set[str] = set()  # 去重
    fragments: list[dict[str, Any]] = []  # 引用
    gate: dict[str, Any] = {}  # 门限（取第一次知识检索的）
    for msg in messages:  # 按顺序
        if not (isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None)):  # 只看"要调工具"的
            continue  # 跳过
        for call in msg.tool_calls:  # 逐个
            name = str(call.get("name") or "")  # 工具名
            if not name.startswith("search_"):  # 只回收检索类
                continue  # 跳过
            tool = catalog.get(name)  # 取工具
            if tool is None:  # 未注册
                continue  # 跳过
            try:  # 重调（缓存命中，便宜）
                from app.agent.tools.wiring import unwrap_tool_output  # 还原完整包络

                envelope = unwrap_tool_output(await tool.ainvoke(call.get("args") or {}))  # 执行
                data = getattr(envelope, "data", None) or {}  # 结构化出参
            except Exception:  # 失败不影响回答
                continue  # 跳过
            if not gate and isinstance(data.get("gate"), dict):  # 首次门限
                gate = dict(data["gate"])  # 记录
            for item in data.get("evidence") or []:  # 证据
                key = str(item.get("text") or "")  # 去重键
                if not key or key in seen:  # 空/重复
                    continue  # 跳过
                seen.add(key)  # 记
                fragments.append(item)  # 收集
                if len(fragments) >= CITATION_MAX:  # 到上限
                    break  # 停止收集
        if len(fragments) >= CITATION_MAX:  # 已达上限
            break  # 退出外层
    for position, item in enumerate(fragments, start=1):  # 生成层统一编号（多轮检索也不冲突）
        item["index"] = position  # 覆盖工具内的本次编号
    return fragments, gate  # 返回


async def run_react(  # 跑单一 ReAct 兜底
    state: dict[str, Any],  # 父图 State
    *,
    model: Any | None = None,  # chat_model
) -> dict[str, Any]:  # 写回父图的更新
    """构造 ReAct → 跑 → 回填答案；工具调用发 TOOL 事件；防循环追问。"""
    assert model is not None, "兜底 ReAct 需要可用模型"  # 无模型不兜底（走 general/降级）
    # 跟进轮：只要此前已追问过（clarify_count>0）→ 本轮就是用户答复 → 意图已确认，直接执行
    prior_asked = int(state.get("clarify_count") or 0) > 0  # 之前是否问过
    confirmed = bool(state.get("intent_confirmed")) or prior_asked  # 已确认（含"正在答复追问"）
    state = {**state, "intent_confirmed": confirmed}  # 覆盖意图确认态
    messages = [HumanMessage(content=_user_message(state))]  # 单条 human（含全部上下文））
    system_prompt = _system_prompt(state)  # 系统提示（默认手写规则）
    if _CONTEXT_BUILDER:  # 启用装配（生产接线）
        ctx_view = _state_context(state)  # 装配上下文
        if ctx_view is not None:  # 装配成功
            messages = [HumanMessage(content=str(item.get("content") or "")) for item in ctx_view.messages if item.get("content")]  # 用 builder messages
            system_prompt = ctx_view.system  # 用 builder system（含 working 锚点）
    graph = _build_graph(model, system_prompt)  # 图（不缓存））
    run = current_run()  # 事件出口
    result = await graph.ainvoke({"messages": messages})  # 跑一轮（到终态））
    # —— 后置：从最终消息里回放工具调用 → TOOL 事件（时间线可见）——
    if run is not None:  # 有事件出口
        for msg in result.get("messages", []):  # 逐条（保持顺序））
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):  # 模型要调工具
                for tc in msg.tool_calls:  # 逐个调用
                    tname = str(tc.get("name") or "")  # 工具名
                    run.emit(tool_event(name=tname, status="running", variant=_VARIANT.get(tname, "")))  # 运行中
            elif isinstance(msg, ToolMessage):  # 工具结果
                tname = str(msg.name or "")  # 工具名
                out = _message_text(msg)[:200]  # 输出摘要
                failed = bool(msg.status and msg.status != "success")  # 是否失败
                run.emit(tool_event(  # 结果事件
                    name=tname,  # 工具名
                    status="failed" if failed else "done",  # 状态
                    detail=out[:120],  # 摘要
                    variant=_VARIANT.get(tname, ""),  # 变体
                    output=out,  # 输出
                ))  # 发送结束
    answer = ""  # 最终答案
    for msg in reversed(result.get("messages", [])):  # 从后往前找助手最终文本
        if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):  # 无工具调用的 AIMessage
            answer = _message_text(msg)  # 即最终答复
            break  # 停
    # —— 防循环：终答是"提问"则记一次追问；已确认还问 → 改写为诚实收尾，避免死循环 ——
    new_clarify = int(state.get("clarify_count") or 0)  # 当前计数
    if _is_question(answer):  # 是提问
        if confirmed:  # 已确认还问（第二轮再问）→ 强制收尾，不许再循环
            answer = "信息仍不够具体，请换一种更明确的说法再试。"  # 诚实收尾
        else:  # 首次追问
            new_clarify += 1  # 记一次（下一轮 confirmed → 熔断）
    # —— 依据资料作答的门禁：回收引用 + 门限；证据弱/不足时如实提示（不假装有据）——
    fragments, gate = await _collect_evidence(result.get("messages", []))  # 回收证据
    note = ""  # 说明
    decision = str((gate or {}).get("decision") or "")  # pass/weak/reject
    if fragments:  # 有引用
        answer = _add_citation_marks(answer, fragments)  # 给模型回答补角标（保持可追溯）
    if decision == "weak" and not _is_question(answer):  # 证据弱
        note = "证据较弱：以上回答请对照引用原文核对。"  # 提示
    elif decision == "reject" and fragments:  # 门禁判不足
        note = "资料不足以下结论，请以引用原文为准。"  # 提示
    return {  # 写回父图
        "answer": answer or "已尝试，但未能完成，请换一种方式描述。",  # 兜底话术
        "fallback_used": True,  # 标记
        "clarify_count": new_clarify,  # 喂追计数（供下一轮熔断）
        "intent_confirmed": confirmed,  # 意图确认态（跟进轮置真）
        "fragments": fragments,  # 引用（agent 也有资料可点开看全文）
        "gate": gate,  # 门限结论（观测/校验用）
        "note": note,  # 说明
    }  # 返回


def _add_citation_marks(answer: str, fragments: list[dict[str, Any]]) -> str:  # 给回答补引用角标
    """模型回答若没写角标，就在末尾补一条"参考 [1][2]"，保证引用可追溯。"""
    text = str(answer or "").rstrip()  # 去尾空白
    if not text or "[1]" in text or "[来源" in text:  # 已带角标
        return text  # 不动
    marks = "".join(f"[{i}]" for i in range(1, min(len(fragments), 5) + 1))  # 角标
    return f"{text}\n\n参考：{marks}"  # 追加