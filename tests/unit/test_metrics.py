"""指标 schema 与回调采集：派生值正确、模型/工具指标经回调累加。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.callbacks import TraceCallbacks  # 追踪回调
from app.agent.context import RunContext, run_context  # 上下文
from app.agent.metrics import METRICS_VERSION, bump, finalize, new_metrics  # 指标


def test_finalize_derives_uncached_and_ratio() -> None:  # 派生值
    metrics = new_metrics()  # 全 0
    bump(metrics, "tokens_total", 100)  # 总
    bump(metrics, "tokens_cache_read", 40)  # 缓存读
    bump(metrics, "tokens_prompt", 80)  # 输入
    bump(metrics, "tokens_before_compress", 200)  # 压缩前
    bump(metrics, "tokens_after_compress", 150)  # 压缩后
    out = finalize(metrics)  # 汇总
    assert out["tokens_uncached"] == 60  # 未缓存
    assert out["cache_hit_ratio"] == 0.5  # 命中率
    assert out["tokens_saved"] == 50  # 压缩节省
    assert out["metrics_version"] == METRICS_VERSION  # 版本


def test_callback_accumulates_llm_metrics() -> None:  # 模型指标
    ctx = RunContext(run_id="r", question="q")  # 上下文
    with run_context(ctx):  # 绑定当前 run
        cb = TraceCallbacks()  # 回调捕获 ctx.metrics
        cb.on_chat_model_start({}, [[]])  # 模型开始

        class _Resp:  # 假 LLMResult
            llm_output = {  # 元信息
                "usage": {  # 用量
                    "prompt_tokens": 10,  # 输入
                    "completion_tokens": 5,  # 输出
                    "total_tokens": 15,  # 总
                    "cache_read_tokens": 4,  # 缓存读
                }
            }

        cb.on_llm_end(_Resp())  # 模型结束
    assert ctx.metrics["llm_calls"] == 1  # 调用数
    assert ctx.metrics["llm_success"] == 1  # 成功数
    assert ctx.metrics["tokens_total"] == 15  # 总 token
    assert ctx.metrics["tokens_cache_read"] == 4  # 缓存读


def test_trace_callbacks_singleton() -> None:  # 共享单例
    from app.agent.callbacks import trace_callbacks  # 取点

    assert trace_callbacks() is trace_callbacks()  # 始终同一实例


def test_callback_accumulates_tool_metrics() -> None:  # 工具指标
    ctx = RunContext(run_id="r", question="q")  # 上下文
    with run_context(ctx):  # 绑定当前 run
        cb = TraceCallbacks()  # 回调
        cb.on_tool_start({"name": "query_weather"}, '{"city": "北京"}', run_id="x")  # 工具开始
        cb.on_tool_end('{"ok": true, "meta": {"cached": true}}', run_id="x")  # 工具结束（命中缓存）
    assert ctx.metrics["tool_calls"] == 1  # 调用数
    assert ctx.metrics["tool_success"] == 1  # 成功数
    assert ctx.metrics["tool_cached"] == 1  # 缓存命中
    assert ctx.metrics["by_tool"]["query_weather"]["calls"] == 1  # 分工具计数