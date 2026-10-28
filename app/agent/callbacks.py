"""追踪回调：用 LangChain `BaseCallbackHandler` 把调用转成 trace span（类 D · D2）。

设计要点：
- **只观察、不改业务**；`raise_error = False` 保证回调自身出错不会把回答搞挂；
- 每次回调只做一次 `add_span/finish_span`（内部已 fail-open 落库）；
- 父子关系优先用 LangChain 的 `parent_run_id`，没有就挂到当前节点 span 下；
- 不记录 prompt/messages 原文（隐私）；只记大小与元信息。
"""

from __future__ import annotations  # 类型注解延迟求值

import time  # 时间戳
from typing import Any  # 任意值

from langchain_core.callbacks import BaseCallbackHandler  # 框架回调基类
from loguru import logger  # 日志

from app.core import trace as trace_store  # 落库
from app.core.trace import writer as trace_writer  # 异步写
from app.core.trace.recorder import current_trace  # 当前 trace 会话

__all__ = ["TraceCallbacks"]  # 对外导出


def _name_of(serialized: Any, fallback: str) -> str:  # 取名字
    """从 serialized（dict）里找 name/id，都没有就用 fallback。"""
    if isinstance(serialized, dict):  # 常见
        name = serialized.get("name") or serialized.get("id")  # 名字
        if name:  # 有
            return str(name)[-80:]  # 截断
    return fallback  # 回退


class TraceCallbacks(BaseCallbackHandler):  # 追踪回调
    """把 chain / llm / tool / retriever 的起止写成 span。"""

    raise_error = False  # 回调异常不冒泡（fail-open）

    def __init__(self) -> None:  # 构造
        super().__init__()  # 框架基类初始化
        self._session = current_trace()  # 当前 trace 会话
        self._spans: dict[str, int] = {}  # langchain run_id → span_id
        self._tokens: dict[str, int] = {}  # 首个 token 时间（算 TTFT）

    # ---- 内部 ----------------------------------------------------------
    def _parent(self, parent_run_id: Any) -> int | None:  # 找父 span
        key = str(parent_run_id) if parent_run_id else ""  # 键
        if key and key in self._spans:  # 回调内部父子
            return self._spans[key]  # 父
        if self._session is None:  # 没会话
            return None  # 无
        stack = getattr(self._session, "_stack", [])  # 当前节点 span 栈
        return stack[-1] if stack else None  # 挂到当前节点下

    def _start(self, run_id: Any, parent_run_id: Any, name: str, kind: str, inputs: dict | None) -> None:  # 开 span
        try:  # fail-open
            if self._session is None:  # 没 trace
                return  # 跳过
            span = trace_store.add_span(  # 落一条 span
                self._session.run_id,  # 运行
                name,  # 名称
                kind,  # 类别
                parent_span_id=self._parent(parent_run_id),  # 父
                input=inputs,  # 输入摘要
                started_at=time.time(),  # 开始
            )
            if span is not None:  # 有 id
                self._spans[str(run_id)] = int(span)  # 记
        except Exception as exc:  # 回调失败
            logger.opt(exception=exc).warning("trace 回调 start 失败（{}）", name)  # 只记

    def _finish(self, run_id: Any, *, status: str = "ok", output: dict | None = None, error: BaseException | None = None) -> None:  # 关 span
        try:  # fail-open
            span = self._spans.pop(str(run_id), None)  # 取
            if span is None:  # 没开过
                return  # 跳过
            extra: dict[str, Any] = {}  # 附加
            start = self._tokens.pop(str(run_id), 0)  # 首 token 时刻
            if start:  # 有
                extra["ttft_ms"] = int((time.time() - start) * 1000)  # TTFT
            payload = {**(output or {}), **extra}  # 合并输出
            trace_writer.submit(  # 异步写入（不阻塞 token 流）
                lambda: trace_store.finish_span(span, status=status, output=payload, error=error)
            )
        except Exception as exc:  # 回调失败
            logger.opt(exception=exc).warning("trace 回调 finish 失败")  # 只记

    # ---- LLM -----------------------------------------------------------
    def on_llm_start(self, serialized: Any, prompts: Any, **kwargs: Any) -> None:  # 文本模型开始
        self._start(kwargs.get("run_id"), kwargs.get("parent_run_id"), _name_of(serialized, "llm"), "llm", {"prompts": len(prompts or [])})  # 开

    def on_chat_model_start(self, serialized: Any, messages: Any, **kwargs: Any) -> None:  # 对话模型开始
        self._start(kwargs.get("run_id"), kwargs.get("parent_run_id"), _name_of(serialized, "chat_model"), "llm", {"messages": len(messages or [])})  # 开

    def on_llm_new_token(self, token: str, **kwargs: Any) -> None:  # noqa: ARG002 - 新 token 只看 run_id
        try:  # fail-open
            key = str(kwargs.get("run_id") or "")  # 键
            if key and key not in self._tokens:  # 首个
                self._tokens[key] = time.time()  # 记 TTFT 起点
        except Exception:  # 忽略
            pass  # 不阻断

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:  # 结束
        output: dict[str, Any] = {}  # 输出摘要
        try:  # 取用量
            llm_output = getattr(response, "llm_output", None) or {}  # 元信息
            usage = llm_output.get("token_usage") or llm_output.get("usage") or {}  # 用量
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):  # 常见键
                if key in usage:  # 有
                    output[key] = usage[key]  # 记
        except Exception:  # 忽略
            pass  # 不阻断
        self._finish(kwargs.get("run_id"), output=output)  # 关

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:  # 失败
        self._finish(kwargs.get("run_id"), status="error", error=error)  # 关

    # ---- Chain ---------------------------------------------------------
    def on_chain_start(self, serialized: Any, inputs: Any, **kwargs: Any) -> None:  # noqa: ARG002 - 只记名字
        self._start(kwargs.get("run_id"), kwargs.get("parent_run_id"), _name_of(serialized, "chain"), "chain", None)  # 开

    def on_chain_end(self, outputs: Any, **kwargs: Any) -> None:  # noqa: ARG002 - 不记链输出
        self._finish(kwargs.get("run_id"))  # 关

    def on_chain_error(self, error: BaseException, **kwargs: Any) -> None:  # 链失败
        self._finish(kwargs.get("run_id"), status="error", error=error)  # 关

    # ---- Tool ----------------------------------------------------------
    def on_tool_start(self, serialized: Any, input_str: str, **kwargs: Any) -> None:  # 工具开始
        self._start(kwargs.get("run_id"), kwargs.get("parent_run_id"), _name_of(serialized, "tool"), "tool", {"args_chars": len(str(input_str or ""))})  # 开

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:  # 工具结束
        self._finish(kwargs.get("run_id"), output={"output_chars": len(str(output or ""))})  # 关

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:  # 工具失败
        self._finish(kwargs.get("run_id"), status="error", error=error)  # 关

    # ---- Retriever -----------------------------------------------------
    def on_retriever_start(self, serialized: Any, query: str, **kwargs: Any) -> None:  # 检索开始
        self._start(kwargs.get("run_id"), kwargs.get("parent_run_id"), _name_of(serialized, "retriever"), "retriever", {"query_chars": len(str(query or ""))})  # 开

    def on_retriever_end(self, documents: Any, **kwargs: Any) -> None:  # 检索结束
        count = len(documents or [])  # 命中数
        self._finish(kwargs.get("run_id"), output={"docs": count})  # 关
    def on_retriever_error(self, error: BaseException, **kwargs: Any) -> None:  # 检索失败
        self._finish(kwargs.get("run_id"), status="error", error=error)  # 关
