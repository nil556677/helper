"""模型网关：一次 LLM 调用的全部横切（#9 参数 / #10 重试 / #11 校验与结构化）。

业务只调 :meth:`ModelGateway.generate` / :meth:`astream` / :meth:`structured`，
不再各自 ``chat_model(...)``。参数由**调用处**每次传（见 ``ModelParams``），
重试策略与事件出口在网关构造函数里给。
"""

from __future__ import annotations

import asyncio  # 取消穿透
import json  # JSON 模式兜底
import re  # 抽取 JSON
import time  # 计时
from collections.abc import AsyncIterator, Callable  # 流与工厂签名
from typing import Any  # 任意值

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # 消息类型
from loguru import logger  # 日志
from pydantic import BaseModel  # 校验结构化目标必须是 Pydantic 模型

from app.agent.contracts import ErrorCode, EventType, make  # 错误码单源 / 重试事件
from app.agent.llm.params import to_chat_kwargs  # 参数翻译
from app.agent.llm.repair import REGENERATE_LIMIT, structured_repair_request  # 修复请求
from app.core.retry import RetryInfo, RetryPolicy, classify_error, retry_async  # 重试
from app.agent.llm.schema import (  # 结构化错误
    Issue,  # 单条错误
    StructuredModel,  # 基类约束
    issues_from,  # 异常 → 全部错误
    issues_text,  # 错误 → 人话
    parse_into,  # 整份校验
)
from app.agent.llm.types import (  # 数据契约
    ModelChunk,  # 流式块
    ModelParams,  # 参数
    ModelRequest,  # 请求
    ModelResult,  # 非流式结果
    ModelUnavailable,  # 槽位不可用
    ModelUsage,  # 用量
)
from app.core.result import Result  # 唯一结果契约（结构化输出信封）
from app.integrations.llm import chat_model  # 实例工厂（含密钥与缓存）

#: 角色 → LangChain 消息类。
_ROLES = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}  # 角色映射

#: 从「模型把 JSON 夹在说明文字里」的兜底抽取。
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)  # 粗略取第一个对象


def offline() -> None:  # 空操作事件出口
    """没接 UI 时用它，避免每处判 None。"""
    return None  # 什么都不做


def _messages(request: ModelRequest) -> list[Any]:  # 请求 → LangChain 消息
    """system 单独放最前，其余按 role 映射。"""
    result: list[Any] = []  # 结果
    if request.system:  # 有系统提示
        result.append(SystemMessage(content=request.system))  # 放最前
    for item in request.messages:  # 逐条历史/问题
        role = str(item.get("role") or "user")  # 角色
        cls = _ROLES.get(role, HumanMessage)  # 默认 human
        result.append(cls(content=str(item.get("content") or "")))  # 追加
    return result  # 返回


def _text_of(content: Any) -> str:  # 取正文
    """content 可能是 str 或内容块列表（多模态），只拼文本块。"""
    if isinstance(content, str):  # 普通文本
        return content  # 直接返回
    if isinstance(content, list):  # 内容块
        parts: list[str] = []  # 文本片段
        for item in content:  # 逐块
            if isinstance(item, str):  # 纯字符串块
                parts.append(item)  # 收
            elif isinstance(item, dict) and isinstance(item.get("text"), str):  # 文本块
                parts.append(str(item["text"]))  # 收
        return "".join(parts)  # 拼接
    return ""  # 认不出


def _reasoning_of(message: Any) -> str:  # 取思考增量
    """部分兼容网关把思考放 additional_kwargs。"""
    extra = getattr(message, "additional_kwargs", None) or {}  # 额外字段
    value = extra.get("reasoning_content") or extra.get("reasoning")  # 两种写法
    return value if isinstance(value, str) else ""  # 只要字符串


def _usage_parts(message: Any) -> dict[str, int]:  # 取用量明细
    """把各家的 usage_metadata 归一成扁平字典。"""
    usage = getattr(message, "usage_metadata", None)  # 用量
    if not isinstance(usage, dict):  # 没有
        return {}  # 空
    parts: dict[str, int] = {}  # 结果
    prompt = usage.get("input_tokens") or usage.get("prompt_tokens")  # 输入
    completion = usage.get("output_tokens") or usage.get("completion_tokens")  # 输出
    total = usage.get("total_tokens")  # 总数
    if isinstance(prompt, int):  # 有输入
        parts["prompt_tokens"] = prompt  # 记
    if isinstance(completion, int):  # 有输出
        parts["completion_tokens"] = completion  # 记
    if isinstance(total, int):  # 有总数
        parts["total_tokens"] = total  # 记
    input_details = usage.get("input_token_details") or {}  # 输入明细
    output_details = usage.get("output_token_details") or {}  # 输出明细
    if isinstance(input_details.get("cache_read"), int):  # 缓存读
        parts["cache_read_tokens"] = input_details["cache_read"]  # 记
    if isinstance(input_details.get("cache_creation"), int):  # 缓存写（LangChain 的键叫 cache_creation，不是 cache_write）
        parts["cache_creation_tokens"] = input_details["cache_creation"]  # 记
    if isinstance(output_details.get("reasoning"), int):  # 推理 token
        parts["reasoning_tokens"] = output_details["reasoning"]  # 记
    return parts  # 返回


def _to_model_usage(parts: dict[str, int]) -> ModelUsage:  # 扁平 → ModelUsage
    """prompt/completion 进正字段，其余进 extra。"""
    extra = {key: value for key, value in parts.items() if key not in ("prompt_tokens", "completion_tokens", "total_tokens")}  # 明细
    return ModelUsage(  # 组装
        prompt_tokens=int(parts.get("prompt_tokens") or 0),  # 输入
        completion_tokens=int(parts.get("completion_tokens") or 0),  # 输出
        total_tokens=parts.get("total_tokens"),  # 总数
        extra=extra,  # 其余
    )  # 组装结束


def _to_chunk(message: Any) -> ModelChunk:  # 消息 → 流式块
    """一个增量块。"""
    return ModelChunk(  # 块
        text=_text_of(getattr(message, "content", "")),  # 正文
        reasoning=_reasoning_of(message),  # 思考
        usage=_usage_parts(message),  # 用量
    )  # 块结束


async def _aclose(source: Any) -> None:  # 关流
    """取消/失败时把上游 HTTP 连接关掉，别让它继续生成。"""
    closer = getattr(source, "aclose", None)  # 异步关闭器
    if closer is None:  # 没有
        return  # 跳过
    try:  # 关闭可能因已耗尽而报错
        await closer()  # 关
    except Exception:  # 关失败不影响主流程
        logger.debug("关闭模型流失败（已忽略）")  # 调试日志


class ModelGateway:  # 模型网关
    """持槽位 / 策略 / 事件出口；参数每次调用传入。"""

    def __init__(  # 构造
        self,  # 网关
        *,
        slot: str = "llm",  # 槽位名
        policy: RetryPolicy | None = None,  # 重试策略
        emit: Callable[[Any], None] | None = None,  # 事件出口（发 retry）
        factory: Callable[[str, dict[str, Any]], Any] | None = None,  # 模型工厂（测试可注入）
    ) -> None:  # 无返回
        self._slot = slot  # 槽位
        self._policy = policy or RetryPolicy()  # 策略
        self._emit = emit or offline  # 事件出口
        self._factory = factory or _default_factory  # 工厂

    def available(self, params: ModelParams) -> bool:  # 槽位可用吗
        """没配全 / 密钥解不开 → False，调用方走兜底。"""
        try:  # 试着建
            self._build(params, streaming=False)  # 建实例
        except ModelUnavailable:  # 不可用
            return False  # 否
        return True  # 可用

    def _build(self, params: ModelParams, *, streaming: bool) -> Any:  # 建模型实例
        """参数显式翻译后交给工厂；工厂给 None 就是不可用。"""
        kwargs = to_chat_kwargs(params)  # 翻译
        kwargs.pop("streaming", None)  # 流式由调用面决定
        kwargs.setdefault("max_retries", 0)  # SDK 不重试：重试统一由网关 RetryPolicy 做（指数退避、最多 3 次）
        if streaming:  # 要流
            kwargs["streaming"] = True  # 打开
        model = self._factory(self._slot, kwargs)  # 造实例
        if model is None:  # 没配全
            raise ModelUnavailable(f"模型槽位 {self._slot!r} 不可用（未配置或密钥解不开）")  # 报不可用
        return model  # 返回

    def _on_retry(self, info: RetryInfo) -> None:  # 发重试事件
        """把 RetryInfo 转成契约事件。"""
        self._emit(  # 发
            make(  # 构造
                EventType.RETRY,  # 类型
                attempt=info.attempt,  # 第几次
                max_attempts=info.max_attempts,  # 总共
                error_kind=info.error_kind,  # 类别
                error_message=info.error_message,  # 信息
                delay_ms=info.delay_ms,  # 等待
                scope=info.scope,  # 作用域
            )  # 构造结束
        )  # emit 结束

    def _config(self) -> dict[str, Any] | None:  # 调用配置
        """有活跃 trace 时挂追踪回调；没有就返回 None（不影响调用）。"""
        from app.agent.callbacks import TraceCallbacks  # 延迟导入避免循环
        from app.core.trace.recorder import current_trace  # 当前 trace

        return {"callbacks": [TraceCallbacks()]} if current_trace() is not None else None  # 配置

    def _invoke_kwargs(self) -> dict[str, Any]:  # 传参
        """只有真有回调时才带 ``config``，避免给不支持配置的模型塞 None。"""
        config = self._config()  # 取配置
        return {"config": config} if config else {}  # 有才传

    async def astream(self, request: ModelRequest, params: ModelParams) -> AsyncIterator[ModelChunk]:  # 流式
        """**只在首 token 之前重试**；一旦出字就交给调用方处理中途失败。"""
        model = self._build(params, streaming=True)  # 建流式实例
        messages = _messages(request)  # 转消息

        async def open_stream():  # 建流并取第一块（可重试的边界）
            """首 token 的错误才算「还没开始」，可以重试。"""
            source = model.astream(messages, **self._invoke_kwargs())  # 上游流（有回调才带 config）
            try:  # 取首块
                first = await source.__anext__()  # 首 token
            except StopAsyncIteration:  # 空流
                await _aclose(source)  # 关掉
                return source, None  # 没有首块
            except BaseException:  # 首 token 前失败
                await _aclose(source)  # 关掉
                raise  # 交给重试
            return source, first  # 成功

        source, first = await retry_async(  # 首 token 前可重试
            open_stream,  # 动作
            self._policy,  # 策略
            on_retry=self._on_retry,  # 事件
            scope="model",  # 作用域
        )  # 重试结束
        try:  # 保证关流
            if first is not None:  # 有首块
                yield _to_chunk(first)  # 先给
            async for item in source:  # 后续块
                yield _to_chunk(item)  # 逐块给
        finally:  # 无论怎样
            await _aclose(source)  # 关上游

    async def generate(self, request: ModelRequest, params: ModelParams) -> ModelResult:  # 非流式
        """整次调用可重试（非流式没有「首 token 之后不能再试」的问题）。"""
        model = self._build(params, streaming=False)  # 建实例
        messages = _messages(request)  # 转消息
        started = time.perf_counter()  # 开始
        counter = {"attempt": 0}  # 尝试次数

        async def call():  # 一次调用
            """计数后真正请求。"""
            counter["attempt"] += 1  # 计一次
            return await model.ainvoke(messages, **self._invoke_kwargs())  # 请求

        message = await retry_async(  # 重试包装
            call,  # 动作
            self._policy,  # 策略
            on_retry=self._on_retry,  # 事件
            scope="model",  # 作用域
        )  # 包装结束
        metadata = getattr(message, "response_metadata", None) or {}  # 响应元信息
        return ModelResult(  # 结果
            text=_text_of(getattr(message, "content", "")),  # 正文
            usage=_to_model_usage(_usage_parts(message)),  # 用量
            attempt=counter["attempt"],  # 尝试次数
            elapsed_ms=_elapsed_ms(started),  # 耗时
            finish_reason=str(metadata.get("finish_reason") or ""),  # 结束原因
            model=self._slot,  # 槽位
        )  # 结果结束

    async def structured(  # 结构化输出
        self,  # 网关
        request: ModelRequest,  # 请求
        schema: type[Any],  # 目标 Pydantic 模型
        params: ModelParams,  # 参数
        *,
        repair_limit: int = REGENERATE_LIMIT,  # 最多再生成几次（不逐条重试）
    ) -> Result:  # 固定完整信封
        """解析一次 → **收全所有字段错误** → 整包回传重生成 → 用尽给 error 信封。

        不做「中文是→True」「null→默认值」这类猜测式兜底：值错了就带着错误信息让模型改，
        并且**一次校验的全部错误一起回传**，不因为一个字段错就多调一次模型。
        """
        _require_structured_schema(schema)  # 校验目标模型（Pydantic + 基类）
        started = time.perf_counter()  # 开始
        model = self._build(params, streaming=False)  # 控制面不流式
        messages = _messages(request)  # 转消息

        try:  # 首选：原生结构化输出（include_raw 才能拿到原文做修复）
            data, issues, raw, attempt = await self._call_structured(model, messages, schema)  # 调
        except asyncio.CancelledError:  # 取消穿透
            raise  # 不兜底
        except Exception as exc:  # 结构化不可用（网关不支持）
            if classify_error(exc) == str(ErrorCode.CONFIG):  # 配置错，降级也没用
                return self._err("config", exc, 1, started, retryable=False)  # 直接失败
            logger.warning("结构化输出不可用，降级 JSON 模式：{}", exc)  # 记一笔
            try:  # 降级
                data, issues, raw, attempt = await self._call_json(model, messages, schema)  # 调
            except asyncio.CancelledError:  # 取消穿透
                raise  # 不兜底
            except Exception as inner:  # 两条路都不通
                return self._err("schema_invalid", inner, 1, started, retryable=True)  # 失败信封

        repairs = 0  # 已重生成次数
        while issues and repairs < repair_limit:  # 有错且还有次数
            repairs += 1  # 计一次
            self._emit_struct_repair(repairs, repair_limit, issues)  # 让前端看到
            repair_request = structured_repair_request(request, schema, raw, issues)  # 带着**全部**错误
            result = await self.generate(repair_request, params)  # 重新生成（非流式）
            attempt += 1  # 计一次
            raw = result.text  # 新原文
            data, issues = _parse_text(schema, raw)  # 重新整份校验

        if issues:  # 修不好
            return self._err_text(str(ErrorCode.SCHEMA_INVALID), issues_text(issues), attempt, started, retryable=True)  # 全量错误
        return self._ok(data, attempt, started)  # 成功

    async def _call_structured(  # 原生结构化通道
        self,  # 网关
        model: Any,  # 模型实例
        messages: list[Any],  # 消息
        schema: type[Any],  # 目标模型
    ) -> tuple[Any, list[Issue], str, int]:  # (对象, 错误, 原文, 次数)
        """解析失败只返回 issues；网络/配置错误照旧抛给上层决定。"""
        counter = {"attempt": 0}  # 次数
        runner = model.with_structured_output(schema, include_raw=True)  # 要原文才能修

        async def call() -> Any:  # 一次调用
            """计数后请求。"""
            counter["attempt"] += 1  # 计一次
            return await runner.ainvoke(messages, **self._invoke_kwargs())  # 请求

        out = await retry_async(  # 网络层重试
            call,  # 动作
            self._policy,  # 策略
            on_retry=self._on_retry,  # 事件
            scope="structured",  # 作用域
        )  # 重试结束
        data, issues, raw = _split_structured(out, schema)  # 拆结果
        return data, issues, raw, counter["attempt"]  # 返回

    async def _call_json(  # JSON 模式降级通道
        self,  # 网关
        model: Any,  # 模型实例
        messages: list[Any],  # 消息
        schema: type[Any],  # 目标模型
    ) -> tuple[Any, list[Issue], str, int]:  # (对象, 错误, 原文, 次数)
        """网关不认 json_schema 时：要求纯 JSON + 强校验。"""
        counter = {"attempt": 0}  # 次数
        bound = model.bind(response_format={"type": "json_object"}) if hasattr(model, "bind") else model  # 要求 JSON

        async def call() -> Any:  # 一次调用
            """计数后请求。"""
            counter["attempt"] += 1  # 计一次
            return await bound.ainvoke(messages, **self._invoke_kwargs())  # 请求

        message = await retry_async(  # 网络层重试
            call,  # 动作
            self._policy,  # 策略
            on_retry=self._on_retry,  # 事件
            scope="structured",  # 作用域
        )  # 重试结束
        raw = _text_of(getattr(message, "content", ""))  # 原文
        data, issues = _parse_text(schema, raw)  # 解析
        return data, issues, raw, counter["attempt"]  # 返回

    def _emit_struct_repair(self, attempt: int, max_attempts: int, issues: list[Issue]) -> None:  # 修复事件
        """让用户看到「第几次、哪几个字段不对」。"""
        self._emit(  # 发
            make(  # 构造
                EventType.RETRY,  # 类型
                attempt=attempt,  # 第几次
                max_attempts=max_attempts,  # 总共
                error_kind=str(ErrorCode.SCHEMA_INVALID),  # 格式/字段错
                error_message=issues_text(issues)[:300],  # 全部错误摘要
                delay_ms=0,  # 立刻重生成
                scope="structured",  # 作用域
            )  # 构造结束
        )  # emit 结束

    def _ok(self, data: Any, attempt: int, started: float) -> Result:  # 成功信封
        """成功：ok=True + 校验过的 data，域信息进 meta。"""
        return Result(  # 统一结果
            ok=True,  # 成功
            code="ok",  # 成功码
            data=data,  # 校验过的对象
            meta={  # 元信息（统一扩展位）
                "attempt": attempt,  # 第几次
                "model": self._slot,  # 槽位
                "elapsed_ms": _elapsed_ms(started),  # 耗时
                "schema_version": 1,  # 协议版本
            },  # 元信息结束
        )  # 返回

    def _err(self, code: str, exc: BaseException, attempt: int, started: float, *, retryable: bool) -> Result:  # 失败信封
        """错误只由后端填（不交给模型）。"""
        return self._err_text(code, str(exc), attempt, started, retryable=retryable)  # 复用

    def _err_text(self, code: str, message: str, attempt: int, started: float, *, retryable: bool) -> Result:  # 失败信封
        """错误文本已在手（比如全量字段错误）。"""
        return Result(  # 统一结果
            ok=False,  # 失败
            code=code,  # 错误码
            message=message[:800],  # 说明（截断）
            retryable=retryable,  # 是否可重试
            meta={  # 元信息
                "attempt": attempt,  # 第几次
                "model": self._slot,  # 槽位
                "elapsed_ms": _elapsed_ms(started),  # 耗时
                "schema_version": 1,  # 协议版本
            },  # 元信息结束
        )  # 返回


def _default_factory(slot: str, kwargs: dict[str, Any]) -> Any:  # 默认工厂
    """走 integrations 的实例工厂（密钥解密 + 缓存都在那儿）。"""
    return chat_model(slot=slot, **kwargs)  # 造实例


def _elapsed_ms(started: float) -> int:  # 耗时（毫秒）
    """给结果与信封用。"""
    return int((time.perf_counter() - started) * 1000)  # 毫秒


def _extract_json(text: str) -> Any:  # 从句子里抽 JSON
    """只做「取第一个对象 + json.loads」，不 eval。"""
    match = _JSON_OBJECT.search(text or "")  # 找对象
    if match is None:  # 没找到
        raise ValueError("模型输出里没有 JSON 对象")  # 报错
    return json.loads(match.group(0))  # 解析


def _require_structured_schema(schema: type[Any]) -> None:  # 结构化目标校验
    """必须是 Pydantic 模型，且继承 StructuredModel（字段有说明与默认值）。"""
    if not (isinstance(schema, type) and issubclass(schema, BaseModel)):  # 非 Pydantic
        raise TypeError(  # 明确报错
            f"结构化输出的 schema 必须是 Pydantic 模型，收到 {schema!r}；"
            "dataclass（如 RouteDecision）请先转成 Pydantic 并继承 StructuredModel"
        )  # 报错结束
    if not issubclass(schema, StructuredModel):  # 没走统一基类
        raise TypeError(  # 明确报错
            f"{schema.__name__} 必须继承 app.agent.llm.StructuredModel："
            "它会强制每个字段都有 Field(description=...) 与默认值"
        )  # 报错结束


def _split_structured(out: Any, schema: type[Any]) -> tuple[Any, list[Issue], str]:  # 拆结构化结果
    """返回（对象｜None，**全部**字段错误，模型原文）。"""
    if isinstance(out, dict) and "parsed" in out:  # include_raw 的返回
        raw_message = out.get("raw")  # 原始消息
        raw = _text_of(getattr(raw_message, "content", "")) if raw_message is not None else ""  # 原文
        error = out.get("parsing_error")  # 解析错误
        parsed = out.get("parsed")  # 已解析对象
        if error is not None:  # 解析/校验失败
            return None, issues_from(error), raw  # 全量错误
        if parsed is None:  # 没解析出东西
            return None, [Issue(path="<整体>", message="模型没有给出可解析的结构化结果")], raw  # 一条
        if not isinstance(parsed, schema):  # 类型不对
            return None, [Issue(path="<整体>", message=f"解析结果类型不对：{type(parsed).__name__}")], raw  # 一条
        return parsed, [], raw  # 成功
    if isinstance(out, schema):  # 没走 include_raw 的兜底
        return out, [], ""  # 当成功
    return None, [Issue(path="<整体>", message=f"结构化结果类型不对：{type(out).__name__}")], ""  # 一条


def _parse_text(schema: type[Any], raw: str) -> tuple[Any, list[Issue]]:  # 文本 → 对象
    """抽 JSON（可能失败）再整份校验；错误一次收全。"""
    try:  # 先抽
        payload = _extract_json(raw)  # 对象
    except Exception as exc:  # 不是合法 JSON
        return None, issues_from(exc)  # 一条整体错误
    return parse_into(schema, payload)  # 整份校验
