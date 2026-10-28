"""流式三处护栏：终态唯一、冲突标记不外泄、脱敏不被 chunk 切断。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 事件循环

import pytest  # 测试框架

from app.agent import events  # 事件构造器
from app.agent import stream as stream_mod  # 事件出口
from app.agent.context import RunCancelled, RunContext  # 上下文
from app.agent.fsm.states import RunEvent  # 状态事件
from app.agent.middleware import CancelMiddleware, MiddlewareChain  # 取消中间件
from app.agent.middleware.base import ModelRequest, ModelResponse  # 中间件契约
from app.agent.middleware.pii import PIIMiddleware  # 脱敏中间件
from app.agent.nodes.answer import node as answer_mod  # 冲突标记过滤器
from app.rag.verify import TraceSnapshot  # 溯源快照

EVIDENCE = [  # 一条证据
    {  # 证据字典
        "index": 1,  # 序号
        "origin": "第 1 段",  # 出处
        "source_name": "资料",  # 来源
        "score": 0.1,  # 分数
        "score_kind": "rrf",  # 分数语义
        "text": "证据正文",  # 正文
        "snippet": "证据",  # 命中段
        "doc_id": 3,  # 文档（引用校验要溯源）
        "chunk_id": 11,  # 片段
        "parent_id": 7,  # 父块
        "version": 2,  # 版本
        "text_hash": "hash-3",  # 指纹
    }  # 证据结束
]  # 列表结束

_SNAPSHOT = TraceSnapshot(  # 一切正常
    documents={3: {"text_hash": "hash-3", "stored_path": "a.docx"}},  # 文档
    chunks={11: {"id": 11}},  # 片段
    active={3: 2},  # 版本
    files={3: True},  # 文件在
)  # 快照结束


def test_sink_drops_events_after_terminal() -> None:  # 终态唯一
    """done 之后再来的 error 必须被丢掉，否则界面会停在失败态。"""
    async def main() -> list:  # 异步壳
        """在事件循环里造队列。"""
        queue: asyncio.Queue = asyncio.Queue()  # 队列
        sink = stream_mod._EventSink(queue)  # 出口
        sink(events.delta("半句"))  # 正文
        sink(events.done(10, 20))  # 终态
        sink(events.error("late", "收尾又炸了"))  # 迟到的错误
        sink(events.cancelled())  # 迟到的取消
        return [queue.get_nowait() for _ in range(queue.qsize())]  # 全部取出

    collected = asyncio.run(main())  # 跑
    assert [event.type for event in collected] == [events.EventType.DELTA, events.EventType.DONE]  # 只剩两帧


def test_marker_never_leaks_across_tokens() -> None:  # 标记不流出去
    """标记被切在两个 token 里时，前端也不该看到半截。"""
    guard = answer_mod._MarkerGuard(answer_mod._CONFLICT_MARKER)  # 过滤器
    sent = "".join(  # 拼出所有放行的文本
        guard.feed(token)  # 逐 token 喂
        for token in ("答案", " C [1]\n[[资", "料冲突]] 不一致")  # 三段
    )  # 拼接结束
    sent += guard.flush()  # 收尾
    assert "[[资料" not in sent  # 没有半截标记
    assert "[[资料冲突]]" not in sent  # 没有完整标记
    assert sent == "答案 C [1]\n"  # 正文完整、标记之后的不算正文


def test_marker_guard_delays_tail_then_flushes() -> None:  # 尾巴不丢
    """普通文本只是被扣住几个字符，收尾必须全部放出来。"""
    guard = answer_mod._MarkerGuard(answer_mod._CONFLICT_MARKER)  # 过滤器
    sent = guard.feed("hello world")  # 喂一句
    sent += guard.flush()  # 收尾
    assert sent == "hello world"  # 一个字都不少


def test_pii_is_caught_when_split_across_chunks() -> None:  # 跨 chunk 脱敏
    """手机号被切成两半时也必须脱敏（逐 chunk 匹配会漏）。"""
    async def tokens():  # 假模型流
        """把号码拆到两个 chunk。"""
        yield "电话 1380013"  # 前半
        yield "8000 结束"  # 后半

    async def main() -> str:  # 异步壳
        """跑一次中间件并收集输出。"""
        async def call_next(_req: ModelRequest) -> ModelResponse:  # 假模型
            """直接返回流。"""
            return ModelResponse(stream=tokens())  # 流

        middleware = PIIMiddleware()  # 中间件
        ctx = RunContext(run_id="r", question="q")  # 上下文
        response = await middleware.wrap_model_call(ctx, ModelRequest(model="llm", messages=[]), call_next)  # 调
        assert response.stream is not None  # 有流
        return "".join([chunk async for chunk in response.stream])  # 全部收下

    out = asyncio.run(main())  # 跑
    assert "13800138000" not in out  # 号码不出现
    assert "[REDACTED:phone]" in out  # 已脱敏
    assert "结束" in out  # 正文没丢


def test_normal_stream_is_not_duplicated(fake_root, monkeypatch) -> None:  # 正常流
    """扣尾再放行不能把文字发两遍（这是过滤器最容易写错的地方）。"""
    class _Chain:  # 假链
        """只吐两段，没标记。"""

        async def model_call(self, ctx, request, call):  # 调模型
            """返回假流。"""
            async def tokens():  # 生成器
                """两段正文。"""
                yield "答案"  # 第一段
                yield " [1]"  # 第二段

            return ModelResponse(stream=tokens())  # 流

    monkeypatch.setattr(answer_mod, "_CHAIN", _Chain())  # 替换链
    monkeypatch.setattr(answer_mod, "store_lookup", lambda items: _SNAPSHOT)  # 溯源正常
    ctx = RunContext(run_id="r", question="q")  # 上下文
    ctx.transition(RunEvent.START)  # 开始
    ctx.transition(RunEvent.CONTEXT_LOADED)  # 上下文
    ctx.transition(RunEvent.RETRIEVAL_DONE)  # 有证据
    sent: list[str] = []  # 收到的事件
    ctx.emit = lambda event: sent.append(str(getattr(event.payload, "text", "") or ""))  # 只记文本
    result = asyncio.run(  # 跑节点
        answer_mod.answer_node({"question": "q", "evidence": EVIDENCE}, {"configurable": {"run_ctx": ctx}})  # 调用
    )  # 结束
    assert "".join(sent) == "答案 [1]"  # 一字不多一字不少
    assert result.get("answer") == "答案 [1]"  # 落库文本一致


def test_cancel_closes_upstream(fake_root, monkeypatch) -> None:  # 取消关上游
    """取消要一路关到最里层：否则模型还在生成、还在计费。"""
    class _Chunk:  # 假 chunk
        """answer 节点只读 content / additional_kwargs / usage_metadata。"""

        def __init__(self, text: str) -> None:  # 构造
            self.content = text  # 正文
            self.additional_kwargs: dict = {}  # 额外字段
            self.usage_metadata = None  # 用量

    class _Source:  # 假上游流
        """记录自己有没有被关闭。"""

        def __init__(self, ctx: RunContext) -> None:  # 构造
            self.ctx = ctx  # 上下文
            self.closed = False  # 是否已关

        async def gen(self):  # 生成器
            """吐一段后请求取消。"""
            try:  # 关闭时置位
                yield _Chunk("答案")  # 第一段
                self.ctx.request_cancel()  # 请求取消
                yield _Chunk(" [1]")  # 第二段（到不了前端）
            finally:  # 被关闭/耗尽
                self.closed = True  # 记录

    class _Model:  # 假模型
        """astream 直接返回上游流。"""

        def __init__(self, source: _Source) -> None:  # 构造
            self._source = source  # 上游

        def astream(self, messages):  # 流式接口
            """忽略消息，返回上游生成器。"""
            return self._source.gen()  # 生成器

    ctx = RunContext(run_id="r", question="q")  # 上下文
    ctx.transition(RunEvent.START)  # 开始
    ctx.transition(RunEvent.CONTEXT_LOADED)  # 上下文
    ctx.transition(RunEvent.RETRIEVAL_DONE)  # 有证据
    source = _Source(ctx)  # 上游
    monkeypatch.setattr(  # 模型入口现在是网关，假模型挂在网关的工厂上
        "app.agent.llm.gateway.chat_model", lambda **kwargs: _Model(source)  # 假模型
    )  # 替换结束
    monkeypatch.setattr(  # 用真中间件链，验证每层都能把关闭传下去
        answer_mod, "_CHAIN", MiddlewareChain([CancelMiddleware(), PIIMiddleware()])  # 两层包装
    )  # 替换结束
    with pytest.raises(RunCancelled):  # 期望取消
        asyncio.run(  # 跑节点
            answer_mod.answer_node({"question": "q", "evidence": EVIDENCE}, {"configurable": {"run_ctx": ctx}})  # 调用
        )  # 结束
    assert source.closed is True  # 最上游被关掉
    assert ctx.partial_answer == "答案"  # 已生成部分保留
