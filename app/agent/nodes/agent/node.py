"""agent 节点：工具循环（ReAct 式）——模型决策调工具，无 tool_calls 时产出最终回答。

顶层仍是单节点（LangGraph 视角 `load_context → agent → finalize`），工具循环收敛在节点内部，
避免给「单发」的 RunMachine 引入重复节点完成导致 InvalidTransition。工具经 `wiring.bind_tools`
绑定到模型，循环里每次 ainvoke 都能带出 tool_calls。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage  # 消息类型
from langchain_core.runnables import RunnableConfig  # 图配置

from app.agent.agent_builder import build_react_agent  # 官方 agent 构造
from app.agent.callbacks import TraceCallbacks  # 本地追踪回调（单挂）
from app.agent.context import RunCancelled, current_run, ctx_from_config  # 上下文
from app.agent.contexts import AgentContextSpec  # 上下文声明
from app.agent.contexts import build as assemble_context  # 上下文装配器
from app.agent.contexts.history import messages_from  # 轮次 -> 消息
from app.agent.contracts import StageGroup, Turn  # 分组 / 轮次
from app.agent.events import citation as citation_event  # 引用事件
from app.agent.events import context as context_event  # 上下文事件
from app.agent.events import delta as delta_event  # 增量事件
from app.agent.events import settings as settings_event  # 设置事件
from app.agent.events import usage as usage_event  # 用量事件
from app.agent.events import verify_report as verify_event  # 校验结论
from app.agent.stages import stage  # 步骤事件
from app.agent.tools.wiring import collect  # 收集工具
import app.agent.tools.library as _LIB  # 工具库模块（用于 collect）
from app.core.llm import store as llm_store  # 模型名
from app.integrations.llm import chat_model  # 模型实例工厂
from app.rag.answer import extractive  # 原文回退
from app.rag.verify import store_lookup, verify  # 引用校验（L1 + L3）

#: 本轮工具上限：防 agent↔工具无限循环（与 driver 的 RECURSION_LIMIT 呼应）。
MAX_TOOL_ROUNDS = 6  # 最多多少轮决策

#: 答题 agent 的上下文声明；沿用现有 answer 的声明，保证提示词模板一致。
_SPEC = AgentContextSpec(agent="answer")  # 声明

#: 三个工具收集一次，供 bind_tools 用（进程级复用）。
_TOOLS = collect(_LIB)  # 工具列表


def _text_of(content: Any) -> str:  # 取消息正文
    """content 可能是 str 或内容块列表，只拼文本块。"""
    if isinstance(content, str):  # 纯文本
        return content  # 直接
    if isinstance(content, list):  # 内容块
        parts: list[str] = []  # 文本块
        for item in content:  # 逐块
            if isinstance(item, str):  # 字符串
                parts.append(item)  # 收
            elif isinstance(item, dict) and isinstance(item.get("text"), str):  # 文本块
                parts.append(str(item["text"]))  # 收
        return "".join(parts)  # 拼
    return ""  # 认不出


def _model() -> Any:  # 取模型实例
    """llm 槽位模型（未配置返回 None，调用方走原文兜底）。"""
    return chat_model(slot="llm", streaming=False)  # 实例


def _lc_messages(msgs: list[dict[str, Any]]) -> list[Any]:  # dict → LangChain 消息
    """转 LC 消息；system 由 create_agent 的 system_prompt 承担，这里去掉首条系统消息。"""
    return _to_lc(msgs, "")[1:]  # 去掉空系统消息


def _evidence_from_messages(messages: list[Any], seed: list[dict[str, Any]]) -> list[dict[str, Any]]:  # 从工具结果补证据
    """遍历输出消息，把检索工具 Result 里的 evidence 累加（重新编号）。"""
    import json  # 局部导入解析包络

    evidence = list(seed)  # 复制种子
    for message in messages:  # 逐条
        if not isinstance(message, ToolMessage):  # 只看工具结果
            continue  # 跳过
        data: Any = message.content  # 正文
        if isinstance(data, str):  # JSON 文本
            try:  # 解析
                data = json.loads(data)  # 字典
            except Exception:  # 非 JSON
                continue  # 跳过
        if not isinstance(data, dict):  # 非包络
            continue  # 跳过
        items = (data.get("data") or {}).get("evidence") or []  # 证据
        for item in items:  # 逐条
            row = dict(item)  # 复制
            row["index"] = len(evidence) + 1  # 重新编号
            evidence.append(row)  # 累加
    return evidence  # 返回


def _to_lc(messages: list[dict[str, Any]], system: str) -> list[Any]:  # dict -> LangChain 消息
    """把中间件能处理的 dict 消息转回 LangChain 消息给模型。"""
    out: list[Any] = [SystemMessage(content=system)]  # 系统提示置首
    for message in messages:  # 逐条
        role = str(message.get("role") or "user")  # 角色
        content = str(message.get("content") or "")  # 正文
        if role == "tool":  # 工具结果
            out.append(  # 结果消息
                ToolMessage(  # 工具消息
                    content=content,  # 结果正文
                    tool_call_id=str(message.get("tool_call_id") or ""),  # 关联调用
                    name=str(message.get("name") or ""),  # 工具名
                )  # 结束
            )  # 追加
        elif role == "assistant" and message.get("tool_calls"):  # 助手带工具调用
            out.append(AIMessage(content=content, tool_calls=message["tool_calls"]))  # 调用记录
        elif role == "assistant":  # 助手正文
            out.append(AIMessage(content=content))  # 加
        else:  # 其余当用户
            out.append(HumanMessage(content=content))  # 加
    return out  # 返回


async def tool_agent_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:  # 工具循环节点
    """装载上下文 → 工具循环 → 校验引用 → 返回回答。"""
    ctx = ctx_from_config(config)  # 运行上下文
    if ctx is None:  # 不在新链路
        return {}  # 不处理
    question = str(state.get("question") or "")  # 问题
    attachments = list(state.get("attachments") or [])  # 附件（正文不入 state）
    readable_attach = [  # 只留可读附件
        {"name": str(item.get("name") or "附件"), "text": _readable_text(item)}  # 单条（正文现读）
        for item in attachments  # 遍历
    ]  # 列表结束
    readables = [item for item in readable_attach if item["text"].strip()]  # 有正文的
    built = assemble_context(  # 装配基础上下文（历史+问题+附件，无工具结果）
        _SPEC,  # 声明
        history=messages_from([Turn.model_validate(item) for item in (state.get("turns") or [])]),  # 历史
        summary_text=str(state.get("summary") or ""),  # 摘要
        attachments=readables,  # 可读附件
        question=question,  # 问题
    )  # 装配结束
    ctx.context_usage = built.usage  # 记录占用
    system, messages = built.system, list(built.messages)  # 系统 + 消息
    model = _model()  # 模型实例（未配置为 None）
    evidence: list[dict[str, Any]] = list(state.get("evidence") or [])  # 播种证据（rag 已取）+ 工具新取
    answer_text = ""  # 最终回答
    note = ""  # 说明（只进 state）
    note_kind = "plain"  # 说明样式（失败类置 error）
    usage: dict[str, Any] = {}  # 用量（只进 state）
    citations: list[dict[str, Any]] = []  # 引用（只进 state）
    settings: dict[str, Any] = {}  # 本轮设置
    usage_total: dict[str, int] = {}  # 累计用量
    streamed: list[str] = []  # 已流式发送的文本
    msgs = [dict(m) for m in messages]  # 可变的 dict 消息（历史 + 问题）

    if model is None:  # 无模型 → 原文兜底
        answer_text = extractive(evidence)  # 回退原文
        note = "模型未配置，以下为资料原文。"  # 说明
        note_kind = "error"  # 错误样式
    else:  # 官方 create_agent：模型/工具/取消/PII/限额由框架与中间件承担
        agent = build_react_agent(model, list(_TOOLS), system, run_limit=MAX_TOOL_ROUNDS)  # 构造
        out_messages: list[Any] = []  # 输出消息
        with stage("工具决策", group=StageGroup.TOOL) as info:  # 过程步骤
            try:  # 流式跑 agent
                async for mode, payload in agent.astream(  # 双模式：token + 状态快照
                    {"messages": _lc_messages(msgs)},  # 输入（system 走 system_prompt）
                    config={"callbacks": [TraceCallbacks()]},  # 本地追踪单挂（唯一）
                    stream_mode=["messages", "values"],  # 模型 token + 完整状态
                ):  # 迭代
                    if mode == "messages":  # token 块
                        chunk = payload[0] if isinstance(payload, tuple) else payload  # 消息块
                        if getattr(chunk, "type", "") == "AIMessageChunk":  # 只看模型输出
                            text = _text_of(getattr(chunk, "content", ""))  # 文本
                            if text:  # 有内容
                                ctx.mark_first_token()  # 首 token 计时
                                streamed.append(text)  # 累积
                                ctx.emit(delta_event(text))  # 实时发
                    elif mode == "values":  # 完整状态
                        out_messages = list((payload or {}).get("messages") or [])  # 记录
            except RunCancelled:  # 取消穿透
                raise  # 交给上层
            except Exception:  # 模型/工具失败 → 原文兜底
                answer_text = extractive(evidence)  # 回退原文
                note = "模型调用失败，以下为资料原文。"  # 说明
                note_kind = "error"  # 错误样式
            if out_messages:  # 成功
                answer_text = _text_of(getattr(out_messages[-1], "content", ""))  # 最终回答
                usage_total = _usage_of(out_messages[-1])  # 用量
                evidence = _evidence_from_messages(out_messages, evidence)  # 从工具结果补证据
                info.detail = f"已执行 {len(evidence)} 条引用"  # 明细

    # ---- 与现有 answer 相同的收尾：校验 / 回退 / 引用 / 事件 ----
    model_name = "未配置模型"  # 默认
    try:  # 读模型名
        row = llm_store.get("llm")  # 槽位
        model_name = str(row["model"]) if row is not None and row["model"] else model_name  # 模型
    except Exception:  # 读不到不阻断
        pass  # 默认
    settings = {"model": model_name, "temperature": 0.2, "system_prompt": system}  # 本轮设置（只进 state）
    ctx.emit(settings_event(model_name, 0.2, system, {"evidence": len(evidence)}))  # 设置事件

    if not answer_text and not evidence and not any(_readable_text(item).strip() for item in attachments):  # 空答
        note = "没有在本地资料里找到相关内容，换个说法试试。"  # 说明
        note_kind = "error"  # 错误样式
        return {"note": note, "note_kind": note_kind}  # 说明

    if not evidence:  # 无证据但有附件/原文
        answer_text = answer_text or extractive([])  # 兜底
    result = verify(answer_text, evidence, lookup=store_lookup)  # 引用校验
    cleaned, used = result.text, result.valid  # 清理后正文与有效编号
    fallback = extractive(evidence)  # 原文
    if evidence and answer_text.strip() and not used and answer_text.strip() != fallback.strip():  # 有证据却没引
        note = "模型没给出能溯源的引用，已改为资料原文。"  # 说明
        cleaned = fallback  # 换原文
    elif result.missing:  # 有引用但资料失效
        note = "部分引用的资料已不可用，相关角标已移除。"  # 说明
    if result.unsupported and not note:  # 有句没被支撑
        note = "部分内容未能在资料中找到出处，请对照引用原文核对。"  # 只提示不删
    citations = _fragments(used, evidence)  # 记录引用
    if result.missing:  # 失效的也列出
        citations += _fragments(result.missing, evidence, verified=False)  # 追加
    ctx.partial_answer = cleaned  # 记录（流式中间态；取消落库用）
    usage = dict(usage_total)  # 用量
    usage["completion_chars"] = len(cleaned)  # 简单用量
    ctx.emit(  # 引用事件
        citation_event(citations),  # 引用
    )  # 发射结束
    ctx.emit(  # 校验结论
        verify_event(  # 构造
            result.valid,  # 有效
            result.invalid,  # 非法
            result.missing,  # 失效
            result.coverage,  # 覆盖率
            result.reasons,  # 原因
            result.unsupported,  # 未支撑句
        )  # 构造结束
    )  # 发射结束
    ctx.emit(  # 用量事件
        usage_event(  # 构造
            int(usage.get("prompt_tokens") or 0),  # 输入
            int(usage.get("completion_tokens") or len(cleaned)),  # 输出
            extra={k: v for k, v in usage.items() if k in {"cache_read_tokens", "cache_creation_tokens", "reasoning_tokens"}},  # 明细
        )  # 构造结束
    )  # 发射结束
    if cleaned and not streamed:  # 有正文且尚未流式发送（非流式/兜底）
        ctx.emit(delta_event(cleaned))  # 一次性发全文
    return {  # 写回
        "answer": cleaned,  # 答案
        "fragments": citations,  # 引用
        "note": note,  # 说明
        "note_kind": note_kind,  # 说明样式
        "evidence": evidence,  # 证据
        "usage": usage,  # 用量
        "settings": settings,  # 本轮设置
        "reasoning": "",  # 思考流（非流式无）
        "conflict": "",  # 冲突提示（非流式无）
    }  # 返回结束


def _usage_of(message: Any) -> dict[str, int]:  # 取用量
    """从 message.usage_metadata 归一并返回扁平字典。"""
    usage = getattr(message, "usage_metadata", None)  # 用量
    if not isinstance(usage, dict):  # 没有
        return {}  # 空
    out: dict[str, int] = {}  # 结果
    for plain, keys in (  # (输出键, 候选输入键)
        ("prompt_tokens", ("input_tokens", "prompt_tokens")),  # 输入
        ("completion_tokens", ("output_tokens", "completion_tokens")),  # 输出
        ("total_tokens", ("total_tokens",)),  # 总数
    ):  # 遍历映射
        for key in keys:  # 逐个候选
            value = usage.get(key)  # 取值
            if isinstance(value, int):  # 有效
                out[plain] = value  # 记录
                break  # 命中即停
    return out  # 返回


def _readable_text(item: dict[str, Any]) -> str:  # 附件正文
    """按 disk 读附件正文；延迟导入附件服务避免循环。"""
    from app.services import attachments as attach  # 附件服务（延迟避免循环）

    return attach.load_text(item)  # 正文文本


def _fragments(  # 组装引用（与 answer 的字段一致）
    used: list[int],  # 序号
    evidence: list[dict[str, Any]],  # 证据
    *,
    verified: bool = True,  # 是否通过溯源
) -> list[dict[str, Any]]:  # 片段
    """把被引用的证据转成前端 Fragment。"""
    result: list[dict[str, Any]] = []  # 结果
    for number in used:  # 逐个被引号
        item = evidence[number - 1] if 0 < number <= len(evidence) else {}  # 对应证据
        result.append({  # 一条
            "index": item.get("index"),  # 序号
            "source_name": item.get("source_name", "资料"),  # 来源
            "origin": item.get("origin", "资料"),  # 出处
            "score": item.get("score", 0.0),  # 分数
            "score_kind": item.get("score_kind", "rrf"),  # 分数语义
            "text": item.get("text", ""),  # 正文
            "snippet": item.get("snippet", ""),  # 命中片段
            "selected": True,  # 被引用
            "verified": verified,  # 溯源
            "doc_id": int(item.get("doc_id") or 0),  # 文档 id
            "chunk_id": int(item.get("chunk_id") or 0),  # 片段 id
            "parent_id": int(item.get("parent_id") or 0),  # 父块
            "version": int(item.get("version") or 0),  # 版本
            "text_hash": str(item.get("text_hash") or ""),  # 指纹
        })  # 追加
    return result  # 返回