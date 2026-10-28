"""answer 节点：经中间件拼上下文、流式生成、校验引用。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

from langchain_core.runnables import RunnableConfig  # 图配置

from app.agent.context import RunCancelled, ctx_from_config  # 上下文
from app.agent.contexts import AgentContextSpec  # 上下文声明
from app.agent.contexts import build as assemble_context  # 上下文装配器
from app.agent.contexts.history import messages_from  # 轮次 -> 消息
from app.agent.contracts import StageGroup, Turn  # 步骤分组/轮次
from app.agent.events import citation as citation_event  # 引用事件
from app.agent.events import conflict as conflict_event  # 冲突事件
from app.agent.events import context as context_event  # 上下文事件
from app.agent.events import delta as delta_event  # 增量事件
from app.agent.events import reasoning as reasoning_event  # 思考事件
from app.agent.events import replace as replace_event  # 整段替换
from app.agent.events import settings as settings_event  # 设置事件
from app.agent.events import usage as usage_event  # 用量事件
from app.agent.events import verify_report as verify_event  # 校验结论
from app.agent.fsm.states import RunEvent  # 状态事件
from app.agent.llm import DEFAULT_PARAMS, ModelGateway  # 模型网关（B）
from app.agent.middleware import ModelRequest, ModelResponse, default_chain  # 中间件
from app.agent.stages import stage  # 步骤事件
from app.agent.streaming import aclose  # 关闭上游流
from app.core.llm import store as llm_store  # 模型名
from app.rag.answer import extractive  # 原文回退
from app.rag.verify import store_lookup, verify  # 引用校验（L1 + L3）
from app.services import attachments as attach_service  # 附件读取（别名，避开局部同名）

_CHAIN = default_chain()  # 进程级中间件链

#: 答题 agent 的上下文声明：默认 S1/S2 即 `system_base` + `answer_rules`；暂不注入静态环境/事实。
ANSWER_SPEC = AgentContextSpec(agent="answer")  # 声明

#: 答题参数：共用默认（温度 0.2 / 超时 60s）+ 本调用点只把流式打开。
ANSWER_PARAMS = DEFAULT_PARAMS.with_overrides(streaming=True)  # 答题参数

#: 模型在正文末尾追加的冲突标记：只给后端看，不能流到前端。
_CONFLICT_MARKER = "[[资料冲突]]"  # 标记文本


class _MarkerGuard:  # 流式过滤器
    """扣住尾部，保证冲突标记一个字都不会漏给前端。

    为什么不能"等流结束再 split"：标记是按 token 流出去的，用户会先看到 ``[[资料``。
    扣住「标记长度 - 1」个字符就够——任何还没拼完整的标记都留在这个尾巴里。
    """

    def __init__(self, marker: str) -> None:  # 构造
        self._marker = marker  # 要拦的标记
        self._pending = ""  # 已收到、还没放行的尾巴
        self._stopped = False  # 是否已经遇到标记

    def feed(self, text: str) -> str:  # 喂一个 token
        """返回现在可以安全发出的部分（可能为空串）。"""
        if self._stopped:  # 标记之后的内容不属于正文
            return ""  # 全部扣住（正文已在 split 时处理）
        self._pending += text  # 先攒
        cut = self._pending.find(self._marker)  # 找完整标记
        if cut >= 0:  # 拼出来了
            text = self._pending[:cut]  # 标记之前才是正文
            self._pending = ""  # 已经交出去，收尾别再发一次
            self._stopped = True  # 之后不再发
            return text  # 立即放行
        hold = len(self._marker) - 1  # 需要扣住的长度
        if hold <= 0:  # 标记只有一个字符（防御）
            text, self._pending = self._pending, ""  # 全部放行
            return text  # 返回
        if len(self._pending) <= hold:  # 还不够长
            return ""  # 继续扣
        safe = self._pending[:-hold]  # 尾部可能正在拼标记，不能发
        self._pending = self._pending[-hold:]  # 只留尾巴
        return safe  # 放行安全部分

    def flush(self) -> str:  # 收尾
        """流结束时把扣住的都放出去（此时不可能再有标记）。"""
        text = self._pending  # 取尾巴
        self._pending = ""  # 清空
        return text  # 返回


def _evidence_body(evidence: list[dict[str, Any]]) -> str:  # 证据正文
    """`[n] 标题\n正文`，与引用角标一一对应（会作为 search_kb 的结果回传）。"""
    parts: list[str] = []  # 块列表
    for item in evidence:  # 逐条
        heading = str(item.get("heading") or item.get("origin") or "资料")  # 标题
        parts.append(f"[{item.get('index')}] {heading}\n{item.get('text')}")  # 一条
    return "\n\n".join(parts)  # 合并


def _fragments(  # 组装引用
    used: list[int],  # 序号
    evidence: list[dict[str, Any]],  # 证据
    *,
    verified: bool = True,  # 是否通过溯源校验
) -> list[dict[str, Any]]:  # 片段
    """与前端 Fragment 字段一致。"""
    result: list[dict[str, Any]] = []  # 结果
    for index in used:  # 被引用的序号
        item = evidence[index - 1]  # 对应证据
        result.append({  # 片段
            "index": item.get("index"),  # 序号
            "source_name": item.get("source_name", "资料"),  # 来源
            "origin": item.get("origin", "资料"),  # 出处
            "score": item.get("score", 0.0),  # 分数
            "score_kind": item.get("score_kind", "rrf"),  # 分数语义
            "text": item.get("text", ""),  # 正文
            "snippet": item.get("snippet", ""),  # 命中片段
            "selected": True,  # 被引用
            "verified": verified,  # 是否通过溯源校验
            "doc_id": int(item.get("doc_id") or 0),  # 文档 id（引用校验要回查）
            "chunk_id": int(item.get("chunk_id") or 0),  # 命中片段 id
            "parent_id": int(item.get("parent_id") or 0),  # 父块 id
            "version": int(item.get("version") or 0),  # 索引版本
            "text_hash": str(item.get("text_hash") or ""),  # 内容指纹
        })  # 追加
    return result  # 返回


async def answer_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:  # 生成节点
    """有证据才生成；流式 token 直接发事件。"""
    ctx = ctx_from_config(config)  # 运行上下文
    evidence = list(state.get("evidence") or [])  # 证据
    attachments = list(state.get("attachments") or [])  # 附件（正文不入 state）
    readable_files = any(attach_service.load_text(item).strip() for item in attachments)  # 可读附件（按 disk）
    question = str(state.get("question") or "")  # 问题
    if ctx is None:  # 不在新链路
        return {}  # 不处理
    if not evidence and not readable_files:  # 没有证据也没有附件
        ctx.note = "没有在本地资料里找到相关内容，换个说法试试。"  # 说明
        return {"note": ctx.note}  # 直接给说明

    tool_items: list[dict[str, Any]] = []  # V1 工具结果（检索 = 一次 search_kb 调用）
    if evidence:  # 有证据
        tool_items.append(  # 加一条检索结果
            {"call_id": "kb", "name": "search_kb", "status": "ok", "body": _evidence_body(evidence)}  # 构造
        )  # 追加结束
    attach_items = [  # V2 上传资料
        {"name": str(item.get("name") or "附件"), "text": attach_service.load_text(item)}  # 单条（正文现读）
        for item in attachments  # 遍历
        if attach_service.load_text(item).strip()  # 只要可读
    ]  # 列表结束
    built = assemble_context(  # 装配
        ANSWER_SPEC,  # 声明
        history=messages_from([Turn.model_validate(item) for item in (state.get("turns") or [])]),  # F3 最近轮次（只喂 done）
        summary_text=str(state.get("summary") or ""),  # F2 压缩摘要（投影）
        tool_results=tool_items,  # V1
        attachments=attach_items,  # V2
        question=question,  # V3
    )
    system, messages = built.system, built.messages  # 取出 system 与消息
    ctx.context_usage = built.usage  # 记录分块占用
    ctx.emit(  # 上下文占用事件（大板块/小板块/最细一并给前端）
        context_event(  # 构造
            len(messages),  # 轮数（消息数）
            built.usage.total,  # 总 token
            trimmed=0,  # 裁剪数（C3 接）
            window=built.usage.window,  # 窗口
            tiers=built.usage.tiers,  # 大板块
            blocks=built.usage.blocks,  # 小板块
            subblocks=built.usage.subblocks,  # 最细一层
            prompt_versions=built.usage.prompt_versions,  # 模板版本
        )  # 事件结束
    )  # emit 结束
    request = ModelRequest(model="llm", messages=messages, system=system)  # 请求
    model_name = "未配置模型"  # 默认
    try:  # 读模型名给设置弹窗
        row = llm_store.get("llm")  # 槽位
        model_name = str(row["model"]) if row is not None and row["model"] else model_name  # 模型
    except Exception:  # 读不到不阻断
        pass  # 保持默认
    temperature = float(ANSWER_PARAMS.temperature or 0.0)  # 本轮温度（调用处声明，不写死）
    ctx.settings = {  # 本轮设置
        "model": model_name,  # 模型
        "temperature": temperature,  # 温度
        "system_prompt": system,  # 系统提示词
        "evidence": len(evidence),  # 证据数
        "top_k": len(evidence),  # 引用上限
    }  # 设置结束
    ctx.emit(settings_event(model_name, temperature, system, {"evidence": len(evidence)}))  # 事件

    gateway = ModelGateway(slot="llm", emit=ctx.emit)  # 模型网关（重试事件直达前端）

    async def call(req: ModelRequest) -> ModelResponse:  # 真实模型调用
        """没有配置模型就用原文；配置了就走流式（首 token 前自动重试）。"""
        if not gateway.available(ANSWER_PARAMS):  # 槽位没配全
            return ModelResponse(text=extractive(evidence))  # 原文

        async def token_stream():  # token 流
            """只吐非空正文；顺手收集思考与用量。"""
            async for chunk in gateway.astream(req, ANSWER_PARAMS):  # 网关逐块
                if chunk.reasoning:  # 有思考
                    ctx.reasoning += chunk.reasoning  # 累积
                    ctx.emit(reasoning_event(chunk.reasoning))  # 发给前端
                for key, value in chunk.usage.items():  # 用量明细
                    ctx.usage[key] = int(value)  # 记录（键与旧口径一致）
                if chunk.text:  # 有正文
                    yield chunk.text  # 放行

        return ModelResponse(stream=token_stream())  # 返回流

    generated = False  # 正文是否由模型生成（决定"无引用"要不要降级）
    with stage("生成回答", group=StageGroup.GENERATE) as info:  # 生成步骤
        try:  # 模型/中间件失败回退原文
            response = await _CHAIN.model_call(ctx, request, call)  # 调链
            if response.stream is not None:  # 流式
                generated = True  # 确实是模型生成的
                guard = _MarkerGuard(_CONFLICT_MARKER)  # 扣住冲突标记
                try:  # 无论正常结束还是取消，都要把尾巴放出去
                    async for token in response.stream:  # 逐 token
                        ctx.check_cancelled()  # 可停止
                        ctx.mark_first_token()  # 首 token
                        ctx.partial_answer += token  # 累积（含标记，收尾 split 要用）
                        safe = guard.feed(token)  # 过滤未拼完的标记
                        if safe:  # 有可发的
                            ctx.emit(delta_event(safe))  # 发事件
                finally:  # 取消/失败也走这里
                    rest = guard.flush()  # 放掉扣住的尾巴
                    if rest:  # 还有剩
                        ctx.emit(delta_event(rest))  # 发完再抛
                    await aclose(response.stream)  # 关整条包装链（一路传到 HTTP）
                answer = ctx.partial_answer  # 最终文本（含标记，下面会拆）
            else:  # 非流式（未配置模型）
                answer = response.text  # 文本
                ctx.partial_answer = answer  # 累积
                if answer:  # 有内容
                    ctx.emit(delta_event(answer))  # 一次性发
        except RunCancelled:  # 用户取消
            raise  # 交给 runtime 收敛
        except Exception:  # 模型失败
            if ctx.partial_answer:  # 已有部分输出
                answer = ctx.partial_answer  # 保留
                ctx.note = "模型生成中断，以上为已生成部分。"  # 说明
            else:  # 没有输出
                answer = extractive(evidence)  # 回退原文
                ctx.partial_answer = answer  # 记录
                ctx.note = "模型生成失败，以下为资料原文。"  # 说明
                ctx.emit(delta_event(answer))  # 发原文
        info.detail = f"生成 {len(answer)} 字"  # 明细

    marker = _CONFLICT_MARKER  # 模型冲突标记（与流式过滤器同一个常量）
    conflict = ""  # 冲突说明
    if marker in answer:  # 有标记
        answer, conflict = answer.split(marker, 1)  # 拆出
        answer = answer.strip()  # 清正文
        conflict = conflict.strip()  # 清说明
        ctx.conflict = conflict  # 记录
        ctx.emit(conflict_event(conflict))  # 提示前端
    result = verify(answer, evidence, lookup=store_lookup)  # 引用校验：L1 编号 + L3 溯源
    cleaned, used = result.text, result.valid  # 清理后正文与有效编号
    fallback = extractive(evidence)  # 资料原文（多处降级都用它）
    if generated and answer.strip() and not used and answer.strip() != fallback.strip():  # 模型答了但没有能溯源的引用
        ctx.note = "模型没给出能溯源的引用，已改为资料原文。"  # 说明
        ctx.partial_answer = fallback  # 覆盖已生成部分（落库/重画都用它）
        cleaned = fallback  # 正文换成原文
        ctx.emit(replace_event(fallback))  # 整段替换（正文已经流出去了）
    elif result.missing:  # 有引用但资料已不可用
        ctx.note = "部分引用的资料已不可用，相关角标已移除。"  # 说明
    if result.unsupported and not ctx.note:  # 有句子没被证据支撑
        ctx.note = "部分内容未能在资料中找到出处，请对照引用原文核对。"  # 只提示，不删句
    fragments = _fragments(used, evidence)  # 有效引用
    if result.missing:  # 失效的也列出来（标为未验证），让用户看得到
        fragments += _fragments(result.missing, evidence, verified=False)  # 追加
    ctx.citations = fragments  # 记录
    ctx.usage["completion_chars"] = len(cleaned)  # 简单用量
    usage_extra = {  # 缓存/推理明细
        key: value  # 键值
        for key, value in ctx.usage.items()  # 遍历
        if key in {"cache_read_tokens", "cache_creation_tokens", "reasoning_tokens"}  # 只带明细
    }  # 明细结束
    if not cleaned.strip():  # 模型没吐内容
        ctx.note = "模型没有返回内容，请稍后重试。"  # 说明
    if fragments:  # 有引用
        ctx.emit(citation_event(fragments))  # 发引用
    ctx.emit(  # 校验结论（前端据此把角标标灰/标"资料已不可用"）
        verify_event(  # 构造
            result.valid,  # 有效
            result.invalid,  # 非法
            result.missing,  # 失效
            result.coverage,  # 覆盖率
            result.reasons,  # 原因
            result.unsupported,  # 未支撑句
        )  # 构造结束
    )  # 发事件
    ctx.emit(  # 用量事件
        usage_event(  # 构造
            int(ctx.usage.get("prompt_tokens") or 0),  # 输入
            int(ctx.usage.get("completion_tokens") or len(cleaned)),  # 输出
            extra=usage_extra,  # 明细
        )  # 构造结束
    )  # 发事件
    ctx.transition(RunEvent.GENERATION_DONE)  # 生成完成
    return {  # 写回
        "answer": cleaned,  # 答案
        "fragments": fragments,  # 引用
        "note": ctx.note,  # 说明
        "usage": ctx.usage,  # 用量
    }  # 返回结束
