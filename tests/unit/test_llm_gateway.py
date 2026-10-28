"""类 B 模型网关：参数翻译、重试、流式/结构化、清洗校验、修复与兜底。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 事件循环
from typing import Any  # 任意值

import pytest  # 测试框架
from langchain_core.messages import AIMessage  # 假响应
from pydantic import BaseModel, Field, ValidationError  # 结构化样本 / 字段 / 校验异常

from app.agent.contracts import EventType  # 事件类型
from app.agent.llm import (  # 被测对象
    DEFAULT_PARAMS,  # 共用默认参数
    ModelGateway,  # 网关
    ModelParams,  # 参数
    ModelRequest,  # 请求
    ModelUnavailable,  # 不可用
    RetryPolicy,  # 重试策略
    StructuredModel,  # 结构化输出基类
    ValidationReport,  # 校验报告
    classify_error,  # 错误分类
    extractive_answer,  # 兜底原文
    note_for,  # 兜底说明
    regenerate,  # 修复调用
    repair_loop,  # 有限次修复
    repair_request,  # 修复请求
    retry_async,  # 通用重试
    sanitize_markdown,  # 清洗
    to_chat_kwargs,  # 参数翻译
    validate_answer,  # 校验入口
)
from app.agent.llm.params import cache_key  # 缓存键


class _StatusErr(Exception):  # 带状态码的假异常
    """模拟 OpenAI SDK 的 APIStatusError。"""

    def __init__(self, message: str, status: int) -> None:  # 构造
        super().__init__(message)  # 基类
        self.status_code = status  # 状态码


def _request(text: str = "问题") -> ModelRequest:  # 造请求
    """单条用户消息 + 系统提示。"""
    return ModelRequest(model="llm", messages=[{"role": "user", "content": text}], system="系统")  # 请求


def _policy(**kwargs: Any) -> RetryPolicy:  # 不真等的策略
    """退避归零，测试跑得快（次数仍是默认的 3 次重试）。"""
    base = {"max_retries": 3, "base_delay_s": 0.0, "jitter_s": 0.0}  # 默认
    base.update(kwargs)  # 覆盖
    return RetryPolicy(**base)  # 返回


def test_params_only_pass_explicit_values() -> None:  # 参数翻译
    """没给的不出现；top_k 走 extra_body，seed 是一等字段。"""
    assert to_chat_kwargs(ModelParams()) == {}  # 全空
    kwargs = to_chat_kwargs(ModelParams(temperature=0.2, top_k=5, seed=7, stop=("完",)))  # 给几项
    assert kwargs["temperature"] == 0.2  # 温度
    assert kwargs["stop"] == ["完"]  # 停止词转 list
    assert kwargs["seed"] == 7  # seed 直接传（ChatOpenAI 有该字段）
    assert kwargs["extra_body"] == {"top_k": 5}  # 只有 top_k 进额外体
    assert "top_p" not in kwargs  # 没给就不传


def test_params_cache_key_changes_with_params() -> None:  # 不串实例
    """换任一参数就是另一个缓存键。"""
    assert cache_key(ModelParams(temperature=0.1)) != cache_key(ModelParams(temperature=0.2))  # 不同
    assert cache_key(ModelParams(temperature=0.1)) == cache_key(ModelParams(temperature=0.1))  # 相同


def test_default_params_shared_and_overridable() -> None:  # 共用默认 + 调用处覆盖
    """默认只有一份；调用点只写自己不一样的项，原实例不被改。"""
    assert DEFAULT_PARAMS.temperature == 0.2  # 默认温度
    assert DEFAULT_PARAMS.streaming is False  # 默认非流式
    assert DEFAULT_PARAMS.timeout_s == 60.0  # 默认超时
    assert DEFAULT_PARAMS.max_retries == 0  # SDK 不重试（重试只留网关一层）
    derived = DEFAULT_PARAMS.with_overrides(temperature=0.0, streaming=True, max_tokens=128)  # 改写调用点
    assert derived.temperature == 0.0  # 覆盖温度
    assert derived.streaming is True  # 覆盖流式
    assert derived.max_tokens == 128  # 覆盖上限
    assert derived.top_p == DEFAULT_PARAMS.top_p  # 其余沿用默认
    assert DEFAULT_PARAMS.temperature == 0.2  # 原实例不变
    with pytest.raises(ValidationError):  # 越界直接报错
        DEFAULT_PARAMS.with_overrides(temperature=9.0)  # 温度非法


def test_answer_node_derives_from_shared_defaults() -> None:  # 调用点派生
    """答题节点从共用默认派生，只把流式打开。"""
    from app.agent.nodes.answer import node as answer_mod  # 节点模块

    assert answer_mod.ANSWER_PARAMS.streaming is True  # 打开流式
    assert answer_mod.ANSWER_PARAMS.temperature == DEFAULT_PARAMS.temperature  # 温度沿用默认


def test_gateway_disables_sdk_retries() -> None:  # 只有一层重试
    """网关建模型时把 SDK 重试设成 0：重试只由 RetryPolicy 做，避免 3×3 放大。"""
    captured: dict[str, Any] = {}  # 抓参数

    def factory(slot: str, kwargs: dict[str, Any]) -> Any:  # 假工厂
        """记录参数后给假模型。"""
        captured.update(kwargs)  # 记
        return _FakeModel()  # 模型

    gateway = ModelGateway(policy=_policy(), factory=factory)  # 网关
    asyncio.run(gateway.generate(_request(), ModelParams()))  # 调一次
    assert captured["max_retries"] == 0  # SDK 不重试


def test_classify_error() -> None:  # 错误分类
    """429/5xx/超时可重试；400/401 是配置错。"""
    assert classify_error(_StatusErr("限流", 429)) == "rate_limit"  # 429
    assert classify_error(_StatusErr("炸了", 502)) == "server"  # 5xx
    assert classify_error(_StatusErr("key 错", 401)) == "config"  # 401
    assert classify_error(TimeoutError("超时")) == "timeout"  # 超时
    assert classify_error(RuntimeError("???")) == "unknown"  # 认不出


def test_retry_async_retries_then_succeeds() -> None:  # 重试后成功
    """两次失败后成功，回调记录每次尝试。"""
    calls = {"n": 0}  # 次数
    infos: list[Any] = []  # 回调

    async def flaky() -> str:  # 前两次失败
        calls["n"] += 1  # 计数
        if calls["n"] < 3:  # 还没到第三次
            raise _StatusErr("忙", 500)  # 失败
        return "ok"  # 成功

    result = asyncio.run(retry_async(flaky, _policy(), on_retry=infos.append, sleep=_no_sleep()))  # 跑
    assert result == "ok"  # 成功
    assert [info.attempt for info in infos] == [1, 2]  # 两次重试
    assert all(info.error_kind == "server" for info in infos)  # 类别正确


def test_retry_async_does_not_retry_config() -> None:  # 配置错不重试
    """401 立刻抛，不浪费用户时间。"""
    infos: list[Any] = []  # 回调

    async def bad() -> str:  # 永远 401
        raise _StatusErr("key 错", 401)  # 抛

    with pytest.raises(_StatusErr):  # 期望原样抛
        asyncio.run(retry_async(bad, _policy(), on_retry=infos.append, sleep=_no_sleep()))  # 跑
    assert infos == []  # 没重试


def test_retry_policy_is_exponential_and_capped() -> None:  # 指数退避
    """0.5s → 1s → 2s → 4s，封顶 8s。"""
    policy = RetryPolicy(max_retries=3, base_delay_s=0.5, max_delay_s=8.0, jitter_s=0.0)  # 无抖动
    assert policy.max_retries == 3  # 最多重试 3 次
    assert policy.delay_s(1) == 0.5  # 第 1 次
    assert policy.delay_s(2) == 1.0  # 第 2 次
    assert policy.delay_s(3) == 2.0  # 第 3 次
    assert policy.delay_s(5) == 8.0  # 封顶


def test_retry_async_stops_at_max_retries() -> None:  # 次数上限
    """两次失败后（max_retries=2）用尽，原样抛。"""
    infos: list[Any] = []  # 回调

    async def bad() -> str:  # 永远 500
        raise _StatusErr("炸", 500)  # 抛

    with pytest.raises(_StatusErr):  # 期望抛出
        asyncio.run(retry_async(bad, _policy(max_retries=2), on_retry=infos.append, sleep=_no_sleep()))  # 跑
    assert [info.attempt for info in infos] == [1, 2]  # 重试两次
    assert all(info.max_attempts == 2 for info in infos)  # 事件里的上限一致


def _no_sleep():  # 假 sleep
    """记录等待但不真等。"""
    calls: list[float] = []  # 记录

    async def sleep(seconds: float) -> None:  # 签名一致
        calls.append(seconds)  # 记

    sleep.calls = calls  # type: ignore[attr-defined]
    return sleep  # 返回


class _FakeModel:  # 假模型
    """按需实现 ainvoke / astream / with_structured_output / bind。"""

    def __init__(self, *, text: str = "答案 [1]", usage: dict | None = None) -> None:  # 构造
        self.text = text  # 正文
        self.usage = usage  # 用量
        self.calls = 0  # 调用次数

    async def ainvoke(self, messages):  # 非流式
        """返回一条完整消息。"""
        self.calls += 1  # 计数
        return AIMessage(content=self.text, usage_metadata=self.usage)  # 消息


def _gateway(**kwargs: Any) -> tuple[ModelGateway, list[Any]]:  # 造网关 + 事件出口
    """默认接到假工厂，事件收进列表。"""
    events: list[Any] = []  # 事件
    gateway = ModelGateway(policy=_policy(), emit=events.append, **kwargs)  # 网关
    return gateway, events  # 返回


def test_gateway_generate_reads_text_and_usage() -> None:  # 非流式
    """正文/用量/尝试次数都回填。"""
    model = _FakeModel(text="答案 [1]", usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})  # 假模型
    gateway, _ = _gateway(factory=lambda slot, kwargs: model)  # 网关
    result = asyncio.run(gateway.generate(_request(), ModelParams(temperature=0.0)))  # 调
    assert result.text == "答案 [1]"  # 正文
    assert result.usage.prompt_tokens == 3  # 输入
    assert result.usage.completion_tokens == 2  # 输出
    assert result.attempt == 1  # 一次成功


def test_gateway_usage_reads_cache_creation() -> None:  # 缓存写键
    """LangChain 的输入明细键是 cache_creation；读 cache_write 会永远 0（旧 bug）。"""
    usage = {  # 假用量
        "input_tokens": 10,  # 输入
        "output_tokens": 5,  # 输出
        "total_tokens": 15,  # 总数
        "input_token_details": {"cache_read": 3, "cache_creation": 4},  # 缓存读/写
        "output_token_details": {"reasoning": 2},  # 推理 token
    }  # 用量结束
    model = _FakeModel(usage=usage)  # 假模型
    gateway, _ = _gateway(factory=lambda slot, kwargs: model)  # 网关
    result = asyncio.run(gateway.generate(_request(), ModelParams()))  # 调
    assert result.usage.extra["cache_creation_tokens"] == 4  # 缓存写
    assert result.usage.extra["cache_read_tokens"] == 3  # 缓存读
    assert result.usage.extra["reasoning_tokens"] == 2  # 推理


def test_gateway_unavailable_raises() -> None:  # 槽位不可用
    """工厂给 None 就是不可用，调用方走兜底。"""
    gateway, _ = _gateway(factory=lambda slot, kwargs: None)  # 永远 None
    assert gateway.available(ModelParams()) is False  # 判定不可用
    with pytest.raises(ModelUnavailable):  # 期望异常
        asyncio.run(gateway.generate(_request(), ModelParams()))  # 调


def test_gateway_stream_retries_before_first_token() -> None:  # 首 token 前重试
    """第一次 429，第二次成功；前端收到一条 retry 事件。"""
    calls = {"n": 0}  # 次数
    events: list[Any] = []  # 事件

    class _Model:  # 假模型
        def astream(self, messages):  # 流式
            """第一次抛 429。"""
            calls["n"] += 1  # 计数
            attempt = calls["n"]  # 本次

            async def gen():  # 生成器
                if attempt == 1:  # 第一次
                    raise _StatusErr("限流", 429)  # 首 token 前失败
                yield AIMessage(content="好")  # 第二次成功

            return gen()  # 返回

    gateway = ModelGateway(policy=_policy(), emit=events.append, factory=lambda slot, kwargs: _Model())  # 网关

    async def collect() -> list[str]:  # 收集
        """把流拼起来。"""
        return [chunk.text async for chunk in gateway.astream(_request(), ModelParams(streaming=True))]  # 收集

    assert asyncio.run(collect()) == ["好"]  # 内容
    retries = [event for event in events if event.type is EventType.RETRY]  # 重试事件
    assert len(retries) == 1  # 只有一次重试
    assert retries[0].payload.error_kind == "rate_limit"  # 类别
    assert retries[0].payload.attempt == 1  # 第几次


class _Sample(StructuredModel):  # 结构化样本
    """一个用途一个模型。"""

    intent: str = Field(default="kb", description="意图：kb / tool")  # 意图
    need_tool: bool = Field(default=False, description="是否调用工具")  # 布尔字段


def _validation_error(data: dict[str, Any]) -> ValidationError:  # 造多字段错误
    """用真实校验拿 Pydantic 的 ValidationError（一次就含全部错误）。"""
    try:  # 故意给错
        _Sample.model_validate(data)  # 校验
    except ValidationError as error:  # 捕获
        return error  # 返回
    raise AssertionError("这组数据居然是合法的")  # 不该发生


class _ScriptedModel:  # 脚本化假模型
    """第一次给坏结果（含全部字段错误），修复调用给好 JSON。"""

    def __init__(self, *, first_error: ValidationError | None = None, first_json: str = "", repaired_json: str = "") -> None:  # 构造
        self.first_error = first_error  # 首轮解析错误
        self.first_json = first_json  # 首轮原文
        self.repaired_json = repaired_json  # 修复后的原文
        self.repair_prompts: list[str] = []  # 收到的修复提示

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:  # 结构化入口
        """返回固定结果（带 include_raw 形状）。"""
        error, text = self.first_error, self.first_json  # 闭包

        class _Runner:  # 运行器
            async def ainvoke(self, messages: list[Any]) -> dict[str, Any]:  # 调
                """按脚本返回。"""
                return {"parsed": None, "raw": AIMessage(content=text), "parsing_error": error}  # 带原文

        return _Runner()  # 返回

    def bind(self, **kwargs: Any) -> _ScriptedModel:  # JSON 模式
        """返回自己（不支持结构化时走这里）。"""
        return self  # 自己

    async def ainvoke(self, messages: list[Any]) -> Any:  # 修复调用（generate 走这里）
        """记录提示，返回修复后的原文。"""
        self.repair_prompts.append("\n".join(str(getattr(item, "content", "")) for item in messages))  # 记
        return AIMessage(content=self.repaired_json)  # 返回


def test_gateway_structured_requires_structured_base() -> None:  # 基类强制
    """普通 BaseModel 不能当结构化目标：必须继承 StructuredModel。"""
    class _Plain(BaseModel):  # 普通模型
        """没有 description/默认值约束。"""

        value: int = 0  # 字段

    gateway, _ = _gateway(factory=lambda slot, kwargs: _FakeModel())  # 网关
    with pytest.raises(TypeError):  # 期望明确报错
        asyncio.run(gateway.structured(_request(), _Plain, ModelParams()))  # 传普通模型


def test_gateway_structured_uses_structured_output() -> None:  # 首选结构化
    """with_structured_output 可用时直接拿对象。"""

    class _Model:  # 假模型
        def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:  # 结构化
            """返回固定对象（include_raw 形状）。"""
            class _Runner:  # 运行器
                async def ainvoke(self, messages: list[Any]) -> dict[str, Any]:  # 调
                    """固定结果。"""
                    return {"parsed": _Sample(intent="kb"), "raw": AIMessage(content='{"intent":"kb"}'), "parsing_error": None}  # 成功

            return _Runner()  # 返回

    gateway, _ = _gateway(factory=lambda slot, kwargs: _Model())  # 网关
    result = asyncio.run(gateway.structured(_request(), _Sample, ModelParams(streaming=False)))  # 调
    assert result.ok is True  # 成功
    assert isinstance(result.data, _Sample)  # 已校验对象
    assert result.status == "ok"  # 状态


def test_structured_repairs_all_errors_in_one_call() -> None:  # 一次修全部
    """两个字段同时错 → **一次**修复调用里带全部错误，不是错一个修一次。"""
    error = _validation_error({"intent": 5, "need_tool": "是"})  # 两个字段都错
    assert len(error.errors()) == 2  # 确认是两条
    model = _ScriptedModel(  # 假模型
        first_error=error,  # 首轮两个错误
        first_json='{"intent": 5, "need_tool": "是"}',  # 首轮原文
        repaired_json='{"intent": "tool", "need_tool": true}',  # 修好
    )  # 构造结束
    events: list[Any] = []  # 事件
    gateway = ModelGateway(policy=_policy(), emit=events.append, factory=lambda slot, kwargs: model)  # 网关
    result = asyncio.run(gateway.structured(_request(), _Sample, ModelParams()))  # 调
    assert result.ok is True  # 成功
    assert result.data.intent == "tool"  # 新值
    assert len(model.repair_prompts) == 1  # **只调一次**模型，不是每个错误一次
    prompt = model.repair_prompts[0]  # 那一次提示
    assert "1. intent" in prompt  # 第一条
    assert "2. need_tool" in prompt  # 第二条
    assert result.meta.attempt == 2  # 首轮 + 一次修复
    retries = [event for event in events if event.type is EventType.RETRY]  # 重试事件
    assert retries and retries[0].payload.error_kind == "schema_invalid"  # 格式错可见


def test_gateway_structured_falls_back_to_json() -> None:  # JSON 兜底
    """网关不支持结构化时，用 JSON 模式 + 强校验。"""

    class _Model:  # 假模型
        def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:  # 结构化不可用
            """直接抛。"""
            raise ValueError("不支持 json_schema")  # 模拟老网关

        def bind(self, **kwargs: Any) -> _Model:  # 逼 JSON 模式
            """返回自己。"""
            return self  # 自己

        async def ainvoke(self, messages: list[Any]) -> Any:  # 调
            """把 JSON 夹在废话里，验证抽取。"""
            return AIMessage(content='好的：{"intent": "tool"}')  # 带前后缀

    gateway, _ = _gateway(factory=lambda slot, kwargs: _Model())  # 网关
    result = asyncio.run(gateway.structured(_request(), _Sample, ModelParams()))  # 调
    assert result.ok is True  # 成功
    assert result.data.intent == "tool"  # 抽取成功


def test_structured_gives_up_after_limit_with_all_errors() -> None:  # 修不好
    """修 3 次仍不过 → error 信封里带**全部**错误。"""
    error = _validation_error({"intent": 5, "need_tool": "是"})  # 两个错误
    model = _ScriptedModel(  # 假模型
        first_error=error,  # 首轮错
        first_json='{"intent": 5, "need_tool": "是"}',  # 首轮原文
        repaired_json='{"intent": 5, "need_tool": "是"}',  # 修了还是错
    )  # 构造结束
    gateway, _ = _gateway(factory=lambda slot, kwargs: model)  # 网关
    result = asyncio.run(gateway.structured(_request(), _Sample, ModelParams()))  # 调
    assert result.status == "error"  # 失败
    assert result.error.code == "schema_invalid"  # 错误码
    assert "intent" in result.error.message  # 带 intent 的错
    assert "need_tool" in result.error.message  # 带 need_tool 的错
    assert len(model.repair_prompts) == 3  # 最多修 3 次


def test_gateway_structured_config_error_skips_fallback() -> None:  # 配置错不兜底
    """401 还去试 JSON 模式没意义。"""
    touched = {"bind": 0}  # 监控

    class _Model:  # 假模型
        def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:  # 结构化
            """抛 401。"""
            raise _StatusErr("key 错", 401)  # 配置错

        def bind(self, **kwargs):  # JSON 模式
            """被调用就计数。"""
            touched["bind"] += 1  # 计数
            return self  # 自己

        async def ainvoke(self, messages):  # 调
            """不会走到。"""
            return AIMessage(content="{}")  # 占位

    gateway, _ = _gateway(factory=lambda slot, kwargs: _Model())  # 网关
    result = asyncio.run(gateway.structured(_request(), _Sample, ModelParams()))  # 调
    assert result.status == "error"  # 失败
    assert result.error.code == "config"  # 配置错
    assert touched["bind"] == 0  # 没有降级


def test_gateway_structured_rejects_non_pydantic_schema() -> None:  # 护栏
    """dataclass 不能直接当结构化目标（RouteDecision 就是 dataclass）。"""
    from dataclasses import dataclass  # 局部导入

    @dataclass
    class _Plain:  # 假的 dataclass
        intent: str  # 字段

    gateway, _ = _gateway(factory=lambda slot, kwargs: _FakeModel())  # 网关
    with pytest.raises(TypeError):  # 期望明确报错
        asyncio.run(gateway.structured(_request(), _Plain, ModelParams()))  # 传 dataclass


def test_sanitize_removes_dangerous_content() -> None:  # 输出安全
    """脚本/iframe/on* 事件/危险协议都被剥掉；安全标签保留。"""
    dirty = '<script>alert(1)</script>正文<iframe src="x"></iframe><img src=x onerror=alert(1)>'  # 输入
    cleaned = sanitize_markdown(dirty)  # 清洗
    assert "script" not in cleaned.lower()  # 没有脚本
    assert "iframe" not in cleaned.lower()  # 没有 iframe
    assert "onerror" not in cleaned  # 没有事件属性
    assert "正文" in cleaned  # 正文保留
    assert sanitize_markdown("<b>粗</b>") == "<b>粗</b>"  # 白名单保留
    assert "javascript:" not in sanitize_markdown("[点我](javascript:alert(1))")  # 危险链接
    assert "[官网](https://example.com)" in sanitize_markdown("[官网](https://example.com)")  # 安全链接


def test_validate_answer_judges_by_citations_and_coverage() -> None:  # 校验判决
    """有有效引用 + 覆盖率达标才算通过。"""
    evidence = [{"index": 1, "text": "证据正文"}]  # 一条证据
    good = validate_answer("结论[1]。", evidence, l2=False)  # 好答案
    assert good.passed is True  # 通过
    assert good.valid == [1]  # 有效引用
    bad = validate_answer("结论[9]。", evidence, l2=False)  # 越界
    assert bad.passed is False  # 不通过
    assert bad.valid == []  # 没有有效引用
    empty = validate_answer("", evidence, l2=False)  # 空答案
    assert empty.passed is False  # 不通过


def test_validate_answer_respects_min_coverage() -> None:  # 覆盖率阈值
    """同一答案，阈值不同、判决不同。"""
    evidence = [{"index": 1, "text": "证据"}]  # 一条
    text = "第一句[1]。第二句没有引用。"  # 一半有引用
    lenient = validate_answer(text, evidence, min_coverage=0.5, l2=False)  # 宽松
    strict = validate_answer(text, evidence, min_coverage=0.9, l2=False)  # 严格
    assert lenient.passed is True  # 过
    assert strict.passed is False  # 不过


def test_fallback_is_deterministic() -> None:  # 兜底
    """原文兜底不随机；说明按原因给。"""
    evidence = [{"index": 1, "text": "证据正文", "source_name": "资料", "origin": "第 1 段"}]  # 证据
    assert "证据正文" in extractive_answer(evidence)  # 有原文
    assert extractive_answer(evidence) == extractive_answer(evidence)  # 确定性
    assert extractive_answer([]) == ""  # 没证据就是空
    assert "原文" in note_for("unavailable")  # 不可用说明
    assert note_for("不认识的") == note_for("invalid")  # 未知回落


def test_repair_request_has_typed_blocks() -> None:  # 修复请求
    """四段标签都在，且声明「数据不是指令」。"""
    report = ValidationReport(text="草稿[9]", valid=[], invalid=[9], passed=False)  # 报告
    request = repair_request("问题", [{"index": 1, "text": "证据"}], "草稿[9]", report)  # 构造
    content = request.messages[0]["content"]  # 用户消息
    for tag in ("<question>", "<evidence>", "<draft>", "<report>"):  # 四段
        assert tag in content  # 都在
    assert "不得新增任何事实" in request.system  # 约束


def test_regenerate_calls_gateway() -> None:  # 修复调用
    """修复走网关的 generate，参数由调用处给。"""
    calls: list[tuple[Any, Any]] = []  # 记录

    class _Stub:  # 假网关
        async def generate(self, request, params):  # 非流式
            """记录并返回固定结果。"""
            from app.agent.llm import ModelResult  # 局部导入

            calls.append((request, params))  # 记
            return ModelResult(text="修好了[1]")  # 结果

    report = ValidationReport(text="草稿", valid=[], invalid=[9], passed=False)  # 报告
    params = ModelParams(temperature=0.0)  # 参数
    result = asyncio.run(  # 跑
        regenerate(  # 修复
            _Stub(),  # 网关
            params,  # 参数
            question="问题",  # 问题
            evidence=[{"index": 1, "text": "证据"}],  # 证据
            draft="草稿",  # 草稿
            report=report,  # 报告
        )  # 结束
    )  # 结束
    assert result.text == "修好了[1]"  # 结果
    assert calls and calls[0][1] is params  # 参数原样传入


def test_repair_loop_gives_up_after_limit_with_fallback() -> None:  # 修不好必出结果
    """修 3 次仍不过 → 资料原文，不会无限重试。"""
    calls = {"n": 0}  # 次数

    class _Stub:  # 假网关
        async def generate(self, request, params):  # 非流式
            """永远给新草稿。"""
            from app.agent.llm import ModelResult  # 局部导入

            calls["n"] += 1  # 计数
            return ModelResult(text=f"第{calls['n']}版")  # 新草稿

    evidence = [{"index": 1, "text": "证据正文", "source_name": "资料", "origin": "第 1 段"}]  # 证据
    bad = ValidationReport(text="草稿", valid=[], passed=False)  # 一直不过
    text, report = asyncio.run(  # 跑
        repair_loop(  # 修复循环
            _Stub(),  # 网关
            ModelParams(temperature=0.0),  # 参数
            question="问题",  # 问题
            evidence=evidence,  # 证据
            draft="草稿",  # 草稿
            report=bad,  # 报告
            check=lambda value: ValidationReport(text=value, valid=[], passed=False),  # 永远不过
        )  # 结束
    )  # 结束
    assert calls["n"] == 3  # 只修 3 次
    assert "证据正文" in text  # 原文兜底
    assert report.passed is False  # 报告如实


def test_repair_loop_stops_when_passed() -> None:  # 修好就停
    """一次就过，不再多调。"""
    calls = {"n": 0}  # 次数

    class _Stub:  # 假网关
        async def generate(self, request, params):  # 非流式
            """给修好的答案。"""
            from app.agent.llm import ModelResult  # 局部导入

            calls["n"] += 1  # 计数
            return ModelResult(text="好[1]")  # 修好

    good = ValidationReport(text="好[1]", valid=[1], coverage=1.0, passed=True)  # 通过的报告
    text, report = asyncio.run(  # 跑
        repair_loop(  # 修复循环
            _Stub(),  # 网关
            ModelParams(),  # 参数
            question="问题",  # 问题
            evidence=[{"index": 1, "text": "证据"}],  # 证据
            draft="草稿",  # 草稿
            report=ValidationReport(text="草稿", passed=False),  # 初版不过
            check=lambda value: good,  # 一次即过
        )  # 结束
    )  # 结束
    assert calls["n"] == 1  # 只调一次
    assert text == "好[1]"  # 用修好的
    assert report.passed is True  # 通过


def test_answer_node_emits_retry_event(fake_root, monkeypatch) -> None:  # 节点级集成
    """answer 节点走网关：首 token 前 429 → 自动重试，前端收到 retry 事件。"""
    from app.agent.context import RunContext  # 运行上下文
    from app.agent.fsm.states import RunEvent  # 状态事件
    from app.agent.nodes.answer import node as answer_mod  # 被测节点

    monkeypatch.setattr(  # 退避归零，测试不真等
        "app.agent.llm.gateway.RetryPolicy",
        lambda: RetryPolicy(max_retries=3, base_delay_s=0.0, jitter_s=0.0),  # 无等待，最多重试 3 次
    )

    calls = {"n": 0}  # 调用次数

    class _Model:  # 假模型
        def astream(self, messages):  # 流式
            """第一次 429，第二次给一句话。"""
            calls["n"] += 1  # 计数
            attempt = calls["n"]  # 本次

            async def gen():  # 生成器
                if attempt == 1:  # 第一次
                    raise _StatusErr("限流", 429)  # 首 token 前失败
                yield AIMessage(content="答案")  # 第二次成功

            return gen()  # 返回

    monkeypatch.setattr(  # 网关的模型入口换成假模型
        "app.agent.llm.gateway.chat_model", lambda **kwargs: _Model()  # 假模型
    )
    ctx = RunContext(run_id="r", question="q")  # 上下文
    ctx.transition(RunEvent.START)  # 开始
    ctx.transition(RunEvent.CONTEXT_LOADED)  # 上下文
    ctx.transition(RunEvent.RETRIEVAL_DONE)  # 有证据
    sent: list[Any] = []  # 收到的事件
    ctx.emit = sent.append  # 记录
    evidence = [{"index": 1, "text": "证据", "source_name": "资料", "origin": "第 1 段"}]  # 证据
    asyncio.run(  # 跑节点
        answer_mod.answer_node({"question": "q", "evidence": evidence}, {"configurable": {"run_ctx": ctx}})  # 调用
    )  # 结束
    retries = [event for event in sent if event.type is EventType.RETRY]  # 重试事件
    assert retries, "answer 节点没有把 retry 事件发给前端"  # 必须有
    assert retries[0].payload.error_kind == "rate_limit"  # 类别对
    assert calls["n"] == 2  # 重试了一次
