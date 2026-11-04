"""一次 run 的指标 schema 与聚合（只加不删、默认 0、纯数字/枚举，不落原文）。

用途：为以后优化提供数据基础——外部调用/重试/成功率、token 全口径、延迟拆解、压缩收益、
检索质量、工具缓存等。**单一来源**：各步在 span 里记明细，收尾用 :func:`finalize` 汇总一次。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

#: 指标版本（只加不删；历史数据缺的字段按 0 处理）。
METRICS_VERSION = 1  # 版本号

#: 全部计数器初始键（新字段默认 0，历史读不会 KeyError）。
_COUNTER_KEYS: tuple[str, ...] = (  # 计数器键表
    # run 基础
    "question_chars", "answer_chars", "queue_ms",
    # 模型 / 参数
    "llm_calls", "llm_success", "llm_error", "llm_retries",
    # 外部 HTTP
    "http_calls", "http_success", "http_error", "http_retries",
    # token
    "tokens_prompt", "tokens_completion", "tokens_total",
    "tokens_cache_read", "tokens_cache_creation", "tokens_reasoning",
    # 延迟
    "ttft_ms", "model_ms", "tool_ms", "retrieval_ms", "compress_ms", "persist_ms",
    # 上下文 / 压缩
    "context_budget", "context_used", "tokens_before_compress", "tokens_after_compress",
    "history_turns",
    # 检索
    "dense_hits", "sparse_hits", "fused_hits", "selected_hits",
    # 工具
    "tool_calls", "tool_success", "tool_error", "tool_cached",
    # 错误 / 稳定性
    "failover_count", "fallback_used", "retry_wasted_ms",
)


def new_metrics() -> dict[str, Any]:  # 全 0 指标包
    """初始化一次 run 的指标（含分组字典）。"""
    metrics: dict[str, Any] = {key: 0 for key in _COUNTER_KEYS}  # 全 0
    metrics["by_tool"] = {}  # 分工具明细
    metrics["by_api"] = {}  # 分接口明细
    metrics["error_codes"] = {}  # 错误码计数
    metrics["metrics_version"] = METRICS_VERSION  # 版本
    return metrics  # 返回


def bump(metrics: dict[str, Any], key: str, delta: int = 1) -> None:  # 累加一个计数器
    """对单个计数器加 delta（缺键按 0 起步）。"""
    metrics[key] = int(metrics.get(key) or 0) + int(delta)  # 累加


def bump_nested(metrics: dict[str, Any], group: str, name: str, key: str, delta: int = 1) -> None:  # 累加分组
    """对 by_tool/by_api/error_codes 这类二级计数累加。"""
    bucket = metrics.setdefault(group, {})  # 分组
    item = bucket.setdefault(str(name), {})  # 子项
    item[key] = int(item.get(key) or 0) + int(delta)  # 累加


def finalize(metrics: dict[str, Any]) -> dict[str, Any]:  # 收尾汇总
    """补派生指标（缓存未命中、命中率、压缩收益），返回可直接落库的字典。"""
    out = {key: int(metrics.get(key) or 0) for key in _COUNTER_KEYS}  # 计数器归整
    out["by_tool"] = dict(metrics.get("by_tool") or {})  # 分工具
    out["by_api"] = dict(metrics.get("by_api") or {})  # 分接口
    out["error_codes"] = dict(metrics.get("error_codes") or {})  # 错误码
    total = int(out.get("tokens_total") or 0)  # 总 token
    cache_read = int(out.get("tokens_cache_read") or 0)  # 缓存读取
    prompt = int(out.get("tokens_prompt") or 0)  # 输入 token
    out["tokens_uncached"] = max(0, total - cache_read)  # 未使用缓存的 token
    out["cache_hit_ratio"] = round(cache_read / prompt, 4) if prompt > 0 else 0.0  # 缓存命中率
    out["tokens_saved"] = max(0, int(out.get("tokens_before_compress") or 0) - int(out.get("tokens_after_compress") or 0))  # 压缩节省
    out["metrics_version"] = METRICS_VERSION  # 版本
    return out  # 返回