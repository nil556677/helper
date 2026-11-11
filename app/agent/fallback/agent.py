"""兜底单一 ReAct agent：可调 天气/时间/本地资料检索 3 个工具。

替代原「计划→执行」子图：不再 planner/replanner 多步规划，直接用 `create_agent`（React）
带完整记忆 + 候选线索 决策。带去人所有不确定（代指/含糊/未命中/多轮）。

护栏（防追问循环）：
- `intent_confirmed`：意图已确认 → 不再进"信息不足→追问"，直接执行；
- `clarify_count`：最多追问 1 次；超过 → 强制按已有信息作答或诚实说明；
- 全程消息列表保留，工具结果如实回填，失败如实降级（不编造）。
"""

from __future__ import annotations  # 延迟求值注解

import ast  # Python 字面量解析（ToolMessage 把 dict 转 repr）
import json  # 解析工具结果 JSON
from typing import Any  # 任意值

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # 消息类型

from app.agent.context import current_run  # 当前 run（发 TOOL 事件）
from app.agent.events import context as context_event  # 上下文事件
from app.agent.events import tool as tool_event  # 工具事件
from app.agent.tools.catalog import tool_catalog  # 工具目录（单一来源）

#: 兜底 agent 可调的工具名（时间/天气/检索/订单(需审)/物流）。
TOOL_NAMES: tuple[str, ...] = (
    "current_time",  # 时间
    "query_weather",  # 天气
    "search_knowledge_base",  # 检索
    "query_order",  # 订单（approval=manual → 审批门）
    "query_logistics",  # 物流（第三方）
)  # 5 工具

#: 工具名 → UI variant（时间线图标归类）。
_VARIANT: dict[str, str] = {  # 变体
    "current_time": "time",  # 时间
    "query_weather": "weather",  # 天气
    "search_knowledge_base": "search",  # 检索
    "query_order": "order",  # 订单
    "query_logistics": "logistics",  # 物流
}  # 变体结束

#: 说话者标签（多轮记忆格式化用）。
_MAX_TURNS = 6  # 最多带最近 6 轮记忆


def _state_context(state: dict[str, Any]) -> Any | None:  # 用 ContextBuilder 装配上下文
    """从 state 取工作记忆/近轮/段/事实/问题 → build_context；失败返回 None（退回手拼）。"""
    try:  # 任何失败退回旧路径
        from app.agent.contexts.builder import build_context  # builder（嵌入已在图装配时注入一次）
        from app.agent.memory.manager import recall  # 记忆统一入口

        conversation_id = str(state.get("conversation_id") or "")  # 会话
        segments: list[dict[str, Any]] = []  # 段
        try:  # 段取数失败不影响装配
            if conversation_id:  # 有会话
                from app.services import conversations  # 会话服务

                segments = conversations.load_segments(conversation_id, limit=1)  # 最近 1 段（滚动摘要已含此前全部内容，避免重复注入）
        except Exception:  # 无表/坏库
            segments = []  # 空
        facts: list[dict[str, Any]] = list(state.get("memory_facts") or [])  # 优先用 load_context 已召回的（单轮只查一次）
        profile_text = str(state.get("memory_profile_text") or "")  # 优先用入口画像
        if not facts:  # 没有（如独立调用/测试）→ 兜底召回
            try:  # 兜底召回失败不影响装配
                from app.agent.memory.manager import recall  # 记忆统一入口

                bundle = recall(str(state.get("question") or ""), top_k=6)  # 召回（事实+画像）
                facts = list(bundle.facts)  # 事实
                profile_text = "\n".join(bundle.profile_lines) or profile_text  # 画像（入口没有才用兜底）
            except Exception:  # 无表/坏库
                facts = []  # 空
        hints: list[str] = []  # 防追问护栏（候选/确认/追问预算）
        hints.append(f"候选方向（仅供参考，不盲从）：{_candidate_hint(state)}")  # 候选
        confirmed = bool(state.get("intent_confirmed"))  # 意图确认
        clarify = int(state.get("clarify_count") or 0)  # 追问数
        hints.append(f"意图已确认：{confirmed}；已追问次数：{clarify}（>1 严禁再追问）")  # 预算
        if clarify >= 1:  # 已问过
            hints.append("已追问过用户；这次必须直接作答或诚实说明，严禁再次提问。")  # 硬约束
        if confirmed:  # 已确认
            hints.append("意图已确认：直接执行请求，不要二次确认。")  # 硬约束
        return build_context(  # 装配（system 用现有工具规则）
            system=_system_prompt(state),  # S 规则
            working=state.get("working_memory") or {},  # 锚点
            facts=facts,  # 事实
            segments=segments,  # 段
            recent=list(state.get("turns") or []),  # 近轮
            hints=hints,  # 护栏（防追问循环）
            profile_text=profile_text,  # 画像块（入口或兜底召回）
            question=str(state.get("question") or ""),  # 问题
        )  # 返回
    except Exception:  # 失败
        return None  # 退回旧路径


def _build_graph(model: Any, prompt: str) -> Any:  # 构造 ReAct 图
    """模型实例 + 系统提示 → 编译好的自定义 ReAct 图（agent→审批门→tools）。

    真流式 sink 在这里自建（用 ``current_run()``），不改变 run_react 对外的两参调用，
    因此测试桩 ``(model, prompt)`` 依然兼容；无 run（单测/独立调用）时退回非流式。
    """
    from app.agent.context import current_run  # 当前 run
    from app.agent.events import delta as delta_event  # 增量事件
    from app.agent.react.graph import build_react  # 自定义图（含审批门）

    token_sink: Any = None  # 默认非流式
    run = current_run()  # 事件出口（图内 agent 节点发 DELTA 需要）
    if run is not None:  # 有 UI 消费者才流式
        def _token_sink(token: str) -> None:  # 逐 token 透传
            if run.first_token_at is None:  # 还没标首 token
                run.mark_first_token()  # 记录（修活 ttft 指标）
            run.partial_answer += token  # 累加部分答案（失败/取消保留、REPLACE 不再重复）
            run.emit(delta_event(token))  # 发增量给前端（真流式正文）
        token_sink = _token_sink  # 挂上
    catalog = tool_catalog()  # 工具目录
    tools = [catalog[name] for name in TOOL_NAMES if name in catalog]  # 取 3 工具
    return build_react(model, tools, prompt, token_sink=token_sink)  # 自定义图（审批门内联，当前工具 approval=none 直通）


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
    ctx_view: Any = None  # builder 装配结果（C5 观测用）
    from app.agent.contexts.feature import builder_enabled  # 特性开关（生产开/单测关）

    if builder_enabled():  # 生产注入 ContextBuilder
        ctx_view = _state_context(state)  # 装配上下文
        if ctx_view is not None:  # 装配成功
            messages = [HumanMessage(content=str(item.get("content") or "")) for item in ctx_view.messages if item.get("content")]  # 用 builder messages
            system_prompt = ctx_view.system  # 用 builder system（含 working 锚点）
    run = current_run()  # 事件出口
    graph = _build_graph(model, system_prompt)  # 图（不缓存）——流式 sink 在 _build_graph 内自建
    if ctx_view is not None and run is not None:  # C5：上下文事件带剪枝/丢弃/缓存前缀
        from app.agent.contexts.assembler import ContextUsage  # 上下文占用
        from app.agent.contexts.budget import Budget  # 预算

        run.context_usage = ContextUsage(  # 供 persist 落 turn_json → UI 占用条读取
            window=Budget().window,  # 窗口
            total=int(sum(getattr(b, "tokens", 0) for b in ctx_view.blocks)),  # token
            pruned=dict(ctx_view.pruned),  # 剪枝
            dropped=dict(ctx_view.dropped),  # 预算丢
            cache_prefix_tokens=ctx_view.cache_prefix_tokens,  # 缓存前缀
            constraints_kept=float(getattr(ctx_view, "constraints_kept", 0.0) or 0.0),  # 约束保留率（P5 指标）
        )  # 结束
        run.emit(context_event(  # 上下文事件
            len(ctx_view.messages),  # 条数
            int(sum(getattr(b, "tokens", 0) for b in ctx_view.blocks)),  # token（块内）
            pruned=dict(ctx_view.pruned),  # 剪枝
            dropped=dict(ctx_view.dropped),  # 预算丢弃
            cache_prefix_tokens=ctx_view.cache_prefix_tokens,  # 缓存前缀
        ))  # 发送结束
    result = await graph.ainvoke({"messages": messages})  # 跑一轮（到终态））
    # —— 后置：从最终消息里回放工具调用 → TOOL 事件（时间线可见）+ 工作记忆记步 ——
    from app.agent.memory import (  # 工作记忆写侧
        blank_working,  # 空白板
        record_step_working,  # 记步
        touch_working,  # 合并关键事实
        update_working,  # 更新当前步/待办
    )  # 导入结束
    white = state.get("working_memory") if isinstance(state.get("working_memory"), dict) else blank_working()  # 读白板
    if run is not None:  # 有事件出口
        for msg in result.get("messages", []):  # 逐条（保持顺序））
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):  # 模型要调工具
                for tc in msg.tool_calls:  # 逐个调用
                    tname = str(tc.get("name") or "")  # 工具名
                    if run is not None:  # 有事件出口
                        run.emit(tool_event(name=tname, status="running", variant=_VARIANT.get(tname, "")))  # 运行中
                    white = update_working(white, current_step=f"调用 {tname}")  # 设当前步（进行中）
                    white = record_step_working(white, f"调用 {tname}")  # 归档为已一步（并清当前步）
            elif isinstance(msg, ToolMessage):  # 工具结果
                tname = str(msg.name or "")  # 工具名
                out = _message_text(msg)[:200]  # 输出摘要
                failed = bool(msg.status and msg.status != "success")  # 是否失败
                if run is not None:  # 有事件出口
                    run.emit(tool_event(  # 结果事件
                        name=tname,  # 工具名
                        status="failed" if failed else "done",  # 状态
                        detail=out[:120],  # 摘要
                        variant=_VARIANT.get(tname, ""),  # 变体
                        output=out,  # 输出
                    ))  # 发送结束
                # 从工具结果结构化 data 挑正确性锚点（白名单式，不整包塞）
                anchors = _anchor_facts(tname, msg)  # 挑锚点
                if anchors:  # 有锚点
                    white = touch_working(white, anchors)  # 合并进 key_facts
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
    searched = bool(gate) or bool(fragments)  # 是否真发起了知识检索（gate 非空才可能 reject）
    if fragments:  # 有引用
        answer = _add_citation_marks(answer, fragments)  # 给模型回答补角标（保持可追溯）
    if decision == "weak" and not _is_question(answer):  # 证据弱
        note = "证据较弱：以上回答请对照引用原文核对。"  # 提示
    elif decision == "reject" and searched:  # 门禁判不足：强制不凭空作答（不假装有据）
        if not _is_question(answer):  # 别把追问顶掉
            answer = "本地资料里没找到足够回答这个问题的内容。换个说法试试，或告诉我你想查的方向（时间/天气/本地资料）。"  # 诚实说明（不臆造）
        note = "未检索到足够依据，已如实说明。"  # 说明（reject 即"不值得依据回答"）
    return {  # 写回父图
        "answer": answer or "已尝试，但未能完成，请换一种方式描述。",  # 兜底话术
        "fallback_used": True,  # 标记
        "clarify_count": new_clarify,  # 喂追计数（供下一轮熔断）
        "intent_confirmed": confirmed,  # 意图确认态（跟进轮置真）
        "working_memory": dict(white),  # 工作记忆（记步 + 关键事实锚点，随 checkpoint 持久化）
        "fragments": fragments,  # 引用（agent 也有资料可点开看全文）
        "gate": gate,  # 门限结论（观测/校验用）
        "note": note,  # 说明
        "approval_pending": list(result.get("approval_pending") or []),  # 待审批元信息（上抛路由级弹框）
    }  # 返回


def _anchor_facts(tool_name: str, msg: ToolMessage) -> dict[str, Any]:  # 从工具结果挑正确性锚点
    """白名单式挑 key_facts（正确性锚点，不是垃圾桶）。

    数据来源优先取 ``msg.content``（content_and_artifact 下它是成功 data 字典）；
    取不到再退 ``msg.artifact``（完整包络 JSON 字符串）。只挑白名单字段，其余交给审计通道。
    """
    anchors: dict[str, Any] = {}  # 结果
    data: dict[str, Any] = {}  # 结构化数据
    try:  # 优先 content（模型视角 = 成功 data）
        if isinstance(msg.content, dict):  # dict 形态
            data = msg.content  # 直接用
        elif isinstance(msg.content, str):  # 字符串形态
            text = msg.content.strip()  # 去空白
            try:  # 先按 JSON 解析
                parsed = json.loads(text)  # JSON
            except Exception:  # 非 JSON → 可能是 Python 字面量（ToolMessage 会把 dict 转 repr）
                parsed = ast.literal_eval(text)  # Python 字面量
            if isinstance(parsed, dict):  # 解析出 dict
                data = parsed  # 用
    except Exception:  # 解析失败
        data = {}  # 空
    if not isinstance(data, dict):  # 非 dict
        data = {}  # 空
    if not data:  # content 拿不到，退 artifact
        try:  # artifact 是完整包络 JSON 字符串
            raw = getattr(msg, "artifact", None)  # 取 artifact
            parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})  # 解析
            data = parsed.get("data") if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict) else {}  # 取 data 段
        except Exception:  # 解析失败
            data = {}  # 空
    anchor_map = {  # 工具 → 锚点字段白名单
        "query_weather": ("city", "date"),  # 天气：城市+日期
        "query_order": ("order_id",),  # 订单：单号
        "query_logistics": ("order_id", "tracking_no"),  # 物流：单号/运单号
        "current_time": ("value",),  # 时间：当前值
    }  # 白名单表
    fields = anchor_map.get(tool_name, ())  # 取该工具白名单
    for field in fields:  # 逐个字段
        if field in data and data[field] is not None:  # 存在且非空
            anchors[field] = data[field]  # 记锚点
    return anchors  # 返回


def _add_citation_marks(answer: str, fragments: list[dict[str, Any]]) -> str:  # 给回答补引用角标
    """模型回答若没写角标，就在末尾补一条"参考 [1][2]"，保证引用可追溯。"""
    text = str(answer or "").rstrip()  # 去尾空白
    if not text or "[1]" in text or "[来源" in text:  # 已带角标
        return text  # 不动
    marks = "".join(f"[{i}]" for i in range(1, min(len(fragments), 5) + 1))  # 角标
    return f"{text}\n\n参考：{marks}"  # 追加
