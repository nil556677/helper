"""agent 节点：工具循环（ReAct 式）——模型决策调工具，无 tool_calls 时产出最终回答。

顶层仍是单节点（LangGraph 视角 `load_context → agent → finalize`），工具循环收敛在节点内部，
避免给「单发」的 RunMachine 引入重复节点完成导致 InvalidTransition。工具经 `wiring.bind_tools`
绑定到模型，循环里每次 ainvoke 都能带出 tool_calls。
"""

from __future__ import annotations  # 延迟求值注解

import json  # 入参体积计数
import time  # 工具耗时
from typing import Any  # 任意值

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage  # 消息类型
from langchain_core.runnables import RunnableConfig  # 图配置

from app.agent.context import RunCancelled, current_run, ctx_from_config  # 上下文
from app.core.trace.recorder import trace_tool  # 工具 trace 打点
from app.agent.contexts import AgentContextSpec  # 上下文声明
from app.agent.contexts import build as assemble_context  # 上下文装配器
from app.agent.contexts.history import messages_from  # 轮次 -> 消息
from app.agent.contracts import ErrorCode, StageGroup, Turn  # 错误码 / 分组 / 轮次
from app.agent.events import citation as citation_event  # 引用事件
from app.agent.events import context as context_event  # 上下文事件
from app.agent.events import delta as delta_event  # 增量事件
from app.agent.events import settings as settings_event  # 设置事件
from app.agent.events import retry as retry_event  # 重试事件
from app.agent.events import usage as usage_event  # 用量事件
from app.agent.events import verify_report as verify_event  # 校验结论
from app.agent.stages import stage  # 步骤事件
from app.agent.tools.library import (  # 三个内置工具
    current_time,  # 时间
    query_weather,  # 天气
    search_knowledge_base,  # 知识库检索
)  # 导入结束
from app.agent.tools.wiring import collect, err, render  # 收集工具 / 包络
import app.agent.tools.library as _LIB  # 工具库模块（用于 collect）
from app.core.llm import store as llm_store  # 模型名
from app.core.retry import RetryPolicy, retry_async  # 模型请求重试
from app.integrations.llm import chat_model  # 模型实例工厂
from app.agent.middleware import ModelRequest, ModelResponse, default_chain  # 中间件链
from app.rag.answer import extractive  # 原文回退
from app.rag.verify import store_lookup, verify  # 引用校验（L1 + L3）

#: 本轮工具上限：防 agent↔工具无限循环（与 driver 的 RECURSION_LIMIT 呼应）。
MAX_TOOL_ROUNDS = 6  # 最多多少轮决策

#: 工具名 -> 用户可见的“查询中”步骤标题。
_TOOL_LABELS: dict[str, str] = {  # 标题映射
    "current_time": "查询时间",  # 时间
    "query_weather": "查询天气",  # 天气
    "search_knowledge_base": "检索资料",  # 知识库
}  # 映射结束

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


def _bound_model() -> Any:  # 绑定工具的模型
    """把三个工具 bind 到当前 llm 槽位；未配置则返回 None（调用方走兜底）。"""
    model = chat_model(slot="llm", streaming=False)  # 真实实例（未配置返回 None）
    if model is None:  # 不可用
        return None  # 无
    return model.bind_tools(list(_TOOLS))  # 绑定


#: 中间件链：每次模型调用都过它，保住 PII / 取消 / 追踪横切（与 answer 一致）。
_CHAIN = default_chain()  # 中间件链

#: 模型请求重试策略：只重试瞬时类，配置错不试（避免白等）。
_RETRY = RetryPolicy(max_retries=3, base_delay_s=0.5, max_delay_s=4.0, jitter_s=0.1)  # 重试策略


def _calls_of(reply: Any) -> list[dict[str, Any]]:  # 取工具调用
    """从 AIMessage.tool_calls 抽成普通 dict 列表（隐藏实现细节）。"""
    calls = getattr(reply, "tool_calls", None) or []  # 原始调用
    out: list[dict[str, Any]] = []  # 结果
    for item in calls:  # 逐个
        out.append({  # 复刻
            "id": str(item.get("id") or item.get("tool_call_id") or ""),  # 调用 id
            "name": str(item.get("name") or ""),  # 工具名
            "args": dict(item.get("args") or item.get("arguments") or {}),  # 参数
        })  # 追加结束
    return out  # 返回


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


def _make_call(bound: Any) -> Any:  # 造最内层模型调用
    """把 dict 消息转成 LangChain、调绑定工具的模型、回 ModelResponse。

    对瞬时错误（限流/5xx/超时/连接）做分类重试（指数退避），配置错不试；重试发事件给前端。
    """
    async def call(request: ModelRequest) -> ModelResponse:  # 最内层调用
        ctx = current_run()  # 当前 run（发重试事件用）
        async def once() -> Any:  # 单次请求
            return await bound.ainvoke(_to_lc(request.messages, request.system))  # 带工具调
        def on_retry(info: Any) -> None:  # 每次重试回调
            if ctx is not None:  # 有 run 才发
                ctx.emit(  # 重试事件
                    retry_event(  # 构造
                        info.attempt,  # 第几次
                        info.max_attempts,  # 总共
                        info.error_kind,  # 类别
                        error_message=info.error_message,  # 原文
                        delay_ms=info.delay_ms,  # 等待
                        scope=info.scope,  # 作用域
                    )  # 构造结束
                )  # emit 结束
        reply = await retry_async(once, _RETRY, on_retry=on_retry, scope="model")  # 分类重试
        return ModelResponse(  # 响应
            text=_text_of(getattr(reply, "content", "")),  # 正文
            tool_calls=_calls_of(reply),  # 工具调用
            usage=_usage_of(reply),  # 用量
        )  # 响应结束
    return call  # 返回


def _make_fallback_call(evidence: list[dict[str, Any]]) -> Any:  # 无模型的最内层调用
    """模型不可用时回退原文；仍让调用点走中间件链（保持与 answer 一致的可观测）。"""
    async def call(request: ModelRequest) -> ModelResponse:  # 最内层调用
        del request  # 不回模型
        return ModelResponse(text=extractive(evidence))  # 回退原文
    return call  # 返回


async def _exec_tool(name: str, args: dict[str, Any], evidence: list[dict[str, Any]]) -> str:  # 执行单个工具
    """按名分派；统一把 core.Result 渲染成 JSON 喂模型；检索工具把证据累进 evidence。

    每个工具执行包一个“查询中”步骤（running → done/failed）让前端看到进度；
    结束后对此次调用做可观测记录：trace 打点 + run 级汇总（耗时/成败/缓存命中/错误码）。
    """
    started = time.perf_counter()  # 记录开始（耗时）
    label = _TOOL_LABELS.get(name) or f"调用 {name}"  # 步骤标题（含“查询中”语义）
    envelope: Any = None  # 统一结果（各分支都给）
    with stage(label, group=StageGroup.TOOL, name=str(name), detail=f"{label}…") as info:  # 查询中
        if name == "search_knowledge_base":  # 知识库检索
            try:  # 工具自身失败转带内
                envelope = await search_knowledge_base.ainvoke(args)  # 统一结果
            except Exception as exc:  # 抛了
                envelope = err(str(ErrorCode.EXECUTION_FAILED), f"检索执行失败：{exc}", retryable=True)  # 带内错误
            items = (getattr(envelope, "data", None) or {}).get("evidence") or []  # 本次证据
            for item in items:  # 逐条证据
                row = dict(item)  # 复制
                row["index"] = len(evidence) + 1  # 重新编号（引用校验按 1 起）
                evidence.append(row)  # 累加
            info.detail = f"检索到 {len(items)} 段"  # 明细
            info.detail = f"{info.detail}（命中缓存）" if bool((envelope.meta or {}).get("cached")) else info.detail  # 命中提示
        else:  # 其他工具
            tool = {t.name: t for t in _TOOLS}.get(name)  # 按名取
            if tool is None:  # 不认识（防御）
                envelope = err(str(ErrorCode.PROTOCOL_TOOL_NOT_FOUND), f"工具 {name} 不存在")  # 带内错误
                info.detail = "工具不存在"  # 明细
            else:  # 正常工具
                try:  # 工具自身失败转带内
                    envelope = await tool.ainvoke(args)  # 统一结果
                except Exception as exc:  # 抛了
                    envelope = err(str(ErrorCode.EXECUTION_FAILED), f"{name} 执行失败：{exc}", retryable=True)  # 带内错误
                info.detail = "完成"  # 明细
                if bool((envelope.meta or {}).get("cached")):  # 命中缓存
                    info.detail = "完成（命中缓存）"  # 提示秒回
    # ---- 可观测：trace 打点 + run 级汇总 ----
    ms = int((time.perf_counter() - started) * 1000)  # 本次耗时
    cached = bool((envelope.meta or {}).get("cached"))  # 缓存命中
    args_chars = len(json.dumps(args, ensure_ascii=False))  # 入参体积
    output_chars = len(render(envelope))  # 喂模型的输出体积
    trace_tool(  # 写进 trace（span kind=tool）
        name,  # 工具名
        ok=bool(envelope.ok),  # 成败
        code=str(envelope.code or ""),  # 错误码
        retryable=bool(envelope.retryable),  # 可重试
        cached=cached,  # 缓存命中
        args_chars=args_chars,  # 入参体积
        output_chars=output_chars,  # 输出体积
        error_message="" if envelope.ok else str(envelope.message or "")[:300],  # 错误摘要
    )  # 结束
    ctx = current_run()  # run 上下文（汇总用）
    if ctx is not None:  # 有 run
        ctx.tool_calls.append(  # 记录到 run 级
            {  # 一条
                "name": name,  # 工具名
                "ok": bool(envelope.ok),  # 成败
                "cached": cached,  # 缓存命中
                "ms": ms,  # 耗时
                "code": str(envelope.code or ""),  # 错误码
            }  # 一条结束
        )  # 追加结束
    return render(envelope)  # 统一 Result 的 JSON 喂模型


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
    bound = _bound_model()  # 带工具的模型（不可用为 None）
    evidence: list[dict[str, Any]] = list(state.get("evidence") or [])  # 播种证据（rag 已取）+ 工具新取
    answer_text = ""  # 最终回答
    note = ""  # 说明（只进 state）
    note_kind = "plain"  # 说明样式（失败类置 error）
    usage: dict[str, Any] = {}  # 用量（只进 state）
    citations: list[dict[str, Any]] = []  # 引用（只进 state）
    settings: dict[str, Any] = {}  # 本轮设置
    usage_total: dict[str, int] = {}  # 累计用量
    msgs = [dict(m) for m in messages]  # 可变的 dict 消息（历史 + 问题）

    # 最内层调用：模型可用则带工具，不可用则回退原文（仍走中间件链，与 answer 一致）
    call = _make_call(bound) if bound is not None else _make_fallback_call(evidence)  # 最内层
    if bound is None:  # 无模型
        note = "模型未配置，以下为资料原文。"  # 说明
        note_kind = "error"  # 错误样式
    with stage("工具决策", group=StageGroup.TOOL) as info:  # 步骤
        for _round in range(MAX_TOOL_ROUNDS):  # 每轮决策
            ctx.check_cancelled()  # 可取消
            request = ModelRequest(model="llm", messages=list(msgs), system=system)  # 请求
            try:  # 中间件/模型失败回退
                response = await _CHAIN.model_call(ctx, request, call)  # 走中间件链
            except RunCancelled:  # 取消穿透
                raise  # 交给上层
            except Exception:  # 其他失败
                answer_text = extractive(evidence)  # 回退原文
                note = "模型调用失败，以下为资料原文。"  # 说明
                note_kind = "error"  # 错误样式
                break  # 退出循环
            call_items = getattr(response, "tool_calls", None) or []  # 工具调用
            if not call_items:  # 不再要工具
                answer_text = response.text  # 这就是最终回答
                usage_total = dict(getattr(response, "usage", None) or {})  # 用量
                break  # 收尾
            for call_item in call_items:  # 逐个工具
                content = await _exec_tool(call_item["name"], call_item["args"], evidence)  # 执行
                msgs.append({"role": "assistant", "content": "", "tool_calls": [call_item]})  # 助手调用
                msgs.append(  # 工具结果
                    {"role": "tool", "content": content, "tool_call_id": call_item["id"], "name": call_item["name"]},  # 结果
                )  # 追加结束
            info.detail = f"已执行 {len(evidence)} 条引用"  # 明细
        else:  # 轮次用尽没说话
            if not answer_text:  # 避免覆盖已回退内容
                answer_text = "多次尝试后仍未完成回答，请补充信息或稍后再试。"  # 兜底
                note = "工具循环达到上限。"  # 说明
                note_kind = "error"  # 错误样式

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
    if cleaned:  # 有正文
        ctx.emit(delta_event(cleaned))  # 一次性发全文（非流式）
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